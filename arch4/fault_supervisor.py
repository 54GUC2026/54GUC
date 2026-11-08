"""Typed fault classification and evidence authority.

Suppression budgets are diagnostic thresholds only. Worker liveness and retry
state belong exclusively to :mod:`arch4.worker_health`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Any

from arch4.event_journal import (
    ControllerEventKind,
    EvidenceDeliveryFault,
    ForensicEventJournal,
)


class FaultSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class FaultBoundaryClass(str, Enum):
    PROCESS = "process"
    WORKER = "worker"
    STORAGE = "storage"
    CODEC = "codec"
    GUI = "gui"
    HARDWARE = "hardware"


@dataclass(frozen=True)
class FaultRecord:
    boundary: FaultBoundaryClass
    owner: str
    operation: str
    generation: int
    severity: FaultSeverity
    retryable: bool
    degraded_behavior: str
    error_type: str
    detail: str
    occurrence: int
    suppression_budget: int
    ts: float
    incident_id: str = ""

    @property
    def budget_exhausted(self) -> bool:
        return self.occurrence > self.suppression_budget

    def to_payload(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.value,
            "owner": self.owner,
            "operation": self.operation,
            "generation": self.generation,
            "severity": self.severity.value,
            "retryable": self.retryable,
            "degraded_behavior": self.degraded_behavior,
            "error_type": self.error_type,
            "detail": self.detail,
            "occurrence": self.occurrence,
            "suppression_budget": self.suppression_budget,
            "budget_exhausted": self.budget_exhausted,
            "incident_id": self.incident_id,
        }


@dataclass(frozen=True)
class FaultRecoveryRecord:
    boundary: FaultBoundaryClass
    owner: str
    operation: str
    generation: int
    detail: str
    occurrence: int
    ts: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.value,
            "owner": self.owner,
            "operation": self.operation,
            "generation": self.generation,
            "detail": self.detail,
            "occurrence": self.occurrence,
            "recovered": True,
        }


class FaultSupervisor:
    def __init__(self, journal: ForensicEventJournal):
        if not isinstance(journal, ForensicEventJournal):
            raise TypeError("fault_supervisor_requires_event_journal")
        self._journal = journal
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, str], int] = {}
        self._recovery_counts: dict[tuple[str, str], int] = {}
        self._last_fault: FaultRecord | None = None
        self._last_recovery: FaultRecoveryRecord | None = None
        self._journal_failure_count = 0
        self._last_journal_error = ""
        self._evidence_delivery_fault_count = 0
        self._last_evidence_delivery_fault: EvidenceDeliveryFault | None = None
        self._journal.bind_delivery_fault_sink(self)

    def record_evidence_delivery_fault(
        self,
        fault: EvidenceDeliveryFault,
    ) -> None:
        if not isinstance(fault, EvidenceDeliveryFault):
            raise TypeError("fault_supervisor_evidence_delivery_fault_required")
        with self._lock:
            self._evidence_delivery_fault_count += 1
            self._last_evidence_delivery_fault = fault
            # Keep the established status projection while retaining the
            # dedicated typed evidence-fault surface separately.
            self._journal_failure_count += 1
            self._last_journal_error = f"{fault.error_type}: {fault.detail}"

    def record(
        self,
        *,
        boundary: FaultBoundaryClass,
        owner: str,
        operation: str,
        generation: int,
        severity: FaultSeverity,
        retryable: bool,
        degraded_behavior: str,
        error: BaseException,
        suppression_budget: int,
        incident_id: str = "",
    ) -> FaultRecord:
        if not isinstance(boundary, FaultBoundaryClass):
            raise TypeError("fault_boundary_class_required")
        key = (str(owner), str(operation), type(error).__name__)
        with self._lock:
            occurrence = self._counts.get(key, 0) + 1
            self._counts[key] = occurrence
            record = FaultRecord(
                boundary=boundary,
                owner=str(owner or "unknown"),
                operation=str(operation or "unknown"),
                generation=int(generation or 0),
                severity=severity,
                retryable=bool(retryable),
                degraded_behavior=str(degraded_behavior or "none"),
                error_type=type(error).__name__,
                detail=str(error),
                occurrence=occurrence,
                suppression_budget=max(0, int(suppression_budget)),
                ts=time.time(),
                incident_id=str(incident_id or ""),
            )
            self._last_fault = record
        self._journal.publish(
            ControllerEventKind.FAULT,
            generation=record.generation,
            owner="fault_supervisor",
            payload=record.to_payload(),
            ts=record.ts,
        )
        return record

    def record_recovery(
        self,
        *,
        boundary: FaultBoundaryClass,
        owner: str,
        operation: str,
        generation: int,
        detail: str,
    ) -> FaultRecoveryRecord:
        """Retain proof that one previously degraded boundary recovered."""

        if not isinstance(boundary, FaultBoundaryClass):
            raise TypeError("fault_boundary_class_required")
        normalized_owner = str(owner or "unknown")
        normalized_operation = str(operation or "unknown")
        key = (normalized_owner, normalized_operation)
        with self._lock:
            occurrence = self._recovery_counts.get(key, 0) + 1
            self._recovery_counts[key] = occurrence
            record = FaultRecoveryRecord(
                boundary=boundary,
                owner=normalized_owner,
                operation=normalized_operation,
                generation=int(generation or 0),
                detail=str(detail or "recovered"),
                occurrence=occurrence,
                ts=time.time(),
            )
            self._last_recovery = record
        self._journal.publish(
            ControllerEventKind.RECOVERY,
            generation=record.generation,
            owner="fault_supervisor",
            payload=record.to_payload(),
            ts=record.ts,
        )
        return record

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": "fault_supervisor",
                "fault_count": sum(self._counts.values()),
                "by_key": {
                    ":".join(key): count
                    for key, count in sorted(self._counts.items())
                },
                "last_fault": self._last_fault.to_payload() if self._last_fault else None,
                "recovery_count": sum(self._recovery_counts.values()),
                "last_recovery": (
                    self._last_recovery.to_payload()
                    if self._last_recovery is not None
                    else None
                ),
                "journal_failure_count": self._journal_failure_count,
                "last_journal_error": self._last_journal_error,
                "evidence_delivery_fault_count": self._evidence_delivery_fault_count,
                "last_evidence_delivery_fault": (
                    self._last_evidence_delivery_fault.to_payload()
                    if self._last_evidence_delivery_fault is not None
                    else None
                ),
            }


def require_fault_supervisor(owner: object) -> FaultSupervisor:
    supervisor = getattr(owner, "fault_supervisor", None)
    if not isinstance(supervisor, FaultSupervisor):
        raise RuntimeError("fault_supervisor_unavailable")
    return supervisor


__all__ = [
    "FaultBoundaryClass",
    "FaultRecord",
    "FaultRecoveryRecord",
    "FaultSeverity",
    "FaultSupervisor",
    "require_fault_supervisor",
]
