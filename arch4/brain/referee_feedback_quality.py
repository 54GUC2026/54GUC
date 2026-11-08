"""Canonical Referee feedback-quality classifier.

This module owns one narrow question:

Did Referee feedback carry useful learning signal, or was it low-information
passive/no-action noise?

It does not grade actions and it does not change action policy. Referee owns
grades; self-learning/replay consume this normalized quality surface.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "brain_referee_feedback_quality.v1"
OWNER = "arch4.brain.referee_feedback_quality"


def classify_referee_feedback_quality(
    referee: Mapping[str, Any] | None = None,
    *,
    counterfactual_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify normalized Referee feedback into learning-signal quality.

    No-action feedback is only low-information when there is no counterfactual
    proof.  When the counterfactual audit says no action was needed or says a
    candidate was missed, that is learning signal owned by this contract.
    """

    source = _mapping(referee)
    audit = _mapping(counterfactual_audit)
    level = _lower(source.get("level"))
    code = _lower(source.get("code"))
    grade = _lower(source.get("grade") or source.get("outcome_grade"))
    learning_update = _lower(source.get("learning_update"))
    audit_status = _lower(audit.get("status"))
    audit_reason = _text(audit.get("reason"))
    candidate_generation_failed = _bool(audit.get("candidate_generation_failed")) or audit_status == (
        "needs_action_candidate"
    )
    should_have_candidate = _bool(audit.get("should_have_candidate"))
    if not level and not code and not grade:
        quality = "missing"
        learning_signal = False
        diagnostic_signal = False
        reason = "referee_feedback_missing"
    elif code in {"no_action", "no_action_valid"} or grade in {"none"}:
        if candidate_generation_failed:
            quality = "counterfactual_diagnostic"
            learning_signal = True
            diagnostic_signal = True
            reason = "referee_no_action_candidate_generation_gap"
        elif audit_status == "ok" and not should_have_candidate:
            quality = "valid_no_action"
            learning_signal = True
            diagnostic_signal = True
            reason = "counterfactual_proved_no_action_needed"
        elif audit_status == "blocked_by_governor":
            quality = "governor_blocked_no_action"
            learning_signal = True
            diagnostic_signal = True
            reason = "counterfactual_proved_no_action_governor_blocked"
        else:
            quality = "low_information"
            learning_signal = False
            diagnostic_signal = False
            reason = "referee_no_action_feedback"
    elif level in {"good", "bad", "neutral"} or grade in {"accepted", "rejected", "neutral"}:
        quality = "outcome_signal"
        learning_signal = True
        diagnostic_signal = False
        reason = "referee_outcome_grade_available"
    elif level == "blocked" or learning_update == "suppressed":
        quality = "blocked"
        learning_signal = False
        diagnostic_signal = True
        reason = "referee_learning_update_blocked"
    elif level in {"deferred", "pending"} or grade in {"deferred", "pending"}:
        quality = "deferred"
        learning_signal = False
        diagnostic_signal = False
        reason = "referee_feedback_deferred"
    else:
        quality = "weak"
        learning_signal = False
        diagnostic_signal = False
        reason = "referee_feedback_not_actionable"
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "quality": quality,
        "learning_signal": bool(learning_signal),
        "diagnostic_signal": bool(diagnostic_signal),
        "reason": reason,
        "counterfactual_status": audit_status,
        "counterfactual_reason": audit_reason,
        "counterfactual_should_have_candidate": bool(should_have_candidate),
        "counterfactual_candidate_generation_failed": bool(candidate_generation_failed),
    }


def referee_feedback_quality_from_surfaces(
    *,
    self_learning_authority: Mapping[str, Any] | None = None,
    brain_referee: Mapping[str, Any] | None = None,
    referee_outcome: Mapping[str, Any] | None = None,
    counterfactual_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read feedback quality from known public surfaces using the same owner."""

    authority = _mapping(self_learning_authority)
    quality = authority.get("referee_feedback_quality")
    if isinstance(quality, Mapping) and _text(quality.get("quality")):
        return _normalize_existing_quality(quality)

    public_referee = _mapping(brain_referee)
    if public_referee:
        grade = _text(public_referee.get("grade"))
        verdict = _mapping(public_referee.get("verdict"))
        return classify_referee_feedback_quality(
            {
                "level": verdict.get("level"),
                "code": verdict.get("code"),
                "grade": grade,
                "learning_update": public_referee.get("learning_update"),
            },
            counterfactual_audit=counterfactual_audit,
        )

    raw_referee = _mapping(referee_outcome)
    if raw_referee:
        return classify_referee_feedback_quality(raw_referee, counterfactual_audit=counterfactual_audit)
    return {}


def feedback_kind_from_learning_quality(learning_quality: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify aggregate referee feedback rates from the learning quality profile."""
    profile = _mapping(learning_quality)
    authority_observe_rate = _float(profile.get("recent_authority_observe_monitor_rate"))
    observe_rate = _float(profile.get("recent_observe_monitor_rate"))
    rejected_rate = _float(profile.get("recent_rejected_rate"))
    if authority_observe_rate >= 0.2:
        kind = "authority_observe_monitor"
        summary = "Learning sees repeated authority-side observe/monitor fallback."
    elif observe_rate >= 0.2:
        kind = "observe_monitor"
        summary = "Learning sees repeated observe/monitor fallback."
    elif rejected_rate >= 0.2:
        kind = "rejected"
        summary = "Learning sees repeated rejected outcomes."
    else:
        kind = ""
        summary = ""
    return {
        "schema": "brain_referee_feedback_profile_kind.v1",
        "owner": OWNER,
        "kind": kind,
        "summary": summary,
        "recent_authority_observe_monitor_rate": authority_observe_rate,
        "recent_observe_monitor_rate": observe_rate,
        "recent_rejected_rate": rejected_rate,
    }


def _normalize_existing_quality(value: Mapping[str, Any]) -> dict[str, Any]:
    quality = _text(value.get("quality"))
    if not quality:
        return {}
    return {
        "schema": _text(value.get("schema")) or SCHEMA,
        "owner": _text(value.get("owner")) or OWNER,
        "quality": quality,
        "learning_signal": _bool(value.get("learning_signal")),
        "diagnostic_signal": _bool(value.get("diagnostic_signal")),
        "reason": _text(value.get("reason")),
        "counterfactual_status": _text(value.get("counterfactual_status")),
        "counterfactual_reason": _text(value.get("counterfactual_reason")),
        "counterfactual_should_have_candidate": _bool(value.get("counterfactual_should_have_candidate")),
        "counterfactual_candidate_generation_failed": _bool(
            value.get("counterfactual_candidate_generation_failed")
        ),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _lower(value)
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "OWNER",
    "SCHEMA",
    "classify_referee_feedback_quality",
    "feedback_kind_from_learning_quality",
    "referee_feedback_quality_from_surfaces",
]
