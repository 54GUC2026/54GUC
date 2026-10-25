"""Brain TSAR user/reporting projection owned by the clean smart loop.

This module does not generate candidates. It presents the already-published
``arch4.smart_loop.closed_loop`` strategy, command, TSAR gate, and proof state
as one readable per-tick artifact.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from arch4.interfaces import LeverAction, LeverResult


SMART_LOOP_OWNER = "arch4.smart_loop.closed_loop"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _lower(value) in {"1", "true", "yes", "on", "y"}


def _lever_name(action: LeverAction) -> str:
    lever = getattr(action, "lever", "")
    return _text(getattr(lever, "name", lever))


def _action_bands(params: Mapping[str, Any]) -> list[Any]:
    for key in ("resolved_target_bands", "bands", "selected_band_set", "target_bands"):
        bands = params.get(key)
        if isinstance(bands, (list, tuple)):
            return list(bands)
    return []


def _candidate_from_action(
    *,
    action: Optional[LeverAction],
    action_suppressed: bool,
) -> Dict[str, Any]:
    if action is None:
        return {}
    params = _mapping(getattr(action, "params", {}))
    return {
        "present": True,
        "source": "lever_action",
        "state": "suppressed" if action_suppressed else "selected",
        "lever": _lever_name(action),
        "intent": _text(getattr(action, "intent", "")),
        "objective_id": _text(getattr(action, "objective_id", "")),
        "reason": _text(getattr(action, "reason", "")),
        "bands": _action_bands(params),
    }


def _candidate_from_trace(trace: Mapping[str, Any]) -> Dict[str, Any]:
    final = _text(trace.get("final"))
    proposed = _text(trace.get("proposed"))
    reason = _text(trace.get("tsar_reason") or trace.get("primary_blocker_reason"))
    if final and final.lower() != "none":
        return {
            "present": True,
            "source": "decision_trace",
            "state": "selected",
            "lever": final,
            "intent": "",
            "objective_id": "",
            "reason": reason,
            "bands": [],
        }
    if proposed and proposed.lower() != "none":
        return {
            "present": True,
            "source": "decision_trace",
            "state": "suppressed",
            "lever": proposed,
            "intent": "",
            "objective_id": "",
            "reason": reason,
            "bands": [],
        }
    return {
        "present": False,
        "source": "decision_trace",
        "state": "none",
        "lever": "",
        "intent": "",
        "objective_id": "",
        "reason": reason,
        "bands": [],
    }


def _smart_loop_strategy(vectors: Mapping[str, Any]) -> Mapping[str, Any]:
    strategy = _mapping(vectors.get("self_learning_strategy"))
    if strategy.get("owner") == SMART_LOOP_OWNER:
        return strategy
    return {}


def _smart_loop_authority(vectors: Mapping[str, Any], strategy: Mapping[str, Any]) -> Mapping[str, Any]:
    authority = _mapping(vectors.get("smart_loop_authority"))
    if authority.get("owner") == SMART_LOOP_OWNER:
        return authority
    candidate_projection = _mapping(vectors.get("smart_loop_candidate_projection"))
    nested = _mapping(candidate_projection.get("authority"))
    if nested.get("owner") == SMART_LOOP_OWNER:
        return nested
    gate = _mapping(strategy.get("tsar_gate"))
    action_transaction_authority = _mapping(
        strategy.get("action_transaction_authority")
    )
    selected_action_stage = _lower(
        _mapping(action_transaction_authority.get("active")).get("stage")
    )
    return {
        "owner": SMART_LOOP_OWNER if strategy else "",
        "status": _text(strategy.get("outcome")),
        "reason": _text(strategy.get("reason_code") or strategy.get("reason")),
        "required_next_step": _text(strategy.get("required_next_step")),
        "candidate_generation_gap": _text(strategy.get("outcome")) == "candidate_missing_counterfactual",
        "candidate_operationalization_gap": _text(strategy.get("outcome")) == "action_rejected",
        "candidate_count": len(strategy.get("candidates") or []),
        "actionable_count": (
            1 if selected_action_stage in {"pending", "claimed"} else 0
        ),
        "tsar_verdict": _text(gate.get("verdict")),
    }


def _derive_intent(
    *,
    candidate: Mapping[str, Any],
    strategy: Mapping[str, Any],
    authority: Mapping[str, Any],
    constraint: Mapping[str, Any],
    brain_tsar: Mapping[str, Any],
    trace: Mapping[str, Any],
    action_suppressed: bool,
) -> tuple[str, str]:
    candidate_present = _bool(candidate.get("present"))
    candidate_state = _lower(candidate.get("state"))
    candidate_intent = _lower(candidate.get("intent"))
    candidate_reason = _text(candidate.get("reason"))
    if candidate_present and candidate_state == "selected" and not action_suppressed:
        if "restore_ca" in candidate_intent:
            return "restore_ca", candidate_reason or "selected_action"
        if any(token in candidate_intent for token in ("recover", "fault", "escape", "restart")):
            return "recover", candidate_reason or "selected_action"
        if "explore" in candidate_intent or "probe" in candidate_intent:
            return "probe", candidate_reason or "selected_action"
        return "act", candidate_reason or "selected_action"
    if candidate_present and (candidate_state == "suppressed" or action_suppressed):
        return "hold", _text(trace.get("tsar_reason") or candidate_reason or "candidate_suppressed")
    if _bool(constraint.get("observe_only")):
        return "observe", "observe_only_mode"
    if _bool(constraint.get("planned_radio_quiet_active")):
        return "hold", "planned_radio_quiet"
    bouncer = _mapping(constraint.get("bouncer"))
    if _lower(bouncer.get("state")) == "blocked" or _lower(brain_tsar.get("bouncer_state")) == "blocked":
        return "hold", _text(bouncer.get("trip_reason") or brain_tsar.get("bouncer_trip_reason") or "bouncer_blocked")
    selected_action_stage = _lower(
        _mapping(
            _mapping(strategy.get("action_transaction_authority")).get(
                "active"
            )
        ).get("stage")
    )
    if selected_action_stage in {"pending", "claimed"}:
        return "act", _text(
            strategy.get("reason_code") or "brain_selected_action_ready"
        )
    if _bool(authority.get("candidate_operationalization_gap")):
        return "hold", _text(authority.get("reason") or strategy.get("reason_code") or "candidate_not_operationalized")
    if _bool(authority.get("candidate_generation_gap")):
        return "observe", _text(authority.get("reason") or "candidate_generation_gap")
    if strategy:
        return "observe", _text(strategy.get("reason_code") or strategy.get("outcome") or "smart_loop_observe")
    return "idle", "smart_loop_strategy_missing"


def _action_budget(
    *,
    constraint: Mapping[str, Any],
    brain_tsar: Mapping[str, Any],
    candidate: Mapping[str, Any],
    action_suppressed: bool,
    intent_reason: str,
) -> Dict[str, Any]:
    bouncer = _mapping(constraint.get("bouncer"))
    observe_only = _bool(constraint.get("observe_only"))
    planned_quiet = _bool(constraint.get("planned_radio_quiet_active"))
    supports = _bool(constraint.get("supports_active_control", True))
    bouncer_blocked = _lower(bouncer.get("state")) == "blocked" or _lower(brain_tsar.get("bouncer_state")) == "blocked"
    candidate_suppressed = _lower(candidate.get("state")) == "suppressed" or action_suppressed
    blocker = ""
    if observe_only:
        blocker = "observe_only_mode"
    elif planned_quiet:
        blocker = "planned_radio_quiet"
    elif not supports:
        blocker = "active_control_not_supported"
    elif bouncer_blocked:
        blocker = _text(bouncer.get("trip_reason") or brain_tsar.get("bouncer_trip_reason") or "bouncer_blocked")
    elif candidate_suppressed:
        blocker = intent_reason or "candidate_suppressed"
    mode = "blocked" if blocker else "available"
    return {
        "mode": mode,
        "safe_probe_allowed": mode == "available",
        "blocker": blocker,
        "reason": intent_reason or blocker,
        "source": "constraint_vector",
    }


def _verification_plan(
    *,
    candidate: Mapping[str, Any],
    action_result: Optional[LeverResult],
    execution_outcome: Mapping[str, Any],
    strategy: Mapping[str, Any],
    action_suppressed: bool,
) -> Dict[str, Any]:
    if _bool(candidate.get("present")) and _lower(candidate.get("state")) == "selected" and not action_suppressed:
        result_known = action_result is not None or bool(execution_outcome)
        return {
            "status": "active" if result_known else "required",
            "window": "post_action_settle",
            "success_criteria": [
                "requested lever produced observable effect",
                "radio stayed connected or recovered inside safe window",
                "smart-loop proof closes accepted, rejected, or neutral inside the timed window",
            ],
            "execution_success": (
                bool(getattr(action_result, "success", False))
                if action_result is not None
                else execution_outcome.get("action_success")
            ),
            "proof_state": "pending" if strategy.get("opened_proof") else "",
        }
    if strategy.get("opened_proof") or _mapping(strategy.get("state")).get("pending_proof"):
        return {
            "status": "active",
            "window": "smart_loop_referee_window",
            "success_criteria": ["close proof window and update ROI memory"],
            "execution_success": None,
            "proof_state": "pending",
        }
    return {
        "status": "observe",
        "window": "next_logic_tick",
        "success_criteria": ["continue collecting evidence through the smart-loop register"],
        "execution_success": None,
        "proof_state": "",
    }


def _opportunity_audit(
    *,
    strategy: Mapping[str, Any],
    authority: Mapping[str, Any],
    candidate: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> Dict[str, Any]:
    candidate_present = _bool(candidate.get("present"))
    candidate_count = int(authority.get("candidate_count", 0) or 0)
    actionable_count = int(authority.get("actionable_count", 0) or 0)
    reason = _text(authority.get("reason") or strategy.get("reason_code") or strategy.get("reason"))
    blocked = _lower(budget.get("mode")) == "blocked"
    generation_gap = _bool(authority.get("candidate_generation_gap"))
    operationalization_gap = _bool(authority.get("candidate_operationalization_gap"))
    if blocked:
        status = "blocked"
        audit_reason = _text(budget.get("blocker") or budget.get("reason"))
    elif candidate_present or actionable_count > 0:
        status = "being_tested"
        audit_reason = "smart_loop_action_selected"
    elif generation_gap or operationalization_gap:
        status = "not_proven"
        audit_reason = reason or "smart_loop_gap"
    elif strategy:
        status = "covered"
        audit_reason = reason or _text(strategy.get("outcome") or "smart_loop_no_action")
    else:
        status = "not_proven"
        audit_reason = "smart_loop_strategy_missing"
    checks = [
        {
            "name": "smart_loop_strategy",
            "status": "available" if strategy else "missing",
            "detail": _text(strategy.get("outcome") or ""),
        },
        {
            "name": "smart_loop_candidate_projection",
            "status": "available" if candidate_count else "missing",
            "detail": f"candidates={candidate_count} actionable={actionable_count} reason={reason}",
        },
        {
            "name": "smart_loop_tsar_gate",
            "status": _text(_mapping(strategy.get("tsar_gate")).get("verdict") or "missing"),
            "detail": reason,
        },
        {
            "name": "active_control",
            "status": _lower(budget.get("mode")) or "unknown",
            "detail": _text(budget.get("blocker") or budget.get("reason")),
        },
        {
            "name": "candidate_action",
            "status": "available" if candidate_present else "not_selected",
            "detail": _text(candidate.get("lever") or candidate.get("reason")),
        },
    ]
    return {
        "status": status,
        "reason": audit_reason,
        "checks": checks,
        "nothing_left_on_table": {
            "status": status,
            "reason": audit_reason,
        },
    }


def build_tsar_autopilot_decision(
    *,
    vectors: Mapping[str, Any],
    trace: Optional[Mapping[str, Any]] = None,
    state_name: str = "",
    action: Optional[LeverAction] = None,
    action_result: Optional[LeverResult] = None,
    action_suppressed: bool = False,
    execution_outcome: Optional[Mapping[str, Any]] = None,
    bouncer: Optional[Mapping[str, Any]] = None,
    referee: Optional[Mapping[str, Any]] = None,
    decision_trace_id: str = "",
) -> Dict[str, Any]:
    """Build the TSAR projection from smart-loop-owned strategy only."""

    _ = bouncer, referee
    safe_vectors = _mapping(vectors)
    trace_map = _mapping(trace)
    execution = _mapping(execution_outcome)
    constraint = _mapping(safe_vectors.get("constraint_vector"))
    brain_tsar = _mapping(safe_vectors.get("brain_tsar"))
    strategy = _smart_loop_strategy(safe_vectors)
    authority = _smart_loop_authority(safe_vectors, strategy)
    candidate = _candidate_from_action(action=action, action_suppressed=action_suppressed)
    if not candidate:
        candidate = _candidate_from_trace(trace_map)
    intent, intent_reason = _derive_intent(
        candidate=candidate,
        strategy=strategy,
        authority=authority,
        constraint=constraint,
        brain_tsar=brain_tsar,
        trace=trace_map,
        action_suppressed=action_suppressed,
    )
    budget = _action_budget(
        constraint=constraint,
        brain_tsar=brain_tsar,
        candidate=candidate,
        action_suppressed=action_suppressed,
        intent_reason=intent_reason,
    )
    verification = _verification_plan(
        candidate=candidate,
        action_result=action_result,
        execution_outcome=execution,
        strategy=strategy,
        action_suppressed=action_suppressed,
    )
    audit = _opportunity_audit(
        strategy=strategy,
        authority=authority,
        candidate=candidate,
        budget=budget,
    )
    trace_id = _text(decision_trace_id or trace_map.get("decision_trace_id") or execution.get("decision_trace_id"))
    return {
        "schema": "tsar_autopilot_decision_v1",
        "owner": SMART_LOOP_OWNER,
        "state": _text(state_name or brain_tsar.get("state")),
        "decision_trace_id": trace_id,
        "tick_contract": {
            "external_tick": "live_modem_truth_read",
            "logic_phase_a": "smart_loop_register_absorbs_owner_inputs",
            "logic_phase_b": "smart_loop_closed_loop_selects_or_refuses",
            "action_phase": "command_authority_executes_brain_selected_action",
            "feedback_phase": "smart_loop_proof_referee_updates_roi",
        },
        "vectors_used": [
            "constraint_vector",
            "self_learning_strategy",
            "smart_loop_candidate_projection",
            "smart_loop_authority",
        ],
        "intent": intent,
        "intent_reason": intent_reason,
        "action_budget": budget,
        "candidate_action": dict(candidate),
        "verification_plan": verification,
        "opportunity_audit": audit,
    }


__all__ = ["build_tsar_autopilot_decision"]
