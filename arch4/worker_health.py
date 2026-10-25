"""Canonical runtime worker and finite-job health authority.

Fault evidence and execution health are deliberately separate contracts:
``FaultSupervisor`` records what failed, while this authority owns whether a
continuous service is live/recovering or a finite job completed/failed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock
from time import time
from typing import Any, Mapping


class WorkerHealthState(str, Enum):
    REGISTERED = "registered"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    TERMINAL = "terminal"


class WorkerExecutionKind(str, Enum):
    CONTINUOUS_SERVICE = "continuous_service"
    FINITE_JOB = "finite_job"


@dataclass(frozen=True, slots=True)
class WorkerHealthRecord:
    worker_id: str
    execution_kind: WorkerExecutionKind
    required: bool
    retryable: bool
    state: WorkerHealthState
    registered_at: float
    started_at: float = 0.0
    updated_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    next_retry_at: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    failure_streak: int = 0
    active_execution_count: int = 0
    last_error_type: str = ""
    last_error: str = ""
    stop_reason: str = ""
    terminal_reason: str = ""

    def is_recovering(self, *, now: float) -> bool:
        return (
            self.execution_kind is WorkerExecutionKind.CONTINUOUS_SERVICE
            and self.retryable
            and self.state is WorkerHealthState.RETRYING
            and self.next_retry_at > 0.0
            and float(now) <= self.next_retry_at
        )

    def is_healthy(self, *, now: float) -> bool:
        if self.execution_kind is WorkerExecutionKind.CONTINUOUS_SERVICE:
            return (
                self.state is WorkerHealthState.RUNNING
                and not self.last_error
            ) or self.is_recovering(now=now)
        return self.state in {
            WorkerHealthState.REGISTERED,
            WorkerHealthState.RUNNING,
            WorkerHealthState.COMPLETED,
        } and not self.last_error

    def to_payload(self, *, now: float) -> dict[str, Any]:
        return {
            "schema": "WorkerHealthRecord.v2",
            "owner": "worker_health_authority",
            "worker_id": self.worker_id,
            "execution_kind": self.execution_kind.value,
            "required": self.required,
            "retryable": self.retryable,
            "state": self.state.value,
            "healthy": self.is_healthy(now=now),
            "recovering": self.is_recovering(now=now),
            "completed": self.state is WorkerHealthState.COMPLETED,
            "failed": self.state in {
                WorkerHealthState.FAILED,
                WorkerHealthState.TERMINAL,
            },
            "registered_at": self.registered_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "next_retry_at": self.next_retry_at,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "failure_streak": self.failure_streak,
            "active_execution_count": self.active_execution_count,
            "last_error_type": self.last_error_type,
            "last_error": self.last_error,
            "stop_reason": self.stop_reason,
            "terminal_reason": self.terminal_reason,
        }


class WorkerHealthAuthority:
    """The sole mutable owner of worker liveness and retry state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, WorkerHealthRecord] = {}

    @staticmethod
    def _identity(worker_id: str) -> str:
        identity = str(worker_id or "").strip()
        if not identity:
            raise ValueError("worker_health_identity_required")
        return identity

    def register(
        self,
        worker_id: str,
        *,
        execution_kind: WorkerExecutionKind,
        required: bool,
        retryable: bool,
        now: float | None = None,
    ) -> WorkerHealthRecord:
        identity = self._identity(worker_id)
        if not isinstance(execution_kind, WorkerExecutionKind):
            raise TypeError(
                f"worker_health_execution_kind_required:{identity}"
            )
        observed_at = float(time() if now is None else now)
        with self._lock:
            existing = self._records.get(identity)
            if existing is not None:
                if (
                    existing.required is not bool(required)
                    or existing.retryable is not bool(retryable)
                    or existing.execution_kind is not execution_kind
                ):
                    raise RuntimeError(
                        f"worker_health_registration_conflict:{identity}"
                    )
                return existing
            record = WorkerHealthRecord(
                worker_id=identity,
                execution_kind=execution_kind,
                required=bool(required),
                retryable=bool(retryable),
                state=WorkerHealthState.REGISTERED,
                registered_at=observed_at,
                updated_at=observed_at,
            )
            self._records[identity] = record
            return record

    def _record(self, worker_id: str) -> WorkerHealthRecord:
        identity = self._identity(worker_id)
        record = self._records.get(identity)
        if record is None:
            raise RuntimeError(f"worker_health_not_registered:{identity}")
        return record

    def mark_started(
        self, worker_id: str, *, now: float | None = None
    ) -> WorkerHealthRecord:
        observed_at = float(time() if now is None else now)
        with self._lock:
            current = self._record(worker_id)
            if current.state is WorkerHealthState.TERMINAL:
                raise RuntimeError(
                    f"worker_health_terminal_restart_forbidden:{current.worker_id}"
                )
            updated = replace(
                current,
                state=WorkerHealthState.RUNNING,
                started_at=observed_at,
                updated_at=observed_at,
                next_retry_at=0.0,
                active_execution_count=(
                    current.active_execution_count + 1
                    if current.execution_kind is WorkerExecutionKind.FINITE_JOB
                    else 1
                ),
                stop_reason="",
            )
            self._records[current.worker_id] = updated
            return updated

    def mark_success(
        self, worker_id: str, *, now: float | None = None
    ) -> WorkerHealthRecord:
        observed_at = float(time() if now is None else now)
        with self._lock:
            current = self._record(worker_id)
            if current.state is WorkerHealthState.TERMINAL:
                raise RuntimeError(
                    f"worker_health_terminal_success_forbidden:{current.worker_id}"
                )
            updated = replace(
                current,
                state=(
                    WorkerHealthState.COMPLETED
                    if current.execution_kind is WorkerExecutionKind.FINITE_JOB
                    and current.active_execution_count <= 1
                    else WorkerHealthState.RUNNING
                ),
                updated_at=observed_at,
                last_success_at=observed_at,
                next_retry_at=0.0,
                success_count=current.success_count + 1,
                failure_streak=0,
                active_execution_count=(
                    max(0, current.active_execution_count - 1)
                    if current.execution_kind is WorkerExecutionKind.FINITE_JOB
                    else 1
                ),
                last_error_type="",
                last_error="",
            )
            self._records[current.worker_id] = updated
            return updated

    def mark_failure(
        self,
        worker_id: str,
        error: BaseException,
        *,
        next_retry_at: float,
        now: float | None = None,
    ) -> WorkerHealthRecord:
        observed_at = float(time() if now is None else now)
        with self._lock:
            current = self._record(worker_id)
            if current.state is WorkerHealthState.TERMINAL:
                raise RuntimeError(
                    f"worker_health_terminal_failure_forbidden:{current.worker_id}"
                )
            if not current.retryable and float(next_retry_at or 0.0) > 0.0:
                raise RuntimeError(
                    f"worker_health_retry_forbidden:{current.worker_id}"
                )
            retry_at = max(0.0, float(next_retry_at or 0.0))
            recovering = (
                current.execution_kind is WorkerExecutionKind.CONTINUOUS_SERVICE
                and current.retryable
                and retry_at > observed_at
            )
            updated = replace(
                current,
                state=(
                    WorkerHealthState.RETRYING
                    if recovering
                    else WorkerHealthState.FAILED
                ),
                updated_at=observed_at,
                last_failure_at=observed_at,
                next_retry_at=retry_at,
                failure_count=current.failure_count + 1,
                failure_streak=current.failure_streak + 1,
                active_execution_count=(
                    max(0, current.active_execution_count - 1)
                    if current.execution_kind is WorkerExecutionKind.FINITE_JOB
                    else current.active_execution_count
                ),
                last_error_type=type(error).__name__,
                last_error=str(error),
            )
            self._records[current.worker_id] = updated
            return updated

    def mark_stopped(
        self,
        worker_id: str,
        *,
        reason: str,
        now: float | None = None,
    ) -> WorkerHealthRecord:
        observed_at = float(time() if now is None else now)
        with self._lock:
            current = self._record(worker_id)
            if current.execution_kind is WorkerExecutionKind.FINITE_JOB:
                raise RuntimeError(
                    f"worker_health_finite_job_stop_forbidden:{current.worker_id}"
                )
            if current.state is WorkerHealthState.TERMINAL:
                return current
            updated = replace(
                current,
                state=WorkerHealthState.STOPPED,
                updated_at=observed_at,
                next_retry_at=0.0,
                active_execution_count=0,
                stop_reason=str(reason or "stopped"),
            )
            self._records[current.worker_id] = updated
            return updated

    def mark_terminal(
        self,
        worker_id: str,
        error: BaseException,
        *,
        reason: str,
        now: float | None = None,
    ) -> WorkerHealthRecord:
        observed_at = float(time() if now is None else now)
        with self._lock:
            current = self._record(worker_id)
            updated = replace(
                current,
                state=WorkerHealthState.TERMINAL,
                updated_at=observed_at,
                last_failure_at=observed_at,
                next_retry_at=0.0,
                failure_count=current.failure_count + 1,
                failure_streak=current.failure_streak + 1,
                active_execution_count=0,
                last_error_type=type(error).__name__,
                last_error=str(error),
                terminal_reason=str(reason or "terminal_exit"),
            )
            self._records[current.worker_id] = updated
            return updated

    def record(self, worker_id: str) -> WorkerHealthRecord:
        with self._lock:
            return self._record(worker_id)

    def status(self, *, now: float | None = None) -> Mapping[str, Any]:
        observed_at = float(time() if now is None else now)
        with self._lock:
            records = tuple(self._records[key] for key in sorted(self._records))
        failed_required = tuple(
            row.worker_id
            for row in records
            if row.required
            and row.state in {
                WorkerHealthState.FAILED,
                WorkerHealthState.TERMINAL,
            }
        )
        stopped_required = tuple(
            row.worker_id
            for row in records
            if row.required and row.state is WorkerHealthState.STOPPED
        )
        unhealthy_required = tuple(
            row.worker_id
            for row in records
            if row.required and not row.is_healthy(now=observed_at)
        )
        recovering_required = tuple(
            row.worker_id
            for row in records
            if row.required and row.is_recovering(now=observed_at)
        )
        optional_failed = tuple(
            row.worker_id
            for row in records
            if not row.required
            and row.state in {
                WorkerHealthState.FAILED,
                WorkerHealthState.TERMINAL,
            }
        )
        return {
            "schema": "WorkerHealthAuthority.v2",
            "owner": "worker_health_authority",
            "healthy": not unhealthy_required,
            "degraded": bool(recovering_required),
            "failed": bool(unhealthy_required),
            "required_unhealthy": list(unhealthy_required),
            "required_recovering": list(recovering_required),
            "required_failed": list(failed_required),
            "required_stopped": list(stopped_required),
            "optional_failed": list(optional_failed),
            "workers": {
                row.worker_id: row.to_payload(now=observed_at)
                for row in records
            },
        }


def require_worker_health_authority(owner: object) -> WorkerHealthAuthority:
    authority = getattr(owner, "worker_health", None)
    if not isinstance(authority, WorkerHealthAuthority):
        raise RuntimeError("worker_health_authority_unavailable")
    return authority


__all__ = [
    "WorkerHealthAuthority",
    "WorkerExecutionKind",
    "WorkerHealthRecord",
    "WorkerHealthState",
    "require_worker_health_authority",
]
