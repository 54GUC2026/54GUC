"""Canonical action-proof summary owner for Brain learning effect."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from arch4.brain.action_proof_register import action_proof_roi_surface

OWNER = "arch4.brain.learning_effect_action_proof_owner"
SCHEMA = "brain_learning_effect_action_proof_summary.v1"


def action_proof_summary(
    self_learning_candidate_surfaces: Iterable[Mapping[str, Any]] | None,
    *,
    action_proof_surface: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [row for row in (self_learning_candidate_surfaces or ()) if isinstance(row, Mapping)]
    recent = 0
    good = 0
    bad = 0
    last_label = ""
    last_referee_code = ""
    last_reason = ""
    last_secondary_learning_status = ""
    last_selected_action_command_id = ""
    last_selected_action_proof_id = ""
    last_smart_loop_reason_ref = ""
    last_smart_loop_context_key = ""
    last_smart_loop_identity_source = ""
    memory_commit = 0
    historical_commit = 0
    crystal_ball_update = 0
    crystal_ball_regrade = 0
    owner_roi = action_proof_roi_surface(action_proof_surface or {})
    recent += _int(owner_roi.get("action_proof_recent_count"), 0)
    good += _int(owner_roi.get("action_proof_good_count"), 0)
    bad += _int(owner_roi.get("action_proof_bad_count"), 0)
    memory_commit += _int(owner_roi.get("action_proof_memory_commit_count"), 0)
    historical_commit += _int(owner_roi.get("action_proof_historical_commit_count"), 0)
    crystal_ball_update += _int(owner_roi.get("action_proof_crystal_ball_update_count"), 0)
    crystal_ball_regrade += _int(owner_roi.get("action_proof_crystal_ball_regrade_count"), 0)
    owner_label = _text(owner_roi.get("last_action_proof_label"))
    if owner_label:
        last_label = owner_label
        last_referee_code = _text(owner_roi.get("last_action_proof_referee_code"))
        last_reason = _text(owner_roi.get("last_action_proof_reason"))
        last_secondary_learning_status = _text(
            owner_roi.get("last_action_proof_secondary_learning_status")
        )
        last_selected_action_command_id = _text(owner_roi.get("last_action_proof_selected_action_command_id"))
        last_selected_action_proof_id = _text(owner_roi.get("last_action_proof_selected_action_proof_id"))
        last_smart_loop_reason_ref = _text(owner_roi.get("last_action_proof_smart_loop_reason_ref"))
        last_smart_loop_context_key = _text(owner_roi.get("last_action_proof_smart_loop_context_key"))
        if last_selected_action_proof_id:
            last_smart_loop_identity_source = "action_proof_register"
    for row in rows:
        if str(row.get("schema") or "") == "smart_loop.closed_loop.v1":
            evidence = _mapping(row.get("evidence"))
            roi = {
                "action_proof_recent_count": evidence.get("action_proof_recent_count"),
                "action_proof_good_count": 1
                if str(_mapping(row.get("closed_proof")).get("label") or "") == "good"
                else 0,
                "action_proof_bad_count": 1
                if str(_mapping(row.get("closed_proof")).get("label") or "") == "bad"
                else 0,
                "action_proof_memory_commit_count": evidence.get("action_proof_memory_commit_count"),
                "action_proof_historical_commit_count": evidence.get("action_proof_historical_commit_count", 0),
                "action_proof_crystal_ball_update_count": evidence.get(
                    "action_proof_crystal_ball_update_count"
                ),
                "action_proof_crystal_ball_regrade_count": evidence.get(
                    "action_proof_crystal_ball_regrade_count"
                ),
                "last_action_proof_label": _mapping(row.get("closed_proof")).get("label"),
                "last_action_proof_referee_code": _mapping(row.get("closed_proof")).get("reason_code"),
                "last_action_proof_reason": _mapping(row.get("closed_proof")).get("reason_code"),
                "last_action_proof_selected_action_command_id": _mapping(row.get("closed_proof")).get("command_id"),
                "last_action_proof_selected_action_proof_id": _mapping(row.get("closed_proof")).get("proof_id"),
                "last_action_proof_smart_loop_reason_ref": _mapping(row.get("closed_proof")).get("reason_ref"),
                "last_action_proof_smart_loop_context_key": _mapping(row.get("closed_proof")).get("context_key"),
                "last_action_proof_secondary_learning_status": "committed"
                if bool(row.get("learning_loop_closed"))
                else "",
            }
        else:
            roi = _mapping(row.get("roi_surface"))
        recent += _int(roi.get("action_proof_recent_count"), 0)
        good += _int(roi.get("action_proof_good_count"), 0)
        bad += _int(roi.get("action_proof_bad_count"), 0)
        memory_commit += _int(roi.get("action_proof_memory_commit_count"), 0)
        historical_commit += _int(roi.get("action_proof_historical_commit_count"), 0)
        crystal_ball_update += _int(roi.get("action_proof_crystal_ball_update_count"), 0)
        crystal_ball_regrade += _int(roi.get("action_proof_crystal_ball_regrade_count"), 0)
        label = _text(roi.get("last_action_proof_label"))
        if label:
            last_label = label
            last_referee_code = _text(roi.get("last_action_proof_referee_code"))
            last_reason = _text(roi.get("last_action_proof_reason"))
            last_secondary_learning_status = _text(
                roi.get("last_action_proof_secondary_learning_status")
            )
        row_proof_id = _text(roi.get("last_action_proof_selected_action_proof_id"))
        if row_proof_id:
            last_selected_action_command_id = _text(roi.get("last_action_proof_selected_action_command_id"))
            last_selected_action_proof_id = row_proof_id
            last_smart_loop_reason_ref = _text(roi.get("last_action_proof_smart_loop_reason_ref"))
            last_smart_loop_context_key = _text(roi.get("last_action_proof_smart_loop_context_key"))
            last_smart_loop_identity_source = (
                "strategy_closed_proof"
                if str(row.get("schema") or "") == "smart_loop.closed_loop.v1"
                else "roi_surface"
            )
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "recent_count": recent,
        "good_count": good,
        "bad_count": bad,
        "memory_commit_count": memory_commit,
        "historical_commit_count": historical_commit,
        "crystal_ball_update_count": crystal_ball_update,
        "crystal_ball_regrade_count": crystal_ball_regrade,
        "secondary_commit_count": memory_commit + historical_commit + crystal_ball_update,
        "last_label": last_label,
        "last_referee_code": last_referee_code,
        "last_reason": last_reason,
        "last_secondary_learning_status": last_secondary_learning_status,
        "last_selected_action_command_id": last_selected_action_command_id,
        "last_selected_action_proof_id": last_selected_action_proof_id,
        "last_smart_loop_reason_ref": last_smart_loop_reason_ref,
        "last_smart_loop_context_key": last_smart_loop_context_key,
        "last_smart_loop_identity_source": last_smart_loop_identity_source,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "OWNER",
    "SCHEMA",
    "action_proof_summary",
]
