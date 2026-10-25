"""Bounded memory-first scheduling for typed CGF evidence owners.

This module owns no event meaning and no durable store.  Domain owners submit
already-completed facts and provide their own typed sinks.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import logging
import math
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any, Callable, Mapping
from weakref import ref


log = logging.getLogger(__name__)

CRITICAL_LANE = "critical"
ROUTINE_LANE = "routine"
LOG_PROCESSOR_LANES = frozenset({CRITICAL_LANE, ROUTINE_LANE})


class LogProcessorDrainError(RuntimeError):
    """Pending store work remains owned; shutdown must not claim completion."""


@dataclass(frozen=True, slots=True)
class MemoryLogRecord:
    key: str
    lane: str
    observed_at: float
    payload: Mapping[str, Any]
    admitted_at: float

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key.strip():
            raise ValueError("log_processor_record_key_required")
        if self.lane not in LOG_PROCESSOR_LANES:
            raise ValueError("log_processor_record_lane_invalid")
        if float(self.observed_at) <= 0.0:
            raise ValueError("log_processor_record_timestamp_invalid")
        if not isinstance(self.payload, Mapping):
            raise TypeError("log_processor_record_payload_required")


class MemoryFirstLogProcessor:
    """Two bounded lanes with one ordered writer for this store only."""

    def __init__(
        self,
        *,
        owner: str,
        critical_sink: Callable[[tuple[MemoryLogRecord, ...]], None],
        routine_sink: Callable[[tuple[MemoryLogRecord, ...]], None],
        critical_interval_s: float = 2.0,
        routine_interval_s: float = 60.0,
        hot_retention_s: float = 12.0 * 60.0,
        max_critical_records: int = 65536,
        max_routine_records: int = 4096,
    ) -> None:
        if type(owner) is not str or not owner.strip():
            raise ValueError("log_processor_owner_required")
        if not callable(critical_sink) or not callable(routine_sink):
            raise TypeError("log_processor_typed_sinks_required")
        if critical_interval_s <= 0.0 or routine_interval_s <= 0.0:
            raise ValueError("log_processor_interval_invalid")
        if hot_retention_s <= 0.0:
            raise ValueError("log_processor_hot_retention_invalid")
        self.owner = owner
        self._critical_sink = critical_sink
        self._routine_sink = routine_sink
        self._critical_interval_s = float(critical_interval_s)
        self._routine_interval_s = float(routine_interval_s)
        self._hot_retention_s = float(hot_retention_s)
        self._max_critical_records = max(1, int(max_critical_records))
        self._max_routine_records = max(1, int(max_routine_records))
        self._lock = RLock()
        self._service_lock = Lock()
        self._critical: OrderedDict[str, MemoryLogRecord] = OrderedDict()
        self._routine: OrderedDict[str, MemoryLogRecord] = OrderedDict()
        self._hot: deque[MemoryLogRecord] = deque()
        now = monotonic()
        self._next_critical_flush = now + self._critical_interval_s
        self._next_routine_flush = now + self._routine_interval_s
        self._critical_flushes = 0
        self._routine_flushes = 0
        self._flush_failures = 0
        self._last_error = ""
        self._critical_loss_count = 0
        self._routine_loss_count = 0
        self._closed = False
        self._closing = False
        self._drain_requested = False
        self._inflight_started_at = 0.0
        self._last_write_progress_at = now
        self._wake = Event()
        self._writer = Thread(target=self._run_writer, args=(ref(self), self._wake),
                              name="54guc-log-processor:" + owner, daemon=True)
        self._writer.start()

    @staticmethod
    def _run_writer(reference, wake: Event) -> None:
        while True:
            processor = reference()
            if processor is None:
                return
            with processor._lock:
                force = processor._drain_requested
                processor._drain_requested = False
            processor._service(now=monotonic(), force=force)
            with processor._lock:
                if processor._closing and not processor._critical and not processor._routine:
                    processor._closed = True
                    return
            # An unused store is not kept alive solely by its writer thread.
            del processor
            wake.wait(0.05)
            wake.clear()

    def submit(
        self,
        *,
        key: str,
        lane: str,
        observed_at: float,
        payload: Mapping[str, Any],
        replace: bool = False,
    ) -> bool:
        record = MemoryLogRecord(
            key=key,
            lane=lane,
            observed_at=float(observed_at),
            payload=dict(payload),
            admitted_at=monotonic(),
        )
        with self._lock:
            if self._closing or self._closed:
                raise RuntimeError("log_processor_closed")
            pending = self._critical if lane == CRITICAL_LANE else self._routine
            if key in pending and not replace:
                return False
            replaced = pending.get(key)
            if replaced is not None:
                self._hot = deque(
                    item for item in self._hot if item is not replaced
                )
            limit = (
                self._max_critical_records
                if lane == CRITICAL_LANE
                else self._max_routine_records
            )
            if key not in pending and len(pending) >= limit:
                _evicted_key, evicted = pending.popitem(last=False)
                self._hot = deque(
                    item for item in self._hot if item is not evicted
                )
                if lane == CRITICAL_LANE:
                    self._critical_loss_count += 1
                else:
                    self._routine_loss_count += 1
            pending[key] = record
            pending.move_to_end(key)
            self._hot.append(record)
            self._purge_hot_locked(record.admitted_at)
        self._wake.set()
        return True

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._critical or key in self._routine

    def hot_records(self, *, lane: str | None = None) -> tuple[MemoryLogRecord, ...]:
        if lane is not None and lane not in LOG_PROCESSOR_LANES:
            raise ValueError("log_processor_record_lane_invalid")
        with self._lock:
            self._purge_hot_locked(monotonic())
            return tuple(
                record
                for record in self._hot
                if lane is None or record.lane == lane
            )

    def discard_hot_before(self, *, observed_at: float, lane: str | None = None) -> None:
        """Apply an owner retention cutoff without touching pending writes."""

        if lane is not None and lane not in LOG_PROCESSOR_LANES:
            raise ValueError("log_processor_record_lane_invalid")
        cutoff = float(observed_at)
        with self._lock:
            self._hot = deque(
                record
                for record in self._hot
                if (
                    (lane is not None and record.lane != lane)
                    or record.observed_at >= cutoff
                )
            )

    def _purge_hot_locked(self, now: float) -> None:
        cutoff = now - self._hot_retention_s
        while self._hot and self._hot[0].admitted_at < cutoff:
            self._hot.popleft()

    def _flush_lane(self, lane: str, *, force: bool, now: float) -> None:
        with self._lock:
            pending = self._critical if lane == CRITICAL_LANE else self._routine
            due = (
                now >= self._next_critical_flush
                if lane == CRITICAL_LANE
                else now >= self._next_routine_flush
            )
            if not pending or (not force and not due):
                return
            batch = tuple(pending.values())
        sink = self._critical_sink if lane == CRITICAL_LANE else self._routine_sink
        with self._lock:
            self._inflight_started_at = monotonic()
        try:
            sink(batch)
        except Exception as exc:
            with self._lock:
                self._flush_failures += 1
                self._last_error = f"{type(exc).__name__}:{exc}"
                if lane == CRITICAL_LANE:
                    self._next_critical_flush = now + self._critical_interval_s
                else:
                    self._next_routine_flush = now + self._routine_interval_s
            return
        finally:
            with self._lock:
                self._inflight_started_at = 0.0
        with self._lock:
            pending = self._critical if lane == CRITICAL_LANE else self._routine
            for record in batch:
                if pending.get(record.key) == record:
                    pending.pop(record.key, None)
            if lane == CRITICAL_LANE:
                flushed = {id(record) for record in batch}
                self._hot = deque(
                    record for record in self._hot if id(record) not in flushed
                )
            self._last_error = ""
            self._last_write_progress_at = monotonic()
            if lane == CRITICAL_LANE:
                self._critical_flushes += 1
                self._next_critical_flush = now + self._critical_interval_s
            else:
                self._routine_flushes += 1
                self._next_routine_flush = now + self._routine_interval_s

    def _service(
        self,
        *,
        now: float,
        force: bool = False,
        blocking: bool = False,
    ) -> None:
        if not self._service_lock.acquire(blocking=blocking):
            return
        try:
            with self._lock:
                if self._closed and not force:
                    return
                self._purge_hot_locked(now)
            self._flush_lane(CRITICAL_LANE, force=force, now=now)
            self._flush_lane(ROUTINE_LANE, force=force, now=now)
        finally:
            self._service_lock.release()

    def flush(self) -> None:
        self._service(now=monotonic(), force=True, blocking=True)

    def flush_through(self, *, cutoff: float, timeout_s: float) -> None:
        """Ask the existing writer to persist admitted records through a cutoff."""
        if not math.isfinite(cutoff) or cutoff <= 0 or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("support_history_flush_bounds_invalid")
        deadline = monotonic() + timeout_s
        with self._lock:
            failures = self._flush_failures
            self._drain_requested = True
        self._wake.set()
        waiter = Event()
        while True:
            with self._lock:
                if self._closed or self._closing:
                    raise RuntimeError("support_history_owner_closed")
                if self._flush_failures != failures:
                    raise RuntimeError("support_history_write_failed:" + self._last_error)
                if not any(r.observed_at <= cutoff for r in (*self._critical.values(), *self._routine.values())):
                    return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("support_history_flush_timeout")
            waiter.wait(min(.01, remaining))

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = monotonic()
            self._purge_hot_locked(now)
            oldest = min((r.admitted_at for r in (*self._critical.values(), *self._routine.values())), default=now)
            return {
                "owner": self.owner,
                "pending_critical": len(self._critical),
                "pending_routine": len(self._routine),
                "hot_records": len(self._hot),
                "hot_retention_s": self._hot_retention_s,
                "critical_flushes": self._critical_flushes,
                "routine_flushes": self._routine_flushes,
                "flush_failures": self._flush_failures,
                "critical_loss_count": self._critical_loss_count,
                "routine_loss_count": self._routine_loss_count,
                "last_error": self._last_error,
                "oldest_pending_age_s": max(0.0, now - oldest),
                "inflight_age_s": max(0.0, now - self._inflight_started_at) if self._inflight_started_at else 0.0,
                "last_write_progress_age_s": max(0.0, now - self._last_write_progress_at),
                "closing": self._closing,
                "closed": self._closed,
                "writer_alive": self._writer.is_alive(),
            }

    def close(self, *, timeout_s: float = 2.0) -> None:
        """Bounded drain; a failed close retains this writer and its pending work."""
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self._drain_requested = True
        self._wake.set()
        self._writer.join(timeout=max(0.0, float(timeout_s)))
        state = self.status()
        if self._writer.is_alive() or state['pending_critical'] or state['pending_routine']:
            raise LogProcessorDrainError(
                f"log_processor_drain_incomplete:{self.owner}:"
                f"pending_critical={state['pending_critical']}:pending_routine={state['pending_routine']}:"
                f"writer_alive={self._writer.is_alive()}:error={state['last_error']}"
            )


__all__ = (
    "CRITICAL_LANE",
    "LOG_PROCESSOR_LANES",
    "LogProcessorDrainError",
    "MemoryFirstLogProcessor",
    "MemoryLogRecord",
    "ROUTINE_LANE",
)
