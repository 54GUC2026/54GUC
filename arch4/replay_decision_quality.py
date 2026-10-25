"""Replay helpers for classifying Brain decision quality windows."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from arch4.action_outcome_contract import action_outcome_contract_from_referee
from arch4.brain.decision_quality_owner import build_decision_quality_summary
from arch4.brain.learning_effect import build_brain_learning_effect
from arch4.brain.referee_feedback_quality import referee_feedback_quality_from_surfaces
from arch4.replay_event_adapter import (
    counterfactual_audit_from_replay_event,
    referee_outcome_from_replay_event,
    smart_loop_candidate_projection_from_replay_event,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _event_kind(row: Mapping[str, Any]) -> str:
    return _text(row.get("event") or row.get("type") or row.get("record_type"))


def _action_name(row: Mapping[str, Any]) -> str:
    return _text(row.get("lever") or row.get("action") or row.get("action_lever")).upper()


def _ca_signature(row: Mapping[str, Any]) -> str:
    return _text(
        row.get("ca_signature")
        or row.get("ca_band_signature")
        or row.get("active_band_set_signature")
        or row.get("band_set_signature")
    )


def count_ca_signature_flips(rows: Iterable[Mapping[str, Any]]) -> int:
    previous = ""
    flips = 0
    for row in rows:
        sig = _ca_signature(row)
        if not sig:
            continue
        if previous and sig != previous:
            flips += 1
        previous = sig
    return flips


def _counterfactual_audits(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [audit for audit in (counterfactual_audit_from_replay_event(row) for row in events) if audit]


def _smart_loop_candidate_surfaces(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [surface for surface in (smart_loop_candidate_projection_from_replay_event(row) for row in events) if surface]


def _referee_feedback_quality_from_event(row: Mapping[str, Any]) -> Mapping[str, Any]:
    projection = smart_loop_candidate_projection_from_replay_event(row)
    authority = projection.get("authority") if isinstance(projection.get("authority"), Mapping) else projection
    audit = counterfactual_audit_from_replay_event(row)
    return referee_feedback_quality_from_surfaces(
        self_learning_authority=authority,
        brain_referee=row.get("brain_referee") if isinstance(row.get("brain_referee"), Mapping) else {},
        referee_outcome=row.get("referee_outcome") if isinstance(row.get("referee_outcome"), Mapping) else {},
        counterfactual_audit=audit,
    )


def _referee_feedback_quality_surfaces(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [quality for quality in (_referee_feedback_quality_from_event(row) for row in events) if quality]


def _replay_action_outcome_contracts(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for row in events:
        if _event_kind(row) not in {"action_grade", "action_learning_outcome"}:
            continue
        contract = action_outcome_contract_from_referee(
            referee_outcome_from_replay_event(row),
            action_present=True,
            action_success=None,
        )
        rows.append(contract)
    return rows


def classify_decision_quality_window(
    *,
    telemetry_rows: Iterable[Mapping[str, Any]],
    event_rows: Iterable[Mapping[str, Any]],
    ca_flip_threshold: int = 6,
) -> dict[str, Any]:
    """Classify whether Brain action behavior matched replay evidence."""

    telemetry = [dict(row) for row in telemetry_rows]
    events = [dict(row) for row in event_rows]
    action_events = [
        row
        for row in events
        if _action_name(row) and _action_name(row) not in {"NONE", "NOOP"}
    ]
    action_outcomes = _replay_action_outcome_contracts(events)
    good_outcomes = [
        row for row in action_outcomes if _text(row.get("learning_outcome_label")) == "good"
    ]
    bad_outcomes = [
        row for row in action_outcomes if _text(row.get("learning_outcome_label")) == "bad"
    ]
    ca_flips = count_ca_signature_flips(telemetry)
    audits = _counterfactual_audits(events)
    smart_loop_surfaces = _smart_loop_candidate_surfaces(events)
    referee_feedback = _referee_feedback_quality_surfaces(events)
    out = build_decision_quality_summary(
        action_count=len(action_events),
        good_outcome_count=len(good_outcomes),
        bad_outcome_count=len(bad_outcomes),
        ca_signature_flips=ca_flips,
        ca_flip_threshold=int(ca_flip_threshold),
        counterfactual_audits=audits,
        self_learning_candidate_surfaces=smart_loop_surfaces,
    )
    learning_effect = build_brain_learning_effect(
        referee_feedback_qualities=referee_feedback,
        decision_quality=out,
        self_learning_candidate_surfaces=smart_loop_surfaces,
    )
    out["referee_feedback_quality_count"] = learning_effect["referee_feedback_quality_count"]
    out["referee_feedback_low_information_count"] = learning_effect[
        "referee_feedback_low_information_count"
    ]
    out["referee_feedback_valid_no_action_count"] = learning_effect[
        "referee_feedback_valid_no_action_count"
    ]
    out["referee_feedback_counterfactual_diagnostic_count"] = learning_effect[
        "referee_feedback_counterfactual_diagnostic_count"
    ]
    out["referee_feedback_outcome_signal_count"] = learning_effect[
        "referee_feedback_outcome_signal_count"
    ]
    out["learning_effect"] = learning_effect
    return out
