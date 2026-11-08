"""Independent liveness owner for Intelligence historical evidence."""

from __future__ import annotations

from threading import Event, RLock, Thread
from time import time
from typing import Any, Callable, Mapping

from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.intelligence.historical_evidence import IntelligenceHistoricalEvidence
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority


WORKER_ID = "intelligence_historical_evidence_supervisor"


class IntelligenceHistoricalEvidenceSupervisor:
    """Poll retry/supersession state without using Core, NGUI, or Brain lanes."""

    def __init__(
        self,
        evidence: IntelligenceHistoricalEvidence,
        *,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
        publish_status: Callable[[Mapping[str, Any]], object],
        interval_s: float = 0.1,
    ) -> None:
        if not isinstance(evidence, IntelligenceHistoricalEvidence):
            raise TypeError("intelligence_historical_evidence_required")
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("intelligence_history_worker_health_required")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("intelligence_history_fault_supervisor_required")
        if not callable(publish_status):
            raise TypeError("intelligence_history_status_publisher_required")
        interval = float(interval_s)
        if interval <= 0.0:
            raise ValueError("intelligence_history_positive_supervisor_interval_required")
        self.evidence = evidence
        self.worker_health = worker_health
        self.fault_supervisor = fault_supervisor
        self.publish_status = publish_status
        self.interval_s = interval
        self._stop = Event()
        self._lock = RLock()
        self._thread: Thread | None = None
        self._poll_count = 0
        self._failure_count = 0
        self._last_error = ""
        self._last_poll_at = 0.0
        self.worker_health.register(
            WORKER_ID,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=False,
            retryable=True,
        )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="intelligence-history-supervisor",
                daemon=True,
            )
            self.worker_health.mark_started(WORKER_ID)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.evidence.service()
                self.publish_status(self.evidence.presentation_status())
                with self._lock:
                    self._poll_count += 1
                    self._last_poll_at = time()
                    self._last_error = ""
                self.worker_health.mark_success(WORKER_ID)
            except Exception as exc:
                with self._lock:
                    self._failure_count += 1
                    self._last_poll_at = time()
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    generation = self._poll_count + self._failure_count
                self.worker_health.mark_failure(
                    WORKER_ID,
                    exc,
                    next_retry_at=time() + self.interval_s,
                )
                try:
                    self.fault_supervisor.record(
                        boundary=FaultBoundaryClass.WORKER,
                        owner=WORKER_ID,
                        operation="poll",
                        generation=generation,
                        severity=FaultSeverity.ERROR,
                        retryable=True,
                        degraded_behavior=(
                            "core_ngui_and_intelligence_live_observation_continue"
                        ),
                        error=exc,
                        suppression_budget=3,
                    )
                except Exception as reporting_exc:
                    # WorkerHealth is the independent typed evidence boundary
                    # when the diagnostic journal itself is degraded.
                    with self._lock:
                        self._last_error = (
                            f"{self._last_error}; fault_report_failed:"
                            f"{type(reporting_exc).__name__}: {reporting_exc}"
                        )
                    self.worker_health.mark_failure(
                        WORKER_ID,
                        reporting_exc,
                        next_retry_at=time() + self.interval_s,
                    )
            self._stop.wait(self.interval_s)
        self.worker_health.mark_stopped(WORKER_ID, reason="supervisor_shutdown")

    def shutdown(self, *, timeout_s: float) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_s)))
        alive = bool(thread is not None and thread.is_alive())
        return {**self.status(), "stopped": not alive}

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            return {
                "schema": "IntelligenceHistoricalEvidenceSupervisorStatus.v1",
                "owner": "54guc.intelligence.historical_evidence_supervisor",
                "running": bool(thread is not None and thread.is_alive()),
                "poll_count": self._poll_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
                "last_poll_at": self._last_poll_at,
            }


__all__ = ("IntelligenceHistoricalEvidenceSupervisor", "WORKER_ID")
