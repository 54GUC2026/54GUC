"""
arch4/extra_contract.py
═══════════════════════════════════════════════════════════════
Extra dict ownership contract.

The `extra` dict flows from ControllerRuntimeFabric through EngineTick into runtime
diagnostics, telemetry/export paths, and non-GUI operational receipts. Multiple
subsystems write keys into extra. This file documents who writes what, when,
and which runtime/export path may read it.

This file is intentionally small: modules still write directly to extra, but
tests enforce that writer-owned keys are declared here before new top-level
surfaces are added.
═══════════════════════════════════════════════════════════════

── KEY OWNERSHIP ─────────────────────────────────────────────────────────────

WRITER: modem_live/acquisition.py (accepted-generation materialization)
  extra["health_checks"]          Health check results from health_checks module

WRITER: Controller Runtime Fabric stages
  extra["tick_stages"]            Per-stage timing (modem_read_ms, decide_ms, etc.)
  extra["firmware_band_lock_risk"] True if modem firmware has known band lock issue

WRITER: engine.py (device_monitor lane)
  extra["device_monitor"]         Device monitor runtime status dict
                                  Contains: backend, emergency_active, reboot_pending,
                                  morning_sequence_recommend_active, etc.
                                  PURPOSE: runtime diagnostics, telemetry logging
                                  NOT READ BY: decide() — see engine.py tick() docstring

WRITER: engine.py (runtime_incident_tracker)
  extra["incident_assessment"]    Hard-incident / episode-tracking dict from runtime_incident_tracker
                                  Contains: incident_class, severity, recommended_action, etc.

WRITER: engine.py (proposal/bouncer/intent)
  extra["controller_posture"]     Immutable Controller Posture Authority receipt
  extra["controller_posture_id"]  Exact posture receipt identity
  extra["action_source_state"]    Action Source State Authority receipt
  extra["radio_transition"]       Radio Transition Authority attribution frame
  extra["brain_action_selection"] Correlated Brain selected-action receipt
  extra["brain_action_selection_suppressed"] Exact selection suppression reason

  extra["proposal_result"]       Explicit proposal-stage contract dict
                                  Contains: stage, authority, lever, intent, objective_id,
                                            reason, score, manual, decision_trace_id

  extra["bouncer_intent"]         BouncerVerdict.to_dict() — intent evaluation result
                                  Contains: level, code, message, details, ts

  extra["intent_verdict_result"]  Explicit intent-verdict contract dict
                                  Contains: stage, authority, level, code, message, decision_trace_id

WRITER: engine.py (action outcome)
  extra["action_outcome"]         Post-action outcome assessment (when action executed)
                                  Contains: before/after modem state, outcome_ok, etc.
  extra["canonical_action_outcome"] Canonical action-only semantic surface
                                     Contains: action_scope, semantic, level, code, message
  extra["canonical_incident_state"] Canonical incident-only semantic surface
                                     Contains: incident_class, severity, probable_cause
  extra["canonical_operator_projection"] Canonical operator-facing projection
                                         Contains: projection_class, status_text

WRITER: engine.py (verification/learning contract)
  extra["verification_result"]    Explicit verification-stage contract dict
                                  Contains: stage, authority, status, note, decision_trace_id

  extra["execution_result"]       Optional explicit execution-stage contract dict
                                  Contains: stage, authority, execution_status, execution_error
                                  Contains: stage, authority, status, note, decision_trace_id

  extra["outcome_verdict_result"] Explicit outcome-verdict contract dict
                                  Contains: stage, authority, level, code, message, decision_trace_id

  extra["classified_learning_commit"] Explicit classified learning-commit receipt
                                  Contains: stage, authority, commit_status, verification_status,
                                            bouncer_intent_code, referee_outcome_code, incident_class

WRITER: modem_live/acquisition.py
  extra["input_module"]           Modem Live acquisition diagnostics
  extra["computed_details"]       Extended computed signal details

WRITER: status_payload.py (build_status_payload)
  Various status keys merged into extra when building GUI payload

WRITER: calibration modules
  extra["calibration"]            Calibration authority runtime status
  extra["calibration_status"]     Current calibration workflow status

WRITER: engine_runtime_status.py (diagnostic projection only)
  extra["operational_service_status"] Exact normalized receipt read from the
                                       sole operational_status runtime register

── DISPLAY RULE ──────────────────────────────────────────────────────────────

NGUI does not read `extra` for live display. Operator-visible fields must pass
through declared runtime registers, NGUI runtime projection, and NGUI page
truth. Any new direct display read from `extra` is an architecture violation.

── LIFECYCLE ─────────────────────────────────────────────────────────────────

1. ControllerRuntimeFabric creates an empty extra dict
2. Fabric-owned AcceptedModemGeneration materialization may add health_checks
   and declared Modem Live diagnostic keys without rereading modem truth
3. device_monitor.observe() runs; result written to extra["device_monitor"]
4. runtime_incident_tracker.assess() runs; result written to extra["incident_assessment"]
5. bouncer.evaluate_intent() runs; result written to extra["bouncer_intent"]
   and extra["intent_verdict_result"]
6. suppression guards run; manual_*_suppressed keys added if relevant
7. action executes; action_outcome may be added
8. bouncer/referee outcome path may write extra["outcome_verdict_result"]
9. EngineTick built with extra dict
10. Runtime export/diagnostic paths may consume declared extra keys

── RULES ─────────────────────────────────────────────────────────────────────

1. Writers: use setdefault() or direct assignment; don't clobber existing keys
2. Readers: always use .get() with appropriate default; extra may be empty
3. Keys should be documented here when added
4. NGUI must neither read nor write extra for live display
5. decide() does NOT read extra — see engine.py tick() docstring for explanation
"""

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

