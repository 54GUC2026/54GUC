"""Canonical Brain learning-effect contract.

This module owns the public answer to one question:

What did Brain learn, diagnose, or fail to operationalize from a decision
window?

It does not choose actions, grade modem outcomes, or rebuild candidate
surfaces. It only summarizes already-owner-stamped surfaces into one stable
learning-effect payload for simulators, replay, and support tooling.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from arch4.brain.learning_effect_action_proof_owner import action_proof_summary
from arch4.brain.learning_effect_feedback_owner import feedback_rows, feedback_summary
from arch4.brain.feed_registers import (
    FEED_BRAIN_LEARNING_EFFECT,
    OWNER_BRAIN_LEARNING_EFFECT,
    require_brain_feed_register_bank,
)
from arch4.smart_loop.closed_loop import (
    CLOSURE_AUDIT_SCHEMA as SMART_LOOP_CLOSURE_AUDIT_SCHEMA,
    OWNER as SMART_LOOP_TSAR_OWNER,
)
from arch4.smart_loop.strategy_register import (
    TSAR_ARBITRATION_SCHEMA,
    TSAR_PLAN_SCHEMA,
)

SCHEMA = "brain_learning_effect.v1"
OWNER = "arch4.brain.learning_effect"
FEED = FEED_BRAIN_LEARNING_EFFECT
DEFAULT_STALE_AFTER_S = 30.0

class BrainLearningEffectContractError(ValueError):
    """Raised when learning-effect input bypasses an owner contract."""


class BrainLearningEffectRegisterUnavailable(ValueError):
    """Raised when the learning-effect register cannot be consumed safely."""


def build_brain_learning_effect(
    *,
    tsar_plan: Mapping[str, Any] | None = None,
    referee_feedback_quality: Mapping[str, Any] | None = None,
    referee_feedback_qualities: Iterable[Mapping[str, Any]] | None = None,
    decision_quality: Mapping[str, Any] | None = None,
    self_learning_candidate_surfaces: Iterable[Mapping[str, Any]] | None = None,
    action_proof_surface: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the single learning-effect surface from existing owner outputs."""

    plan = _mapping(tsar_plan)
    decision = _mapping(decision_quality)
    candidate_surfaces = [
        row for row in (self_learning_candidate_surfaces or ()) if isinstance(row, Mapping)
    ]
    feedback_items = feedback_rows(
        referee_feedback_quality=referee_feedback_quality,
        referee_feedback_qualities=referee_feedback_qualities,
    )
    feedback = feedback_summary(feedback_items)
    action_proof = action_proof_summary(
        candidate_surfaces,
        action_proof_surface=action_proof_surface,
    )
    arbitration = _tsar_arbitration_outcome(plan)
    selected = _mapping(plan.get("selected"))
    proof_chain = _proof_chain_audit(
        expected=_expected_selected_action_proof_identity(plan=plan, candidate_surfaces=candidate_surfaces),
        action_proof=action_proof,
    )
    closure_audit = _latest_smart_loop_closure_audit(candidate_surfaces)
    closure_links = _mapping(closure_audit.get("links"))
    closure_status = _text(closure_audit.get("status"))
    latest_surface_candidates = _latest_smart_loop_candidates(candidate_surfaces)
    considered_raw = plan.get("considered")
    deferred_raw = plan.get("deferred")
    considered = [item for item in considered_raw if isinstance(item, Mapping)] if isinstance(considered_raw, list) else []
    deferred = [item for item in deferred_raw if isinstance(item, Mapping)] if isinstance(deferred_raw, list) else []
    rows = [*considered, *deferred]
    if selected:
        rows.append(selected)
    elif latest_surface_candidates:
        rows.extend(latest_surface_candidates)
    selected_candidate = selected or _latest_smart_loop_decision_candidate(
        candidate_surfaces=candidate_surfaces,
        latest_candidates=latest_surface_candidates,
    )
    penalized = [row for row in rows if _candidate_roi_penalty(row) > 0.0]
    boosted = [row for row in rows if _candidate_roi_benefit(row) > 0.0]
    selected_penalty = _candidate_roi_penalty(selected_candidate)
    selected_benefit = _candidate_roi_benefit(selected_candidate)
    bucket = _text(decision.get("bucket"))
    should_have_acted = _bool(decision.get("should_have_acted"))
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "roi_memory_applied": bool(penalized or boosted),
        "penalized_candidate_count": len(penalized),
        "boosted_candidate_count": len(boosted),
        "referee_feedback_quality": feedback["quality"],
        "referee_feedback_quality_count": feedback["count"],
        "referee_feedback_low_information_count": feedback["low_information_count"],
        "referee_feedback_valid_no_action_count": feedback["valid_no_action_count"],
        "referee_feedback_counterfactual_diagnostic_count": feedback[
            "counterfactual_diagnostic_count"
        ],
        "referee_feedback_outcome_signal_count": feedback["outcome_signal_count"],
        "referee_learning_signal": feedback["learning_signal"],
        "referee_diagnostic_signal": feedback["diagnostic_signal"],
        "referee_feedback_reason": feedback["reason"],
        "action_proof_signal": bool(action_proof["recent_count"] > 0),
        "action_proof_recent_count": action_proof["recent_count"],
        "action_proof_good_count": action_proof["good_count"],
        "action_proof_bad_count": action_proof["bad_count"],
        "action_proof_memory_commit_count": action_proof["memory_commit_count"],
        "action_proof_historical_commit_count": action_proof["historical_commit_count"],
        "action_proof_crystal_ball_update_count": action_proof["crystal_ball_update_count"],
        "action_proof_crystal_ball_regrade_count": action_proof["crystal_ball_regrade_count"],
        "action_proof_secondary_learning_commit_count": action_proof["secondary_commit_count"],
        "last_action_proof_label": action_proof["last_label"],
        "last_action_proof_referee_code": action_proof["last_referee_code"],
        "last_action_proof_reason": action_proof["last_reason"],
        "last_action_proof_secondary_learning_status": action_proof["last_secondary_learning_status"],
        "last_action_proof_selected_action_command_id": action_proof["last_selected_action_command_id"],
        "last_action_proof_selected_action_proof_id": action_proof["last_selected_action_proof_id"],
        "last_action_proof_smart_loop_reason_ref": action_proof["last_smart_loop_reason_ref"],
        "last_action_proof_smart_loop_context_key": action_proof["last_smart_loop_context_key"],
        "last_action_proof_smart_loop_identity_source": action_proof["last_smart_loop_identity_source"],
        "expected_smart_loop_proof_source": proof_chain["expected_source"],
        "expected_selected_action_command_id": proof_chain["expected_command_id"],
        "expected_selected_action_proof_id": proof_chain["expected_proof_id"],
        "expected_smart_loop_reason_ref": proof_chain["expected_reason_ref"],
        "expected_smart_loop_context_key": proof_chain["expected_context_key"],
        "selected_action_proof_identity_status": proof_chain["status"],
        "selected_action_proof_identity_matched": proof_chain["matched"],
        "smart_loop_closure_status": closure_status,
        "smart_loop_closure_reason_code": _text(closure_audit.get("reason_code")),
        "smart_loop_closure_required_next_step": _text(closure_audit.get("required_next_step")),
        "smart_loop_closure_missing_links": _text_list(closure_audit.get("missing_links")),
        "smart_loop_closure_candidate_generated": _bool(closure_links.get("candidate_generated")),
        "smart_loop_closure_tsar_arbitrated": _bool(closure_links.get("tsar_arbitrated")),
        "smart_loop_closure_action_selected": _bool(closure_links.get("action_selected")),
        "smart_loop_closure_selected_action_materialized": _bool(
            closure_links.get("selected_action_materialized")
        ),
        "smart_loop_closure_proof_opened": _bool(closure_links.get("proof_opened")),
        "smart_loop_closure_proof_closed": _bool(closure_links.get("proof_closed")),
        "smart_loop_closure_memory_admitted": _bool(closure_audit.get("memory_admitted")),
        "smart_loop_closure_prediction_error_recorded": _bool(
            closure_audit.get("prediction_error_recorded")
        ),
        "smart_loop_closure_policy_update_applied": _bool(
            closure_audit.get("policy_update_applied")
        ),
        "smart_loop_closure_future_policy_effect_status": _text(
            closure_audit.get("future_policy_effect_status")
        ),
        "selected_candidate_penalized": bool(selected_penalty > 0.0),
        "selected_candidate_boosted": bool(selected_benefit > 0.0),
        "selected_avoided_penalized_candidate": bool(selected_candidate and selected_penalty <= 0.0 and penalized),
        "selected_action": _text(selected_candidate.get("action")),
        "selected_lever": _text(selected_candidate.get("lever")),
        "selected_target_shape": _mapping(
            selected_candidate.get("target_shape")
            or selected_candidate.get("params")
        ),
        "selected_band": _int(selected_candidate.get("band") or selected_candidate.get("target_band"), 0),
        "selected_reason_ref": _text(selected_candidate.get("reason_ref")),
        "tsar_arbitration_verdict": _text(arbitration.get("verdict")),
        "tsar_arbitration_reason": _text(arbitration.get("reason")),
        "tsar_arbitration_reason_code": _text(arbitration.get("reason_code")),
        "tsar_context_ref": _text(arbitration.get("context_ref")),
        "tsar_arbitration_required_next_step": _text(arbitration.get("required_next_step")),
        "tsar_no_action_reason": _text(arbitration.get("no_action_reason")),
        "tsar_deferred_reason_codes": _text_list(arbitration.get("deferred_reason_codes")),
        "decision_quality_bucket": bucket,
        "decision_quality_reason": _text(decision.get("decision_quality_reason")),
        "brain_acted": _int(decision.get("action_count"), 0) > 0,
        "brain_should_have_acted": should_have_acted,
        "candidate_generation_gap_detected": _int(decision.get("missed_candidate_audit_count"), 0) > 0
        or _text(decision.get("should_have_acted_reason")) == "candidate_generation_failed",
        "candidate_operationalization_gap_detected": _int(
            decision.get("candidate_operationalization_gap_count"), 0
        )
        > 0
        or _text(decision.get("should_have_acted_reason")) == "candidate_not_operationalized",
        "candidate_generation_gap_reasons": _text_list(decision.get("missed_candidate_reasons")),
        "candidate_operationalization_gap_reasons": _text_list(
            decision.get("candidate_operationalization_gap_reasons")
        ),
        "counterfactual_problem_reasons": _text_list(decision.get("counterfactual_problem_reasons")),
        "counterfactual_missing_evidence": _text_list(decision.get("counterfactual_missing_evidence")),
        "counterfactual_governor_blockers": _text_list(decision.get("counterfactual_governor_blockers")),
        "learning_signal": bool(
            feedback["learning_signal"]
            or action_proof["recent_count"] > 0
            or closure_status
            in {
                "proof_pending",
                "closed_policy_update_applied_effect_pending",
                "closed_policy_effect_observed",
                "closed_reward_admitted_policy_not_applicable",
            }
            or penalized
            or boosted
            or bucket
            in {
                "brain_acted_justified",
                "brain_acted_unjustified",
                "brain_did_not_act_should_have",
                "brain_did_not_act_justified",
            }
        ),
        "diagnostic_signal": bool(
            feedback["diagnostic_signal"]
            or action_proof["bad_count"] > 0
            or closure_status
            in {
                "candidate_generation_gap",
                "action_rejected",
                "proof_invalid",
                "proof_closed_not_learned",
            }
            or should_have_acted
            or bucket in {"brain_acted_unjustified", "counterfactual_audit_missing"}
        ),
    }


