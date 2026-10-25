"""Process-local supervision for NGUI workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arch4.event_journal import ForensicEventJournal
from arch4.fault_supervisor import FaultSupervisor
from arch4.worker_health import WorkerHealthAuthority
from .fault_evidence import NGUIFaultEvidenceJournal


@dataclass(frozen=True, slots=True)
class NGUISupervision:
    worker_health: WorkerHealthAuthority
    fault_supervisor: FaultSupervisor
    fault_evidence: NGUIFaultEvidenceJournal


def build_ngui_supervision(
    report_error: Callable[[str], None],
    runtime_dir: str | Path,
) -> NGUISupervision:
    if not callable(report_error):
        raise TypeError("ngui_supervision_report_error_required")
    fault_evidence = NGUIFaultEvidenceJournal(
        runtime_dir,
        report_error=report_error,
    )
    worker_health = WorkerHealthAuthority()
    fault_supervisor = FaultSupervisor(
        ForensicEventJournal(fault_evidence)
    )
    return NGUISupervision(
        worker_health=worker_health,
        fault_supervisor=fault_supervisor,
        fault_evidence=fault_evidence,
    )


__all__ = ["NGUISupervision", "build_ngui_supervision"]