EXTRA_KEYS = {
    # private/canonical status surfaces
    "_base_status",
    "_canonical_status",
    # segmented runtime surfaces
    "status",
    "action",
    "learning",
    "faults",
    "exports",
    # engine core
    "health_checks",
    "tick_stages",
    "engine_lanes",
    "firmware_band_lock_risk",
    # device_monitor lane
    "device_monitor",
    "modem_health",
    "watchdog_surface",
    "runtime_recovery",
    "radio_mutation_window",
    "radio_transition",
    "runtime_state_memory",
    "durable_memory_runtime_status",
    "operational_service_status",
    "policy_overlay",
    "connection_lost",
    "controller_faults",
    # incident/bouncer
    "incident_assessment",
    "proposal_result",
    "bouncer_intent",
    "bouncer_outcome",
    "referee_outcome",
    "intent_verdict_result",
    "brain",
    "brain_vectors",
    "brain_bouncer",
    "brain_referee",
    "brain_transaction",
    "brain_transaction_pending_proof",
    "brain_transaction_register",
    "controller_posture",
    "controller_posture_id",
    "action_source_state",
    "brain_action_selection",
    "brain_action_selection_suppressed",
    "controller_event_journal",
    "controller_fault_supervisor",
    "brain_referee_register",
    "brain_feed_registers",
    "brain_feed_registers_status",
    "modem_sample_publication_status",
    "opportunity_inventory",
    "counterfactual_opportunity_audit",
    "smart_loop_candidate_projection",
    "smart_loop_authority",
    "self_learning_strategy",
    "decision_quality",
    "brain_learning_effect",
    "smart_loop_status",
    "smart_loop_reason",
    "smart_loop_required_next_step",
    "smart_loop_operationalization_gap",
    "smart_loop_generation_gap",
    "counterfactual_status",
    "counterfactual_reason",
    "counterfactual_should_have_candidate",
    "counterfactual_candidate_generation_failed",
    "action_admission",
    "action_admission_authority",
    "decision_input",
    "decision_input_blocking_reasons",
    "decision_input_trust_reasons",
    "auto_action_suppressions",
    "auto_action_suppressed",
    "outage_window_active",
    # action outcome
    "action_outcome",
    "canonical_action_outcome",
    "canonical_incident_state",
    "canonical_operator_projection",
    # explicit contract stages
    "verification_result",
    "execution_result",
    "outcome_verdict_result",
    "classified_learning_commit",
    # live modem/input module
    "live_register",
    "planned_radio_action_quiet",
    "modem_live_fast_lane",
    "modem_live_stale",
    "modem_read_missed",
    "modem_quarantine",
    "tick_fault",
    "backend_faults",
    "signal_sinr_scale",
    "signal_rsrp_scale",
    "signal_rsrq_scale",
    "signal_rssi_scale",
    # input module
    "input_module",
    "computed_details",
    "sms_runtime",
    "persistence",
    "external_memory_prior",
    "suppressed_faults",
    "extra_contract_faults",
    # calibration
    "calibration",
    "calibration_status",
    "calibration_learning_gate",
    "calibration_learning_feed",
    "learning_gate",
    "learning_gate_register",
    "learning_write_gate_checks",
    # runtime/app projection overlays
    "runtime_identity",
    "compatibility_identity",
    "runtime_health",
    "runtime_control_faults",
    "recovery_faults",
    "calibration_context_faults",
    "runtime_adapter_faults",
    "runtime_tick_faults",
    "runtime_ingress_faults",
    "runtime_tick_state_faults",
    "persistence_faults",
    "runtime_status_faults",
    "product_hardening",
    "runtime_clock",
    "runtime_clock_authority",
    "engine_subsystem_authority",
    "runtime_sidecar_write_queue",
    "ordered_output_lane",
    "runtime_authority_contract",
    "runtime_session_scope",
    "verified_modem_identity",
    "identity_scope_lock",
    "resolved_identity_card",
    "identity",
    "runtime_identity_display",
    "radio_lock_confirmation",
    "manual_cell_lock_timer",
    "json_sidecar_write_health",
    "jitter_formula",
    "state",
    "backend",
    "support_tier",
    "support_tier_label",
    "ue_category",
    "capability_profile",
    "capability_contract",
    "runtime_capabilities",
    "unsupported_fields",
    "session_changed",
    "session_scope_key",
    "__truncated_items__",
    "deployed_bands",
    # explicit action/learning/calibration projections
    "bouncer",
    "mutating_action_transaction",
    "calibration_authority",
    "calibration_learning",
    "calibration_state",
    "calibration_surface",
    "calibration_workflow",
    "calibration_runtime_policy",
    "learning_authority",
    "learning_calibration_handoff",
    "memory_tick_skipped",
    "historical_tick_skipped",
    "staged_roi_updates",
}


