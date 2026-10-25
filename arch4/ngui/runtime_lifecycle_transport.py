"""Acknowledged lifecycle transport for the NGUI child process."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
import math
from queue import Empty, Full, Queue
from threading import BoundedSemaphore, Event, RLock, Thread
from time import monotonic
from typing import Any

from .runtime_ipc import (
    RuntimeIPCEvent,
    RuntimeLifecycleAcknowledgement,
    RuntimeLifecycleEnvelope,
    runtime_ipc_event_digest,
)


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleReservation:
    publisher_id: int
    reservation_id: int

    def __post_init__(self) -> None:
        for name in ("publisher_id", "reservation_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(
                    f"runtime_lifecycle_reservation_{name}_invalid"
                )


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleTerminationReceipt:
    terminated: bool
    drained: bool
    thread_alive: bool
    cancelled_reservations: int
    outstanding: int

    def __post_init__(self) -> None:
        for name in ("terminated", "drained", "thread_alive"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(
                    f"runtime_lifecycle_termination_{name}_bool_required"
                )
        for name in ("cancelled_reservations", "outstanding"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"runtime_lifecycle_termination_{name}_invalid"
                )
        expected = (
            self.drained
            and not self.thread_alive
            and self.outstanding == 0
        )
        if self.terminated is not expected:
            raise ValueError(
                "runtime_lifecycle_termination_disposition_mismatch"
            )


class RuntimeLifecycleDeliveryDisposition(StrEnum):
    PENDING_INDETERMINATE = "pending_indeterminate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TRANSPORT_FAILED = "transport_failed"


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleDeliveryObservation:
    sequence: int
    event_digest: str
    disposition: RuntimeLifecycleDeliveryDisposition
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime_lifecycle_observation_sequence_invalid")
        if type(self.event_digest) is not str:
            raise TypeError("runtime_lifecycle_observation_digest_string_required")
        digest = self.event_digest
        if (
            len(digest) != 64
            or digest != digest.lower()
            or digest != digest.strip()
            or any(
            character not in "0123456789abcdef" for character in digest
            )
        ):
            raise ValueError("runtime_lifecycle_observation_digest_invalid")
        if not isinstance(
            self.disposition,
            RuntimeLifecycleDeliveryDisposition,
        ):
            raise TypeError(
                "runtime_lifecycle_observation_disposition_required"
            )
        if type(self.reason) is not str:
            raise TypeError("runtime_lifecycle_observation_reason_string_required")
        reason = self.reason
        if reason != reason.strip():
            raise ValueError("runtime_lifecycle_observation_reason_not_canonical")
        if (
            self.disposition
            is RuntimeLifecycleDeliveryDisposition.ACCEPTED
            and reason
        ):
            raise ValueError(
                "runtime_lifecycle_accepted_observation_reason_forbidden"
            )
        if (
            self.disposition
            in {
                RuntimeLifecycleDeliveryDisposition.REJECTED,
                RuntimeLifecycleDeliveryDisposition.TRANSPORT_FAILED,
            }
            and not reason
        ):
            raise ValueError(
                "runtime_lifecycle_terminal_observation_reason_required"
            )
        if (
            self.disposition
            is RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
            and reason
        ):
            raise ValueError(
                "runtime_lifecycle_pending_observation_reason_forbidden"
            )
        object.__setattr__(self, "event_digest", digest)
        object.__setattr__(self, "reason", reason)

    @property
    def terminal(self) -> bool:
        return self.disposition is not (
            RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
        )


class RuntimeLifecycleDelivery:
    """Local handle for one lifecycle envelope."""

    def __init__(self, envelope: RuntimeLifecycleEnvelope) -> None:
        if not isinstance(envelope, RuntimeLifecycleEnvelope):
            raise TypeError("runtime_lifecycle_delivery_envelope_required")
        self.envelope = envelope
        self._completed = Event()
        self._lock = RLock()
        self._disposition = (
            RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
        )
        self._reason = ""

    def _finish(
        self,
        *,
        disposition: RuntimeLifecycleDeliveryDisposition,
        reason: str = "",
    ) -> None:
        if disposition is (
            RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
        ):
            raise ValueError(
                "runtime_lifecycle_delivery_terminal_disposition_required"
            )
        observation = RuntimeLifecycleDeliveryObservation(
            sequence=self.envelope.sequence,
            event_digest=self.envelope.event_digest,
            disposition=disposition,
            reason=reason,
        )
        with self._lock:
            if self._completed.is_set():
                if self.observation() != observation:
                    raise RuntimeError(
                        "runtime_lifecycle_delivery_terminal_changed"
                    )
                return
            self._disposition = disposition
            self._reason = observation.reason
            self._completed.set()

    def observation(
        self,
        timeout_s: float = 0.0,
    ) -> RuntimeLifecycleDeliveryObservation:
        timeout = _nonnegative_timeout(
            timeout_s,
            "runtime_lifecycle_observation_timeout_invalid",
        )
        self._completed.wait(timeout)
        with self._lock:
            return RuntimeLifecycleDeliveryObservation(
                sequence=self.envelope.sequence,
                event_digest=self.envelope.event_digest,
                disposition=self._disposition,
                reason=self._reason,
            )


@dataclass(slots=True)
class _PendingLifecycleDelivery:
    delivery: RuntimeLifecycleDelivery


class RuntimeLifecyclePublisher:
    """Bounded child outbox with one acknowledged event in flight."""

    def __init__(
        self,
        lifecycle_queue: Any,
        acknowledgement_queue: Any,
        *,
        capacity: int = 64,
        logger: logging.Logger | None = None,
    ) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("runtime lifecycle capacity must be positive")
        self._lifecycle_queue = lifecycle_queue
        self._acknowledgement_queue = acknowledgement_queue
        self._capacity = capacity
        self._capacity_slots = BoundedSemaphore(self._capacity)
        self._pending: Queue[_PendingLifecycleDelivery] = Queue()
        self._lock = RLock()
        self._idle = Event()
        self._idle.set()
        self._stop = Event()
        self._thread: Thread | None = None
        self._publisher_id = id(self)
        self._next_reservation_id = 0
        self._next_sequence = 0
        self._open_reservations: set[int] = set()
        self._outstanding = 0
        self._admission_open = True
        self._acknowledgement_backlog: dict[
            tuple[str, int, str, str],
            RuntimeLifecycleAcknowledgement,
        ] = {}
        self._logger = logger or logging.getLogger(
            "arch4.ngui.runtime_lifecycle_transport"
        )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("runtime lifecycle publisher already started")
            self._thread = Thread(
                target=self._run,
                name="ngui-runtime-lifecycle-publisher",
                daemon=True,
            )
            self._thread.start()

    def try_reserve(self) -> RuntimeLifecycleReservation | None:
        with self._lock:
            if not self._admission_open or self._stop.is_set():
                return None
            if not self._capacity_slots.acquire(blocking=False):
                return None
            self._next_reservation_id += 1
            reservation_id = self._next_reservation_id
            self._open_reservations.add(reservation_id)
            self._outstanding += 1
            self._idle.clear()
        return RuntimeLifecycleReservation(self._publisher_id, reservation_id)

    def reserve(self, timeout_s: float) -> RuntimeLifecycleReservation | None:
        timeout = _nonnegative_timeout(
            timeout_s,
            "runtime_lifecycle_reservation_timeout_invalid",
        )
        deadline = monotonic() + timeout
        while not self._stop.is_set():
            with self._lock:
                if not self._admission_open:
                    return None
            reservation = self.try_reserve()
            if reservation is not None:
                return reservation
            if monotonic() >= deadline:
                return None
            self._stop.wait(min(0.02, max(0.0, deadline - monotonic())))
        return None

    def cancel(self, reservation: RuntimeLifecycleReservation) -> None:
        reservation_id = self._take_reservation(reservation)
        if reservation_id is None:
            raise RuntimeError("runtime lifecycle reservation is not active")
        self._complete_outstanding()

    def commit(
        self,
        reservation: RuntimeLifecycleReservation,
        event: RuntimeIPCEvent,
    ) -> RuntimeLifecycleDelivery:
        reservation_id = self._take_reservation(reservation)
        if reservation_id is None:
            raise RuntimeError("runtime lifecycle reservation is not active")
        try:
            with self._lock:
                self._next_sequence += 1
                sequence = self._next_sequence
            envelope = RuntimeLifecycleEnvelope(
                sequence=sequence,
                event=event,
                enqueued_at=monotonic(),
            )
            if runtime_ipc_event_digest(envelope.event) != envelope.event_digest:
                raise RuntimeError(
                    "runtime lifecycle event mutated during commit"
                )
            delivery = RuntimeLifecycleDelivery(envelope)
            self._pending.put_nowait(
                _PendingLifecycleDelivery(delivery)
            )
            return delivery
        except Exception:
            self._complete_outstanding()
            raise

    def publish_and_wait(
        self,
        event: RuntimeIPCEvent,
        *,
        timeout_s: float,
    ) -> RuntimeLifecycleDelivery:
        timeout = _nonnegative_timeout(
            timeout_s,
            "runtime_lifecycle_publish_timeout_invalid",
        )
        deadline = monotonic() + timeout
        reservation = self.reserve(max(0.0, deadline - monotonic()))
        if reservation is None:
            raise RuntimeError("runtime lifecycle outbox has no available capacity")
        delivery = self.commit(reservation, event)
        delivery.observation(max(0.0, deadline - monotonic()))
        return delivery

    def publish_terminal(
        self,
        event: RuntimeIPCEvent,
        *,
        timeout_s: float,
    ) -> RuntimeLifecycleEnvelope:
        """Publish the final child receipt without waiting for a parent verdict."""

        timeout = _nonnegative_timeout(
            timeout_s,
            "runtime_lifecycle_terminal_timeout_invalid",
        )
        deadline = monotonic() + timeout
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("runtime lifecycle publisher already stopped")
            self._admission_open = False
        if not self.wait_until_idle(max(0.0, deadline - monotonic())):
            raise RuntimeError(
                "runtime lifecycle terminal publication blocked by active delivery"
            )
        with self._lock:
            if self._outstanding != 0 or self._open_reservations:
                raise RuntimeError(
                    "runtime lifecycle terminal publication has active delivery"
                )
            self._next_sequence += 1
            envelope = RuntimeLifecycleEnvelope(
                sequence=self._next_sequence,
                event=event,
                enqueued_at=monotonic(),
            )
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                raise RuntimeError(
                    "runtime lifecycle terminal publication timed out"
                )
            try:
                self._lifecycle_queue.put(
                    envelope,
                    timeout=min(0.1, remaining),
                )
                return envelope
            except Full:
                continue

    def wait_until_idle(self, timeout_s: float) -> bool:
        return self._idle.wait(
            _nonnegative_timeout(
                timeout_s,
                "runtime_lifecycle_idle_timeout_invalid",
            )
        )

    def stop(
        self,
        *,
        timeout_s: float,
    ) -> RuntimeLifecycleTerminationReceipt:
        timeout = _nonnegative_timeout(
            timeout_s,
            "runtime_lifecycle_stop_timeout_invalid",
        )
        with self._lock:
            self._admission_open = False
            cancelled = len(self._open_reservations)
            if cancelled:
                self._open_reservations.clear()
                self._outstanding -= cancelled
                if self._outstanding < 0:
                    raise RuntimeError(
                        "runtime lifecycle outstanding count underflow"
                    )
                for _ in range(cancelled):
                    self._capacity_slots.release()
                if self._outstanding == 0:
                    self._idle.set()
        drained = self.wait_until_idle(timeout)
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        thread_alive = bool(thread is not None and thread.is_alive())
        with self._lock:
            outstanding = self._outstanding
        return RuntimeLifecycleTerminationReceipt(
            terminated=bool(drained and not thread_alive and outstanding == 0),
            drained=bool(drained),
            thread_alive=thread_alive,
            cancelled_reservations=cancelled,
            outstanding=outstanding,
        )

    def _take_reservation(
        self,
        reservation: RuntimeLifecycleReservation,
    ) -> int | None:
        if (
            not isinstance(reservation, RuntimeLifecycleReservation)
            or reservation.publisher_id != self._publisher_id
        ):
            return None
        with self._lock:
            reservation_id = int(reservation.reservation_id)
            if reservation_id not in self._open_reservations:
                return None
            self._open_reservations.remove(reservation_id)
            return reservation_id

    def _complete_outstanding(self) -> None:
        with self._lock:
            self._outstanding -= 1
            if self._outstanding < 0:
                raise RuntimeError("runtime lifecycle outstanding count underflow")
            if self._outstanding == 0:
                self._idle.set()
        self._capacity_slots.release()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._pending.empty():
            try:
                pending = self._pending.get(timeout=0.05)
            except Empty:
                continue
            try:
                acknowledgement = self._deliver_exact(pending.delivery.envelope)
                pending.delivery._finish(
                    disposition=(
                        RuntimeLifecycleDeliveryDisposition.ACCEPTED
                        if acknowledgement.accepted
                        else RuntimeLifecycleDeliveryDisposition.REJECTED
                    ),
                    reason=acknowledgement.reason,
                )
            except Exception as exc:
                self._logger.error(
                    "Runtime lifecycle publisher stopped before exact delivery: %s",
                    exc,
                )
                pending.delivery._finish(
                    disposition=(
                        RuntimeLifecycleDeliveryDisposition.TRANSPORT_FAILED
                    ),
                    reason=f"{type(exc).__name__}: {exc}",
                )
            finally:
                self._pending.task_done()
                self._complete_outstanding()

    def _deliver_exact(
        self,
        envelope: RuntimeLifecycleEnvelope,
    ) -> RuntimeLifecycleAcknowledgement:
        while not self._stop.is_set():
            if runtime_ipc_event_digest(envelope.event) != envelope.event_digest:
                raise RuntimeError(
                    "runtime lifecycle event mutated before send"
                )
            try:
                self._lifecycle_queue.put(envelope, timeout=0.1)
            except Full:
                continue
            expected_key = _event_acknowledgement_key(envelope)
            retained = self._take_retained_acknowledgement(expected_key)
            if retained is not None:
                return retained
            while not self._stop.is_set():
                try:
                    acknowledgement = self._acknowledgement_queue.get(timeout=0.2)
                except Empty:
                    break
                if not isinstance(
                    acknowledgement,
                    RuntimeLifecycleAcknowledgement,
                ):
                    self._logger.error(
                        "Discarded invalid runtime lifecycle acknowledgement"
                    )
                    continue
                if (
                    _acknowledgement_key(acknowledgement)
                    != expected_key
                ):
                    self._retain_acknowledgement(acknowledgement)
                    continue
                return acknowledgement
        raise RuntimeError("runtime lifecycle publisher stopped before acknowledgement")

    def _retain_acknowledgement(
        self,
        acknowledgement: RuntimeLifecycleAcknowledgement,
    ) -> None:
        key = _acknowledgement_key(acknowledgement)
        with self._lock:
            existing = self._acknowledgement_backlog.get(key)
            if existing is not None and existing != acknowledgement:
                raise RuntimeError(
                    "runtime_lifecycle_acknowledgement_conflict"
                )
            self._acknowledgement_backlog[key] = acknowledgement
            if len(self._acknowledgement_backlog) > self._capacity:
                raise RuntimeError(
                    "runtime_lifecycle_acknowledgement_backlog_overflow"
                )

    def _take_retained_acknowledgement(
        self,
        key: tuple[str, int, str, str],
    ) -> RuntimeLifecycleAcknowledgement | None:
        with self._lock:
            return self._acknowledgement_backlog.pop(key, None)


def _nonnegative_timeout(value: Any, reason: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(reason)
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0.0:
        raise ValueError(reason)
    return timeout


def _event_acknowledgement_key(
    envelope: RuntimeLifecycleEnvelope,
) -> tuple[str, int, str, str]:
    return (
        envelope.event.generation_identity.identity_digest,
        envelope.sequence,
        envelope.event.kind.value,
        envelope.event_digest,
    )


def _acknowledgement_key(
    acknowledgement: RuntimeLifecycleAcknowledgement,
) -> tuple[str, int, str, str]:
    return (
        acknowledgement.generation_identity.identity_digest,
        acknowledgement.sequence,
        acknowledgement.event_kind.value,
        acknowledgement.event_digest,
    )

__all__ = [
    "RuntimeLifecycleDelivery",
    "RuntimeLifecycleDeliveryDisposition",
    "RuntimeLifecycleDeliveryObservation",
    "RuntimeLifecyclePublisher",
    "RuntimeLifecycleReservation",
    "RuntimeLifecycleTerminationReceipt",
]
