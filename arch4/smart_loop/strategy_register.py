"""Live strategy register for the clean smart loop."""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.brain.feed_registers import (
    BrainFeedRegisterBank,
    FEED_SELF_LEARNING_STRATEGY,
    OWNER_SELF_LEARNING_STRATEGY,
    require_brain_feed_register_bank,
)
from arch4.smart_loop.closed_loop import OWNER, SCHEMA


DEFAULT_STALE_AFTER_S = 10.0
TSAR_PLAN_SCHEMA = "brain_tsar_candidate_plan.v1"
TSAR_ARBITRATION_SCHEMA = "brain_tsar_arbitration_outcome.v1"


class SmartLoopStrategyUnavailable(RuntimeError):
    """Raised when the clean smart-loop strategy feed is missing or invalid."""


def publish_smart_loop_strategy_register(
    engine: Any,
    *,
    publication: Mapping[str, Any],
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    return publish_smart_loop_strategy_to_bank(
        require_brain_feed_register_bank(engine),
        publication=publication,
        now=now,
        stale_after_s=stale_after_s,
    )


def publish_smart_loop_strategy_to_bank(
    bank: BrainFeedRegisterBank,
    *,
    publication: Mapping[str, Any],
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    payload = require_smart_loop_strategy(publication)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED_SELF_LEARNING_STRATEGY,
        owner=OWNER_SELF_LEARNING_STRATEGY,
        payload=payload,
        source="smart_loop_closed_loop",
        updated_at=now_ts,
        valid=True,
        stale_after_s=max(0.0, float(stale_after_s or 0.0)),
    )
    return {
        "published": True,
        "feed": FEED_SELF_LEARNING_STRATEGY,
        "owner": OWNER_SELF_LEARNING_STRATEGY,
        "schema": SCHEMA,
    }


def touch_smart_loop_strategy_register(
    engine: Any,
    *,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    try:
        bank.touch_feeder(
            FEED_SELF_LEARNING_STRATEGY,
            owner=OWNER_SELF_LEARNING_STRATEGY,
            source="smart_loop_closed_loop",
            updated_at=now_ts,
            valid=True,
            stale_after_s=max(0.0, float(stale_after_s or 0.0)),
        )
    except Exception as exc:
        return {"touched": False, "reason": str(exc or "smart_loop_strategy_touch_failed")}
    return {
        "touched": True,
        "feed": FEED_SELF_LEARNING_STRATEGY,
        "owner": OWNER_SELF_LEARNING_STRATEGY,
        "schema": SCHEMA,
    }


def read_smart_loop_strategy_register(
    engine: Any,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    return read_smart_loop_strategy_from_bank(
        require_brain_feed_register_bank(engine),
        allow_stale=allow_stale,
        now=now,
    )


def read_smart_loop_strategy_from_bank(
    bank: BrainFeedRegisterBank,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    row = bank.read_feeder(FEED_SELF_LEARNING_STRATEGY).as_dict(now=now_ts)
    if str(row.get("error") or ""):
        raise SmartLoopStrategyUnavailable(str(row.get("error") or "missing"))
    if not bool(row.get("valid", False)):
        raise SmartLoopStrategyUnavailable("smart_loop_strategy_register_invalid")
    if bool(row.get("stale", False)) and not bool(allow_stale):
        raise SmartLoopStrategyUnavailable("smart_loop_strategy_register_stale")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return require_smart_loop_strategy(payload)


def smart_loop_strategy_fault(
    *,
    reason: str,
    required_next_step: str = "publish_smart_loop_strategy_register",
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "outcome": "fault",
        "reason": str(reason or "smart_loop_strategy_unavailable"),
        "reason_code": str(reason or "smart_loop_strategy_unavailable"),
        "required_next_step": str(required_next_step or "publish_smart_loop_strategy_register"),
        "learning_loop_closed": False,
        "learning_signal": False,
        "diagnostic_signal": True,
        "candidate_counts": {
            "total": 0,
            "operational": 0,
            "actionable": 0,
            "advisory": 0,
            "blocked": 0,
        },
        "candidates": [],
        "selected": None,
        "tsar_gate": {
            "verdict": "refused",
            "reason_code": str(reason or "smart_loop_strategy_unavailable"),
            "required_next_step": str(required_next_step or "publish_smart_loop_strategy_register"),
        },
        "inputs": {
            "tsar": {
                "verdict": "refused",
                "reason_code": str(reason or "smart_loop_strategy_unavailable"),
                "required_next_step": str(required_next_step or "publish_smart_loop_strategy_register"),
            },
            "self_learning": {
                "status": "fault",
                "reason": str(reason or "smart_loop_strategy_unavailable"),
                "required_next_step": str(required_next_step or "publish_smart_loop_strategy_register"),
            },
        },
        "opened_proof": None,
        "closed_proof": None,
        "state": {"roi": {}, "pending_proof": None},
    }


def publish_smart_loop_strategy_action_block(
    engine: Any,
    *,
    current_strategy: Mapping[str, Any],
    reason_code: str,
    reason: str,
    required_next_step: str,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    return publish_smart_loop_strategy_action_block_to_bank(
        require_brain_feed_register_bank(engine),
        current_strategy=current_strategy,
        reason_code=reason_code,
        reason=reason,
        required_next_step=required_next_step,
        now=now,
        stale_after_s=stale_after_s,
    )


def publish_smart_loop_strategy_action_block_to_bank(
    bank: BrainFeedRegisterBank,
    *,
    current_strategy: Mapping[str, Any],
    reason_code: str,
    reason: str,
    required_next_step: str,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    """Publish the canonical smart-loop state when execution is blocked.

    This is the explicit adapter between Action Availability and the
    self-learning strategy feed. A blocked selected action is not proof-pending and
    must not keep an opened proof in public state.
    """

    row = require_smart_loop_strategy(current_strategy)
    code = str(reason_code or "action_availability_blocked")
    text = str(reason or code)
    next_step = str(required_next_step or _required_next_step_for_block(code))
    candidates = [_blocked_candidate(candidate, code) for candidate in list(row.get("candidates") or [])]
    total = len(candidates)
    previous_inputs = _mapping(row.get("inputs"))
    inputs = dict(previous_inputs)
    inputs["action_availability"] = {
        "schema": "smart_loop.action_availability.v1",
        "owner": "arch4.smart_loop.action_availability",
        "verdict": "refused",
        "reason_code": code,
        "reason": text,
        "required_next_step": next_step,
    }
    state = _mapping(row.get("state"))
    if state:
        state = dict(state)
        state["pending_proof"] = None
    else:
        state = {"roi": {}, "pending_proof": None}
    publication = {
        **row,
        "outcome": "action_rejected",
        "reason": text,
        "reason_code": code,
        "required_next_step": next_step,
        "learning_loop_closed": False,
        "learning_signal": False,
        "diagnostic_signal": True,
        "candidate_counts": {
            "total": total,
            "operational": 0,
            "actionable": 0,
            "advisory": 0,
            "blocked": total,
        },
        "candidates": candidates,
        "selected": None,
        "tsar_gate": {
            "verdict": "refused",
            "reason_code": code,
            "required_next_step": next_step,
        },
        "inputs": inputs,
        "opened_proof": None,
        "closed_proof": None,
        "state": state,
    }
    return publish_smart_loop_strategy_to_bank(
        bank,
        publication=publication,
        now=now,
        stale_after_s=stale_after_s,
    )


def strategy_candidate_projection(strategy: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(strategy)
    evidence = _mapping(row.get("evidence"))
    action_family_roi = _mapping(evidence.get("action_family_roi_summary"))
    if not action_family_roi:
        action_family_roi = _mapping(evidence.get("action_family_roi"))
    return {
        "schema": "smart_loop.candidate_projection.v1",
        "owner": OWNER,
        "status": str(row.get("outcome") or ""),
        "reason": str(row.get("reason_code") or row.get("reason") or ""),
        "required_next_step": str(row.get("required_next_step") or ""),
        "candidate_actions": list(row.get("candidates") or []),
        "learning_quality": _mapping(evidence.get("learning_quality")),
        "action_family_roi": action_family_roi,
        "authority": strategy_authority_projection(row),
    }


def strategy_authority_projection(strategy: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(strategy)
    counts = _mapping(row.get("candidate_counts"))
    outcome = str(row.get("outcome") or "")
    operationalization_gap = _candidate_operationalization_gap(row)
    return {
        "schema": "smart_loop.authority_projection.v1",
        "owner": OWNER,
        "status": outcome,
        "reason": str(row.get("reason_code") or row.get("reason") or ""),
        "required_next_step": str(row.get("required_next_step") or ""),
        "candidate_generation_gap": outcome == "candidate_missing_counterfactual",
        "candidate_operationalization_gap": operationalization_gap,
        "candidate_count": int(counts.get("total", 0) or 0),
        "actionable_count": int(counts.get("actionable", 0) or 0),
        "roi_surface": dict(row.get("evidence") or {}),
    }


def strategy_tsar_plan_projection(strategy: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(strategy)
    gate = _mapping(row.get("tsar_gate"))
    selected = _mapping(row.get("selected"))
    outcome = str(row.get("outcome") or "")
    reason_code = str(
        gate.get("reason_code")
        or row.get("reason_code")
        or row.get("reason")
        or outcome
        or "smart_loop_strategy_missing"
    )
    verdict = str(gate.get("verdict") or "")
    if not verdict:
        verdict = "act" if selected else "refused" if outcome == "action_rejected" else "no_action"
    required_next_step = str(gate.get("required_next_step") or row.get("required_next_step") or "")
    context_ref = str(
        selected.get("context_key")
        or _mapping(row.get("opened_proof")).get("context_key")
        or _mapping(_mapping(row.get("state")).get("pending_proof")).get("context_key")
        or f"smart_loop:{row.get('outcome', '')}"
    )
    selected_target_bands = selected.get("target_bands") if isinstance(selected.get("target_bands"), list) else []
    if not selected_target_bands and selected.get("target_band"):
        selected_target_bands = [selected.get("target_band", 0)]
    selected_plan = (
        {
            "action": selected.get("action", ""),
            "band": selected.get("target_band", 0),
            "target_bands": list(selected_target_bands),
            "reason_ref": selected.get("reason_ref", ""),
            "context_key": selected.get("context_key", ""),
            "reason": selected.get("reason", ""),
        }
        if selected
        else {}
    )
    if selected_plan:
        for key, value in selected.items():
            if key not in selected_plan and key not in {"target_band"}:
                selected_plan[key] = value
    projection_reason = str(selected.get("reason_code") or row.get("reason_code") or row.get("reason") or "")
    return {
        "schema": TSAR_PLAN_SCHEMA,
        "owner": OWNER,
        "status": "selected" if selected_plan else "none",
        "reason": projection_reason,
        "context_ref": context_ref,
        "selected": selected_plan,
        "considered": list(row.get("candidates") or []),
        "deferred": [],
        "arbitration_outcome": {
            "schema": TSAR_ARBITRATION_SCHEMA,
            "owner": OWNER,
            "verdict": verdict,
            "reason_code": reason_code,
            "reason": reason_code,
            "required_next_step": required_next_step,
            "no_action_reason": reason_code if verdict in {"no_action", "refused"} else "",
            "deferred_reason_codes": [],
            "context_ref": context_ref,
        },
    }


def require_smart_loop_strategy(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload or {})
    if str(row.get("schema") or "") != SCHEMA:
        raise SmartLoopStrategyUnavailable("smart_loop_strategy_schema_mismatch")
    if str(row.get("owner") or "") != OWNER:
        raise SmartLoopStrategyUnavailable("smart_loop_strategy_owner_mismatch")
    if not str(row.get("outcome") or ""):
        raise SmartLoopStrategyUnavailable("smart_loop_strategy_outcome_missing")
    return row


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _required_next_step_for_block(reason_code: str) -> str:
    code = str(reason_code or "")
    if code in {"radio_stability_hold", "action_cooldown", "objective_feedback"}:
        return "wait_for_cooldown"
    if code in {"health_hold", "incident_hold"}:
        return "wait_for_safety_release"
    return "wait_for_action_availability"


_JUSTIFIED_ACTION_REJECTION_REASONS = {
    "advisory_avoid_band_recorded",
    "already_on_candidate_configuration",
    "band_authority_unavailable",
    "candidate_not_executable",
    "cooldown_active",
    "current_anchor_already_best",
    "current_anchor_penalized_no_better_candidate",
    "current_anchor_underperforms_no_better_candidate",
    "learning_confidence_low",
    "learning_evidence_low",
    "radio_stability_hold",
    "mutation_cooldown_active",
    "profile_rf_control_manual_only",
    "proof_pending",
    "routeros_rf_control_best_effort_manual_only",
    "target_band_outside_authority",
    "target_pool_outside_authority",
    "user_traffic_active",
}

_JUSTIFIED_REQUIRED_NEXT_STEPS = {
    "collect_more_samples",
    "generate_executable_candidate",
    "generate_better_executable_candidate",
    "keep_observing",
    "update_modem_profile_authority",
    "wait_for_cooldown",
    "wait_for_radio_authority",
    "wait_for_referee_window",
    "wait_for_safety_release",
}


def _candidate_operationalization_gap(row: Mapping[str, Any]) -> bool:
    outcome = str(row.get("outcome") or "")
    if outcome == "candidate_present_but_not_operational":
        return True
    if outcome != "action_rejected":
        return False
    reason = str(row.get("reason_code") or row.get("reason") or "")
    if reason.startswith("safety_block:"):
        return False
    if reason in _JUSTIFIED_ACTION_REJECTION_REASONS:
        return False
    if str(row.get("required_next_step") or "") in _JUSTIFIED_REQUIRED_NEXT_STEPS:
        return False
    candidates = [_mapping(candidate) for candidate in list(row.get("candidates") or [])]
    concrete_candidates = [candidate for candidate in candidates if candidate]
    if concrete_candidates and all(_candidate_is_non_operational(candidate) for candidate in concrete_candidates):
        return False
    counts = _mapping(row.get("candidate_counts"))
    return int(counts.get("operational", 0) or 0) > 0 or int(counts.get("actionable", 0) or 0) > 0


def _candidate_is_non_operational(candidate: Mapping[str, Any]) -> bool:
    candidate_class = str(candidate.get("candidate_class") or "")
    action = str(candidate.get("action") or "")
    if candidate_class in {"advisory", "blocked"}:
        return True
    if not bool(candidate.get("direct_action_allowed", False)) and not bool(candidate.get("executable", False)):
        return True
    if action == "avoid_band":
        return True
    return False


def _blocked_candidate(candidate: Any, reason_code: str) -> dict[str, Any]:
    row = _mapping(candidate)
    if not row:
        return {}
    row["candidate_class"] = "blocked"
    row["direct_action_allowed"] = False
    row["direct_action_block_reason"] = str(reason_code or "action_availability_blocked")
    row["executable"] = False
    return row


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "TSAR_ARBITRATION_SCHEMA",
    "TSAR_PLAN_SCHEMA",
    "SmartLoopStrategyUnavailable",
    "publish_smart_loop_strategy_action_block",
    "publish_smart_loop_strategy_register",
    "read_smart_loop_strategy_register",
    "require_smart_loop_strategy",
    "smart_loop_strategy_fault",
    "touch_smart_loop_strategy_register",
    "strategy_authority_projection",
    "strategy_candidate_projection",
    "strategy_tsar_plan_projection",
]