EXTRA_SEGMENTS = {
    "status": frozenset(
        {
            "health_checks",
            "device_monitor",
            "modem_health",
            "watchdog_surface",
            "runtime_recovery",
            "radio_mutation_window",
            "radio_transition",
            "runtime_state_memory",
            "durable_memory_runtime_status",
            "operational_service_status",
            "runtime_control_faults",
            "recovery_faults",
            "calibration_context_faults",
            "runtime_adapter_faults",
            "runtime_tick_faults",
            "runtime_ingress_faults",
            "runtime_tick_state_faults",
            "persistence_faults",
            "runtime_status_faults",
            "engine_lanes",
            "runtime_clock",
            "runtime_clock_authority",
            "engine_subsystem_authority",
            "runtime_sidecar_write_queue",
            "ordered_output_lane",
            "runtime_authority_contract",
            "runtime_session_scope",
            "verified_modem_identity",
            "identity_scope_lock",
            "resolved_identity_card",
            "identity",
            "runtime_identity_display",
            "radio_lock_confirmation",
            "json_sidecar_write_health",
            "state",
            "backend",
            "support_tier",
            "support_tier_label",
            "ue_category",
            "capability_profile",
            "capability_contract",
            "runtime_capabilities",
            "unsupported_fields",
            "session_changed",
            "session_scope_key",
            "policy_overlay",
            "connection_lost",
            "controller_faults",
            "controller_posture",
            "controller_posture_id",
            "runtime_identity",
            "compatibility_identity",
            "runtime_health",
            "deployed_bands",
        }
    ),
    "action": frozenset(
        {
            "brain",
            "brain_bouncer",
            "brain_referee",
            "brain_transaction",
            "brain_transaction_pending_proof",
            "brain_transaction_register",
            "action_source_state",
            "brain_action_selection",
            "brain_action_selection_suppressed",
            "controller_event_journal",
            "controller_fault_supervisor",
            "brain_referee_register",
            "brain_feed_registers",
            "brain_feed_registers_status",
            "modem_sample_publication_status",
            "opportunity_inventory",
            "counterfactual_opportunity_audit",
            "smart_loop_candidate_projection",
            "smart_loop_authority",
            "self_learning_strategy",
            "decision_quality",
            "brain_learning_effect",
            "smart_loop_status",
            "smart_loop_reason",
            "smart_loop_required_next_step",
            "smart_loop_operationalization_gap",
            "smart_loop_generation_gap",
            "counterfactual_status",
            "counterfactual_reason",
            "counterfactual_should_have_candidate",
            "counterfactual_candidate_generation_failed",
            "action_admission",
            "action_admission_authority",
            "decision_input",
            "decision_input_blocking_reasons",
            "decision_input_trust_reasons",
            "incident_assessment",
            "proposal_result",
            "bouncer_intent",
            "bouncer",
            "bouncer_outcome",
            "referee_outcome",
            "intent_verdict_result",
            "mutating_action_transaction",
            "action_outcome",
            "canonical_action_outcome",
            "canonical_incident_state",
            "canonical_operator_projection",
            "verification_result",
            "execution_result",
            "outcome_verdict_result",
            "auto_action_suppressions",
            "auto_action_suppressed",
            "outage_window_active",
        }
    ),
    "learning": frozenset(
        {
            "brain_vectors",
            "smart_loop_candidate_projection",
            "smart_loop_authority",
            "classified_learning_commit",
            "external_memory_prior",
            "calibration",
            "calibration_status",
            "calibration_authority",
            "calibration_learning",
            "calibration_state",
            "calibration_surface",
            "calibration_workflow",
            "calibration_runtime_policy",
            "calibration_learning_gate",
            "calibration_learning_feed",
            "learning_gate",
            "learning_gate_register",
            "learning_write_gate_checks",
            "learning_authority",
            "learning_calibration_handoff",
            "memory_tick_skipped",
            "historical_tick_skipped",
            "staged_roi_updates",
        }
    ),
    "faults": frozenset(
        {
            "tick_fault",
            "backend_faults",
            "suppressed_faults",
            "extra_contract_faults",
            "runtime_status_faults",
            "product_hardening",
            "runtime_control_faults",
            "recovery_faults",
            "calibration_context_faults",
            "runtime_adapter_faults",
            "runtime_tick_faults",
            "runtime_ingress_faults",
            "runtime_tick_state_faults",
            "persistence_faults",
        }
    ),
    "exports": frozenset(
        {
            "tick_stages",
            "engine_lanes",
            "firmware_band_lock_risk",
            "live_register",
            "planned_radio_action_quiet",
            "modem_live_fast_lane",
            "modem_live_stale",
            "modem_read_missed",
            "modem_quarantine",
            "signal_sinr_scale",
            "signal_rsrp_scale",
            "signal_rsrq_scale",
            "signal_rssi_scale",
            "input_module",
            "computed_details",
            "sms_runtime",
            "persistence",
            "durable_memory_runtime_status",
            "manual_cell_lock_timer",
            "jitter_formula",
            "__truncated_items__",
        }
    ),
}

