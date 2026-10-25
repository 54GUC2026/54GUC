"""Sole Intelligence owner for same-modem historical evidence.

This independently supervised customer owns scan, atomic commit, immutable
generation publication, optional chart service, retry, and supersession.  It
never calls NGUI, Foundation settings, Engine, or Modem Live.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future
import hashlib
import json
import logging
from pathlib import Path
from threading import RLock
import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.core_contracts.accepted_root_outbox import CoreAcceptedRootOutboxPort
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.evidence_scope import EvidenceScope, validated_evidence_scope
from arch4.external_memory_evidence_scope import (
    external_memory_formula_vector,
    external_projection_scope,
)
from arch4.external_memory_runtime import (
    external_memory_prior_status,
    load_external_memory_prior_pack,
)
from arch4.customer_contracts.historical_evidence import (
    HistoricalEvidenceChartRequest,
    HistoricalEvidenceChartResult,
    HistoricalEvidenceGeneration,
    HistoricalEvidenceImportRequest,
    HistoricalEvidenceStatus,
)
from arch4.intelligence.historical_chart_reader import read_historical_chart
from arch4.historical_continuity_process import (
    HistoricalContinuityBulkExecutionPort,
    HistoricalContinuityBulkRequest,
    HistoricalContinuityChildFault,
)
from arch4.historical_continuity_store import (
    AUTO_STATUS_FILENAME,
    CATALOG_FILENAME,
    CHART_HISTORY_FILENAME,
    cleanup_abandoned_staging,
    commit_staged_generation,
    committed_artifact_path,
    committed_generation_status,
    create_staging_generation,
    discard_staging_generation,
)
from arch4.fault_supervisor import (
    FaultSupervisor,
    FaultBoundaryClass,
    FaultSeverity,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority
from arch4.portable_state_import import (
    admit_confirmed_next_start_handoff_evidence,
)


log = logging.getLogger("arch4.intelligence.historical_evidence")

SCHEMA = "IntelligenceHistoricalEvidenceState.v1"
OWNER = "54guc.intelligence.historical_evidence"
STATUS_FILENAME = "historical_evidence_status.json"
MAX_AGE_DAYS = 14
MIN_TELEMETRY_ROWS = 100
RETRY_BASE_DELAY_S = 5.0
RETRY_MAX_DELAY_S = 300.0
CONSUMERS = (
    "historical_evidence_generation",
    "ngui_chart_history",
)
INTELLIGENCE_CONSUMERS = (
    "self_learning_prior",
    "crystal_ball_advisory",
    "self_calibration_advisory",
)
SERVICE_CLIENT_ID = "intelligence.historical_evidence"
RESTORE_WORKER_ID = "intelligence_historical_evidence_restore"
HISTORICAL_PRESENTATION_SOURCES = (
    "history.auto_import_status",
    "history.brain_influence",
    "history.build_age",
    "history.channel",
    "history.fact_count",
    "history.history_window",
    "history.import_status",
    "history.matched_tier",
    "history.merged_packs",
    "history.observation_count",
    "history.predictive_window",
    "history.prior_source",
    "history.soak_status",
    "history.workflow_status",
)


class HistoricalEvidenceRequestSuperseded(RuntimeError):
    """A valid latched request overtaken by a newer Core target namespace."""

    def __init__(self, *, transaction_id: str) -> None:
        self.transaction_id = str(transaction_id or "")
        self.reason = (
            "confirmed-login namespace was superseded before Intelligence execution"
        )
        super().__init__(self.reason)


class IntelligenceHistoricalEvidence:
    """Own one restart-safe, non-blocking bulk continuity transaction."""

    def __init__(
        self,
        *,
        stage_dir: str | Path,
        runtime_dir: str | Path,
        memory_dir: str | Path,
        executor: Executor,
        bulk_execution: HistoricalContinuityBulkExecutionPort,
        accepted_root_outbox: CoreAcceptedRootOutboxPort,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        self.stage_dir = Path(stage_dir).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.memory_dir = Path(memory_dir).resolve()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.executor = executor
        self.bulk_execution = bulk_execution
        for method_name in ("claim_next", "acknowledge", "release", "status"):
            if not callable(getattr(accepted_root_outbox, method_name, None)):
                raise TypeError("historical_evidence_accepted_root_port_required")
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("historical_evidence_worker_health_required")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("historical_evidence_fault_supervisor_required")
        self.accepted_root_outbox = accepted_root_outbox
        self.worker_health = worker_health
        self.fault_supervisor = fault_supervisor
        self.status_path = self.memory_dir / STATUS_FILENAME
        self._lock = RLock()
        self._future: Future[Any] | None = None
        self._last_progress_write = 0.0
        self._latest_target: dict[str, Any] = {}
        self._latest_observation_key = ""
        cleanup_abandoned_staging(self.memory_dir)
        self._state = self._restore()
        self._status_sequence = max(
            1,
            int(self._state.get("status_sequence", 1) or 1),
        )

    def request(
        self,
        request: HistoricalEvidenceImportRequest,
    ) -> HistoricalEvidenceStatus:
        """Queue work and return immediately; never scan on the caller lane."""

        if not isinstance(request, HistoricalEvidenceImportRequest):
            raise TypeError("historical_evidence_typed_request_required")
        self.consume_core_observations()
        with self._lock:
            target = dict(self._latest_target)
        try:
            scope = external_projection_scope(target, evidence_count=1)
        except (TypeError, ValueError) as exc:
            self._publish(
                status="waiting_for_identity",
                phase="identity_gate",
                progress_pct=0.0,
                reason=f"exact accepted modem identity unavailable: {exc}",
            )
            return self.status()
        if not str(target.get("provider_plmn") or target.get("provider_name") or "").strip():
            self._publish(
                status="waiting_for_identity",
                phase="identity_gate",
                progress_pct=0.0,
                reason="provider identity unavailable",
            )
            return self.status()
        transaction_id = _transaction_id(scope)
        login_scope = request.confirmed_login_scope.to_mapping()
        handoff_receipt = request.confirmed_handoff_receipt.to_mapping()
        sources = tuple(
            str(Path(path).expanduser().resolve())
            for path in request.preferred_sources
            if str(path or "").strip()
        )
        with self._lock:
            running = self._future is not None and not self._future.done()
            current_id = str(self._state.get("transaction_id") or "")
            current_status = str(self._state.get("status") or "")
            previously_admitted = bool(
                current_id == transaction_id
                and isinstance(self._state.get("confirmed_login_scope"), Mapping)
                and bool(self._state.get("confirmed_login_scope"))
            )
            if login_scope:
                try:
                    admitted_scope = validated_evidence_scope(login_scope)
                    target_scope = validated_evidence_scope(
                        target.get("evidence_scope")
                        if isinstance(target.get("evidence_scope"), Mapping)
                        else {}
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"historical continuity confirmed-login scope invalid: {exc}"
                    ) from exc
                if not admitted_scope.same_physical_namespace(target_scope):
                    raise HistoricalEvidenceRequestSuperseded(
                        transaction_id=transaction_id,
                    )
            elif previously_admitted:
                login_scope = dict(self._state.get("confirmed_login_scope") or {})
                if not handoff_receipt:
                    handoff_receipt = dict(
                        self._state.get("confirmed_handoff_receipt") or {}
                    )
            else:
                raise RuntimeError(
                    "historical continuity import requires exact confirmed-login scope"
                )
            if running:
                if current_id != transaction_id:
                    self._state.update(
                        {
                            "pending_transaction_id": transaction_id,
                            "pending_preferred_sources": list(sources),
                            "pending_force": request.force,
                            "pending_max_age_days": request.max_age_days,
                            "pending_min_telemetry_rows": request.min_telemetry_rows,
                            "pending_confirmed_login_scope": login_scope,
                            "pending_confirmed_handoff_receipt": handoff_receipt,
                            "updated_at_ts": time.time(),
                        }
                    )
                    self._mark_state_changed_locked()
                    self._save_locked()
                return self._status_contract_locked()
            if (
                current_id == transaction_id
                and current_status == "complete"
                and not request.force
            ):
                return self._status_contract_locked()
            attempt = (
                int(self._state.get("attempt", 0) or 0) + 1
                if current_id == transaction_id
                else 1
            )
            self._state = self._state_payload(
                transaction_id=transaction_id,
                status="queued",
                phase="background_queue",
                progress_pct=1.0,
                reason="urgent confirmed-login import completed; bulk import queued",
                target_evidence_scope=scope.as_dict(),
                preferred_sources=list(sources),
                confirmed_login_scope=login_scope,
                confirmed_handoff_receipt=handoff_receipt,
                request_id=request.request_id,
                max_age_days=request.max_age_days,
                min_telemetry_rows=request.min_telemetry_rows,
                attempt=attempt,
                started_at_ts=time.time(),
                finished_at_ts=0.0,
                deliveries={consumer: "pending" for consumer in CONSUMERS},
                intelligence_deliveries={
                    consumer: "unattached"
                    for consumer in INTELLIGENCE_CONSUMERS
                },
                errors={},
            )
            self._mark_state_changed_locked()
            self._save_locked()
            worker_health = self.worker_health
            worker_id = "intelligence_historical_evidence_import"
            worker_health.register(
                worker_id,
                execution_kind=WorkerExecutionKind.FINITE_JOB,
                required=False,
                retryable=True,
            )
            worker_health.mark_started(worker_id)
            try:
                self._future = self.executor.submit(
                    self._run,
                    target,
                    sources,
                    request.force,
                    request.max_age_days,
                    request.min_telemetry_rows,
                    transaction_id,
                    handoff_receipt,
                    attempt,
                )
            except Exception as exc:
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time() + _retry_delay_s(attempt),
                )
                self.fault_supervisor.record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=worker_id,
                    operation="submit",
                    generation=attempt,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="history_import_retry_pending",
                    error=exc,
                    suppression_budget=3,
                )
                self._finish(
                    transaction_id,
                    status="retry_pending",
                    phase="background_queue",
                    reason=f"{type(exc).__name__}: {exc}",
                )
                return self._status_contract_locked()

            def observe(completed: Future[Any]) -> None:
                try:
                    completed.result()
                except Exception as exc:
                    worker_health.mark_terminal(
                        worker_id,
                        exc,
                        reason="history_import_future_uncaught",
                    )
                    self.fault_supervisor.record(
                        boundary=FaultBoundaryClass.WORKER,
                        owner=worker_id,
                        operation="future_result",
                        generation=attempt,
                        severity=FaultSeverity.ERROR,
                        retryable=False,
                        degraded_behavior="history_import_terminal_evidence_retained",
                        error=exc,
                        suppression_budget=0,
                    )
                    return
                with self._lock:
                    status = dict(self._state)
                if str(status.get("status") or "") == "complete":
                    worker_health.mark_success(worker_id)
                    return
                error = RuntimeError(
                    str(status.get("reason") or "history import retry pending")
                )
                worker_health.mark_failure(
                    worker_id,
                    error,
                    next_retry_at=float(status.get("next_retry_at_ts", 0.0) or 0.0),
                )

            self._future.add_done_callback(observe)
            return self._status_contract_locked()

    def service(
        self,
        *,
        now_ts: float | None = None,
    ) -> HistoricalEvidenceStatus:
        """Restart admitted retry work without scanning on the service caller lane."""

        now = float(time.time() if now_ts is None else now_ts)
        self.consume_core_observations()
        with self._lock:
            snapshot = dict(self._state)
            running = self._future is not None and not self._future.done()
            target = dict(self._latest_target)
        try:
            current_transaction = _transaction_id(
                external_projection_scope(target, evidence_count=1)
            )
        except (TypeError, ValueError):
            return self.status()
        state_transaction = str(snapshot.get("transaction_id") or "")
        if current_transaction != state_transaction:
            if (
                not running
                and bool(snapshot.get("pending_confirmed_login_scope"))
                and current_transaction
                == str(snapshot.get("pending_transaction_id") or "")
            ):
                return self.request(
                    HistoricalEvidenceImportRequest(
                        request_id=(
                            "historical-evidence-pending-"
                            + str(snapshot.get("pending_transaction_id") or "")
                        ),
                        created_at=now,
                        preferred_sources=tuple(
                            snapshot.get("pending_preferred_sources") or ()
                        ),
                        confirmed_login_scope=CoreFrozenPayload.from_mapping(
                            dict(snapshot.get("pending_confirmed_login_scope") or {})
                        ),
                        confirmed_handoff_receipt=CoreFrozenPayload.from_mapping(
                            dict(snapshot.get("pending_confirmed_handoff_receipt") or {})
                        ),
                        force=bool(snapshot.get("pending_force", False)),
                        max_age_days=int(
                            snapshot.get("pending_max_age_days") or MAX_AGE_DAYS
                        ),
                        min_telemetry_rows=int(
                            snapshot.get("pending_min_telemetry_rows")
                            or MIN_TELEMETRY_ROWS
                        ),
                    ),
                )
            return self._admit_current_handoff(
                target,
                transaction_id=current_transaction,
                now_ts=now,
            )
        if snapshot.get("status") == "no_source":
            if now < float(snapshot.get("next_retry_at_ts", 0.0) or 0.0):
                return self.status()
            return self._admit_current_handoff(
                target,
                transaction_id=current_transaction,
                now_ts=now,
            )
        if snapshot.get("status") != "retry_pending":
            return self.status()
        if not snapshot.get("confirmed_login_scope"):
            return self.status()
        if now < float(snapshot.get("next_retry_at_ts", 0.0) or 0.0):
            return self.status()
        if running:
            return self.status()
        return self.request(
            HistoricalEvidenceImportRequest(
                request_id=(
                    "historical-evidence-retry-"
                    + str(snapshot.get("transaction_id") or "")
                    + "-"
                    + str(int(snapshot.get("attempt", 0) or 0) + 1)
                ),
                created_at=now,
                preferred_sources=tuple(snapshot.get("preferred_sources") or ()),
                confirmed_login_scope=CoreFrozenPayload.from_mapping(
                    dict(snapshot.get("confirmed_login_scope") or {})
                ),
                confirmed_handoff_receipt=CoreFrozenPayload.from_mapping(
                    dict(snapshot.get("confirmed_handoff_receipt") or {})
                ),
                max_age_days=int(snapshot.get("max_age_days") or MAX_AGE_DAYS),
                min_telemetry_rows=int(
                    snapshot.get("min_telemetry_rows") or MIN_TELEMETRY_ROWS
                ),
            ),
        )

    def _admit_current_handoff(
        self,
        target: Mapping[str, Any],
        *,
        transaction_id: str,
        now_ts: float,
    ) -> HistoricalEvidenceStatus:
        """Run the read-only archive admission only on the Intelligence lane."""

        receipt = admit_confirmed_next_start_handoff_evidence(
            self.stage_dir,
            confirmed_identity=target,
            runtime_memory_dir=self.memory_dir,
        )
        if not receipt:
            self._publish(
                status="no_source",
                phase="archive_discovery",
                progress_pct=0.0,
                reason="no exact-modem historical handoff is available",
                transaction_id=transaction_id,
                confirmed_login_scope=dict(target.get("evidence_scope") or {}),
                confirmed_handoff_receipt={},
                next_retry_at_ts=now_ts + RETRY_MAX_DELAY_S,
            )
            return self.status()
        return self.request(
            HistoricalEvidenceImportRequest(
                request_id=(
                    "intelligence-historical-evidence-auto-" + transaction_id
                ),
                created_at=now_ts,
                confirmed_login_scope=CoreFrozenPayload.from_mapping(
                    dict(target.get("evidence_scope") or {})
                ),
                confirmed_handoff_receipt=CoreFrozenPayload.from_mapping(receipt),
                max_age_days=MAX_AGE_DAYS,
                min_telemetry_rows=MIN_TELEMETRY_ROWS,
            )
        )

    def status(self) -> HistoricalEvidenceStatus:
        with self._lock:
            return self._status_contract_locked()

    def _status_contract_locked(self) -> HistoricalEvidenceStatus:
        detail = dict(self._state)
        return HistoricalEvidenceStatus(
            sequence=max(1, self._status_sequence),
            published_at=float(
                detail.get("updated_at_ts") or time.time()
            ),
            status=str(detail.get("status") or "idle"),
            phase=str(detail.get("phase") or "idle"),
            progress_pct=float(detail.get("progress_pct", 0.0) or 0.0),
            reason=str(detail.get("reason") or ""),
            transaction_id=str(detail.get("transaction_id") or ""),
            attempt=int(detail.get("attempt", 0) or 0),
            detail=CoreFrozenPayload.from_mapping(detail),
        )

    def _mark_state_changed_locked(self) -> None:
        self._status_sequence += 1
        self._state["status_sequence"] = self._status_sequence

    def consume_core_observations(self, *, max_items: int = 128) -> int:
        """Advance this customer's cursor without running work in Core."""

        consumed = 0
        for _ in range(max(1, int(max_items))):
            record = self.accepted_root_outbox.claim_next(SERVICE_CLIENT_ID)
            if record is None:
                break
            frame = record.root.observation_frame
            try:
                target = _target_from_core_observation(frame)
            except Exception as exc:
                self.accepted_root_outbox.release(
                    SERVICE_CLIENT_ID,
                    key=record.key,
                    error=exc,
                )
                self.fault_supervisor.record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=SERVICE_CLIENT_ID,
                    operation="consume_core_observation",
                    generation=frame.source_sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=(
                        "historical_evidence_waits_for_valid_core_observation"
                    ),
                    error=exc,
                    suppression_budget=3,
                )
                return consumed
            with self._lock:
                self._latest_target = target
                self._latest_observation_key = record.key
            self.accepted_root_outbox.acknowledge(
                SERVICE_CLIENT_ID,
                key=record.key,
            )
            consumed += 1
        return consumed

    def generation(self) -> HistoricalEvidenceGeneration | None:
        """Return only one atomically committed, identity-checked generation."""

        self.consume_core_observations()
        with self._lock:
            target = dict(self._latest_target)
            state = dict(self._state)
        if str(state.get("status") or "") != "complete" or not target:
            return None
        prior = external_memory_prior_status(
            self.memory_dir,
            current_identity=target,
        )
        if not bool(prior.get("present")):
            return None
        commit = dict(state.get("commit") or {})
        chart = dict(state.get("chart_history") or {})
        committed_at = float(
            commit.get("committed_at_ts")
            or state.get("finished_at_ts")
            or 0.0
        )
        return HistoricalEvidenceGeneration(
            transaction_id=str(state.get("transaction_id") or ""),
            committed_at=committed_at,
            commit=CoreFrozenPayload.from_mapping(commit),
            prior=CoreFrozenPayload.from_mapping(prior),
            chart=CoreFrozenPayload.from_mapping(chart),
        )

    def request_chart(
        self,
        request: HistoricalEvidenceChartRequest,
    ) -> Future[HistoricalEvidenceChartResult]:
        """Queue one bounded chart read behind the Intelligence bulk lane."""

        if not isinstance(request, HistoricalEvidenceChartRequest):
            raise TypeError("historical_evidence_typed_chart_request_required")
        self.consume_core_observations()
        with self._lock:
            target = dict(self._latest_target)
        if not target:
            raise RuntimeError("historical_evidence_chart_core_identity_unavailable")
        raw_scope = target.get("evidence_scope")
        if not isinstance(raw_scope, Mapping):
            raise RuntimeError("historical_evidence_chart_core_scope_unavailable")
        scope = validated_evidence_scope(raw_scope)
        if request.session_scope_key != scope.session_scope_key:
            raise RuntimeError("historical_evidence_chart_session_scope_mismatch")
        worker_id = "intelligence_historical_evidence_chart"
        self.worker_health.register(
            worker_id,
            execution_kind=WorkerExecutionKind.FINITE_JOB,
            required=False,
            retryable=True,
        )
        self.worker_health.mark_started(worker_id)
        try:
            future = self.executor.submit(
                read_historical_chart,
                runtime_dir=self.runtime_dir,
                memory_dir=self.memory_dir,
                request=request,
                target=target,
            )
        except Exception as exc:
            self.worker_health.mark_failure(
                worker_id,
                exc,
                next_retry_at=time.time() + RETRY_BASE_DELAY_S,
            )
            self.fault_supervisor.record(
                boundary=FaultBoundaryClass.WORKER,
                owner=worker_id,
                operation="submit_committed_chart_read",
                generation=max(1, self._status_sequence),
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior=(
                    "live_core_and_ngui_continue_without_historical_chart_seed"
                ),
                error=exc,
                suppression_budget=3,
            )
            raise

        def observe(completed: Future[HistoricalEvidenceChartResult]) -> None:
            try:
                completed.result()
            except Exception as exc:
                self.worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time() + RETRY_BASE_DELAY_S,
                )
                self.fault_supervisor.record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=worker_id,
                    operation="read_committed_chart_chunks",
                    generation=max(1, self._status_sequence),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=(
                        "live_core_and_ngui_continue_without_historical_chart_seed"
                    ),
                    error=exc,
                    suppression_budget=3,
                )
            else:
                self.worker_health.mark_success(worker_id)

        future.add_done_callback(observe)
        return future

    def presentation_status(self) -> dict[str, Any]:
        """Return history fields to the sole Intelligence NGUI publisher."""

        self.consume_core_observations()
        with self._lock:
            state = dict(self._state)
            target = dict(self._latest_target)
        prior = (
            external_memory_prior_status(
                self.memory_dir,
                current_identity=target,
            )
            if target
            else {}
        )
        values, validity, reasons = _historical_presentation_values(
            state,
            prior,
        )
        return {
            "values": values,
            "validity": validity,
            "reasons": reasons,
            "sample_sequence": max(1, self._status_sequence),
        }

    def shutdown(self, *, timeout_s: float) -> Mapping[str, Any]:
        """Stop child work before its coordinator executor is torn down."""

        return self.bulk_execution.shutdown(timeout_s=max(0.0, float(timeout_s)))

    def _run(
        self,
        target: dict[str, Any],
        preferred_sources: tuple[str, ...],
        force: bool,
        max_age_days: int,
        min_telemetry_rows: int,
        transaction_id: str,
        confirmed_handoff_receipt: dict[str, Any],
        attempt: int,
    ) -> None:
        staging_generation: Path | None = None
        try:
            prepared = {} if force else self._committed_preparation(transaction_id)
            if not prepared:
                staging_generation = create_staging_generation(
                    self.memory_dir,
                    transaction_id=transaction_id,
                    attempt=attempt,
                )
                prepared = dict(
                    self.bulk_execution.execute(
                        HistoricalContinuityBulkRequest(
                            transaction_id=transaction_id,
                            attempt=attempt,
                            stage_dir=str(self.stage_dir),
                            staging_generation_dir=str(staging_generation),
                            target_identity=target,
                            preferred_sources=preferred_sources,
                            include_default_sources=True,
                            force=force,
                            max_age_days=max_age_days,
                            min_telemetry_rows=min_telemetry_rows,
                            confirmed_handoff_receipt=confirmed_handoff_receipt,
                        ),
                        lambda pct, phase, detail: self._progress(
                            transaction_id,
                            max(2.0, min(98.0, float(pct))),
                            phase=str(phase or "background"),
                            reason=str(detail or ""),
                        ),
                    )
                )
                if int(prepared.get("child_pid") or 0) == int(
                    prepared.get("parent_pid") or -1
                ):
                    raise RuntimeError(
                        "historical continuity bulk execution did not cross a process boundary"
                    )
            import_status = str(prepared.get("status") or "")
            status = dict(prepared.get("auto_import") or {})
            if import_status != "prepared":
                retryable = import_status in {
                    "waiting_for_identity",
                    "no_candidates",
                    "rejected",
                }
                self._finish(
                    transaction_id,
                    status="retry_pending" if retryable else "failed",
                    phase="promotion_gate",
                    reason=str(status.get("reason") or import_status or "import failed"),
                    auto_import=dict(status),
                )
                return
            if not self._target_is_current(target):
                self._finish(
                    transaction_id,
                    status="superseded",
                    phase="identity_recheck",
                    reason="live exact modem generation changed during background import",
                )
                return
            commit_receipt = dict(prepared.get("commit") or {})
            if not commit_receipt:
                if staging_generation is None:
                    raise RuntimeError(
                        "historical continuity prepared result has no staging generation"
                    )
                commit_receipt = commit_staged_generation(
                    self.memory_dir,
                    staging_generation,
                    transaction_id=transaction_id,
                    attempt=attempt,
                    target_identity=target,
                    prepared_artifacts=(
                        prepared.get("artifact_receipt")
                        if isinstance(prepared.get("artifact_receipt"), Mapping)
                        else None
                    ),
                )
                staging_generation = None
            pack = load_external_memory_prior_pack(self.memory_dir)
            if not pack:
                raise RuntimeError("committed historical prior pack missing after commit")
            chart_receipt = dict(prepared.get("chart_history") or {})
            if not chart_receipt.get("catalog_published"):
                raise RuntimeError("committed historical chart catalog missing")
            self._progress(
                transaction_id,
                99.0,
                phase="consumer_delivery",
                reason=(
                    "complete approved history committed; publishing typed evidence generation"
                ),
            )
            deliveries: dict[str, str] = {}
            errors: dict[str, str] = {}
            if not self._target_is_current(target):
                self._finish(
                    transaction_id,
                    status="superseded",
                    phase="identity_recheck",
                    reason="live exact modem generation changed before consumer delivery",
                )
                return
            prior = external_memory_prior_status(
                self.memory_dir,
                current_identity=target,
            )
            if bool(prior.get("present")):
                deliveries["historical_evidence_generation"] = "acked"
            else:
                deliveries["historical_evidence_generation"] = "failed"
                errors["historical_evidence_generation"] = str(
                    (prior.get("activation") or {}).get("reason")
                    or "committed historical generation unavailable"
                )
            deliveries["ngui_chart_history"] = (
                "acked" if chart_receipt.get("catalog_published") else "failed"
            )
            if deliveries["ngui_chart_history"] == "failed":
                errors["ngui_chart_history"] = str(
                    chart_receipt.get("reason") or "chart history catalog unavailable"
                )
            completed = all(value == "acked" for value in deliveries.values())
            self._finish(
                transaction_id,
                status="complete" if completed else "retry_pending",
                phase="complete" if completed else "consumer_delivery",
                reason=(
                    "all historical continuity consumers acknowledged"
                    if completed
                    else "one or more continuity consumers require retry"
                ),
                deliveries=deliveries,
                errors=errors,
                chart_history=dict(chart_receipt),
                prior=dict(prior),
                auto_import=dict(status),
                commit=dict(commit_receipt),
            )
        except Exception as exc:
            log.exception("Historical continuity import failed")
            self.fault_supervisor.record(
                boundary=FaultBoundaryClass.WORKER,
                owner="historical_continuity_import",
                operation="run",
                generation=attempt,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior="history_import_retry_pending",
                error=exc,
                suppression_budget=3,
            )
            fault_errors = (
                {
                    "bulk_execution": exc.error,
                    "child_traceback": exc.child_traceback,
                }
                if isinstance(exc, HistoricalContinuityChildFault)
                else {}
            )
            self._finish(
                transaction_id,
                status="retry_pending",
                phase="fault",
                reason=f"{type(exc).__name__}: {exc}",
                errors=fault_errors,
            )
        finally:
            if staging_generation is not None:
                discard_staging_generation(self.memory_dir, staging_generation)

    def _committed_preparation(self, transaction_id: str) -> dict[str, Any]:
        status = committed_generation_status(self.memory_dir)
        receipt = dict(status.get("receipt") or {})
        if (
            not status.get("present")
            or str(receipt.get("transaction_id") or "") != transaction_id
        ):
            return {}

        def load(name: str) -> dict[str, Any]:
            path = committed_artifact_path(self.memory_dir, name)
            if path is None:
                raise RuntimeError(f"committed historical artifact missing: {name}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"committed historical artifact invalid: {name}")
            return payload

        catalog = load(CATALOG_FILENAME)
        catalog["catalog_published"] = True
        return {
            "status": "prepared",
            "auto_import": load(AUTO_STATUS_FILENAME),
            "chart_history": catalog,
            "commit": receipt,
            "child_pid": 0,
            "parent_pid": -1,
        }

    def _target_is_current(self, target: Mapping[str, Any]) -> bool:
        self.consume_core_observations()
        with self._lock:
            current = dict(self._latest_target)
        try:
            current_scope = external_projection_scope(current, evidence_count=1)
            target_scope = external_projection_scope(target, evidence_count=1)
        except (TypeError, ValueError):
            return False
        if _transaction_id(current_scope) != _transaction_id(target_scope):
            return False
        return not (
            current_scope.provider_plmn
            and target_scope.provider_plmn
            and current_scope.provider_plmn != target_scope.provider_plmn
        )

    def _progress(
        self,
        transaction_id: str,
        progress_pct: float,
        *,
        phase: str,
        reason: str,
        **extra: Any,
    ) -> None:
        now = time.time()
        with self._lock:
            if str(self._state.get("transaction_id") or "") != transaction_id:
                return
            previous = float(self._state.get("progress_pct", 0.0) or 0.0)
            self._state.update(
                {
                    "status": "running",
                    "phase": str(phase or "background"),
                    "progress_pct": round(max(previous, min(99.0, progress_pct)), 2),
                    "reason": str(reason or ""),
                    "updated_at_ts": now,
                    **extra,
                }
            )
            self._mark_state_changed_locked()
            if now - self._last_progress_write >= 0.5 or progress_pct - previous >= 5.0:
                self._save_locked()
                self._last_progress_write = now

    def _finish(
        self,
        transaction_id: str,
        *,
        status: str,
        phase: str,
        reason: str,
        **extra: Any,
    ) -> None:
        finished = time.time()
        with self._lock:
            if str(self._state.get("transaction_id") or "") != transaction_id:
                return
            self._state.update(
                {
                    "status": str(status),
                    "phase": str(phase),
                    "progress_pct": 100.0 if status == "complete" else float(
                        self._state.get("progress_pct", 0.0) or 0.0
                    ),
                    "reason": str(reason or ""),
                    "updated_at_ts": finished,
                    "finished_at_ts": finished,
                    "next_retry_at_ts": (
                        finished + _retry_delay_s(int(self._state.get("attempt", 1) or 1))
                        if status == "retry_pending"
                        else 0.0
                    ),
                    **extra,
                }
            )
            self._mark_state_changed_locked()
            self._save_locked()

    def _publish(self, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(payload)
            self._state["updated_at_ts"] = time.time()
            self._mark_state_changed_locked()
            self._save_locked()
            return dict(self._state)

    def _state_payload(self, **payload: Any) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "execution_lane": "background_coordinator_plus_spawned_low_priority_process",
            "history_visibility": "atomic_committed_generation_only",
            "automatic_history_source": "production_minimal_jsonl_or_gzip_only",
            "full_json_history": "reference_only",
            "settings_transaction": "independent_foundation_boundary",
            "live_truth_override": False,
            "status": "idle",
            "phase": "idle",
            "progress_pct": 0.0,
            "reason": "",
            "transaction_id": "",
            "attempt": 0,
            "status_sequence": 1,
            "started_at_ts": 0.0,
            "updated_at_ts": time.time(),
            "finished_at_ts": 0.0,
            "next_retry_at_ts": 0.0,
            "confirmed_login_scope": {},
            "confirmed_handoff_receipt": {},
            "pending_transaction_id": "",
            "pending_preferred_sources": [],
            "pending_force": False,
            "pending_max_age_days": MAX_AGE_DAYS,
            "pending_min_telemetry_rows": MIN_TELEMETRY_ROWS,
            "pending_confirmed_login_scope": {},
            "pending_confirmed_handoff_receipt": {},
            "deliveries": {consumer: "not_started" for consumer in CONSUMERS},
            "intelligence_deliveries": {
                consumer: "unattached"
                for consumer in INTELLIGENCE_CONSUMERS
            },
            "errors": {},
            "control_request_ids": [],
            **payload,
        }

    def _restore(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._state_payload()
        except Exception as exc:
            reason = self._report_restore_rejection(
                exc,
                reason="historical_evidence_state_unreadable",
            )
            return self._state_payload(
                status="degraded",
                phase="restore_rejected",
                reason=reason,
            )
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != SCHEMA
            or raw.get("owner") != OWNER
        ):
            exc = RuntimeError(
                "historical_evidence_state_owner_schema_rejected:"
                f"schema={raw.get('schema') if isinstance(raw, dict) else type(raw).__name__};"
                f"owner={raw.get('owner') if isinstance(raw, dict) else ''}"
            )
            reason = self._report_restore_rejection(
                exc,
                reason="historical_evidence_state_owner_schema_rejected",
            )
            return self._state_payload(
                status="degraded",
                phase="restore_rejected",
                reason=reason,
            )
        restored = self._state_payload(**raw)
        if str(restored.get("status") or "") in {"queued", "running"}:
            restored.update(
                {
                    "status": "retry_pending",
                    "phase": "crash_recovery",
                    "reason": "previous background import ended without a terminal receipt",
                    "next_retry_at_ts": 0.0,
                }
            )
        return restored

    def _report_restore_rejection(
        self,
        exc: Exception,
        *,
        reason: str,
    ) -> str:
        """Report rejected durable state without preventing Core startup."""

        self.worker_health.register(
            RESTORE_WORKER_ID,
            execution_kind=WorkerExecutionKind.FINITE_JOB,
            required=False,
            retryable=False,
        )
        self.worker_health.mark_started(RESTORE_WORKER_ID)
        self.worker_health.mark_failure(
            RESTORE_WORKER_ID,
            exc,
            next_retry_at=0.0,
        )
        try:
            self.fault_supervisor.record(
                boundary=FaultBoundaryClass.STORAGE,
                owner=RESTORE_WORKER_ID,
                operation="restore",
                generation=1,
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior=(
                    "intelligence_history_starts_fresh_core_ngui_continue"
                ),
                error=exc,
                suppression_budget=0,
            )
        except Exception as reporting_exc:
            self.worker_health.mark_failure(
                RESTORE_WORKER_ID,
                reporting_exc,
                next_retry_at=0.0,
            )
            return (
                f"{reason}; fault_report_failed:"
                f"{type(reporting_exc).__name__}: {reporting_exc}"
            )
        return f"{reason}: {type(exc).__name__}: {exc}"

    def _save_locked(self) -> None:
        atomic_write_text(
            self.status_path,
            json.dumps(self._state, indent=2, sort_keys=True),
        )


def _transaction_id(scope: EvidenceScope) -> str:
    content = {
        "identity_code": scope.identity_code,
        "catalog_version": scope.identity_catalog_version,
        "catalog_checksum": scope.identity_catalog_checksum,
        "session_scope_key": scope.session_scope_key,
        "source_epoch": scope.source_epoch,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return f"continuity-{digest}"


def _nested_text(value: Any, *keys: str) -> str:
    if isinstance(value, Mapping):
        for key in keys:
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        for nested in value.values():
            candidate = _nested_text(nested, *keys)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for nested in value:
            candidate = _nested_text(nested, *keys)
            if candidate:
                return candidate
    return ""


def _target_from_core_observation(frame: CoreObservationFrame) -> dict[str, Any]:
    if not isinstance(frame, CoreObservationFrame):
        raise TypeError("historical_evidence_typed_core_observation_required")
    if frame.identity_status != "confirmed":
        return {}
    capabilities = frame.capability_facts.to_mapping()
    catalog_version = str(capabilities.get("catalog_version") or "").strip()
    catalog_checksum = str(capabilities.get("catalog_checksum") or "").strip()
    provider_plmn = str(capabilities.get("provider_plmn") or "").strip()
    provider_name = str(capabilities.get("provider_name") or "").strip()
    scope = EvidenceScope(
        identity_code=frame.identity_code,
        identity_status="exact",
        identity_catalog_version=catalog_version,
        identity_catalog_checksum=catalog_checksum,
        session_scope_key=frame.session_id,
        source_epoch=frame.source_epoch,
        source_sequence=frame.source_sequence,
        observed_at=frame.sampled_at,
        evidence_age_s=0.0,
        confidence=1.0,
        evidence_count=1,
        formula_vector=external_memory_formula_vector(),
        access_type_id=str(capabilities.get("access_type_id") or ""),
        provider_plmn=provider_plmn,
    )
    target = {
        **capabilities,
        "identity_code": frame.identity_code,
        "identity_status": "exact",
        "access_type": frame.access_type,
        "provider_name": provider_name,
        "provider_plmn": provider_plmn,
        "session_scope_key": frame.session_id,
        "source_epoch": frame.source_epoch,
        "source_sequence": frame.source_sequence,
        "observed_at": frame.sampled_at,
        "evidence_scope": scope.as_dict(),
    }
    return {
        key: value
        for key, value in target.items()
        if value not in (None, "")
    }


def _historical_presentation_values(
    state: Mapping[str, Any],
    prior: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool], dict[str, str]]:
    status = str(state.get("status") or "idle")
    phase = str(state.get("phase") or "idle")
    progress = float(state.get("progress_pct", 0.0) or 0.0)
    reason = str(state.get("reason") or "")
    auto = prior.get("auto_import") if isinstance(prior.get("auto_import"), Mapping) else {}
    scope_tiers = prior.get("scope_tiers") or []
    matched_tier = (
        ", ".join(str(value) for value in scope_tiers)
        if isinstance(scope_tiers, (list, tuple))
        else str(scope_tiers or "")
    )
    imported_at = float(prior.get("imported_at_ts", 0.0) or 0.0)
    build_age = (
        f"{int(max(0.0, time.time() - imported_at) // 86400)} days"
        if imported_at > 0.0
        else "N/A"
    )
    values = {
        "history.auto_import_status": str(auto.get("status") or status),
        "history.brain_influence": (
            "Advisory available" if status == "complete" else "Unavailable"
        ),
        "history.build_age": build_age,
        "history.channel": "Historical evidence",
        "history.fact_count": int(prior.get("fact_count", 0) or 0),
        "history.history_window": "Up to 14 days",
        "history.import_status": status,
        "history.matched_tier": matched_tier or "N/A",
        "history.merged_packs": int(
            auto.get("source_stage_count", 0)
            or auto.get("merged_packs", 0)
            or 0
        ),
        "history.observation_count": int(
            prior.get("observation_count", 0) or 0
        ),
        "history.predictive_window": None,
        "history.prior_source": str(
            prior.get("authority") or "Committed generation"
        ),
        "history.soak_status": (
            "Committed" if status == "complete" else "Not committed"
        ),
        "history.workflow_status": (
            f"{status} — {phase} ({progress:.0f}%)"
            + (f": {reason}" if reason else "")
        ),
    }
    if set(values) != set(HISTORICAL_PRESENTATION_SOURCES):
        raise RuntimeError("historical_evidence_presentation_catalog_mismatch")
    validity = {source_id: True for source_id in values}
    validity["history.predictive_window"] = False
    reasons = {source_id: "" for source_id in values}
    reasons["history.predictive_window"] = (
        "intelligence_prediction_window_not_published"
    )
    return values, validity, reasons


def _retry_delay_s(attempt: int) -> float:
    exponent = max(0, min(6, int(attempt) - 1))
    return min(RETRY_MAX_DELAY_S, RETRY_BASE_DELAY_S * float(2**exponent))


__all__ = [
    "CATALOG_FILENAME",
    "CHART_HISTORY_FILENAME",
    "CONSUMERS",
    "HISTORICAL_PRESENTATION_SOURCES",
    "HistoricalEvidenceRequestSuperseded",
    "IntelligenceHistoricalEvidence",
    "OWNER",
    "RESTORE_WORKER_ID",
    "SCHEMA",
    "STATUS_FILENAME",
]
