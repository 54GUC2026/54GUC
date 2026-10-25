"""Independent execution fabric for canonical runtime-clock wake requests.

The runtime clock owns time and wake decisions.  Domain registers and queues
own data.  This fabric owns only bounded execution admission: one in-flight
invocation and one latest latched wake per declared clock job.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
import threading
import time
from typing import Any, Callable, Mapping

from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.runtime_clock import ClockedJob, ClockedJobOutcome
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority


ClockJobRunner = Callable[[], bool | ClockedJobOutcome]


@dataclass(frozen=True, slots=True)
class ClockJobWake:
    """One immutable scheduler wake; domain data remains in its owner."""

    job: ClockedJob
    clock_sequence: int
    requested_at: float
    runner: ClockJobRunner


@dataclass(frozen=True, slots=True)
class ClockJobWorkerReadinessReceipt:
    """Bounded proof that every declared clock job has executed cleanly."""

    ready: bool
    timed_out: bool
    declared_jobs: tuple[str, ...]
    ready_jobs: tuple[str, ...]
    pending_jobs: tuple[str, ...]
    error_jobs: tuple[tuple[str, str], ...]
    waited_s: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "ClockJobWorkerReadinessReceipt.v1",
            "owner": "clock_job_worker_fabric",
            "ready": self.ready,
            "timed_out": self.timed_out,
            "declared_jobs": list(self.declared_jobs),
            "ready_jobs": list(self.ready_jobs),
            "pending_jobs": list(self.pending_jobs),
            "error_jobs": {
                job_key: error for job_key, error in self.error_jobs
            },
            "waited_s": round(max(0.0, self.waited_s), 3),
        }


class ClockJobWorkerTerminationDisposition(StrEnum):
    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ClockJobWorkerTerminationReceipt:
    disposition: ClockJobWorkerTerminationDisposition
    shutdown_started_at: float
    finished_at: float
    survivors: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(
            self.disposition,
            ClockJobWorkerTerminationDisposition,
        ):
            raise TypeError(
                "clock_worker_termination_disposition_typed_required"
            )
        for field_name in ("shutdown_started_at", "finished_at"):
            value = getattr(self, field_name)
            if type(value) is not float or value <= 0.0:
                raise ValueError(
                    f"clock_worker_termination_{field_name}_invalid"
                )
        if self.finished_at < self.shutdown_started_at:
            raise ValueError("clock_worker_termination_time_order_invalid")
        if (
            type(self.survivors) is not tuple
            or any(
                type(item) is not str
                or not item
                or item != item.strip()
                for item in self.survivors
            )
            or tuple(sorted(set(self.survivors))) != self.survivors
        ):
            raise TypeError(
                "clock_worker_termination_survivors_invalid"
            )
        if (
            self.disposition
            is ClockJobWorkerTerminationDisposition.DEGRADED
        ) != bool(self.survivors):
            raise ValueError(
                "clock_worker_termination_disposition_inconsistent"
            )

    @property
    def terminated(self) -> bool:
        return self.disposition in {
            ClockJobWorkerTerminationDisposition.STOPPED,
            ClockJobWorkerTerminationDisposition.ALREADY_STOPPED,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "ClockJobWorkerTerminationReceipt.v2",
            "owner": "clock_job_worker_fabric",
            "disposition": self.disposition.value,
            "shutdown_started_at": self.shutdown_started_at,
            "finished_at": self.finished_at,
            "terminated": self.terminated,
            "survivors": list(self.survivors),
        }


@dataclass(slots=True)
class _ClockJobWorkerState:
    job_key: str
    worker_id: str
    in_flight: bool = False
    pending: ClockJobWake | None = None
    dispatch_count: int = 0
    coalesced_wake_count: int = 0
    start_count: int = 0
    completion_count: int = 0
    failure_count: int = 0
    over_budget_count: int = 0
    last_requested_at: float = 0.0
    last_started_at: float = 0.0
    last_finished_at: float = 0.0
    last_duration_ms: float = 0.0
    last_clock_sequence: int = 0
    last_completed_clock_sequence: int = 0
    last_ran: bool = False
    last_backlog: int = 0
    last_error: str = ""
    last_backpressure_reason: str = ""
    last_fault_reporting_error: str = ""


class ClockJobWorkerFabric:
    """Sole execution-admission owner for runtime-clock domain jobs."""

    def __init__(
        self,
        *,
        job_keys: tuple[str, ...],
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        declared = tuple(str(key or "").strip() for key in job_keys)
        if not declared or any(not key for key in declared):
            raise ValueError("clock_job_worker_fabric_requires_declared_jobs")
        if len(set(declared)) != len(declared):
            raise ValueError("clock_job_worker_fabric_job_keys_must_be_unique")
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("clock_job_worker_fabric_requires_worker_health_authority")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("clock_job_worker_fabric_requires_fault_supervisor")

        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._closed = False
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._states: dict[str, _ClockJobWorkerState] = {}
        for job_key in declared:
            worker_id = f"runtime_clock_job:{job_key}"
            worker_health.register(
                worker_id,
                execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
                required=True,
                retryable=True,
            )
            self._states[job_key] = _ClockJobWorkerState(
                job_key=job_key,
                worker_id=worker_id,
            )
        self._executor = ThreadPoolExecutor(
            max_workers=len(declared),
            thread_name_prefix="arch4-clock-job",
        )

    def dispatch(
        self,
        job: ClockedJob,
        *,
        clock_sequence: int,
        requested_at: float,
        runner: ClockJobRunner,
    ) -> ClockedJobOutcome:
        """Latch one wake and return without waiting for domain completion."""

        if not isinstance(job, ClockedJob):
            raise TypeError("clock_job_worker_fabric_requires_clocked_job")
        if not callable(runner):
            raise TypeError("clock_job_worker_fabric_requires_runner")
        sequence = int(clock_sequence or 0)
        if sequence <= 0:
            raise ValueError("clock_job_worker_fabric_requires_clock_sequence")
        wake = ClockJobWake(
            job=job,
            clock_sequence=sequence,
            requested_at=float(requested_at),
            runner=runner,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("clock_job_worker_fabric_closed")
            state = self._states.get(job.key)
            if state is None:
                raise RuntimeError(f"undeclared_clock_job_worker:{job.key}")
            state.dispatch_count += 1
            state.last_requested_at = wake.requested_at
            state.last_clock_sequence = wake.clock_sequence
            if state.in_flight:
                state.pending = wake
                state.coalesced_wake_count += 1
                return ClockedJobOutcome(
                    ran=True,
                    backlog=1,
                    backpressure_reason="clock_job_latest_wake_latched",
                )
            self._submit_locked(state, wake)
        return ClockedJobOutcome(ran=True)

    def _submit_locked(
        self,
        state: _ClockJobWorkerState,
        wake: ClockJobWake,
    ) -> None:
        if self._closed:
            raise RuntimeError("clock_job_worker_fabric_closed")
        if state.in_flight:
            raise RuntimeError(f"clock_job_worker_already_in_flight:{state.job_key}")
        state.in_flight = True
        state.start_count += 1
        state.last_started_at = time.time()
        state.last_clock_sequence = wake.clock_sequence
        try:
            self._executor.submit(self._run_wake, state.job_key, wake)
        except Exception:
            state.in_flight = False
            state.start_count -= 1
            raise

    @staticmethod
    def _normalize_outcome(result: bool | ClockedJobOutcome) -> ClockedJobOutcome:
        if isinstance(result, ClockedJobOutcome):
            return result
        return ClockedJobOutcome(ran=bool(result))

    def _run_wake(self, job_key: str, wake: ClockJobWake) -> None:
        with self._lock:
            state = self._states[job_key]
            worker_id = state.worker_id
        started_perf = time.perf_counter()
        started_at = time.time()
        self._worker_health.mark_started(worker_id, now=started_at)
        fault_reporting_error = ""
        try:
            outcome = self._normalize_outcome(wake.runner())
        except Exception as exc:
            outcome = ClockedJobOutcome(
                ran=False,
                last_error=f"{type(exc).__name__}: {exc}",
                backpressure_reason="clock_job_iteration_failed",
            )
            self._worker_health.mark_failure(
                worker_id,
                exc,
                next_retry_at=time.time() + 0.1,
            )
            try:
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=worker_id,
                    operation=wake.job.name,
                    generation=wake.clock_sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=str(wake.job.recovery_permission or "defer"),
                    error=exc,
                    suppression_budget=3,
                )
            except Exception as report_exc:
                fault_reporting_error = (
                    f"{type(report_exc).__name__}: {report_exc}"
                )
        else:
            self._worker_health.mark_success(worker_id)

        finished_at = time.time()
        duration_ms = max(0.0, (time.perf_counter() - started_perf) * 1000.0)
        with self._lock:
            state = self._states[job_key]
            state.completion_count += 1
            state.last_finished_at = finished_at
            state.last_duration_ms = duration_ms
            state.last_completed_clock_sequence = wake.clock_sequence
            state.last_ran = bool(outcome.ran)
            state.last_backlog = max(0, int(outcome.backlog or 0))
            state.last_error = str(outcome.last_error or "")
            state.last_backpressure_reason = str(
                outcome.backpressure_reason or ""
            )
            state.last_fault_reporting_error = fault_reporting_error
            if outcome.last_error:
                state.failure_count += 1
            if duration_ms > float(wake.job.budget_ms or 0.0):
                state.over_budget_count += 1
            state.in_flight = False
            pending = state.pending
            state.pending = None
            if pending is not None and not self._closed:
                self._submit_locked(state, pending)
            elif self._closed:
                self._worker_health.mark_stopped(
                    state.worker_id,
                    reason="clock_job_worker_fabric_shutdown",
                )
            self._changed.notify_all()

    def wait_for_completions(
        self,
        minimum_by_job: Mapping[str, int],
        *,
        timeout_s: float,
    ) -> bool:
        """Wait for declared execution receipts without changing job truth."""

        required = {
            str(key): max(0, int(count))
            for key, count in dict(minimum_by_job or {}).items()
        }
        unknown = sorted(set(required) - set(self._states))
        if unknown:
            raise RuntimeError(
                f"undeclared_clock_job_completion_wait:{','.join(unknown)}"
            )
        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        with self._changed:
            while any(
                self._states[key].completion_count < count
                for key, count in required.items()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._changed.wait(timeout=remaining)
            return True

    def wait_until_all_declared_ready(
        self,
        *,
        timeout_s: float,
    ) -> ClockJobWorkerReadinessReceipt:
        """Wait for one clean completion from every declared clock job."""

        started = time.monotonic()
        deadline = started + max(0.0, float(timeout_s or 0.0))
        with self._changed:
            while True:
                declared = tuple(sorted(self._states))
                ready = tuple(
                    key
                    for key in declared
                    if self._states[key].completion_count > 0
                    and not self._states[key].last_error
                )
                pending = tuple(key for key in declared if key not in ready)
                errors = tuple(
                    (key, self._states[key].last_error)
                    for key in pending
                    if self._states[key].last_error
                )
                if not pending:
                    return ClockJobWorkerReadinessReceipt(
                        ready=True,
                        timed_out=False,
                        declared_jobs=declared,
                        ready_jobs=ready,
                        pending_jobs=(),
                        error_jobs=(),
                        waited_s=time.monotonic() - started,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return ClockJobWorkerReadinessReceipt(
                        ready=False,
                        timed_out=True,
                        declared_jobs=declared,
                        ready_jobs=ready,
                        pending_jobs=pending,
                        error_jobs=errors,
                        waited_s=time.monotonic() - started,
                    )
                self._changed.wait(timeout=remaining)

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            rows = {
                key: {
                    "worker_id": state.worker_id,
                    "in_flight": state.in_flight,
                    "pending_latest_wake": state.pending is not None,
                    "pending_clock_sequence": (
                        int(state.pending.clock_sequence)
                        if state.pending is not None
                        else 0
                    ),
                    "dispatch_count": state.dispatch_count,
                    "coalesced_wake_count": state.coalesced_wake_count,
                    "start_count": state.start_count,
                    "completion_count": state.completion_count,
                    "failure_count": state.failure_count,
                    "over_budget_count": state.over_budget_count,
                    "last_requested_at": state.last_requested_at,
                    "last_started_at": state.last_started_at,
                    "last_finished_at": state.last_finished_at,
                    "last_duration_ms": round(state.last_duration_ms, 3),
                    "last_clock_sequence": state.last_clock_sequence,
                    "last_completed_clock_sequence": (
                        state.last_completed_clock_sequence
                    ),
                    "last_ran": state.last_ran,
                    "last_backlog": state.last_backlog,
                    "last_error": state.last_error,
                    "last_backpressure_reason": (
                        state.last_backpressure_reason
                    ),
                    "last_fault_reporting_error": (
                        state.last_fault_reporting_error
                    ),
                }
                for key, state in sorted(self._states.items())
            }
            return {
                "schema_version": "clock_job_worker_fabric.v1",
                "owner": "clock_job_worker_fabric",
                "closed": self._closed,
                "declared_job_count": len(self._states),
                "in_flight_count": sum(
                    1 for state in self._states.values() if state.in_flight
                ),
                "pending_latest_wake_count": sum(
                    1 for state in self._states.values() if state.pending is not None
                ),
                "jobs": rows,
            }

    def shutdown(
        self,
        *,
        timeout_s: float = 6.0,
        cancel_futures: bool = True,
    ) -> ClockJobWorkerTerminationReceipt:
        shutdown_started_at = float(time.time())
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._changed:
            already_closed = self._closed
            self._closed = True
            for state in self._states.values():
                state.pending = None
            while any(state.in_flight for state in self._states.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._changed.wait(timeout=remaining)
            survivors = tuple(
                sorted(
                    state.worker_id
                    for state in self._states.values()
                    if state.in_flight
                )
            )
        self._executor.shutdown(
            wait=False,
            cancel_futures=bool(cancel_futures),
        )
        with self._lock:
            for state in self._states.values():
                if not state.in_flight:
                    self._worker_health.mark_stopped(
                        state.worker_id,
                        reason="clock_job_worker_fabric_shutdown",
                    )
            return ClockJobWorkerTerminationReceipt(
                disposition=(
                    ClockJobWorkerTerminationDisposition.DEGRADED
                    if survivors
                    else ClockJobWorkerTerminationDisposition.ALREADY_STOPPED
                    if already_closed
                    else ClockJobWorkerTerminationDisposition.STOPPED
                ),
                shutdown_started_at=shutdown_started_at,
                finished_at=float(time.time()),
                survivors=survivors,
            )


__all__ = [
    "ClockJobWake",
    "ClockJobWorkerFabric",
    "ClockJobWorkerReadinessReceipt",
    "ClockJobWorkerTerminationDisposition",
    "ClockJobWorkerTerminationReceipt",
]
