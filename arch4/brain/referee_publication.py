"""Brain-owned public Referee publication.

RefereeBrain owns strategic grading. This module owns the public
``brain_referee`` surface consumed by GUI, logs, reports, and autopilot.
Runtime assembly modules may adapt inputs into this owner, but they must not
reimplement the state/grade/publication contract.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.action_origin import is_human_operator_origin
from arch4.coerce import safe_bool
from arch4.operator_projection import build_operator_projection


SCHEMA = "brain_referee_publication.v1"
OWNER = "arch4.brain.referee_publication"
FEED = "referee.publication"


class BrainRefereePublicationUnavailable(ValueError):
    """Raised when a consumer cannot read the canonical Referee publication."""


def build_brain_referee_publication(
    *,
    execution_outcome: Mapping[str, Any] | None,
    referee_outcome: Mapping[str, Any] | None = None,
    brain_bouncer: Mapping[str, Any] | None = None,
    canonical_action_outcome: Mapping[str, Any] | None = None,
    canonical_incident_state: Mapping[str, Any] | None = None,
    canonical_operator_projection: Mapping[str, Any] | None = None,
    decision_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only legal public ``brain_referee`` publication."""

    outcome = _mapping(execution_outcome)
    verdict = _mapping(referee_outcome)
    bouncer = _mapping(brain_bouncer)
    canonical_action = _mapping(canonical_action_outcome)
    canonical_incident = _mapping(canonical_incident_state)
    canonical_projection = (
        _mapping(canonical_operator_projection)
        if _mapping(canonical_operator_projection)
        else build_operator_projection(
            action_outcome=canonical_action,
            incident_state=canonical_incident,
        )
    )
    trace = _mapping(decision_trace)

    lever = str(outcome.get("action_lever", "") or "").strip()
    no_action = not lever or lever == "none"
    action_suppressed = safe_bool(outcome.get("action_suppressed", False), False)
    action_executed = safe_bool(outcome.get("action_executed", False), False)
    action_success = outcome.get("action_success")
    outcome_level = str(verdict.get("level", "") or "").strip().lower()
    outcome_code = str(verdict.get("code", "") or "").strip()

    trace_source = str(trace.get("source", "") or "").strip().lower()
    trace_reason = _first_trace_reason(trace)
    bouncer_state = str(bouncer.get("state", "") or "").strip().lower()
    bouncer_trip_source = str(bouncer.get("trip_source", "") or "").strip().lower()

    if no_action and trace_source == "constraint_vector":
        referee_state = "observe_monitor"
        grade = "deferred"
        if trace_reason == "observe_only_mode":
            summary = "Active control is off; 54GUC stayed in observe/monitor mode"
        elif trace_reason == "planned_radio_quiet":
            summary = "Planned radio quiet kept 54GUC in observe/monitor mode"
        else:
            summary = "Constraint vector kept 54GUC in observe/monitor mode"
    elif no_action:
        referee_state = "idle"
        grade = "none"
        summary = "No brain action to grade"
    elif action_suppressed:
        referee_state = "observe_monitor"
        grade = "deferred"
        if bouncer_state == "blocked" and bouncer_trip_source == "bouncer_intent":
            summary = "Bouncer blocked active control; 54GUC stayed in observe/monitor mode"
        else:
            summary = "Action stayed in observe/monitor mode"
    elif action_executed:
        canonical_grade = _grade_from_canonical_action(canonical_action)
        referee_state = "executed" if canonical_grade != "unavailable" else "pending"
        if canonical_grade == "accepted":
            grade = "accepted"
            summary = str(verdict.get("message", "") or "Referee accepted TSAR outcome")
        elif canonical_grade == "neutral":
            grade = "neutral"
            summary = str(verdict.get("message", "") or "Referee closed TSAR outcome as neutral")
        elif canonical_grade == "rejected":
            grade = "rejected"
            summary = str(verdict.get("message", "") or "Referee rejected TSAR outcome")
        elif canonical_grade == "unavailable":
            grade = "unavailable"
            summary = "Command dispatched; waiting for canonical action proof"
        else:
            grade = "unknown"
            summary = str(verdict.get("message", "") or "Action executed; referee grade is unclear")
    else:
        referee_state = "pending"
        grade = "pending"
        summary = "Action was proposed but no execution result is available yet"

    projection_text = str(canonical_projection.get("status_text", "") or "").strip()
    if projection_text and referee_state == "executed":
        summary = projection_text

    outcome_confidence = (
        "high"
        if referee_state == "executed" and grade in {"accepted", "rejected"}
        else "medium"
        if referee_state == "executed" and grade in {"neutral", "unknown"}
        else "low"
        if grade in {"pending", "none"}
        else "medium"
        if grade == "deferred"
        else "low"
    )
    learning_update = _learning_update_from_public_grade(
        referee_state=referee_state,
        grade=grade,
        canonical_action=canonical_action,
    )
    prediction_feedback = (
        "reinforce"
        if grade == "accepted"
        else "penalize"
        if grade == "rejected"
        else "hold"
    )
    judgment_family = (
        "chosen_well"
        if grade == "accepted"
        else "chosen_poorly"
        if grade == "rejected"
        else "unprovably"
    )

    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "decision_trace_id": str(outcome.get("decision_trace_id", "") or ""),
        "state": referee_state,
        "grade": grade,
        "judgment_family": judgment_family,
        "summary": summary,
        "outcome_confidence": outcome_confidence,
        "learning_update": learning_update,
        "prediction_feedback": prediction_feedback,
        "action_lever": lever,
        "action_intent": str(outcome.get("action_intent", "") or ""),
        "action_reason": str(outcome.get("action_reason", "") or ""),
        "action_origin": str(outcome.get("action_origin") or ""),
        "operator_initiated": bool(
            outcome.get("action_origin")
            and is_human_operator_origin(outcome.get("action_origin"))
        ),
        "bouncer": bouncer,
        "verdict": {
            "level": outcome_level,
            "code": outcome_code,
            "message": str(verdict.get("message", "") or ""),
            "action_success": action_success,
            "action_error": str(outcome.get("action_error", "") or ""),
        },
        "canonical": {
            "action_outcome": dict(canonical_action),
            "incident_state": dict(canonical_incident),
            "operator_projection": dict(canonical_projection),
        },
        "advisor_feedback": (
            dict(verdict.get("details", {}).get("advisor_feedback", {}))
            if isinstance(verdict.get("details"), Mapping)
            else {}
        ),
    }


