"""Explicit boundary adapters for replay event rows.

Production runtime surfaces have their own owners. Replay imports named
owner-stamped surfaces only; it does not reconstruct deleted smartness paths
from flat historical fields.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.brain.action_proof_register import (
    EVENT_KIND as ACTION_PROOF_EVENT_KIND,
    action_proof_closure_from_mapping,
    action_proof_roi_surface,
    action_proof_surface_from_rows,
)

SCHEMA = "replay_event_adapter.v1"
OWNER = "arch4.replay_event_adapter"
BOUNDARY = "historical_replay_import"


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


def _event_kind(row: Mapping[str, Any]) -> str:
    return _text(row.get("event") or row.get("type") or row.get("record_type"))


def counterfactual_audit_from_replay_event(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the nested production audit or an explicit legacy-row adapter."""

    audit = row.get("counterfactual_opportunity_audit")
    if isinstance(audit, Mapping):
        return audit
    if "counterfactual_candidate_generation_failed" not in row and "counterfactual_should_have_candidate" not in row:
        return {}
    return {
        "schema": "replay_legacy_counterfactual_audit_adapter.v1",
        "owner": OWNER,
        "boundary": BOUNDARY,
        "source_schema": "legacy_flat_decision_trace",
        "status": _text(row.get("counterfactual_status")),
        "reason": _text(row.get("counterfactual_reason")),
        "should_have_candidate": _bool(row.get("counterfactual_should_have_candidate")),
        "candidate_generation_failed": _bool(row.get("counterfactual_candidate_generation_failed")),
    }


def action_proof_authority_surface_from_replay_event(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Adapt replay action-proof rows into the Brain action-proof surface."""

    proof_surface = row.get("action_proof_closures")
    if isinstance(proof_surface, Mapping):
        return {
            "schema": "replay_action_proof_surface_adapter.v1",
            "owner": OWNER,
            "boundary": BOUNDARY,
            "status": "action_proof_replay",
            "action_proof_closures": dict(proof_surface),
            "roi_surface": action_proof_roi_surface(proof_surface),
        }
    if _event_kind(row) != ACTION_PROOF_EVENT_KIND:
        return {}
    surface = action_proof_surface_from_rows([action_proof_closure_from_mapping(row)])
    return {
        "schema": "replay_action_proof_event_adapter.v1",
        "owner": OWNER,
        "boundary": BOUNDARY,
        "status": "action_proof_replay",
        "action_proof_closures": surface,
        "roi_surface": action_proof_roi_surface(surface),
    }


def smart_loop_candidate_projection_from_replay_event(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a smart-loop projection or action-proof replay evidence."""

    projection = row.get("smart_loop_candidate_projection")
    if isinstance(projection, Mapping):
        return projection
    authority = row.get("smart_loop_authority")
    if isinstance(authority, Mapping):
        return {
            "schema": "smart_loop.candidate_projection.v1",
            "owner": _text(authority.get("owner")) or "arch4.smart_loop.closed_loop",
            "boundary": BOUNDARY,
            "status": _text(authority.get("status")),
            "reason": _text(authority.get("reason")),
            "required_next_step": _text(authority.get("required_next_step")),
            "candidate_actions": [],
            "authority": dict(authority),
        }
    proof = action_proof_authority_surface_from_replay_event(row)
    if proof:
        return proof
    return {}


def referee_outcome_from_replay_event(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Adapt historical action-grade rows before the outcome-contract owner."""

    referee = row.get("referee_outcome")
    if isinstance(referee, Mapping):
        return referee
    legacy_grade = _text(row.get("grade") or row.get("outcome_grade") or row.get("status")).lower()
    if not legacy_grade:
        return {}
    level = {
        "success": "good",
        "outcome_ok": "good",
        "failed": "bad",
        "failure": "bad",
        "worse": "bad",
        "outcome_bad": "bad",
    }.get(legacy_grade, legacy_grade)
    return {
        "schema": "replay_legacy_referee_outcome_adapter.v1",
        "owner": OWNER,
        "boundary": BOUNDARY,
        "level": level,
        "code": legacy_grade,
        "message": "legacy_replay_referee_outcome_adapter",
    }


__all__ = [
    "BOUNDARY",
    "OWNER",
    "SCHEMA",
    "action_proof_authority_surface_from_replay_event",
    "counterfactual_audit_from_replay_event",
    "referee_outcome_from_replay_event",
    "smart_loop_candidate_projection_from_replay_event",
]
