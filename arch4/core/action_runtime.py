"""Core radio-action lifecycle and recovery owner."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CoreCellularObservationGeneration

import logging
import time
from typing import Any, Dict, Mapping, Optional

from arch4.coerce import safe_bool
from arch4.action_transaction import (
    bound_core_command_target_from_action,
)
from arch4.core.action_source_state_authority import (
    require_action_source_state_authority,
)
from arch4.command_transactions import (
    CommandAuthority,
    CommandLifecycleStage,
    CommandTerminalReceipt,
)
from arch4.runtime_boundary_contracts import RadioMutationWindow
from arch4.runtime_sidecar_health import format_runtime_iso_utc
from arch4.runtime_faults import record_suppressed_fault
from arch4.core.mutation_policy import (
    clear_mutating_action_lock_state,
    compute_mutating_action_lock,
)
from arch4.core.band_action_policy import BAND_CONTROL_LEVERS
from arch4.interfaces import LeverAction, LeverType
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CoreCellLockTarget,
    CorePBandTarget,
)
from arch4.core_contracts.commands import CoreCommandAdmissionStatus

log = logging.getLogger(__name__)


def _phase_iso(ts: float) -> str:
    if float(ts or 0.0) <= 0.0:
        return ""
    return format_runtime_iso_utc(float(ts))


def _duration_since(ts: float, base_ts: float) -> Optional[float]:
    if float(ts or 0.0) <= 0.0 or float(base_ts or 0.0) <= 0.0:
        return None
    return round(float(ts) - float(base_ts), 3)


def _radio_mutation_requested_bands(action: LeverAction) -> list[int]:
    target = bound_core_command_target_from_action(action)
    if isinstance(target, CoreBandSetTarget):
        return list(target.bands)
    if isinstance(target, CoreCellLockTarget):
        return [target.band] if target.band is not None else []
    if isinstance(target, CorePBandTarget):
        return list(
            target.ca_pool
            or ((target.pband,) if target.pband > 0 else ())
        )
    return []


def _selected_action_command_id(action: LeverAction) -> str:
    params = action.params if isinstance(action.params, dict) else {}
    if not safe_bool(params.get("_selected_action_command", False), False):
        return ""
    return str(params.get("_selected_action_command_id") or "").strip()


def _record_radio_mutation_evidence_fault(
    engine: Any,
    *,
    source: str,
    exc: Exception,
    action_root_id: str = "",
) -> None:
    current = getattr(engine, "_action_runtime_faults", {})
    payload = current if isinstance(current, dict) else {}
    payload = record_suppressed_fault(
        payload,
        category="authority",
        source=str(source or "radio_mutation_evidence"),
        error_type=type(exc).__name__,
        detail=str(exc),
        metadata={
            "boundary": "command_authority_terminal_projection",
            "action_root_id": str(action_root_id or ""),
        },
    )
    try:
        setattr(engine, "_action_runtime_faults", payload)
    except Exception:
        # The command receipt is final. A failed diagnostic writer must not
        # reopen it or stop unrelated work.
        pass
    log.error("Radio mutation evidence projection failed: %s", exc)


def _close_radio_mutation_evidence_from_terminal(
    engine: Any,
    receipt: CommandTerminalReceipt,
) -> None:
    """Close the timing record from the final command receipt."""

    if not isinstance(receipt, CommandTerminalReceipt):
        raise TypeError("radio_mutation_terminal_receipt_required")
    key = receipt.action_key
    if key is None:
        raise RuntimeError("radio_mutation_terminal_action_key_missing")
    terminal_root = str(key.root_id or "").strip()
    terminal_ts = float(receipt.terminal_ts or time.time())
    engine._last_radio_mutation_context_until_ts = max(
        float(getattr(engine, "_last_radio_mutation_context_until_ts", 0.0) or 0.0),
        terminal_ts + 300.0,
    )
    engine._last_radio_mutation_context = {
        "lever": key.lever,
        "workflow_id": "",
        "verified_ts": terminal_ts,
        "success": receipt.terminal_status.value == "settled",
        "note": str(receipt.reason or receipt.terminal_status.value),
        "action_root_id": terminal_root,
        "terminal_status": receipt.terminal_status.value,
        "target_generation": int(receipt.target_generation or 0),
        "settle_generation": int(receipt.settle_generation or 0),
    }
    engine.diagnostics.log_event(
            "radio_mutation_phase",
            transaction_id=receipt.transaction.command_id,
            event_owner="54guc.core.command_lifecycle",
            phase=(
                "verified"
                if receipt.terminal_status.value == "settled"
                else "terminal"
            ),
            lever=key.lever,
            workflow_id="",
            action_root_id=terminal_root,
            command_id=receipt.transaction.command_id,
            terminal_status=receipt.terminal_status.value,
            success=receipt.terminal_status.value == "settled",
            note=str(receipt.reason or receipt.terminal_status.value),
            target_generation=int(receipt.target_generation or 0),
            settle_generation=int(receipt.settle_generation or 0),
            submitted_to_verified_s=round(
                terminal_ts - receipt.transaction.created_ts,
                3,
            ),
            command_to_verified_s=None,
    )


def publish_command_terminal_evidence(
    engine: Any,
    receipt: CommandTerminalReceipt,
    now: Optional[float] = None,
) -> None:
    """Apply final side effects after the command register is updated."""

    if not isinstance(receipt, CommandTerminalReceipt):
        raise TypeError("command_terminal_receipt_required")
    key = receipt.action_key
    if key is None:
        return
    if key.lever == "RADIO_MODE":
        require_action_source_state_authority(engine).note_radio_mode_terminal(
            root_id=receipt.root_id, settled=receipt.terminal_status.value == "settled",
            observed_at=receipt.terminal_ts, reason=receipt.reason)
    projections = (
        (
            "radio_mutation_terminal_evidence_projection",
            lambda: _close_radio_mutation_evidence_from_terminal(engine, receipt),
        ),
    )
    for source, project in projections:
        try:
            project()
        except Exception as exc:
            _record_radio_mutation_evidence_fault(
                engine,
                source=source,
                exc=exc,
                action_root_id=key.root_id,
            )


def _radio_mutation_workflow_id(action: LeverAction) -> str:
    params = action.params if isinstance(action.params, dict) else {}
    sequence = str(params.get("sequence", "") or "").strip().lower()
    if sequence:
        return sequence
    intent = str(getattr(action, "intent", "") or "").strip()
    if intent == "morning_sequence":
        return "morning"
    objective_id = str(getattr(action, "objective_id", "") or "")
    if objective_id.startswith("morning_sequence:"):
        return "morning"
    if action.lever == LeverType.PBAND and (
        list(params.get("ca_pool") or [])
        or safe_bool(params.get("sequential", False), False)
    ):
        return "pband_plus_ca"
    return action.lever.name.lower()


def _radio_mutation_sequence_index(action: LeverAction) -> int:
    params = action.params if isinstance(action.params, dict) else {}
    for key in ("sequence_step", "workflow_step"):
        try:
            value = int(params.get(key, 0) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    objective_id = str(getattr(action, "objective_id", "") or "")
    if objective_id.startswith("morning_sequence:"):
        parts = objective_id.split(":")
        if parts:
            try:
                return max(0, int(parts[-1]))
            except Exception:
                return 0
    return 0


def _radio_mutation_workflow_payload(action: LeverAction) -> dict[str, Any]:
    params = action.params if isinstance(action.params, dict) else {}
    workflow_id = _radio_mutation_workflow_id(action)
    step = _radio_mutation_sequence_index(action)
    total = 0
    try:
        total = int(params.get("sequence_total", 0) or 0)
    except Exception:
        total = 0
    if workflow_id == "morning" and total <= 0:
        total = 3
    if action.lever == LeverType.PBAND and workflow_id == "pband_plus_ca" and total <= 0:
        total = 2
    return {
        "workflow_id": workflow_id,
        "sequence_step": step,
        "sequence_total": total,
        "sequence_active": bool(total > 1 or workflow_id in {"morning", "pband_plus_ca"}),
    }


def should_log_radio_mutation_phases(action: Optional[LeverAction]) -> bool:
    if action is None:
        return False
    return action.lever in {
        LeverType.BAND_SELECTION,
        LeverType.BAND_LOCK,
        LeverType.CELL_LOCK,
        LeverType.PBAND,
        LeverType.NR_BAND_LOCK,
        LeverType.RADIO_MODE,
        LeverType.RESTART,
    }


def note_radio_mutation_command_sent(
    engine: Any,
    *,
    action: LeverAction,
    action_result: Any,
    physical_stages: tuple[Any, ...],
) -> None:
    raise RuntimeError("radio_mutation_trace_path_illegal")


def extend_auto_action_lockout(engine: Any, duration_s: float, *, reason: str) -> None:
    max_lockout_s = 300.0
    min_same_reason_topup_remaining_s = 15.0
    now = time.time()
    clamped_s = min(max(0.0, float(duration_s)), max_lockout_s)
    if clamped_s <= 0.0:
        return
    prev_until = float(engine._auto_action_lockout_until or 0.0)
    last_logged_reason = str(getattr(engine, "_auto_action_lockout_event_reason", "") or "")
    prev_remaining_s = max(0.0, prev_until - now)
    if (
        prev_until > now
        and str(reason or "") == last_logged_reason
        and prev_remaining_s >= min_same_reason_topup_remaining_s
    ):
        return
    new_until = now + clamped_s
    if new_until <= (prev_until + 0.5):
        return
    engine._auto_action_lockout_until = new_until
    extension_s = max(0.0, float(new_until - prev_until))
    engine.diagnostics.log_event(
        "auto_action_lockout_updated",
        reason=reason,
        previous_remaining_s=max(0, int(round(prev_remaining_s))),
        new_remaining_s=max(0, int(round(new_until - now))),
        extension_s=round(extension_s, 3),
    )
    engine._auto_action_lockout_event_reason = str(reason or "")


def cfg_float_allow_zero(engine: Any, name: str, default: float) -> float:
    raw = getattr(engine.cfg, name, default)
    if raw is None:
        return float(default)
    return float(raw)


def arm_mutating_action_lock(engine: Any, lever: LeverType) -> None:
    if lever.name in BAND_CONTROL_LEVERS:
        raise RuntimeError("band_action_generic_mutating_lock_illegal")
    if lever not in {LeverType.UE_CAPABILITY, LeverType.RESTART}:
        raise RuntimeError(
            f"generic_mutating_action_lock_lever_illegal:{lever.name}"
        )
    lock_s = max(0.0, cfg_float_allow_zero(engine, "mutating_action_lock_s", 60.0))
    if lock_s <= 0.0:
        return
    engine._mutating_action_lock_until, engine._mutating_action_lock_lever, remaining_s = (
        compute_mutating_action_lock(
            current_until=engine._mutating_action_lock_until,
            lever=lever,
            lock_seconds=lock_s,
        )
    )
    engine.diagnostics.log_event(
        "mutating_action_lock_updated",
        reason="arm",
        lever=lever.name,
        remaining_s=remaining_s,
    )


def clear_mutating_action_lock(engine: Any, *, reason: str) -> None:
    changed, new_until, new_lever = clear_mutating_action_lock_state(
        current_until=engine._mutating_action_lock_until,
        current_lever=engine._mutating_action_lock_lever,
    )
    if not changed:
        return
    prior_lever = str(engine._mutating_action_lock_lever or "")
    engine._mutating_action_lock_until = new_until
    engine._mutating_action_lock_lever = new_lever
    engine.diagnostics.log_event(
        "mutating_action_lock_updated",
        reason=reason,
        lever=prior_lever,
        remaining_s=0,
    )


def export_mutating_action_transaction_state(engine: Any) -> Dict[str, Any]:
    return {
        "version": 4,
        "mutating_action_lock": {
            "until_ts": float(getattr(engine, "_mutating_action_lock_until", 0.0) or 0.0),
            "lever": str(getattr(engine, "_mutating_action_lock_lever", "") or ""),
        },
    }


def restore_mutating_action_transaction_state(engine: Any, payload: Any) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if payload.get("version") != 4:
        raise RuntimeError("retired_mutating_action_state_version_rejected")
    if set(payload) != {"version", "mutating_action_lock"}:
        raise RuntimeError("mutating_action_state_fields_invalid")
    if "planned_radio_action_quiet" in payload:
        raise RuntimeError(
            "retired_engine_planned_quiet_restore_rejected"
        )
    if "runtime_recovery" in payload:
        raise RuntimeError("retired_engine_runtime_recovery_restore_rejected")
    if "radio_mutation_trace" in payload:
        raise RuntimeError("independent_radio_mutation_trace_restore_rejected")
    now = time.time()

    lock = payload.get("mutating_action_lock") if isinstance(payload.get("mutating_action_lock"), dict) else {}
    lock_until = float(lock.get("until_ts", 0.0) or 0.0)
    if lock_until > now:
        lever_name = str(lock.get("lever", "") or "")
        if lever_name in BAND_CONTROL_LEVERS:
            raise RuntimeError("persisted_band_action_generic_lock_rejected")
        if lever_name not in {LeverType.UE_CAPABILITY.name, LeverType.RESTART.name}:
            raise RuntimeError("persisted_generic_mutating_lock_lever_invalid")
        engine._mutating_action_lock_until = lock_until
        engine._mutating_action_lock_lever = lever_name

    return True


def verify_mutating_action_settle_for_engine(
    engine: Any,
    *,
    action: LeverAction,
    initial_modem: Optional[CoreCellularObservationGeneration],
    initial_computed: Optional[DerivedSignalFrames],
) -> tuple[bool, str, Optional[CoreCellularObservationGeneration], Optional[DerivedSignalFrames]]:
    del engine, action, initial_modem, initial_computed
    raise RuntimeError(
        "direct_post_action_modem_readback_illegal_use_committed_matrix"
    )


def radio_mutation_window_status(
    engine: Any,
    *,
    now: Optional[float] = None,
    modem_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dirty-window surface for radio mutation/recovery periods."""
    now_ts = float(now if now is not None else time.time())
    reasons: list[str] = []
    until_values: list[float] = []
    sources: Dict[str, Any] = {}

    lifecycle_service = getattr(engine, "command_lifecycle", None)
    admission_reader = getattr(lifecycle_service, "admission_status", None)
    if not callable(admission_reader):
        raise RuntimeError("core_command_lifecycle_health_unavailable")
    command_admission = admission_reader()
    if not isinstance(command_admission, CoreCommandAdmissionStatus):
        raise TypeError("core_command_lifecycle_health_invalid")
    sources["command_lifecycle"] = command_admission.to_payload()
    command_admission_available = command_admission.available
    command_admission_reason = command_admission.reason
    command_protection_active = (
        command_admission.command_protection_active
    )
    command_protection_reason = (
        command_admission.command_protection_reason
    )

    # These clocks affect band admission only. They must not mark telemetry
    # dirty, pause learning, or block unrelated user actions.
    sources["band_action_policy"] = require_action_source_state_authority(
        engine
    ).band_action_policy_status(now=now_ts)
    sources["radio_mode_lease"] = require_action_source_state_authority(engine).radio_mode_lease_status()

    def _add_reason(reason: str, payload: Optional[Dict[str, Any]], *, until_ts: float = 0.0) -> None:
        if reason not in reasons:
            reasons.append(reason)
        if until_ts > now_ts:
            until_values.append(float(until_ts))
        if isinstance(payload, dict):
            sources[reason] = dict(payload)

    modem_health_payload = dict(modem_health or {})
    expected_interruption_active = safe_bool(
        modem_health_payload.get("modem_live_io_quiet_active", False),
        False,
    )
    if expected_interruption_active:
        remaining_s = max(
            0.0,
            float(
                modem_health_payload.get(
                    "modem_live_io_quiet_remaining_s",
                    0.0,
                )
                or 0.0
            ),
        )
        expected_interruption = {
            "active": True,
            "reason": "modem_live_expected_interruption",
            "remaining_s": remaining_s,
            "until_ts": now_ts + remaining_s,
        }
        _add_reason(
            "modem_live_expected_interruption",
            expected_interruption,
            until_ts=now_ts + remaining_s,
        )
    if command_protection_active:
        _add_reason(
            "command_protection",
            {
                "active": True,
                "reason": command_protection_reason,
            },
        )

    lock_until = float(getattr(engine, "_mutating_action_lock_until", 0.0) or 0.0)
    lock_active = lock_until > now_ts
    workflow_transition_active = False
    command_authority = getattr(engine, "command_authority", None)
    if not isinstance(command_authority, CommandAuthority):
        raise RuntimeError("command_authority_unavailable")
    lifecycle = command_authority.active_lifecycle()
    if lifecycle is not None:
        transaction_params = lifecycle.lease.transaction.params_dict()
        receipts = lifecycle.as_dict()["receipts"]
        modem_receipt = (
            dict(receipts.get("modem") or {})
            if isinstance(receipts, Mapping)
            else {}
        )
        physical_stages = list(modem_receipt.get("stages") or [])
        stage_dispatches = [
            float(stage.get("request_dispatched_at") or 0.0)
            for stage in physical_stages
            if isinstance(stage, Mapping)
        ]
        workflow_transition_active = lifecycle.stage in {
            CommandLifecycleStage.ACCEPTED,
            CommandLifecycleStage.CONFIRMED,
        }
        target = lifecycle.key.target.to_mapping()
        workflow_payload = {
            "active": True,
            "lever": lifecycle.key.lever,
            "workflow_id": lifecycle.key.root_id,
            "sequence_step": int(
                transaction_params.get("sequence_step", 0) or 0
            ),
            "sequence_total": int(
                transaction_params.get("sequence_total", 0) or 0
            ),
            "sequence_active": bool(
                int(transaction_params.get("sequence_total", 0) or 0) > 1
            ),
            "objective_id": str(
                transaction_params.get("objective_id") or ""
            ),
            "decision_trace_id": lifecycle.key.correlation_id,
            "submitted_ts": lifecycle.lease.acquired_ts,
            "started_ts": float(
                modem_receipt.get("execution_started_at") or 0.0
            ),
            "command_sent_ts": (
                stage_dispatches[0] if stage_dispatches else 0.0
            ),
            "command2_sent_ts": (
                stage_dispatches[1] if len(stage_dispatches) > 1 else 0.0
            ),
            "requested_bands": list(
                target.get("bands")
                or target.get("ca_pool")
                or (
                    [target.get("pband")]
                    if int(target.get("pband") or 0) > 0
                    else []
                )
            ),
            "action_root_id": lifecycle.key.root_id,
            "command_stage": lifecycle.stage.value,
            "telemetry_transition_active": workflow_transition_active,
        }
        _add_reason("radio_mutation_workflow", workflow_payload)

    if lock_active:
        _add_reason(
            "mutating_action_lock",
            {
                "active": True,
                "until_ts": lock_until,
                "remaining_s": max(0, int(round(lock_until - now_ts))),
                "lever": str(getattr(engine, "_mutating_action_lock_lever", "") or ""),
            },
            until_ts=lock_until,
        )

    timer_state = require_action_source_state_authority(engine).cell_lock_lease_status(
        now=now_ts
    )
    if timer_state.get("active") is True:
        due_ts = float(timer_state.get("due_ts", 0.0) or 0.0)
        remaining_s = max(0.0, due_ts - now_ts) if due_ts > 0.0 else 0.0
        manual_timer = {
            "active": bool(timer_state.get("active") is True),
            "inflight": safe_bool(timer_state.get("inflight", False), False),
            "remaining_s": round(remaining_s, 1),
            "duration_s": float(timer_state.get("duration_s", 0.0) or 0.0),
            "due_ts": due_ts,
            "pci": int(timer_state.get("pci", 0) or 0),
            "earfcn": int(timer_state.get("earfcn", 0) or 0),
        }
        sources["manual_cell_lock_timer"] = manual_timer
        if safe_bool(manual_timer.get("inflight", False), False):
            _add_reason("manual_cell_lock_inflight", manual_timer, until_ts=due_ts)

    if isinstance(modem_health, dict) and modem_health:
        sources["modem_health"] = dict(modem_health)

    telemetry_transition_reasons = {
        "runtime_recovery",
        "manual_cell_lock_inflight",
    }
    telemetry_transition_active = bool(
        workflow_transition_active
        or any(reason in telemetry_transition_reasons for reason in reasons)
    )
    active = bool(reasons)
    until_ts = max(until_values) if until_values else 0.0
    workflow = sources.get("radio_mutation_workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}
    phase = ""
    if workflow:
        if float(workflow.get("command_sent_ts", 0.0) or 0.0) > 0.0:
            phase = "command_sent"
        elif float(workflow.get("started_ts", 0.0) or 0.0) > 0.0:
            phase = "started"
        elif float(workflow.get("submitted_ts", 0.0) or 0.0) > 0.0:
            phase = "submitted"
    raw_window = {
        "active": active,
        "reason": (
            reasons[0]
            if reasons
            else command_admission_reason
            if not command_admission_available
            else ""
        ),
        "reasons": list(reasons),
        "phase": phase,
        "workflow_id": str(workflow.get("workflow_id", "") or ""),
        "workflow_active": safe_bool(workflow.get("active", False), False),
        "sequence_step": int(workflow.get("sequence_step", 0) or 0),
        "sequence_total": int(workflow.get("sequence_total", 0) or 0),
        "sequence_active": safe_bool(workflow.get("sequence_active", False), False),
        "started_ts": 0.0,
        "until_ts": until_ts,
        "remaining_s": max(0, int(round(until_ts - now_ts))) if until_ts > now_ts else 0,
        "verification_due_ts": 0.0,
        "telemetry_trust": "transition" if telemetry_transition_active else "live",
        "telemetry_transition_active": telemetry_transition_active,
        "learning_allowed": not telemetry_transition_active,
        "action_allowed": not active and command_admission_available,
        "command_protection_active": command_protection_active,
        "command_protection_reason": command_protection_reason,
        "watchdog_pause_required": telemetry_transition_active,
        "sources": sources,
    }
    return RadioMutationWindow.from_mapping(raw_window).as_dict()


def manual_action_effect_band_set(
    engine: Any,
    *,
    action: LeverAction,
    modem: Optional[CoreCellularObservationGeneration],
) -> list[int]:
    if modem is None:
        return []
    if not isinstance(modem, CoreCellularObservationGeneration):
        raise TypeError(
            "manual_action_effect_requires_cellular_intelligence_generation"
        )
    if action.lever in {LeverType.BAND_SELECTION, LeverType.BAND_LOCK}:
        bands = getattr(modem.cellular, 'selected_band_set', None) or getattr(modem.cellular, 'active_band_set', None) or []
    else:
        bands = getattr(modem.cellular, 'active_band_set', None) or getattr(modem.cellular, 'selected_band_set', None) or []
    return sorted(int(b) for b in bands)
