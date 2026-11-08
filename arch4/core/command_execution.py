"""Low-latency Core cellular-command execution lane."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping

from arch4.action_admission import (
    action_origin_from_action,
    current_engine_identity,
    ensure_action_command_id,
    selected_action_context_from_action,
)
from arch4.action_origin import is_human_operator_origin
from arch4.coerce import safe_bool
from arch4.command_transactions import (
    CommandTransition,
    RadioMutationLease,
)
from arch4.core.action_guards import (
    apply_canonical_action_admission,
    prepare_core_action_root,
)
from arch4.core.action_source_state_authority import (
    band_action_target_signature,
    require_action_source_state_authority,
)
from arch4.core.band_action_policy import (
    EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS,
    is_band_control_action,
)
from arch4.core.action_runtime import (
    _radio_mutation_requested_bands,
)
from arch4.core.command_safety_adapter import build_core_command_safety_evidence
from arch4.core.lever_action_adapter import lever_action_initiator_provenance
from arch4.core.mutation_policy import uses_generic_mutating_action_lock
from arch4.core.physical_command_evidence import (
    physical_command_receipt_from_result,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.core_contracts.commands import (
    CoreCommandExecutionAbortReceipt,
    CoreCommandExecutionGate,
    CoreCommandExecutionGateDisposition,
    CoreCommandExecutionReceipt,
    CoreCommandOrigin,
    CoreCommandRequest,
    core_command_request_root,
)
from arch4.core_contracts.command_targets import CoreRestartTarget, CoreRadioModeTarget
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyRestartPermit,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    require_fault_supervisor,
)
from arch4.interfaces import LeverAction, LeverResult, LeverType
from arch4.modem.base import TargetGuardError
from arch4.mutation_authority import execute_admitted_modem_action
from arch4.runtime_control_faults import (
    record_runtime_control_fault as _record_runtime_control_fault,
)
from arch4.worker_health import (
    WorkerExecutionKind,
    require_worker_health_authority,
)

log = logging.getLogger(__name__)
OWNER = "54guc.core.command_execution"
CORE_COMMAND_ABORT_JOIN_TIMEOUT_S = 1.0


def _command_lifecycle_service(engine: CoreRuntimePort) -> Any:
    service = getattr(engine, "command_lifecycle", None)
    if service is None:
        raise RuntimeError("core_command_lifecycle_service_unavailable")
    return service


def _execution_receipt(
    request: CoreCommandRequest,
    *,
    submitted: bool,
    reason: str,
    occurred_at: float,
    physical_action_root_id: str = "",
    detail: Mapping[str, Any] | None = None,
) -> CoreCommandExecutionReceipt:
    return CoreCommandExecutionReceipt(
        request_id=request.request_id,
        command_root_id=core_command_request_root(request),
        matrix_identity=request.matrix_identity,
        submitted=submitted,
        reason=reason,
        occurred_at=occurred_at,
        physical_action_root_id=physical_action_root_id,
        detail=CoreFrozenPayload.from_mapping(dict(detail or {})),
    )

def _requested_bands_for_action(action: LeverAction) -> list[int]:
    return list(_radio_mutation_requested_bands(action))

def _safe_log_event(engine: CoreRuntimePort, kind: str, **payload: Any) -> None:
    try:
        engine.diagnostics.log_event(kind, **payload)
    except Exception as exc:
        _record_runtime_control_fault(engine, source=f"{kind}_log", exc=exc)
        log.debug("%s log suppressed: %s", kind, exc)

def _validate_emergency_restart_permit_for_request(
    engine: CoreRuntimePort,
    request: CoreCommandRequest,
    permit: CoreEmergencyRestartPermit,
    *,
    identity_key: str,
    now: float,
) -> bool:
    """Recheck the exact Foundation request before ordinary admission."""

    metadata = request.parameters.to_mapping()
    if (
        request.origin is not CoreCommandOrigin.OPERATOR_AUTOMATION
        or request.client_id != "foundation.internet_emergency"
        or request.command_kind != "RESTART"
        or not isinstance(request.target, CoreRestartTarget)
        or str(metadata.get("automation_source") or "").strip().lower()
        != "watchdog"
        or str(metadata.get("intent") or "").strip()
        != "modem_watchdog_recovery"
        or permit.request_id != request.request_id
        or permit.matrix_identity != request.matrix_identity
        or permit.identity_code != request.identity_code
        or permit.matrix_identity.session_scope_key != identity_key
    ):
        return False
    validate = getattr(engine, "validate_emergency_restart_permit", None)
    return bool(
        callable(validate)
        and validate(permit, now=float(now))
    )

def _request_post_write_register_refresh(
    engine: CoreRuntimePort,
    *,
    action: LeverAction,
    physical_stages: tuple[PhysicalCommandDispatchEvidence, ...],
    deadline_at: float,
) -> dict[str, Any]:
    live_module = engine.input_module
    request = getattr(live_module, "request_priority_live_register_refresh", None)
    if not callable(request):
        return {"accepted": False, "reason": "priority_refresh_unavailable"}
    try:
        return dict(
            request(
                reason=f"core_command_{action.lever.name.lower()}",
                physical_stages=physical_stages,
                deadline_at=deadline_at,
            )
            or {}
        )
    except Exception as exc:
        _record_runtime_control_fault(engine, source="core_command_priority_refresh", exc=exc)
        return {"accepted": False, "reason": type(exc).__name__}


def _release_event_write_barrier(
    engine: CoreRuntimePort,
    *,
    action: LeverAction,
    pending_token: str,
) -> None:
    clear_pending = getattr(
        engine.input_module,
        "clear_event_write_pending",
        None,
    )
    if not callable(clear_pending):
        raise RuntimeError("modem_live_event_write_barrier_release_unavailable")
    try:
        release = clear_pending(
            action,
            token=pending_token,
            source=selected_action_context_from_action(action).origin.source,
        )
        if not isinstance(release, Mapping):
            raise RuntimeError("modem_live_event_write_barrier_release_receipt_invalid")
        if (
            str(release.get("owner") or "") != "modem_live"
            or str(release.get("token") or "") != pending_token
        ):
            raise RuntimeError("modem_live_event_write_barrier_release_receipt_rejected")
    except Exception as exc:
        _record_runtime_control_fault(
            engine,
            source="core_command_event_write_pending_clear",
            exc=exc,
        )
        raise

def _core_command_worker_lock(engine: CoreRuntimePort) -> threading.RLock:
    lock = getattr(engine, "_core_command_worker_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(engine, "_core_command_worker_lock", lock)
    return lock


def _report_physical_stage_evidence_fault(
    engine: CoreRuntimePort,
    *,
    action: LeverAction,
    error: BaseException,
) -> None:
    """Report lost physical evidence before failing the command."""

    try:
        require_fault_supervisor(engine).record(
            boundary=FaultBoundaryClass.CODEC,
            owner="54guc.core.physical_command_evidence",
            operation="adapt_modem_live_result",
            generation=int(
                getattr(
                    getattr(
                        getattr(
                            action,
                            "_action_transaction_key",
                            None,
                        ),
                        "matrix_identity",
                        None,
                    ),
                    "matrix_sequence",
                    0,
                )
                or 0
            ),
            severity=FaultSeverity.ERROR,
            retryable=True,
            degraded_behavior="command_fails_exact_physical_receipt_required",
            error=error,
            suppression_budget=0,
        )
    except Exception as report_error:
        log.error(
            "Physical command timing fault reporting failed: %s: %s",
            type(report_error).__name__,
            report_error,
        )

def _core_command_worker_running(engine: CoreRuntimePort) -> bool:
    lock = _core_command_worker_lock(engine)
    with lock:
        thread = getattr(engine, "_core_command_worker_thread", None)
        running = bool(thread is not None and thread.is_alive())
        if not running:
            status = getattr(engine, "_core_command_worker_status", None)
            if isinstance(status, dict) and status.get("status") == "running":
                status = dict(status)
                status["status"] = "finished_unobserved"
                status["finished_ts"] = time.time()
                setattr(engine, "_core_command_worker_status", status)
        return running

def _set_core_command_worker_status(engine: CoreRuntimePort, payload: dict[str, Any]) -> None:
    lock = _core_command_worker_lock(engine)
    with lock:
        setattr(engine, "_core_command_worker_status", dict(payload))


def _emergency_band_rescue_rejection(engine, action, *, now: float) -> str:
    """Recheck current service immediately before recovery admission/dispatch."""
    if action.intent not in EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS:
        return ""
    view = engine.current_emergency_reachability_view(now=now)
    slot = require_action_source_state_authority(engine).emergency_band_rescue_status(now=now)
    if (not view.confirmed or view.incident_id != slot["emergency_band_rescue_incident_id"]
            or now >= slot["emergency_band_rescue_deadline_at"]):
        return "emergency_band_rescue_no_longer_authorized"
    return ""

def _execute_core_command_transaction(
    engine: CoreRuntimePort,
    *,
    action: LeverAction,
    pending_token: str,
    decision_trace_id: str,
    requested_bands: list[int],
    submitted_ts: float,
    execution_lease: RadioMutationLease,
) -> dict[str, Any]:
    """Execute one gateway-admitted modem write outside the runtime clock."""

    live_module = engine.input_module
    clear_pending = getattr(live_module, "clear_event_write_pending", None)
    if not callable(clear_pending):
        raise RuntimeError("modem_live_event_write_barrier_release_unavailable")
    started_ts = time.time()
    completion = execution_lease.transaction.cellular_completion
    if completion is None:
        raise RuntimeError("core_command_cellular_completion_required")
    command_lifecycle = _command_lifecycle_service(engine)
    try:
        rescue_rejection = _emergency_band_rescue_rejection(engine, action, now=started_ts)
        if rescue_rejection:
            terminal = command_lifecycle.cancel_cellular(
                root_id=execution_lease.root_id, reason=rescue_rejection,
                receipt={"owner": OWNER, "stage": "before_physical_execution"}, now=started_ts)
            return {"ran": True, "reason": terminal.reason,
                    "terminal_status": terminal.terminal_status.value, "terminal_settled": None,
                    "request_accepted": False, "physical_response_available": False,
                    "last_error": rescue_rejection, "priority_refresh": {},
                    "started_ts": started_ts, "finished_ts": time.time()}
        result = execute_admitted_modem_action(
            engine,
            action,
            lease=execution_lease,
            event_write_token=pending_token,
            network_progress=lambda plan, profile: command_lifecycle.note_network_stage(
                execution_lease, plan=plan, coordination_profile=profile, now=time.time()),
        )
    except TargetGuardError as exc:
        _record_runtime_control_fault(engine, source="core_command_target_guard", exc=exc)
        result = LeverResult(
            success=False,
            lever=action.lever,
            error=f"Core command target guard: {exc.reason}",
        )
    except Exception as exc:
        _record_runtime_control_fault(
            engine,
            source="core_command_executor_fault",
            exc=exc,
        )
        command_lifecycle.fail_cellular(
            root_id=execution_lease.root_id,
            reason="core_command_executor_fault",
            receipt={
                "owner": OWNER,
                "error_type": type(exc).__name__,
            },
            now=time.time(),
        )
        raise
    finally:
        _release_event_write_barrier(
            engine,
            action=action,
            pending_token=pending_token,
        )
    finished_ts = time.time()
    physical_receipt = None
    physical_receipt_error = ""
    try:
        physical_receipt = physical_command_receipt_from_result(
            action,
            result,
            request_id=execution_lease.transaction.command_id,
            transaction_root_id=execution_lease.root_id,
            target=execution_lease.transaction.action_key.target,
            execution_started_at=started_ts,
            transaction_completed_at=finished_ts,
        )
    except Exception as exc:
        _report_physical_stage_evidence_fault(
            engine,
            action=action,
            error=exc,
        )
        command_lifecycle.fail_cellular(
            root_id=execution_lease.root_id,
            reason="modem_live_physical_receipt_contract_fault",
            receipt={
                "owner": OWNER,
                "error_type": type(exc).__name__,
            },
            now=time.time(),
        )
        raise
    if physical_receipt is None:
        terminal = command_lifecycle.fail_cellular(
            root_id=execution_lease.root_id,
            reason="modem_live_exact_physical_receipt_required",
            receipt={
                "owner": "54guc.core.command_execution",
                "error": physical_receipt_error,
            },
            now=finished_ts,
        )
        transition = CommandTransition(terminal=terminal)
    else:
        if (
            physical_receipt.accepted
            and completion.target_required
            and uses_generic_mutating_action_lock(action)
        ):
            arm_lock = getattr(engine, "_arm_mutating_action_lock", None)
            if callable(arm_lock):
                try:
                    arm_lock(action.lever)
                except Exception as exc:
                    _record_runtime_control_fault(
                        engine,
                        source="core_command_arm_mutation_lock",
                        exc=exc,
                    )
                    command_lifecycle.fail_cellular(
                        root_id=execution_lease.root_id,
                        reason="core_command_arm_mutation_lock_fault",
                        receipt={
                            "owner": OWNER,
                            "error_type": type(exc).__name__,
                        },
                        now=time.time(),
                    )
                    raise
        transition = command_lifecycle.acknowledge_modem_result(
            execution_lease,
            physical_receipt=physical_receipt,
            now=finished_ts,
        )
    _safe_log_event(
        engine,
        "core_cellular_command_physical_result",
        transaction_id=execution_lease.transaction.command_id,
        event_owner=OWNER,
        lever=action.lever.name,
        bands=list(requested_bands),
        pband=(action.params or {}).get("pband") if isinstance(action.params, dict) else None,
        ca_pool=list((action.params or {}).get("ca_pool") or []) if isinstance(action.params, dict) else [],
        request_accepted=bool(
            physical_receipt is not None and physical_receipt.accepted
        ),
        physical_response_available=bool(
            physical_receipt is not None
            and physical_receipt.stages
            and all(
                stage.confirmed_by_transport for stage in physical_receipt.stages
            )
        ),
        target_verification_pending=bool(
            physical_receipt is not None and physical_receipt.accepted
        ),
        transaction_stage="physical_result_reported",
        error=str(getattr(result, "error", "") or ""),
        physical_reason=(
            physical_receipt.reason
            if physical_receipt is not None
            else physical_receipt_error
        ),
        physical_outcomes=(
            [stage.outcome for stage in physical_receipt.stages]
            if physical_receipt is not None
            else []
        ),
        physical_stage_reasons=(
            [stage.reason for stage in physical_receipt.stages]
            if physical_receipt is not None
            else []
        ),
        decision_trace_id=decision_trace_id,
        objective_id=str(action.objective_id or ""),
        dispatch_owner=OWNER,
        submitted_ts=float(submitted_ts or 0.0),
        dispatch_latency_s=round(max(0.0, started_ts - float(submitted_ts or started_ts)), 3),
        execution_duration_s=round(max(0.0, finished_ts - started_ts), 3),
        total_latency_s=round(max(0.0, finished_ts - float(submitted_ts or started_ts)), 3),
        **lever_action_initiator_provenance(action),
    )
    refresh_status: dict[str, Any] = {}
    admitted_origin = action_origin_from_action(action)
    state_authority = require_action_source_state_authority(engine)
    if (is_human_operator_origin(admitted_origin.kind)
            and physical_receipt is not None
            and any(stage.request_dispatched_at > 0 for stage in physical_receipt.stages)
            and (is_band_control_action(action.lever, action.params)
                 or action.lever in {LeverType.MOBILE_DATA, LeverType.NETWORK_SETTINGS})):
        state_authority.cancel_radio_mode_lease(receipt_id=execution_lease.root_id)
    mode_target = execution_lease.transaction.action_key.target
    if (isinstance(mode_target, CoreRadioModeTarget)
            and admitted_origin.kind.value == "smart_loop"
            and physical_receipt is not None
            and any(stage.request_dispatched_at > 0 for stage in physical_receipt.stages)):
        state_authority.note_radio_mode_dispatch(target=mode_target,
            root_id=execution_lease.root_id,
            identity_key=execution_lease.transaction.action_key.identity_key,
            observed_at=finished_ts)
        if transition.terminal is not None:
            state_authority.note_radio_mode_terminal(root_id=execution_lease.root_id,
                settled=transition.terminal.terminal_status.value == "settled",
                observed_at=max(finished_ts, transition.terminal.terminal_ts), reason=transition.terminal.reason)
    if (
        physical_receipt is not None
        and physical_receipt.accepted
        and physical_receipt.stages
        and is_human_operator_origin(admitted_origin.kind)
    ):
        state_authority.note_operator_band_physical_acceptance(
            lever=action.lever.name,
            params=action.params,
            origin=admitted_origin.kind.value,
            target_signature=band_action_target_signature(
                action.lever.name,
                action.params,
            ),
            observed_at=finished_ts,
            receipt_id=str(getattr(action, "command_id", "") or ""),
        )
    if action.lever is LeverType.CELL_LOCK:
        state_authority.note_cell_lock_result(
            params=action.params,
            success=bool(
                transition.terminal is not None
                and transition.terminal.terminal_status.value == "settled"
            ),
            observed_at=finished_ts,
            receipt_id=str(getattr(action, "command_id", "") or ""),
            origin_kind=admitted_origin.kind.value,
        )
    if (
        physical_receipt is not None
        and completion.target_required
    ):
        inspectable_stages = tuple(stage for stage in physical_receipt.stages
                                   if stage.permits_target_verification)
        if inspectable_stages:
            refresh_status = _request_post_write_register_refresh(
                engine,
                action=action,
                physical_stages=inspectable_stages,
                deadline_at=(transition.active.deadline_ts if transition.active is not None
                             else execution_lease.expires_ts),
            )
            _safe_log_event(
                engine,
                "core_command_register_refresh",
                transaction_id=execution_lease.transaction.command_id,
                event_owner=OWNER,
                lever=action.lever.name,
                decision_trace_id=decision_trace_id,
                dispatch_owner=OWNER,
                refresh_owner=str(refresh_status.get("owner") or "modem_live"),
                accepted=safe_bool(refresh_status.get("accepted", False), False),
                state=str(refresh_status.get("state") or ""),
                physical_write_dispatched_at=float(
                    refresh_status.get("physical_write_dispatched_at", 0.0) or 0.0
                ),
                requested_at=float(refresh_status.get("requested_at", 0.0) or 0.0),
                scheduled_at=float(refresh_status.get("scheduled_at", 0.0) or 0.0),
                remaining_delay_s=float(
                    refresh_status.get("remaining_delay_s", 0.0) or 0.0
                ),
                reason=str(refresh_status.get("reason") or ""),
            )
    return {
        "ran": True,
        "reason": (
            transition.terminal.reason
            if transition.terminal is not None
            else (
                "command_accepted"
                if (
                    physical_receipt is not None
                    and physical_receipt.accepted
                )
                else "command_failed"
            )
        ),
        "terminal_status": (
            transition.terminal.terminal_status.value
            if transition.terminal is not None
            else ""
        ),
        "terminal_settled": (
            True
            if transition.terminal is not None
            and transition.terminal.terminal_status.value == "settled"
            else None
        ),
        "request_accepted": bool(
            physical_receipt is not None and physical_receipt.accepted
        ),
        "physical_response_available": bool(
            physical_receipt is not None
            and physical_receipt.stages
            and all(
                stage.confirmed_by_transport for stage in physical_receipt.stages
            )
        ),
        "last_error": str(getattr(result, "error", "") or ""),
        "priority_refresh": dict(refresh_status or {}),
        "started_ts": started_ts,
        "finished_ts": finished_ts,
    }

def _start_core_command_worker(
    engine: CoreRuntimePort,
    *,
    action: LeverAction,
    execution_gate: CoreCommandExecutionGate,
    pending_token: str,
    decision_trace_id: str,
    requested_bands: list[int],
    submitted_ts: float,
    execution_lease: RadioMutationLease,
) -> bool:
    if not isinstance(execution_gate, CoreCommandExecutionGate):
        raise TypeError("core_command_execution_gate_required")
    worker_health = require_worker_health_authority(engine)
    worker_id = "core_command_execution"
    worker_health.register(
        worker_id,
        execution_kind=WorkerExecutionKind.FINITE_JOB,
        required=True,
        retryable=True,
    )
    lock = _core_command_worker_lock(engine)
    with lock:
        thread = getattr(engine, "_core_command_worker_thread", None)
        if thread is not None and thread.is_alive():
            return False
        _set_core_command_worker_status(
            engine,
            {
                "status": "running",
                "lever": action.lever.name,
                "decision_trace_id": str(decision_trace_id or ""),
                "submitted_ts": float(submitted_ts or 0.0),
            },
        )

        def _worker() -> None:
            status: dict[str, Any]
            try:
                gate_disposition, abort_reason = (
                    execution_gate.wait_for_decision()
                )
                if (
                    gate_disposition
                    is CoreCommandExecutionGateDisposition.ABORTED
                ):
                    abort_errors: list[BaseException] = []
                    barrier_released = False
                    lifecycle_cancelled = False
                    try:
                        _release_event_write_barrier(
                            engine,
                            action=action,
                            pending_token=pending_token,
                        )
                        barrier_released = True
                    except Exception as exc:
                        abort_errors.append(exc)
                    try:
                        _command_lifecycle_service(engine).cancel_cellular(
                            root_id=execution_lease.root_id,
                            reason=abort_reason,
                            receipt={
                                "owner": OWNER,
                                "stage": "gateway_pre_dispatch_abort",
                            },
                            now=time.time(),
                        )
                        lifecycle_cancelled = True
                    except Exception as exc:
                        abort_errors.append(exc)
                    execution_gate.acknowledge_abort(
                        lifecycle_cancelled=lifecycle_cancelled,
                        event_write_barrier_released=barrier_released,
                    )
                    if abort_errors:
                        raise RuntimeError(
                            "core_command_execution_abort_incomplete:"
                            + ",".join(
                                type(error).__name__
                                for error in abort_errors
                            )
                        )
                    status = {
                        "ran": False,
                        "reason": abort_reason,
                        "worker_aborted": True,
                        "finished_ts": time.time(),
                    }
                    worker_health.mark_success(worker_id)
                elif (
                    gate_disposition
                    is CoreCommandExecutionGateDisposition.COMMITTED
                ):
                    status = _execute_core_command_transaction(
                        engine,
                        action=action,
                        pending_token=pending_token,
                        decision_trace_id=decision_trace_id,
                        requested_bands=requested_bands,
                        submitted_ts=submitted_ts,
                        execution_lease=execution_lease,
                    )
                    worker_health.mark_success(worker_id)
                else:
                    raise RuntimeError(
                        "core_command_execution_gate_decision_invalid"
                    )
            except Exception as exc:
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time(),
                )
                _record_runtime_control_fault(engine, source="core_command_worker", exc=exc)
                projection_blocked = False
                try:
                    _command_lifecycle_service(
                        engine
                    ).fail_active_cellular_if_matching(
                        root_id=execution_lease.root_id,
                        reason=(
                            "core_command_worker_failed:"
                            + type(exc).__name__
                        ),
                        receipt={
                            "owner": OWNER,
                            "stage": "worker",
                            "error": str(exc),
                        },
                        now=time.time(),
                    )
                except Exception as projection_error:
                    projection_blocked = True
                    _record_runtime_control_fault(
                        engine,
                        source="core_command_projection_retry",
                        exc=projection_error,
                    )
                status = {
                    "ran": True,
                    "reason": type(exc).__name__,
                    "worker_failed": True,
                    "projection_retry_pending": projection_blocked,
                    "last_error": str(exc),
                    "finished_ts": time.time(),
                }
            status.update(
                {
                    "status": "finished",
                    "lever": action.lever.name,
                    "decision_trace_id": str(decision_trace_id or ""),
                    "submitted_ts": float(submitted_ts or 0.0),
                }
            )
            _set_core_command_worker_status(engine, status)

        def _supervised_worker() -> None:
            try:
                _worker()
            except Exception as exc:
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time(),
                )
                _record_runtime_control_fault(
                    engine,
                    source="core_command_worker_uncaught",
                    exc=exc,
                )
                try:
                    _set_core_command_worker_status(
                        engine,
                        {
                            "status": "failed",
                            "lever": action.lever.name,
                            "decision_trace_id": str(decision_trace_id or ""),
                            "submitted_ts": float(submitted_ts or 0.0),
                            "finished_ts": time.time(),
                            "last_error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                except Exception as status_exc:
                    _record_runtime_control_fault(
                        engine,
                        source="core_command_worker_status",
                        exc=status_exc,
                    )
            finally:
                with lock:
                    if (
                        getattr(engine, "_core_command_worker_thread", None)
                        is threading.current_thread()
                    ):
                        setattr(engine, "_core_command_worker_thread", None)

        worker = threading.Thread(
            target=_supervised_worker,
            name=f"core-cellular-command-{action.lever.name.lower()}",
            daemon=True,
        )
        try:
            setattr(engine, "_core_command_worker_thread", worker)
        except Exception as exc:
            worker_health.mark_failure(
                worker_id,
                exc,
                next_retry_at=time.time(),
            )
            _record_runtime_control_fault(
                engine,
                source="core_command_worker_tracking",
                exc=exc,
            )
            return False
        try:
            worker_health.mark_started(worker_id)
            worker.start()
        except Exception as exc:
            worker_health.mark_failure(
                worker_id,
                exc,
                next_retry_at=time.time(),
            )
            try:
                setattr(engine, "_core_command_worker_thread", None)
            except Exception as status_exc:
                _record_runtime_control_fault(
                    engine,
                    source="core_command_worker_tracking_clear",
                    exc=status_exc,
                )
            _set_core_command_worker_status(
                engine,
                {
                    "status": "dispatch_failed",
                    "lever": action.lever.name,
                    "decision_trace_id": str(decision_trace_id or ""),
                    "submitted_ts": float(submitted_ts or 0.0),
                    "finished_ts": time.time(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                },
            )
            _record_runtime_control_fault(
                engine,
                source="core_command_worker_start",
                exc=exc,
            )
            return False
        return True

def execute_core_cellular_command(
    engine: CoreRuntimePort,
    request: CoreCommandRequest,
    action: LeverAction,
    *,
    now: float | None = None,
    execution_gate: CoreCommandExecutionGate,
    core_runtime_view: CoreAcceptedRuntimeView,
) -> CoreCommandExecutionReceipt:
    """Execute one admitted cellular request on the low-latency lane."""

    current = float(time.time() if now is None else now)
    if not isinstance(request, CoreCommandRequest):
        raise TypeError("core_cellular_command_request_required")
    if not isinstance(execution_gate, CoreCommandExecutionGate):
        raise TypeError("core_command_execution_gate_required")
    if (
        execution_gate.request_id != request.request_id
        or execution_gate.matrix_identity != request.matrix_identity
    ):
        raise RuntimeError("core_command_execution_gate_identity_mismatch")
    if not isinstance(core_runtime_view, CoreAcceptedRuntimeView):
        raise TypeError("core_command_execution_runtime_view_required")
    if not isinstance(action, LeverAction):
        return _execution_receipt(
            request,
            submitted=False,
            reason="core_command_not_lever_action",
            occurred_at=current,
        )
    command_id = ensure_action_command_id(action, now=current)
    if command_id != request.request_id:
        return _execution_receipt(
            request,
            submitted=False,
            reason="core_command_action_identity_mismatch",
            occurred_at=current,
        )
    if _core_command_worker_running(engine):
        return _execution_receipt(
            request,
            submitted=False,
            reason="core_command_worker_inflight",
            occurred_at=current,
            detail={"backlog": 1},
        )
    decision_trace_id = request.correlation_id
    requested_bands = _requested_bands_for_action(action)
    started_ts = current
    rescue_rejection = _emergency_band_rescue_rejection(engine, action, now=started_ts)
    if rescue_rejection:
        return _execution_receipt(request, submitted=False, reason=rescue_rejection, occurred_at=current)
    identity = current_engine_identity(
        engine,
        now=started_ts,
        accepted_view=core_runtime_view,
    )
    identity_key = str(identity.get("identity_key") or "").strip()
    emergency_restart_permit = execution_gate.emergency_restart_permit
    emergency_restart_permit_valid = False
    if emergency_restart_permit is not None:
        emergency_restart_permit_valid = (
            _validate_emergency_restart_permit_for_request(
                engine,
                request,
                emergency_restart_permit,
                identity_key=identity_key,
                now=started_ts,
            )
        )
        if not emergency_restart_permit_valid:
            return _execution_receipt(
                request,
                submitted=False,
                reason="emergency_restart_permit_invalid",
                occurred_at=current,
            )
    if not identity_key or (
        not bool(identity.get("fresh", False))
        and not emergency_restart_permit_valid
    ):
        return _execution_receipt(
            request,
            submitted=False,
            reason="action_identity_unverified",
            occurred_at=current,
        )
    try:
        action_key = prepare_core_action_root(
            engine=engine,
            action=action,
            accepted_view=core_runtime_view,
            now=started_ts,
        )
    except Exception as exc:
        _record_runtime_control_fault(
            engine,
            source="core_command_bouncer_action_root_prepare",
            exc=exc,
        )
        rejection = f"action_root_prepare_failed:{type(exc).__name__}"
        return _execution_receipt(
            request,
            submitted=False,
            reason=rejection,
            occurred_at=current,
        )
    try:
        safety_evidence = build_core_command_safety_evidence(
            engine,
            request=request,
            action=action,
            core_runtime_view=core_runtime_view,
            identity_key=str(identity.get("identity_key") or ""),
            now=started_ts,
        )
    except Exception as exc:
        safety_evidence = None
        _record_runtime_control_fault(
            engine,
            source="core_command_safety_evidence",
            exc=exc,
        )
    submitted_action = action
    action, admission_suppressed, action_admission = apply_canonical_action_admission(
        engine=engine,
        action=action,
        decision_trace_id=decision_trace_id,
        accepted_view=core_runtime_view,
        command_safety_evidence=safety_evidence,
        autopilot_mode=(
            request.origin.value == "autonomous_intelligence"
        ),
        safety_checks_complete=True,
        hard_stop_reason="observe_only" if bool(getattr(engine, "observe_only", False)) else "",
        lanes={"core_command_execution": True, "runtime_clock_100ms": True},
        emergency_restart_permit=emergency_restart_permit,
        now=started_ts,
    )
    if admission_suppressed or action is None:
        rejection = str(action_admission.reason or "action_admission_rejected")
        result = LeverResult(success=False, lever=submitted_action.lever, error=rejection)
        _safe_log_event(
            engine,
            "core_cellular_command_rejected",
            lever=submitted_action.lever.name,
            reason=rejection,
            objective_id=str(submitted_action.objective_id or ""),
            decision_trace_id=decision_trace_id,
            dispatch_owner=OWNER,
            transaction_stage="canonical_action_admission",
            **lever_action_initiator_provenance(submitted_action),
        )
        return _execution_receipt(
            request,
            submitted=False,
            reason=rejection,
            occurred_at=current,
            detail={"action_admission": action_admission.as_dict()},
        )
    if action_admission.transaction_key is None:
        raise RuntimeError("core_command_admitted_action_key_missing")
    action_source = selected_action_context_from_action(action).origin.source
    command_lifecycle = _command_lifecycle_service(engine)
    admission = command_lifecycle.claim_cellular_action(
        action,
        execution_owner=OWNER,
        now=started_ts,
    )
    rejection = "" if admission.accepted else str(
        admission.reason or "cellular_command_rejected"
    )
    if admission is None or not admission.accepted or admission.lease is None:
        result = LeverResult(success=False, lever=action.lever, error=rejection)
        _safe_log_event(
            engine,
            "core_cellular_command_rejected",
            lever=action.lever.name,
            reason=rejection,
            objective_id=str(action.objective_id or ""),
            decision_trace_id=decision_trace_id,
            dispatch_owner=OWNER,
            transaction_stage="pre_dispatch_admission",
            **lever_action_initiator_provenance(action),
        )
        return _execution_receipt(
            request,
            submitted=False,
            reason=rejection,
            occurred_at=current,
        )

    execution_lease = admission.lease
    claimed_key = execution_lease.transaction.action_key
    if claimed_key is None or claimed_key.root_id != execution_lease.root_id:
        raise RuntimeError("core_command_claimed_lifecycle_missing")
    if (
        action.intent in EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS
        and is_band_control_action(action.lever.name, action.params)
    ):
        reserved, reserve_reason = require_action_source_state_authority(
            engine
        ).reserve_emergency_band_rescue(
            command_id=claimed_key.command_id,
            now=started_ts,
        )
        if not reserved:
            command_lifecycle.cancel_cellular(
                root_id=execution_lease.root_id,
                reason=reserve_reason,
                receipt={
                    "owner": OWNER,
                    "stage": "emergency_rescue_reservation",
                },
                now=time.time(),
            )
            return _execution_receipt(
                request,
                submitted=False,
                reason=reserve_reason,
                occurred_at=time.time(),
            )
    live_module = engine.input_module
    mark_pending = getattr(live_module, "note_event_write_pending", None)
    clear_pending = getattr(live_module, "clear_event_write_pending", None)
    pending_token = ""
    try:
        if not callable(mark_pending) or not callable(clear_pending):
            raise RuntimeError("modem_live_event_write_barrier_unavailable")
        pending_status = mark_pending(
            action,
            source=action_source,
        )
        if not isinstance(pending_status, Mapping):
            raise RuntimeError(
                "modem_live_event_write_barrier_receipt_invalid"
            )
        pending_token = str(pending_status.get("token", "") or "").strip()
        if (
            pending_status.get("active") is not True
            or str(pending_status.get("owner") or "") != "modem_live"
            or not pending_token
        ):
            if pending_token:
                clear_pending(
                    action,
                    token=pending_token,
                    source=f"{action_source}_barrier_rejected",
                )
            raise RuntimeError(
                "modem_live_event_write_barrier_receipt_rejected"
            )
    except Exception as exc:
        _record_runtime_control_fault(
            engine,
            source="core_command_event_write_pending",
            exc=exc,
        )
        command_lifecycle.cancel_cellular(
            root_id=execution_lease.root_id,
            reason="core_command_event_write_barrier_rejected",
            receipt={
                "owner": OWNER,
                "stage": "modem_live_event_write_barrier",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            now=time.time(),
        )
        _safe_log_event(
            engine,
            "core_cellular_command_rejected",
            lever=action.lever.name,
            reason="core_command_event_write_barrier_rejected",
            objective_id=str(action.objective_id or ""),
            decision_trace_id=decision_trace_id,
            dispatch_owner=OWNER,
            transaction_stage="modem_live_event_write_barrier",
            error_type=type(exc).__name__,
            error=str(exc),
            **lever_action_initiator_provenance(action),
        )
        return _execution_receipt(
            request,
            submitted=False,
            reason="core_command_event_write_barrier_rejected",
            occurred_at=time.time(),
        )

    worker_setup_error: BaseException | None = None
    try:
        submitted = _start_core_command_worker(
            engine,
            action=action,
            execution_gate=execution_gate,
            pending_token=pending_token,
            decision_trace_id=decision_trace_id,
            requested_bands=requested_bands,
            submitted_ts=started_ts,
            execution_lease=execution_lease,
        )
    except Exception as exc:
        worker_setup_error = exc
        submitted = False
        _record_runtime_control_fault(
            engine,
            source="core_command_worker_setup",
            exc=exc,
        )
    if not submitted:
        if callable(clear_pending):
            try:
                clear_pending(
                    action,
                    token=pending_token,
                    source=f"{action_source}_dispatch_failed",
                )
            except Exception as exc:
                _record_runtime_control_fault(
                    engine,
                    source="core_command_event_write_pending_clear",
                    exc=exc,
                )
        command_lifecycle.cancel_cellular(
            root_id=execution_lease.root_id,
            reason="core_command_worker_start_rejected",
            receipt={"owner": OWNER, "stage": "worker_start"},
            now=time.time(),
        )
        worker_status = getattr(engine, "_core_command_worker_status", {})
        dispatch_failed = bool(
            worker_setup_error is not None
            or (
                isinstance(worker_status, dict)
                and worker_status.get("status") == "dispatch_failed"
            )
        )
        return _execution_receipt(
            request,
            submitted=False,
            reason=(
                "core_command_dispatch_failed"
                if dispatch_failed
                else "core_command_worker_inflight"
            ),
            occurred_at=time.time(),
            detail={
                "backlog": 0 if dispatch_failed else 1,
                "dispatch_failed": dispatch_failed,
                "error_type": (
                    type(worker_setup_error).__name__
                    if worker_setup_error is not None
                    else ""
                ),
            },
        )
    # The worker exists but cannot pass the gateway yet. Record the physical
    # lifecycle now so the durable dispatch checkpoint contains both sides of
    # the command. If this fails, the gateway aborts the prepared worker. The
    # modem cannot be changed while the gate is closed.
    command_lifecycle.mark_dispatched(
        execution_lease,
        receipt={
            "owner": OWNER,
            "decision_trace_id": str(decision_trace_id or ""),
        },
        now=time.time(),
    )
    return _execution_receipt(
        request,
        submitted=True,
        reason="submitted_async",
        occurred_at=started_ts,
        physical_action_root_id=execution_lease.root_id,
        detail={"action_transaction": claimed_key.as_dict()},
    )


def abort_core_cellular_command(
    engine: CoreRuntimePort,
    request: CoreCommandRequest,
    *,
    execution_gate: CoreCommandExecutionGate,
    reason: str,
) -> CoreCommandExecutionAbortReceipt:
    """Stop a prepared worker and wait until it has exited."""

    if not isinstance(request, CoreCommandRequest):
        raise TypeError("core_cellular_command_request_required")
    if not isinstance(execution_gate, CoreCommandExecutionGate):
        raise TypeError("core_command_execution_gate_required")
    if (
        execution_gate.request_id != request.request_id
        or execution_gate.matrix_identity != request.matrix_identity
    ):
        raise RuntimeError("core_command_execution_gate_identity_mismatch")
    lock = _core_command_worker_lock(engine)
    with lock:
        worker = getattr(engine, "_core_command_worker_thread", None)
    if worker is None:
        lifecycle_cancelled = _command_lifecycle_service(
            engine
        ).abort_pre_worker_execution(
            request_id=request.request_id,
            matrix_identity=request.matrix_identity,
            reason=reason,
            now=time.time(),
        )
        return CoreCommandExecutionAbortReceipt(
            request_id=request.request_id,
            matrix_identity=request.matrix_identity,
            reason=reason,
            worker_terminated=True,
            lifecycle_cancelled=lifecycle_cancelled,
            event_write_barrier_released=True,
        )
    execution_gate.abort(reason=reason)
    worker.join(timeout=CORE_COMMAND_ABORT_JOIN_TIMEOUT_S)
    worker_terminated = not worker.is_alive()
    acknowledgement = execution_gate.abort_acknowledgement()
    lifecycle_cancelled = bool(
        acknowledgement is not None and acknowledgement[0] is True
    )
    barrier_released = bool(
        acknowledgement is not None and acknowledgement[1] is True
    )
    return CoreCommandExecutionAbortReceipt(
        request_id=request.request_id,
        matrix_identity=request.matrix_identity,
        reason=reason,
        worker_terminated=worker_terminated,
        lifecycle_cancelled=lifecycle_cancelled,
        event_write_barrier_released=barrier_released,
    )
