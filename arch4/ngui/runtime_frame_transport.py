"""Bounded latest-state transport from the child process to NGUI."""

from __future__ import annotations

from queue import Empty, Full
from threading import Condition, Event, Thread
from time import monotonic, time
from typing import Any, Callable

from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority
from arch4.architecture_contracts.runtime_termination import (
    RuntimeFramePublisherTerminationReceipt,
)

from .runtime_ipc import (
    RuntimeFrameDeliveryReceipt,
    RuntimeFrameEnvelope,
    RuntimeInputFrame,
    measure_runtime_frame_bytes,
    validate_runtime_frame_delivery_receipt,
)


class RuntimeFramePublisherStopping(RuntimeError):
    """Raised when frame transport rejects work during shutdown."""


class LatestRuntimeFramePublisher:
    """Keep one queued frame and at most one newer replacement."""

    def __init__(
        self,
        frame_queue: Any,
        acknowledgement_queue: Any,
        stop_event: Any,
        *,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
        on_receipt: Callable[[RuntimeFrameDeliveryReceipt], None] | None = None,
        acknowledgement_timeout_s: float = 2.0,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("runtime_frame_publisher_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("runtime_frame_publisher_requires_fault_supervisor")
        self._queue = frame_queue
        self._acknowledgement_queue = acknowledgement_queue
        self._stop_event = stop_event
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._on_receipt = on_receipt
        self._acknowledgement_timeout_s = max(
            0.1,
            float(acknowledgement_timeout_s),
        )
        self._worker_id = "ngui_runtime_frame_publisher"
        self._worker_health.register(
            self._worker_id,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=True,
            retryable=True,
        )
        self._changed = Condition()
        self._local_stop = Event()
        self._pending: RuntimeFrameEnvelope | None = None
        self._inflight: RuntimeFrameEnvelope | None = None
        self._thread: Thread | None = None
        self._submitted = 0
        self._delivered = 0
        self._rejected = 0
        self._coalesced = 0
        self._application_rejection_streak = 0
        self._retry_not_before = 0.0
        self._inflight_sent_at = 0.0
        self._replayed = 0
        self._stale_acknowledgements = 0

    def start(self) -> None:
        with self._changed:
            self._local_stop.clear()
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("runtime frame publisher is already running")
            self._worker_health.mark_started(self._worker_id)
            self._thread = Thread(
                target=self._run,
                name="ngui-runtime-frame-publisher",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self._local_stop.set()
                self._thread = None
                self._worker_health.mark_stopped(
                    self._worker_id,
                    reason="frame_publisher_partial_start_rolled_back",
                )
                raise

    def submit(self, frame: RuntimeInputFrame) -> None:
        payload_bytes = measure_runtime_frame_bytes(frame)
        envelope = RuntimeFrameEnvelope(
            frame=frame,
            payload_bytes=payload_bytes,
            enqueued_at=monotonic(),
        )
        with self._changed:
            if self._stop_event.is_set() or self._local_stop.is_set():
                raise RuntimeFramePublisherStopping(
                    "runtime frame publisher is stopping"
                )
            if self._pending is not None:
                self._coalesced += 1
            self._pending = envelope
            self._submitted += 1
            self._changed.notify()

    def stop(
        self,
        *,
        timeout_s: float = 1.0,
    ) -> RuntimeFramePublisherTerminationReceipt:
        with self._changed:
            self._local_stop.set()
            self._changed.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_s)))
        with self._changed:
            thread_alive = bool(thread is not None and thread.is_alive())
            pending_cancelled = self._pending is not None
            inflight_cancelled = self._inflight is not None
            if not thread_alive:
                self._pending = None
                self._inflight = None
                self._inflight_sent_at = 0.0
        return RuntimeFramePublisherTerminationReceipt(
            thread_alive=thread_alive,
            pending_cancelled=pending_cancelled,
            inflight_cancelled=inflight_cancelled,
        )

    def status(self) -> dict[str, int | bool]:
        with self._changed:
            return {
                "submitted": self._submitted,
                "delivered": self._delivered,
                "rejected": self._rejected,
                "coalesced": self._coalesced,
                "replayed": self._replayed,
                "stale_acknowledgements": self._stale_acknowledgements,
                "pending": self._pending is not None,
                "awaiting_acknowledgement": self._inflight is not None,
            }

    def termination_observation(
        self,
    ) -> RuntimeFramePublisherTerminationReceipt:
        """Report liveness without changing transport state."""

        with self._changed:
            thread = self._thread
            return RuntimeFramePublisherTerminationReceipt(
                thread_alive=bool(
                    thread is not None and thread.is_alive()
                ),
                pending_cancelled=False,
                inflight_cancelled=False,
            )

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set() and not self._local_stop.is_set():
                try:
                    with self._changed:
                        while (
                            self._pending is None
                            and self._inflight is None
                            and not self._stop_event.is_set()
                            and not self._local_stop.is_set()
                        ):
                            self._changed.wait(timeout=0.1)
                        if self._stop_event.is_set() or self._local_stop.is_set():
                            return
                        envelope = self._inflight or self._pending
                        if envelope is None:
                            continue
                        if self._inflight is not None:
                            if self._inflight_sent_at <= 0.0:
                                retry_wait_s = max(
                                    0.0,
                                    self._retry_not_before - monotonic(),
                                )
                                if retry_wait_s > 0.0:
                                    self._changed.wait(
                                        timeout=min(0.1, retry_wait_s)
                                    )
                                    continue
                                try:
                                    self._queue.put_nowait(envelope)
                                except Full:
                                    self._changed.wait(timeout=0.01)
                                    continue
                                self._inflight_sent_at = monotonic()
                                self._replayed += 1
                                continue
                            try:
                                receipt = self._acknowledgement_queue.get_nowait()
                            except Empty:
                                if (
                                    self._inflight_sent_at > 0.0
                                    and monotonic() - self._inflight_sent_at
                                    >= self._acknowledgement_timeout_s
                                ):
                                    try:
                                        self._queue.put_nowait(envelope)
                                    except Full:
                                        pass
                                    else:
                                        self._inflight_sent_at = monotonic()
                                        self._replayed += 1
                                self._changed.wait(timeout=0.01)
                                continue
                            if (
                                not isinstance(receipt, RuntimeFrameDeliveryReceipt)
                            ):
                                raise RuntimeError(
                                    "runtime_frame_acknowledgement_mismatch"
                                )
                            if receipt.frame_sequence < envelope.frame.sequence:
                                self._stale_acknowledgements += 1
                                continue
                            validate_runtime_frame_delivery_receipt(
                                envelope.frame,
                                receipt,
                            )
                            if self._on_receipt is not None:
                                self._on_receipt(receipt)
                            self._delivered += 1
                            if not receipt.frame_applied:
                                self._rejected += 1
                                # A negative receipt is final for this envelope.
                                # The application boundary quarantines the bad
                                # frame. Replay is allowed only when no transport
                                # receipt exists.
                                self._inflight = None
                                self._inflight_sent_at = 0.0
                                self._application_rejection_streak = 0
                                self._retry_not_before = 0.0
                                rejection = RuntimeError(
                                    "runtime_frame_application_rejected:"
                                    f"frame={receipt.frame_sequence}"
                                )
                                self._worker_health.mark_failure(
                                    self._worker_id,
                                    rejection,
                                    next_retry_at=time(),
                                )
                                self._fault_supervisor.record(
                                    boundary=FaultBoundaryClass.GUI,
                                    owner=self._worker_id,
                                    operation="application_commit",
                                    generation=receipt.frame_sequence,
                                    severity=FaultSeverity.ERROR,
                                    retryable=False,
                                    degraded_behavior=(
                                        "quarantine_rejected_frame_and_continue_latest"
                                    ),
                                    error=rejection,
                                    suppression_budget=3,
                                )
                            else:
                                self._inflight = None
                                self._inflight_sent_at = 0.0
                                self._application_rejection_streak = 0
                                self._retry_not_before = 0.0
                                self._worker_health.mark_success(self._worker_id)
                            continue
                        retry_wait_s = max(
                            0.0,
                            self._retry_not_before - monotonic(),
                        )
                        if retry_wait_s > 0.0:
                            self._changed.wait(timeout=min(0.1, retry_wait_s))
                            continue
                        try:
                            self._queue.put_nowait(envelope)
                        except Full:
                            # Keep one replacement slot. submit() may replace it
                            # only while this condition is released below.
                            self._changed.wait(timeout=0.01)
                            continue
                        self._pending = None
                        self._inflight = envelope
                        self._inflight_sent_at = monotonic()
                except Exception as exc:
                    current = self._worker_health.record(self._worker_id)
                    retry_s = min(1.0, 0.01 * (2 ** min(current.failure_streak + 1, 6)))
                    self._worker_health.mark_failure(
                        self._worker_id,
                        exc,
                        next_retry_at=time() + retry_s,
                    )
                    self._fault_supervisor.record(
                        boundary=FaultBoundaryClass.GUI,
                        owner=self._worker_id,
                        operation="deliver_latest_frame",
                        generation=int(self._delivered),
                        severity=FaultSeverity.ERROR,
                        retryable=True,
                        degraded_behavior="retain_latest_frame_and_retry",
                        error=exc,
                        suppression_budget=3,
                    )
                    self._local_stop.wait(retry_s)
        finally:
            self._worker_health.mark_stopped(
                self._worker_id,
                reason="frame_publisher_stop_requested",
            )


__all__ = [
    "LatestRuntimeFramePublisher",
    "RuntimeFramePublisherStopping",
    "RuntimeFramePublisherTerminationReceipt",
]
