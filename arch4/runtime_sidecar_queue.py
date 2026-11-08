"""Budget-friendly sidecar write queue for the clocked runtime lane."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.payload_budget import PayloadBudgetExceeded, retained_payload_bytes
from arch4.runtime_sidecar import (
    write_json_sidecar,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority


_SIDECAR_WORKER_ID = "runtime_sidecar_writer"


@dataclass(frozen=True, slots=True)
class SidecarRetryPolicy:
    max_attempts: int = 3
    base_backoff_s: float = 0.25
    max_backoff_s: float = 30.0

    def __post_init__(self) -> None:
        if int(self.max_attempts) < 1:
            raise ValueError("sidecar retry max_attempts must be positive")
        if float(self.base_backoff_s) <= 0.0:
            raise ValueError("sidecar retry base_backoff_s must be positive")
        if float(self.max_backoff_s) < float(self.base_backoff_s):
            raise ValueError(
                "sidecar retry max_backoff_s must not be below base_backoff_s"
            )

    def delay_after(self, attempts: int) -> float:
        exponent = max(0, int(attempts) - 1)
        return min(
            float(self.max_backoff_s),
            float(self.base_backoff_s) * (2.0**exponent),
        )


@dataclass(frozen=True, slots=True)
class SidecarDeadLetter:
    enqueue_seq: int
    target_names: tuple[str, ...]
    attempts: int
    reason: str
    source_lane: str
    source_job: str
    dead_lettered_ts: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeSidecarDeadLetter.v1",
            "owner": "runtime_sidecar_writer_queue",
            "enqueue_seq": int(self.enqueue_seq),
            "target_names": list(self.target_names),
            "attempts": int(self.attempts),
            "reason": str(self.reason),
            "source_lane": str(self.source_lane),
            "source_job": str(self.source_job),
            "dead_lettered_ts": float(self.dead_lettered_ts),
        }


@dataclass
class QueuedSidecarJob:
    targets: list[Path]
    payload: Any = None
    indent: int | None = None
    kind: str = "write_json"
    export_envelope: bool = False
    source_lane: str = ""
    source_job: str = ""
    tick_id: Any = None
    enqueued_ts: float = 0.0
    enqueued_monotonic_ts: float = 0.0
    enqueue_seq: int = 0
    attempts: int = 0
    last_attempt_ts: float = 0.0
    next_attempt_monotonic_ts: float = 0.0
    retained_bytes: int = 0


class RuntimeSidecarWriteQueue:
    """Single-owner queue for sidecar writes outside protected modem reads."""

    def __init__(
        self,
        *,
        fault_supervisor: FaultSupervisor,
        worker_health: WorkerHealthAuthority,
        max_pending: int = 128,
        max_job_bytes: int = 2 * 1024 * 1024,
        max_pending_bytes: int = 8 * 1024 * 1024,
        retry_policy: SidecarRetryPolicy | None = None,
        max_dead_letters: int = 128,
    ) -> None:
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("runtime_sidecar_queue_requires_fault_supervisor")
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("runtime_sidecar_queue_requires_worker_health")
        self._lock = threading.RLock()
        self._supervisor = fault_supervisor
        self._worker_health = worker_health
        self._worker_health.register(
            _SIDECAR_WORKER_ID,
            execution_kind=WorkerExecutionKind.FINITE_JOB,
            required=True,
            retryable=True,
        )
        self._queue: deque[QueuedSidecarJob] = deque()
        self._dead_letters: deque[SidecarDeadLetter] = deque(
            maxlen=max(1, int(max_dead_letters))
        )
        self._retry_policy = retry_policy or SidecarRetryPolicy()
        self._max_pending = max(1, int(max_pending))
        self._max_job_bytes = max(1, int(max_job_bytes))
        self._max_pending_bytes = max(self._max_job_bytes, int(max_pending_bytes))
        self._pending_bytes = 0
        self._high_water_bytes = 0
        self._oversized_rejected = 0
        self._attempted = 0
        self._written = 0
        self._failures = 0
        self._requeued = 0
        self._retry_deferred = 0
        self._dead_letter_count = 0
        self._superseded = 0
        self._dropped = 0
        self._pressure_rejected = 0
        self._high_water_mark = 0
        self._enqueue_seq = 0
        self._last_attempt_seq = 0
        self._last_written_seq = 0
        self._last_failure: dict[str, Any] = {}
        self._last_enqueue_by_target: dict[tuple[str, ...], float] = {}
        self._cadence_skipped = 0
        self._enqueue_failures = 0
        self._last_cadence_skip: dict[str, Any] = {}
        self._writer_instance_id = uuid.uuid4().hex
        self._drain_thread: threading.Thread | None = None
        self._drain_inflight = False
        self._async_launches = 0
        self._async_busy_count = 0
        self._last_async_started_ts = 0.0
        self._last_async_finished_ts = 0.0
        self._last_async_result: dict[str, Any] = {}

    @staticmethod
    def _targets_key(targets: Iterable[Path]) -> tuple[str, ...]:
        return tuple(str(Path(target)) for target in targets)

    def _has_pending_newer_target(self, item: QueuedSidecarJob) -> bool:
        key = self._targets_key(item.targets)
        for pending in self._queue:
            if self._targets_key(pending.targets) == key and pending.enqueue_seq >= item.enqueue_seq:
                return True
        return False

    def _append_job_locked(
        self,
        *,
        key: tuple[str, ...],
        job: QueuedSidecarJob,
        now_ts: float,
    ) -> bool:
        retained_items = [
            item
            for item in self._queue
            if not key or self._targets_key(item.targets) != key
        ]
        candidate_count = len(retained_items) + 1
        candidate_bytes = sum(
            int(item.retained_bytes) for item in retained_items
        ) + int(job.retained_bytes)
        if (
            candidate_count > self._max_pending
            or candidate_bytes > self._max_pending_bytes
        ):
            self._enqueue_failures += 1
            self._pressure_rejected += 1
            self._last_failure = {
                "targets": list(key),
                "stage": "enqueue_pressure",
                "error": "queue_budget_exhausted",
                "candidate_count": int(candidate_count),
                "candidate_bytes": int(candidate_bytes),
                "ts": float(now_ts),
            }
            return False
        if key:
            self._last_enqueue_by_target[key] = float(now_ts)
        self._enqueue_seq += 1
        if key:
            retained: deque[QueuedSidecarJob] = deque()
            for item in self._queue:
                if self._targets_key(item.targets) == key:
                    self._superseded += 1
                    self._pending_bytes = max(0, self._pending_bytes - int(item.retained_bytes))
                    continue
                retained.append(item)
            self._queue = retained
        job.enqueue_seq = int(self._enqueue_seq)
        self._queue.append(job)
        self._pending_bytes += int(job.retained_bytes)
        self._high_water_mark = max(self._high_water_mark, len(self._queue))
        self._high_water_bytes = max(self._high_water_bytes, self._pending_bytes)
        return True

    @staticmethod
    def _target_names(item: QueuedSidecarJob) -> tuple[str, ...]:
        return tuple(Path(target).name for target in item.targets)

    def _dead_letter_locked(
        self,
        item: QueuedSidecarJob,
        *,
        reason: str,
        now_ts: float,
    ) -> SidecarDeadLetter:
        dead_letter = SidecarDeadLetter(
            enqueue_seq=int(item.enqueue_seq),
            target_names=self._target_names(item),
            attempts=int(item.attempts),
            reason=str(reason),
            source_lane=str(item.source_lane or ""),
            source_job=str(item.source_job or ""),
            dead_lettered_ts=float(now_ts),
        )
        self._dead_letters.append(dead_letter)
        self._dead_letter_count += 1
        self._last_failure = dead_letter.to_payload()
        return dead_letter

    def _publish_dead_letter_fault(self, dead_letter: SidecarDeadLetter) -> None:
        self._supervisor.record(
            boundary=FaultBoundaryClass.STORAGE,
            owner="runtime_sidecar_writer_queue",
            operation="write_json_dead_letter",
            generation=max(0, int(dead_letter.enqueue_seq)),
            severity=FaultSeverity.ERROR,
            retryable=False,
            degraded_behavior="sidecar_job_dead_lettered",
            error=RuntimeError(
                f"{dead_letter.reason}:"
                f"{','.join(dead_letter.target_names) or 'no_target'}"
            ),
            suppression_budget=0,
        )

    @staticmethod
    def _payload_tick_id(payload: Any, explicit_tick_id: Any) -> Any:
        if explicit_tick_id not in (None, ""):
            return explicit_tick_id
        if not isinstance(payload, dict):
            return ""
        for key in ("tick_id", "tick", "tick_count", "seq"):
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return ""

    def _payload_for_write(self, item: QueuedSidecarJob) -> Any:
        if not item.export_envelope or not isinstance(item.payload, dict):
            return item.payload
        payload = dict(item.payload)
        tick_id = self._payload_tick_id(payload, item.tick_id)
        payload["export_envelope"] = {
            "schema_version": "runtime_sidecar_export.v1",
            "owner": "runtime_sidecar_writer_queue",
            "writer_instance_id": self._writer_instance_id,
            "source_lane": str(item.source_lane or ""),
            "source_job": str(item.source_job or ""),
            "tick_id": tick_id,
            "tick_id_present": tick_id not in (None, ""),
            "enqueue_seq": int(item.enqueue_seq),
            "enqueued_ts": float(item.enqueued_ts),
            "enqueued_monotonic_ts": float(item.enqueued_monotonic_ts),
            "target_names": [Path(target).name for target in item.targets],
        }
        return payload

    def _disk_payload_supersedes(self, item: QueuedSidecarJob, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        candidate_ts = 0.0
        try:
            candidate_ts = float(payload.get("ts") or 0.0)
        except Exception:
            candidate_ts = 0.0
        candidate_seq = int(item.enqueue_seq or 0)
        for target in item.targets:
            try:
                existing = json.loads(Path(target).read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(existing, dict) or not existing:
                continue
            envelope = existing.get("export_envelope")
            if isinstance(envelope, dict):
                writer_id = str(envelope.get("writer_instance_id") or "")
                try:
                    existing_seq = int(envelope.get("enqueue_seq") or 0)
                except Exception:
                    existing_seq = 0
                if (
                    writer_id == self._writer_instance_id
                    and existing_seq > 0
                    and candidate_seq > 0
                    and existing_seq > candidate_seq
                ):
                    return True
            if item.export_envelope:
                continue
            try:
                existing_ts = float(existing.get("ts") or 0.0)
            except Exception:
                existing_ts = 0.0
            if existing_ts > 0.0 and candidate_ts > 0.0 and existing_ts > candidate_ts:
                return True
        return False

    def _reject_for_cadence_locked(
        self,
        *,
        key: tuple[str, ...],
        now_ts: float,
        min_interval: float,
        force: bool,
    ) -> bool:
        if not key or min_interval <= 0.0 or bool(force):
            return False
        last_enqueue = float(self._last_enqueue_by_target.get(key, 0.0) or 0.0)
        age_s = now_ts - last_enqueue
        if last_enqueue <= 0.0 or age_s >= min_interval:
            return False
        self._cadence_skipped += 1
        self._last_cadence_skip = {
            "targets": list(key),
            "age_s": max(0.0, float(age_s)),
            "min_interval_s": float(min_interval),
            "ts": float(now_ts),
        }
        return True

    def enqueue(
        self,
        *,
        targets: Iterable[Path],
        payload: Any,
        indent: int | None = None,
        now: float | None = None,
        min_interval_s: float = 0.0,
        force: bool = False,
        export_envelope: bool = False,
        source_lane: str = "",
        source_job: str = "",
        tick_id: Any = None,
    ) -> bool:
        target_list = [Path(target) for target in targets]
        key = self._targets_key(target_list)
        now_ts = float(time.time() if now is None else now)
        monotonic_ts = float(time.monotonic())
        min_interval = max(0.0, float(min_interval_s or 0.0))
        if not target_list:
            with self._lock:
                self._enqueue_failures += 1
                self._last_failure = {
                    "targets": [],
                    "stage": "enqueue",
                    "error": "no_targets",
                    "ts": float(now_ts),
                }
            return False
        with self._lock:
            if self._reject_for_cadence_locked(
                key=key,
                now_ts=now_ts,
                min_interval=min_interval,
                force=bool(force),
            ):
                return False
        try:
            payload_bytes = retained_payload_bytes(
                payload,
                max_bytes=self._max_job_bytes,
                label="runtime sidecar job",
            )
            frozen_payload = copy.deepcopy(payload)
            frozen_bytes = retained_payload_bytes(
                frozen_payload,
                max_bytes=self._max_job_bytes,
                label="frozen runtime sidecar job",
            )
        except Exception as exc:
            with self._lock:
                self._enqueue_failures += 1
                if isinstance(exc, PayloadBudgetExceeded):
                    self._oversized_rejected += 1
                self._last_failure = {
                    "targets": list(key),
                    "stage": "enqueue_freeze",
                    "error": type(exc).__name__,
                    "ts": float(now_ts),
                }
            return False
        with self._lock:
            if self._reject_for_cadence_locked(
                key=key,
                now_ts=now_ts,
                min_interval=min_interval,
                force=bool(force),
            ):
                return False
            return self._append_job_locked(
                key=key,
                now_ts=now_ts,
                job=QueuedSidecarJob(
                    targets=target_list,
                    payload=frozen_payload,
                    indent=indent,
                    kind="write_json",
                    export_envelope=bool(export_envelope),
                    source_lane=str(source_lane or ""),
                    source_job=str(source_job or ""),
                    tick_id=tick_id,
                    retained_bytes=max(int(payload_bytes), int(frozen_bytes)),
                    enqueued_ts=now_ts,
                    enqueued_monotonic_ts=monotonic_ts,
                ),
            )

    def drain(
        self,
        *,
        max_items: int = 1,
        monotonic_now: float | None = None,
    ) -> dict[str, Any]:
        attempted = 0
        written = 0
        failures = 0
        requeued = 0
        retry_deferred = 0
        dead_lettered = 0
        superseded = 0
        dead_letters_to_publish: list[SidecarDeadLetter] = []
        for _ in range(max(0, int(max_items))):
            now_mono = float(
                time.monotonic() if monotonic_now is None else monotonic_now
            )
            with self._lock:
                if not self._queue:
                    break
                due_index = next(
                    (
                        index
                        for index, candidate in enumerate(self._queue)
                        if float(candidate.next_attempt_monotonic_ts or 0.0)
                        <= now_mono
                    ),
                    None,
                )
                if due_index is None:
                    retry_deferred += len(self._queue)
                    break
                item = self._queue[due_index]
                del self._queue[due_index]
                self._pending_bytes = max(0, self._pending_bytes - int(item.retained_bytes))
                attempted += 1
                item.attempts += 1
                item.last_attempt_ts = time.time()
                self._last_attempt_seq = int(item.enqueue_seq)

            with self._lock:
                if self._has_pending_newer_target(item):
                    superseded += 1
                    continue

            payload_for_write = self._payload_for_write(item)
            if self._disk_payload_supersedes(item, payload_for_write):
                superseded += 1
                continue
            write_ok = write_json_sidecar(
                targets=item.targets,
                payload=payload_for_write,
                indent=item.indent,
                refresh_current_state=False,
            )

            if write_ok:
                written += 1
                with self._lock:
                    self._last_written_seq = int(item.enqueue_seq)
            else:
                failures += 1
                with self._lock:
                    self._last_failure = {
                        "targets": [str(target) for target in item.targets],
                        "attempts": int(item.attempts),
                        "enqueued_ts": float(item.enqueued_ts),
                        "enqueue_seq": int(item.enqueue_seq),
                        "last_attempt_ts": float(item.last_attempt_ts),
                    }
                    if self._has_pending_newer_target(item):
                        superseded += 1
                    else:
                        if item.attempts >= self._retry_policy.max_attempts:
                            dead_letters_to_publish.append(
                                self._dead_letter_locked(
                                    item,
                                    reason="write_attempt_budget_exhausted",
                                    now_ts=time.time(),
                                )
                            )
                            dead_lettered += 1
                        else:
                            item.next_attempt_monotonic_ts = (
                                now_mono
                                + self._retry_policy.delay_after(item.attempts)
                            )
                            self._queue.append(item)
                            self._pending_bytes += int(item.retained_bytes)
                            requeued += 1
                            self._high_water_mark = max(
                                self._high_water_mark,
                                len(self._queue),
                            )
                            self._high_water_bytes = max(
                                self._high_water_bytes,
                                self._pending_bytes,
                            )

        with self._lock:
            self._attempted += attempted
            self._written += written
            self._failures += failures
            self._requeued += requeued
            self._retry_deferred += retry_deferred
            self._superseded += superseded
            result = {
                "attempted": attempted,
                "written": written,
                "failures": failures,
                "requeued": requeued,
                "retry_deferred": retry_deferred,
                "dead_lettered": dead_lettered,
                "superseded": superseded,
                "remaining": len(self._queue),
            }
        for dead_letter in dead_letters_to_publish:
            self._publish_dead_letter_fault(dead_letter)
        return result

    def schedule_drain(self, *, max_items: int = 1, now: float | None = None) -> dict[str, Any]:
        """Start a bounded sidecar drain without blocking the runtime clock."""

        now_ts = float(time.time() if now is None else now)
        with self._lock:
            thread = self._drain_thread
            if self._drain_inflight or (thread is not None and thread.is_alive()):
                self._async_busy_count += 1
                return {
                    "scheduled": False,
                    "inflight": True,
                    "attempted": 0,
                    "written": 0,
                    "failures": 0,
                    "requeued": 0,
                    "retry_deferred": 0,
                    "dead_lettered": 0,
                    "superseded": 0,
                    "remaining": len(self._queue),
                    "last_result": dict(self._last_async_result),
                }
            if not self._queue:
                return {
                    "scheduled": False,
                    "inflight": False,
                    "attempted": 0,
                    "written": 0,
                    "failures": 0,
                    "requeued": 0,
                    "retry_deferred": 0,
                    "dead_lettered": 0,
                    "superseded": 0,
                    "remaining": 0,
                    "last_result": dict(self._last_async_result),
                }
            self._drain_inflight = True
            self._async_launches += 1
            self._last_async_started_ts = now_ts
            thread = threading.Thread(
                target=self._drain_worker,
                kwargs={"max_items": max(1, int(max_items or 1))},
                name="54guc-sidecar-writer",
                daemon=True,
            )
            self._drain_thread = thread
            remaining = len(self._queue)
            self._worker_health.mark_started(_SIDECAR_WORKER_ID)
        thread.start()
        return {
            "scheduled": True,
            "inflight": True,
            "attempted": 0,
            "written": 0,
            "failures": 0,
            "requeued": 0,
            "superseded": 0,
            "remaining": remaining,
            "last_result": dict(self._last_async_result),
        }

    def _drain_worker(self, *, max_items: int) -> None:
        try:
            result = self.drain(max_items=max_items)
            self._worker_health.mark_success(_SIDECAR_WORKER_ID)
        except Exception as exc:
            self._supervisor.record(
                boundary=FaultBoundaryClass.WORKER,
                owner="runtime_sidecar_writer_queue",
                operation="drain_worker_uncaught",
                generation=0,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior="writer_worker_stopped_job_retained",
                error=exc,
                suppression_budget=0,
            )
            self._worker_health.mark_failure(
                _SIDECAR_WORKER_ID,
                exc,
                next_retry_at=time.time() + self._retry_policy.base_backoff_s,
            )
            with self._lock:
                remaining = len(self._queue)
            result = {
                "attempted": 0,
                "written": 0,
                "failures": 1,
                "requeued": 0,
                "retry_deferred": 0,
                "dead_lettered": 0,
                "superseded": 0,
                "remaining": remaining,
                "error": f"{type(exc).__name__}: {exc}",
            }
        with self._lock:
            self._last_async_result = dict(result or {})
            self._last_async_finished_ts = time.time()
            self._drain_inflight = False

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = float(time.time() if now is None else now)
        with self._lock:
            oldest_age_s = 0.0
            oldest_seq = 0
            newest_seq = 0
            if self._queue:
                oldest_age_s = max(0.0, now_ts - min(float(item.enqueued_ts) for item in self._queue))
                oldest_seq = min(int(item.enqueue_seq) for item in self._queue)
                newest_seq = max(int(item.enqueue_seq) for item in self._queue)
            return {
                "queued": len(self._queue),
                "owner": "runtime_sidecar_writer_queue",
                "enqueue_seq": int(self._enqueue_seq),
                "oldest_queued_seq": int(oldest_seq),
                "newest_queued_seq": int(newest_seq),
                "last_attempt_seq": int(self._last_attempt_seq),
                "last_written_seq": int(self._last_written_seq),
                "attempted": int(self._attempted),
                "written": int(self._written),
                "failures": int(self._failures),
                "enqueue_failures": int(self._enqueue_failures),
                "requeued": int(self._requeued),
                "retry_deferred": int(self._retry_deferred),
                "dead_letter_count": int(self._dead_letter_count),
                "dead_letters": [
                    row.to_payload() for row in list(self._dead_letters)[-8:]
                ],
                "superseded": int(self._superseded),
                "dropped": int(self._dropped),
                "pressure_rejected": int(self._pressure_rejected),
                "cadence_skipped": int(self._cadence_skipped),
                "max_pending": int(self._max_pending),
                "max_job_bytes": int(self._max_job_bytes),
                "max_pending_bytes": int(self._max_pending_bytes),
                "pending_bytes": int(self._pending_bytes),
                "high_water_bytes": int(self._high_water_bytes),
                "oversized_rejected": int(self._oversized_rejected),
                "high_water_mark": int(self._high_water_mark),
                "oldest_age_s": float(oldest_age_s),
                "last_failure": dict(self._last_failure),
                "last_cadence_skip": dict(self._last_cadence_skip),
                "writer_instance_id": self._writer_instance_id,
                "async_inflight": bool(self._drain_inflight),
                "async_launches": int(self._async_launches),
                "async_busy_count": int(self._async_busy_count),
                "last_async_started_ts": float(self._last_async_started_ts),
                "last_async_finished_ts": float(self._last_async_finished_ts),
                "last_async_result": dict(self._last_async_result),
            }


__all__ = [
    "QueuedSidecarJob",
    "RuntimeSidecarWriteQueue",
    "SidecarDeadLetter",
    "SidecarRetryPolicy",
]