def publish_brain_learning_effect_register(
    engine: Any,
    *,
    learning_effect: Mapping[str, Any],
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    """Publish the canonical Brain learning-effect surface into the feed bank."""

    payload = _require_learning_effect_publication(learning_effect)
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED,
        owner=OWNER_BRAIN_LEARNING_EFFECT,
        payload=payload,
        source="brain_learning_effect",
        updated_at=now_ts,
        valid=True,
        stale_after_s=max(0.0, float(stale_after_s or 0.0)),
    )
    return {"published": True, "feed": FEED, "owner": OWNER, "schema": SCHEMA}


def read_brain_learning_effect_register(
    engine: Any,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Read the canonical Brain learning-effect register."""

    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    row = bank.read_feeder(FEED).as_dict(now=now_ts)
    if str(row.get("error") or ""):
        raise BrainLearningEffectRegisterUnavailable(str(row.get("error") or "missing"))
    if not bool(row.get("valid", False)):
        raise BrainLearningEffectRegisterUnavailable("brain_learning_effect_register_invalid")
    if bool(row.get("stale", False)) and not bool(allow_stale):
        raise BrainLearningEffectRegisterUnavailable("brain_learning_effect_register_stale")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return _require_learning_effect_publication(payload)


def _require_learning_effect_publication(effect: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(effect or {}) if isinstance(effect, Mapping) else {}
    if _text(row.get("schema")) != SCHEMA:
        raise BrainLearningEffectContractError("brain_learning_effect_schema_mismatch")
    if _text(row.get("owner")) != OWNER:
        raise BrainLearningEffectContractError("brain_learning_effect_owner_mismatch")
    return row


def _tsar_arbitration_outcome(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    if not plan:
        return {}
    plan_owner = _text(plan.get("owner"))
    if _text(plan.get("schema")) != TSAR_PLAN_SCHEMA or plan_owner != SMART_LOOP_TSAR_OWNER:
        raise BrainLearningEffectContractError("tsar_plan_owner_contract_missing")
    arbitration = _mapping(plan.get("arbitration_outcome"))
    if not arbitration:
        raise BrainLearningEffectContractError("tsar_arbitration_outcome_missing")
    if (
        _text(arbitration.get("schema")) != TSAR_ARBITRATION_SCHEMA
        or _text(arbitration.get("owner")) != plan_owner
    ):
        raise BrainLearningEffectContractError("tsar_arbitration_outcome_owner_mismatch")
    if not _text(arbitration.get("verdict")) or not _text(arbitration.get("reason_code")):
        raise BrainLearningEffectContractError("tsar_arbitration_outcome_incomplete")
    if not _text(arbitration.get("context_ref")):
        raise BrainLearningEffectContractError("tsar_arbitration_context_ref_missing")
    return arbitration


def _expected_selected_action_proof_identity(
    *,
    plan: Mapping[str, Any],
    candidate_surfaces: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    selected = _mapping(plan.get("selected"))
    selected_proof_id = _text(selected.get("proof_id"))
    if selected_proof_id:
        return {
            "source": "selected_tsar_plan",
            "command_id": _text(selected.get("command_id")),
            "proof_id": selected_proof_id,
            "reason_ref": _text(selected.get("reason_ref")),
            "context_key": _text(selected.get("context_key")),
        }
    for surface in reversed(list(candidate_surfaces)):
        if _text(surface.get("schema")) != "smart_loop.closed_loop.v1":
            continue
        if _text(surface.get("owner")) != SMART_LOOP_TSAR_OWNER:
            raise BrainLearningEffectContractError("smart_loop_closed_proof_owner_mismatch")
        closed = _mapping(surface.get("closed_proof"))
        proof_id = _text(closed.get("proof_id"))
        if not proof_id:
            continue
        return {
            "source": "closed_proof",
            "command_id": _text(closed.get("command_id")),
            "proof_id": proof_id,
            "reason_ref": _text(closed.get("reason_ref")),
            "context_key": _text(closed.get("context_key")),
        }
    return {"source": "", "command_id": "", "proof_id": "", "reason_ref": "", "context_key": ""}


def _latest_smart_loop_closure_audit(
    candidate_surfaces: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    for surface in reversed(list(candidate_surfaces)):
        if _text(surface.get("schema")) != "smart_loop.closed_loop.v1":
            continue
        if _text(surface.get("owner")) != SMART_LOOP_TSAR_OWNER:
            raise BrainLearningEffectContractError("smart_loop_closure_surface_owner_mismatch")
        audit = _mapping(surface.get("closure_audit"))
        if not audit:
            continue
        if (
            _text(audit.get("schema")) != SMART_LOOP_CLOSURE_AUDIT_SCHEMA
            or _text(audit.get("owner")) != SMART_LOOP_TSAR_OWNER
        ):
            raise BrainLearningEffectContractError("smart_loop_closure_audit_owner_mismatch")
        return audit
    return {}


def _latest_smart_loop_candidates(
    candidate_surfaces: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    for surface in reversed(list(candidate_surfaces)):
        if _text(surface.get("schema")) != "smart_loop.closed_loop.v1":
            continue
        if _text(surface.get("owner")) != SMART_LOOP_TSAR_OWNER:
            raise BrainLearningEffectContractError("smart_loop_candidate_surface_owner_mismatch")
        candidates = surface.get("candidates")
        if isinstance(candidates, list):
            return [row for row in candidates if isinstance(row, Mapping)]
    return []


def _latest_smart_loop_decision_candidate(
    *,
    candidate_surfaces: Iterable[Mapping[str, Any]],
    latest_candidates: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    candidates = list(latest_candidates)
    if not candidates:
        return {}
    for surface in reversed(list(candidate_surfaces)):
        if _text(surface.get("schema")) != "smart_loop.closed_loop.v1":
            continue
        if _text(surface.get("owner")) != SMART_LOOP_TSAR_OWNER:
            raise BrainLearningEffectContractError("smart_loop_candidate_surface_owner_mismatch")
        if _text(surface.get("outcome")) not in {"no_action_justified", "action_rejected"}:
            return {}
        decision = _mapping(surface.get("bandit_decision"))
        reason_ref = _text(decision.get("reason_ref"))
        if not reason_ref:
            return {}
        for row in candidates:
            if _text(row.get("reason_ref")) == reason_ref:
                return row
        break
    return {}


def _proof_chain_audit(
    *,
    expected: Mapping[str, Any],
    action_proof: Mapping[str, Any],
) -> dict[str, Any]:
    expected_source = _text(expected.get("source"))
    expected_command_id = _text(expected.get("command_id"))
    expected_proof_id = _text(expected.get("proof_id"))
    expected_reason_ref = _text(expected.get("reason_ref"))
    expected_context_key = _text(expected.get("context_key"))
    actual_command_id = _text(action_proof.get("last_selected_action_command_id"))
    actual_proof_id = _text(action_proof.get("last_selected_action_proof_id"))
    actual_context_key = _text(action_proof.get("last_smart_loop_context_key"))
    actual_source = _text(action_proof.get("last_smart_loop_identity_source"))
    if expected_proof_id and actual_proof_id:
        matched = (
            expected_proof_id == actual_proof_id
            and (not expected_command_id or expected_command_id == actual_command_id)
            and (not expected_context_key or expected_context_key == actual_context_key)
            and actual_source == "action_proof_register"
        )
        if matched:
            status = "matched"
        elif (
            expected_source == "closed_proof"
            and expected_proof_id == actual_proof_id
            and actual_source != "action_proof_register"
        ):
            status = "closed_proof_missing_action_proof"
        else:
            status = "mismatch"
    elif expected_proof_id:
        matched = False
        status = (
            "closed_proof_missing_action_proof"
            if expected_source == "closed_proof"
            else "waiting_for_action_proof"
        )
    elif actual_proof_id:
        matched = False
        status = "action_proof_without_current_strategy_identity"
    else:
        matched = False
        status = "no_smart_loop_proof"
    return {
        "expected_source": expected_source,
        "expected_command_id": expected_command_id,
        "expected_proof_id": expected_proof_id,
        "expected_reason_ref": expected_reason_ref,
        "expected_context_key": expected_context_key,
        "status": status,
        "matched": matched,
    }


def _iter_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            text = _text(item)
            if text:
                yield text


def _unique_text(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in out:
            out.append(text)
    return out


def _text_list(value: Any) -> list[str]:
    return _unique_text(_iter_text_values(value))


def _candidate_roi_penalty(candidate: Mapping[str, Any]) -> float:
    row = _mapping(candidate)
    return max(
        _float(row.get("action_shape_roi_penalty"), 0.0),
        _float(row.get("action_shape_delta_roi_penalty"), 0.0),
    )


def _candidate_roi_benefit(candidate: Mapping[str, Any]) -> float:
    row = _mapping(candidate)
    return max(
        _float(row.get("action_shape_roi_benefit"), 0.0),
        _float(row.get("action_shape_delta_roi_benefit"), 0.0),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "BrainLearningEffectContractError",
    "BrainLearningEffectRegisterUnavailable",
    "FEED",
    "OWNER",
    "SCHEMA",
    "build_brain_learning_effect",
    "publish_brain_learning_effect_register",
    "read_brain_learning_effect_register",
]