REQUIRED_EXTRA_SEGMENTS = frozenset(EXTRA_SEGMENTS)


@dataclass(frozen=True)
class TickRuntimeSurface:
    """Immutable segmented view of the tick extra bus."""

    status: Mapping[str, Any]
    action: Mapping[str, Any]
    learning: Mapping[str, Any]
    faults: Mapping[str, Any]
    exports: Mapping[str, Any]

    @classmethod
    def from_extra(
        cls,
        extra: dict,
        *,
        stage: str = "",
        require_segments: bool = True,
        prune_unknown: bool = True,
    ) -> "TickRuntimeSurface":
        working = dict(extra or {}) if isinstance(extra, dict) else {}
        build_segmented_extra_surfaces(working, require_all=require_segments)
        enforce_extra_contract(
            working,
            stage=stage,
            require_segments=require_segments,
            prune_unknown=prune_unknown,
        )
        build_segmented_extra_surfaces(working, require_all=require_segments)
        return cls(
            status=_freeze_surface_mapping(working.get("status")),
            action=_freeze_surface_mapping(working.get("action")),
            learning=_freeze_surface_mapping(working.get("learning")),
            faults=_freeze_surface_mapping(working.get("faults")),
            exports=_freeze_surface_mapping(working.get("exports")),
        )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "status": _thaw_surface_mapping(self.status),
            "action": _thaw_surface_mapping(self.action),
            "learning": _thaw_surface_mapping(self.learning),
            "faults": _thaw_surface_mapping(self.faults),
            "exports": _thaw_surface_mapping(self.exports),
        }