def publish_brain_referee_publication_register(
    engine: Any,
    *,
    publication: Mapping[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Publish the Referee surface into the Brain feed register bank."""

    from arch4.brain.feed_registers import require_brain_feed_register_bank

    payload = dict(publication or {}) if isinstance(publication, Mapping) else {}
    _assert_publication_owner(payload)
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED,
        owner=OWNER,
        payload=payload,
        source="brain_referee_publication",
        updated_at=now_ts,
        valid=True,
        stale_after_s=6.0,
    )
    return {"published": True, "feed": FEED, "owner": OWNER, "schema": SCHEMA}


def read_brain_referee_publication_register(
    engine: Any,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Read the only legal Referee publication input for downstream learning."""

    from arch4.brain.feed_registers import require_brain_feed_register_bank

    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    row = bank.read_feeder(FEED).as_dict(now=now_ts)
    if str(row.get("error") or ""):
        raise BrainRefereePublicationUnavailable(str(row.get("error") or "missing"))
    if not bool(row.get("valid", False)):
        raise BrainRefereePublicationUnavailable("brain_referee_publication_register_invalid")
    if bool(row.get("stale", False)) and not bool(allow_stale):
        raise BrainRefereePublicationUnavailable("brain_referee_publication_register_stale")
    payload = _mapping(row.get("payload"))
    try:
        _assert_publication_owner(payload)
    except ValueError as exc:
        raise BrainRefereePublicationUnavailable(str(exc or "brain_referee_publication_invalid")) from exc
    return dict(payload)


def merge_operator_summary_into_brain_referee_publication(
    payload: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach operator-facing fields through the Referee publication owner."""

    out = dict(payload or {})
    referee = _mapping(out.get("brain_referee"))
    _assert_publication_owner(referee)

    operator_summary = _mapping(summary)
    operator_status = str(operator_summary.get("status", "") or "").strip()
    if operator_status:
        referee["summary"] = operator_status
    referee["pilot_recommendation"] = str(operator_summary.get("pilot_recommendation", "") or "")
    referee["pilot_recommendation_kind"] = str(
        operator_summary.get("pilot_recommendation_kind", "none") or "none"
    )
    referee["incident_class"] = str(operator_summary.get("incident_class", "") or "")
    referee["probable_cause"] = str(operator_summary.get("probable_cause", "") or "")
    referee["warning_class"] = str(operator_summary.get("warning_class", "") or "")
    referee["warning_source"] = str(operator_summary.get("warning_source", "") or "")
    referee["warning_mechanism"] = str(operator_summary.get("warning_mechanism", "") or "")
    referee["cliff_ahead"] = bool(operator_summary.get("cliff_ahead", False))
    referee["load_trend"] = str(operator_summary.get("load_trend", "") or "")
    referee["canonical_action_scope"] = str(operator_summary.get("canonical_action_scope", "") or "")
    referee["canonical_action_semantic"] = str(operator_summary.get("canonical_action_semantic", "") or "")
    referee["canonical_projection_class"] = str(operator_summary.get("canonical_projection_class", "") or "")
    out["brain_referee"] = referee
    return out


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _assert_publication_owner(publication: Mapping[str, Any]) -> None:
    if publication.get("schema") != SCHEMA or publication.get("owner") != OWNER:
        raise ValueError("brain_referee publication owner/schema mismatch")


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _grade_from_canonical_action(canonical_action: Mapping[str, Any]) -> str:
    if not canonical_action:
        return "unavailable"
    label = _lower(canonical_action.get("learning_outcome_label"))
    semantic = _lower(canonical_action.get("operator_semantic") or canonical_action.get("semantic"))
    if label == "good" or semantic == "beneficial":
        return "accepted"
    if label == "bad" or semantic == "harmful":
        return "rejected"
    if label == "neutral" or semantic in {"deferred", "mixed"}:
        return "neutral"
    if semantic == "blocked":
        return "deferred"
    return "unknown"


def _learning_update_from_public_grade(
    *,
    referee_state: str,
    grade: str,
    canonical_action: Mapping[str, Any],
) -> str:
    if referee_state != "executed":
        return "suppressed" if grade in {"deferred", "none"} else "deferred"
    if not canonical_action or grade == "unavailable":
        return "suppressed"
    if not safe_bool(canonical_action.get("learning_ltm_allowed", True), True):
        return "suppressed"
    if grade in {"accepted", "rejected", "neutral"}:
        return "recommended"
    return "deferred"


def _first_trace_reason(trace: Mapping[str, Any]) -> str:
    steps = trace.get("steps") if isinstance(trace.get("steps"), list) else []
    first_step = steps[0] if steps and isinstance(steps[0], Mapping) else {}
    return str(first_step.get("reason", "") or "").strip().lower()


__all__ = [
    "FEED",
    "OWNER",
    "SCHEMA",
    "BrainRefereePublicationUnavailable",
    "build_brain_referee_publication",
    "merge_operator_summary_into_brain_referee_publication",
    "publish_brain_referee_publication_register",
    "read_brain_referee_publication_register",
]
