"""Canonical Brain decision-quality classifier owner."""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from arch4.brain.feed_registers import (
    FEED_DECISION_QUALITY,
    OWNER_DECISION_QUALITY,
    require_brain_feed_register_bank,
)

SCHEMA = "brain_decision_quality_summary.v1"
OWNER = "arch4.brain.decision_quality_owner"
FEED = FEED_DECISION_QUALITY
DEFAULT_STALE_AFTER_S = 30.0


class DecisionQualityRegisterUnavailable(ValueError):
    """Raised when consumers cannot read the canonical decision-quality feed."""


def build_decision_quality_summary(
    *,
    action_count: int = 0,
    good_outcome_count: int = 0,
    bad_outcome_count: int = 0,
    ca_signature_flips: int = 0,
    ca_flip_threshold: int = 6,
    counterfactual_audits: Iterable[Mapping[str, Any]] | None = None,
    self_learning_candidate_surfaces: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify the Brain acted / should-have-acted decision-quality bucket."""

    audits = [audit for audit in (counterfactual_audits or ()) if isinstance(audit, Mapping)]
    surfaces = [
        surface for surface in (self_learning_candidate_surfaces or ()) if isinstance(surface, Mapping)
    ]
    missed_candidate_audits = [
        audit
        for audit in audits
        if _bool(audit.get("candidate_generation_failed"))
        or _text(audit.get("status")) == "needs_action_candidate"
    ]
    justified_no_action_audits = [
        audit
        for audit in audits
        if _text(audit.get("status")) == "ok"
        and not _bool(audit.get("should_have_candidate"))
        and not _bool(audit.get("candidate_generation_failed"))
    ]
    governor_blocked_surfaces = [
        surface
        for surface in surfaces
        if _surface_waits_for_safety(_candidate_authority_surface(surface))
    ]
    recorded_advisory_surfaces = [
        surface
        for surface in surfaces
        if _text(_candidate_authority_surface(surface).get("status")) == "advisory_only"
        and not _bool(_candidate_authority_surface(surface).get("candidate_operationalization_gap"))
    ]
    justified_rejection_surfaces = [
        surface
        for surface in surfaces
        if _surface_is_justified_rejection(_candidate_authority_surface(surface))
    ]
    candidate_operationalization_gaps = [
        surface
        for surface in surfaces
        if _bool(_candidate_authority_surface(surface).get("candidate_operationalization_gap"))
        and not _surface_waits_for_safety(_candidate_authority_surface(surface))
    ]
    missed_reasons = _unique_text(audit.get("reason") for audit in missed_candidate_audits)
    operationalization_reasons = _unique_text(
        (
            _candidate_authority_surface(surface).get("required_next_step")
            or _candidate_authority_surface(surface).get("status")
            or "candidate_operationalization_gap"
        )
        for surface in candidate_operationalization_gaps
    )
    problem_reasons = _unique_text(
        reason
        for audit in audits
        for reason in _iter_text_values(audit.get("problem_reasons"))
    )
    missing_evidence = _unique_text(
        item
        for audit in audits
        for item in _iter_text_values(audit.get("missing_evidence"))
    )
    governor_blockers = _unique_text(audit.get("governor_blocker") for audit in audits)
    should_have_acted = bool(missed_candidate_audits or candidate_operationalization_gaps)
    actions = int(action_count)
    good = int(good_outcome_count)
    bad = int(bad_outcome_count)
    if actions > 0 and good > 0 and bad <= 0:
        bucket = "brain_acted_justified"
    elif actions > 0 and bad > 0:
        bucket = "brain_acted_unjustified"
    elif actions > 0:
        bucket = "brain_acted_outcome_pending"
    elif should_have_acted:
        bucket = "brain_did_not_act_should_have"
    elif (
        justified_no_action_audits
        or governor_blocked_surfaces
        or recorded_advisory_surfaces
        or justified_rejection_surfaces
    ):
        bucket = "brain_did_not_act_justified"
    elif not audits and int(ca_signature_flips) >= int(ca_flip_threshold):
        bucket = "counterfactual_audit_missing"
    else:
        bucket = "no_action_required_or_insufficient_evidence"
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "bucket": bucket,
        "action_count": actions,
        "good_outcome_count": good,
        "bad_outcome_count": bad,
        "ca_signature_flips": int(ca_signature_flips),
        "ca_flip_threshold": int(ca_flip_threshold),
        "counterfactual_audit_count": len(audits),
        "missed_candidate_audit_count": len(missed_candidate_audits),
        "justified_no_action_audit_count": len(justified_no_action_audits)
        + len(governor_blocked_surfaces)
        + len(recorded_advisory_surfaces)
        + len(justified_rejection_surfaces),
        "self_learning_candidate_surface_count": len(surfaces),
        "candidate_operationalization_gap_count": len(candidate_operationalization_gaps),
        "missed_candidate_reasons": missed_reasons,
        "candidate_operationalization_gap_reasons": operationalization_reasons,
        "counterfactual_problem_reasons": problem_reasons,
        "counterfactual_missing_evidence": missing_evidence,
        "counterfactual_governor_blockers": _unique_text(
            [
                *governor_blockers,
                *(
                    _text(_candidate_authority_surface(surface).get("reason")).split(":", 1)[1]
                    for surface in governor_blocked_surfaces
                    if ":" in _text(_candidate_authority_surface(surface).get("reason"))
                ),
            ]
        ),
        "should_have_acted_reason": "candidate_generation_failed"
        if missed_candidate_audits
        else "candidate_not_operationalized"
        if candidate_operationalization_gaps
        else "",
        "decision_quality_reason": missed_reasons[0]
        if missed_reasons
        else operationalization_reasons[0]
        if operationalization_reasons
        else "counterfactual_audit_missing"
        if bucket == "counterfactual_audit_missing"
        else _text(_candidate_authority_surface(governor_blocked_surfaces[0]).get("reason"))
        if governor_blocked_surfaces
        else _text(_candidate_authority_surface(recorded_advisory_surfaces[0]).get("reason"))
        if recorded_advisory_surfaces
        else _text(_candidate_authority_surface(justified_rejection_surfaces[0]).get("reason"))
        if justified_rejection_surfaces
        else "counterfactual_proved_no_action_needed"
        if bucket == "brain_did_not_act_justified"
        else "",
        "should_have_acted": should_have_acted,
    }


def build_decision_quality_from_strategy(
    *,
    strategy: Mapping[str, Any] | None = None,
    candidate_authority: Mapping[str, Any] | None = None,
    action_count: int = 0,
    good_outcome_count: int = 0,
    bad_outcome_count: int = 0,
) -> dict[str, Any]:
    """Build decision quality from the canonical self-learning strategy."""

    strategy_row = strategy if isinstance(strategy, Mapping) else {}
    authority = candidate_authority if isinstance(candidate_authority, Mapping) else {}
    outcome = _text(strategy_row.get("outcome"))
    authority_gap = _bool(authority.get("candidate_operationalization_gap")) and not _surface_waits_for_safety(authority)
    if not outcome and _bool(authority.get("candidate_generation_gap")):
        outcome = "candidate_missing_counterfactual"
    if authority_gap:
        outcome = "candidate_present_but_not_operational"
    if authority_gap:
        reason = (
            _text(strategy_row.get("reason"))
            or _text(strategy_row.get("reason_code"))
            or _text(authority.get("reason"))
            or _text(authority.get("required_next_step"))
        )
    else:
        reason = (
            _text(strategy_row.get("reason"))
            or _text(strategy_row.get("required_next_step"))
            or _text(authority.get("reason"))
            or _text(authority.get("required_next_step"))
        )
    actions = int(action_count)
    good = int(good_outcome_count)
    bad = int(bad_outcome_count)
    if actions > 0 and good > 0 and bad <= 0:
        bucket = "brain_acted_justified"
    elif actions > 0 and bad > 0:
        bucket = "brain_acted_unjustified"
    elif actions > 0:
        bucket = "brain_acted_outcome_pending"
    elif outcome in {"candidate_missing_counterfactual", "candidate_present_but_not_operational"}:
        bucket = "brain_did_not_act_should_have"
    elif outcome in {"no_action_justified", "action_rejected", "safety_wait", "recorded_advisory"}:
        bucket = "brain_did_not_act_justified"
    else:
        bucket = "no_action_required_or_insufficient_evidence"
    should_have = bucket == "brain_did_not_act_should_have"
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "bucket": bucket,
        "action_count": actions,
        "good_outcome_count": good,
        "bad_outcome_count": bad,
        "ca_signature_flips": 0,
        "ca_flip_threshold": 6,
        "counterfactual_audit_count": 0,
        "missed_candidate_audit_count": 1 if outcome == "candidate_missing_counterfactual" else 0,
        "justified_no_action_audit_count": 1 if bucket == "brain_did_not_act_justified" else 0,
        "self_learning_candidate_surface_count": 1 if authority else 0,
        "candidate_operationalization_gap_count": (
            1 if outcome == "candidate_present_but_not_operational" else 0
        ),
        "missed_candidate_reasons": [reason] if outcome == "candidate_missing_counterfactual" and reason else [],
        "candidate_operationalization_gap_reasons": (
            [reason] if outcome == "candidate_present_but_not_operational" and reason else []
        ),
        "counterfactual_problem_reasons": [],
        "counterfactual_missing_evidence": [],
        "counterfactual_governor_blockers": [],
        "should_have_acted_reason": "candidate_generation_failed"
        if outcome == "candidate_missing_counterfactual"
        else "candidate_not_operationalized"
        if outcome == "candidate_present_but_not_operational"
        else "",
        "decision_quality_reason": reason,
        "should_have_acted": should_have,
        "strategy_outcome": outcome,
        "strategy_reason": _text(strategy_row.get("reason")),
        "strategy_owner": _text(strategy_row.get("owner")),
    }


def publish_decision_quality_register(
    engine: Any,
    *,
    decision_quality: Mapping[str, Any],
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    payload = _require_decision_quality(decision_quality)
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED,
        owner=OWNER_DECISION_QUALITY,
        payload=payload,
        source="brain_decision_quality",
        updated_at=now_ts,
        valid=True,
        stale_after_s=max(0.0, float(stale_after_s or 0.0)),
    )
    return {"published": True, "feed": FEED, "owner": OWNER, "schema": SCHEMA}


def read_decision_quality_register(
    engine: Any,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    row = bank.read_feeder(FEED).as_dict(now=now_ts)
    if str(row.get("error") or ""):
        raise DecisionQualityRegisterUnavailable(str(row.get("error") or "missing"))
    if not bool(row.get("valid", False)):
        raise DecisionQualityRegisterUnavailable("decision_quality_register_invalid")
    if bool(row.get("stale", False)) and not bool(allow_stale):
        raise DecisionQualityRegisterUnavailable("decision_quality_register_stale")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return _require_decision_quality(payload)


def _require_decision_quality(decision_quality: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(decision_quality or {}) if isinstance(decision_quality, Mapping) else {}
    if _text(row.get("schema")) != SCHEMA:
        raise ValueError("decision_quality_schema_mismatch")
    if _text(row.get("owner")) != OWNER:
        raise ValueError("decision_quality_owner_mismatch")
    if not _text(row.get("bucket")):
        raise ValueError("decision_quality_bucket_missing")
    return row


def _surface_waits_for_safety(surface: Mapping[str, Any]) -> bool:
    if _text(surface.get("required_next_step")) == "wait_for_safety_or_capability_release":
        return True
    reason = _text(surface.get("reason"))
    return reason.startswith("learning_candidate_advisory_waiting_for_governor:")


def _surface_is_justified_rejection(surface: Mapping[str, Any]) -> bool:
    if _bool(surface.get("candidate_operationalization_gap")) or _bool(surface.get("candidate_generation_gap")):
        return False
    status = _text(surface.get("status"))
    if status in {"no_action_justified", "safety_wait", "recorded_advisory", "advisory_only"}:
        return True
    return status == "action_rejected"


def _candidate_authority_surface(surface: Mapping[str, Any]) -> Mapping[str, Any]:
    authority = surface.get("authority")
    if isinstance(authority, Mapping):
        return authority
    return surface


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


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "DecisionQualityRegisterUnavailable",
    "FEED",
    "OWNER",
    "SCHEMA",
    "build_decision_quality_from_strategy",
    "build_decision_quality_summary",
    "publish_decision_quality_register",
    "read_decision_quality_register",
]