def unknown_extra_keys(extra: dict) -> list[str]:
    """Return top-level extra keys that are not declared in this contract."""
    if not isinstance(extra, dict):
        return []
    return sorted(str(key) for key in extra.keys() if str(key) not in EXTRA_KEYS)


def unsegmented_extra_keys() -> list[str]:
    """Return declared top-level keys that are not owned by a segment."""
    segment_keys: set[str] = set()
    for keys in EXTRA_SEGMENTS.values():
        segment_keys.update(keys)
    exempt = {"_base_status", "_canonical_status", *REQUIRED_EXTRA_SEGMENTS}
    return sorted(key for key in EXTRA_KEYS if key not in segment_keys and key not in exempt)


def build_segmented_extra_surfaces(extra: dict, *, require_all: bool = False) -> dict:
    """Build canonical segmented views from declared top-level runtime surfaces."""
    if not isinstance(extra, dict):
        return extra
    for segment, keys in EXTRA_SEGMENTS.items():
        surface = dict(extra.get(segment) or {}) if isinstance(extra.get(segment), dict) else {}
        for key in sorted(keys):
            if key in extra:
                surface[key] = extra[key]
        if surface or require_all:
            extra[segment] = surface
        else:
            extra.pop(segment, None)
    return extra


def missing_extra_segments(extra: dict) -> list[str]:
    if not isinstance(extra, dict):
        return []
    return sorted(segment for segment in REQUIRED_EXTRA_SEGMENTS if segment not in extra)


def enforce_extra_contract(
    extra: dict,
    *,
    stage: str = "",
    require_segments: bool = False,
    prune_unknown: bool = False,
) -> dict:
    """Attach a contract fault surface when unknown top-level keys appear."""
    if not isinstance(extra, dict):
        return extra
    unknown = unknown_extra_keys(extra)
    missing_segments = missing_extra_segments(extra) if require_segments else []
    if not unknown and not missing_segments:
        extra.pop("extra_contract_faults", None)
        return extra
    extra["extra_contract_faults"] = {
        "stage": str(stage or "unknown"),
        "unknown_keys": unknown,
        "missing_segments": missing_segments,
        "known_key_count": len(EXTRA_KEYS),
    }
    if prune_unknown:
        for key in unknown:
            extra.pop(key, None)
    return extra


def finalize_extra_contract_surfaces(
    extra: dict,
    *,
    stage: str = "",
    prune_unknown: bool = True,
) -> dict:
    """Finalize runtime extra into mandatory contract segments and fault drift."""
    if not isinstance(extra, dict):
        return extra
    build_segmented_extra_surfaces(extra, require_all=True)
    enforce_extra_contract(
        extra,
        stage=stage,
        require_segments=True,
        prune_unknown=prune_unknown,
    )
    build_segmented_extra_surfaces(extra, require_all=True)
    return extra


def _freeze_surface_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    return MappingProxyType({str(key): _freeze_surface_value(raw) for key, raw in value.items()})


def _freeze_surface_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_surface_value(raw) for key, raw in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_surface_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_surface_value(item) for item in value)
    if isinstance(value, set):
        return tuple(_freeze_surface_value(item) for item in sorted(value, key=lambda item: str(item)))
    try:
        return deepcopy(value)
    except Exception:
        return value


def _thaw_surface_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_surface_value(raw) for key, raw in dict(value or {}).items()}


def _thaw_surface_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_surface_value(raw) for key, raw in dict(value or {}).items()}
    if isinstance(value, tuple):
        return [_thaw_surface_value(item) for item in value]
    if isinstance(value, list):
        return [_thaw_surface_value(item) for item in value]
    try:
        return deepcopy(value)
    except Exception:
        return value
