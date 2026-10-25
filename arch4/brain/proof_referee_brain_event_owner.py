"""Brain-event publication owner for staged ROI proof/referee closure."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from arch4.brain.proof_feedback_owner import layered_action_outcome_label
from arch4.brain.proof_referee_evidence import staged_roi_action_outcome_contract

OWNER = "arch4.brain.proof_referee_brain_event_owner"

log = logging.getLogger("arch4.brain.proof_referee_brain_event_owner")


def publish_staged_roi_brain_event(
    *,
    proof_owner: str,
    brain_event_inbox: Any,
    original: Any,
    record: Any,
    horizon: int,
    label: str,
    reason: str,
    referee_code: str,
    secondary: dict[str, Any],
    evidence: dict[str, Any],
    ts: float,
    reward_ledger_key: str = "",
) -> dict[str, Any]:
    if brain_event_inbox is None or not callable(getattr(brain_event_inbox, "publish", None)):
        return {"published": False, "reason": "brain_event_inbox_unavailable"}
    parent_trace = str(getattr(original, "decision_trace_id", "") or "")
    child_trace = str(getattr(record, "decision_trace_id", "") or "")
    original_meta = (
        getattr(original, "objective_metadata", None)
        if isinstance(getattr(original, "objective_metadata", None), Mapping)
        else {}
    )
    record_meta = (
        getattr(record, "objective_metadata", None)
        if isinstance(getattr(record, "objective_metadata", None), Mapping)
        else {}
    )
    layered_feedback = dict(record_meta.get("layered_action_feedback") or {})
    proof_identity = dict(record_meta.get("delayed_reward_proof_identity") or {})
    memory_admitted = bool(dict(secondary or {}).get("reward_admitted") is True)
    payload = {
        "schema_version": "staged_roi_brain_event.v1",
        "owner": proof_owner,
        "parent_trace_id": parent_trace,
        "child_trace_id": child_trace,
        "roi_horizon_s": int(horizon),
        "label": str(label or ""),
        "reason": str(reason or ""),
        "referee_code": str(referee_code or ""),
        "referee_level": str(staged_roi_action_outcome_contract(label, reason).get("level", "") or "neutral"),
        "learning_commit_status": str(getattr(record, "learning_commit_status", "") or ""),
        "parent_status": str(getattr(original, "learning_commit_status", "") or ""),
        "secondary_learning": dict(secondary or {}),
        "evidence": dict(evidence or {}),
        "layered_feedback": layered_feedback,
        "proof_identity": proof_identity,
        "action_root_id": str(proof_identity.get("action_root_id") or ""),
        "action_transaction": dict(
            proof_identity.get("action_transaction") or {}
        ),
        "selected_action_command_id": str(original_meta.get("selected_action_command_id") or ""),
        "selected_action_proof_id": str(original_meta.get("selected_action_proof_id") or ""),
        "smart_loop_reason_ref": str(original_meta.get("smart_loop_reason_ref") or ""),
        "smart_loop_context_key": str(original_meta.get("smart_loop_context_key") or ""),
        "terminal": True,
        "memory_admitted": memory_admitted,
        "grade_kind": "terminal_layered_reward",
        "reward_ledger_key": str(reward_ledger_key or ""),
    }
    try:
        event = brain_event_inbox.publish(
            "action_proof_closed",
            payload=payload,
            source="staged_roi",
            due_ts=float(ts),
            priority=10,
            dedupe_key=(
                f"action_proof_closed:reward:{reward_ledger_key}"
                if str(reward_ledger_key or "")
                else f"action_proof_closed:{parent_trace}:{int(horizon)}"
            ),
        )
    except Exception as exc:
        log.debug("staged ROI brain event publish suppressed: %s", exc)
        return {"published": False, "reason": str(exc or "")}
    return {
        "published": True,
        "event_id": str(getattr(event, "event_id", "") or ""),
        "kind": str(getattr(event, "kind", "") or "action_proof_closed"),
        "dedupe_key": str(getattr(event, "dedupe_key", "") or ""),
    }


def publish_smart_loop_feedback_brain_event(
    *,
    proof_owner: str,
    brain_event_inbox: Any,
    original: Any,
    horizon: int,
    evidence: dict[str, Any],
    layered_feedback: dict[str, Any],
    ts: float,
) -> dict[str, Any]:
    """Publish a non-terminal, identity-matched SmartLoop proof grade."""

    if brain_event_inbox is None or not callable(getattr(brain_event_inbox, "publish", None)):
        return {"published": False, "reason": "brain_event_inbox_unavailable"}
    parent_trace = str(getattr(original, "decision_trace_id", "") or "")
    meta = (
        getattr(original, "objective_metadata", None)
        if isinstance(getattr(original, "objective_metadata", None), Mapping)
        else {}
    )
    label = layered_action_outcome_label(layered_feedback)
    reason = str(layered_feedback.get("reason") or "")
    contract = staged_roi_action_outcome_contract(label, reason)
    referee_code = str(contract.get("code") or "")
    payload = {
        "schema_version": "smart_loop_action_feedback_event.v1",
        "owner": proof_owner,
        "parent_trace_id": parent_trace,
        "child_trace_id": f"{parent_trace}:feedback:{int(horizon)}",
        "roi_horizon_s": int(horizon),
        "label": str(label or "neutral"),
        "reason": str(reason or ""),
        "referee_code": str(referee_code or ""),
        "referee_level": str(contract.get("level", "") or "neutral"),
        "learning_commit_status": "feedback_only",
        "parent_status": "proof_pending",
        "terminal": False,
        "memory_admitted": False,
        "grade_kind": "smart_loop_interim",
        "evidence": dict(evidence or {}),
        "layered_feedback": dict(layered_feedback or {}),
        "selected_action_command_id": str(meta.get("selected_action_command_id") or ""),
        "selected_action_proof_id": str(meta.get("selected_action_proof_id") or ""),
        "smart_loop_reason_ref": str(meta.get("smart_loop_reason_ref") or ""),
        "smart_loop_context_key": str(meta.get("smart_loop_context_key") or ""),
        "action_root_id": str(meta.get("action_root_id") or ""),
        "action_transaction": dict(meta.get("action_transaction") or {}),
    }
    try:
        event = brain_event_inbox.publish(
            "action_proof_feedback",
            payload=payload,
            source="staged_roi",
            due_ts=float(ts),
            priority=10,
            dedupe_key=f"action_proof_feedback:{parent_trace}:{int(horizon)}",
        )
    except Exception as exc:
        log.debug("SmartLoop proof feedback event publish suppressed: %s", exc)
        return {"published": False, "reason": str(exc or "")}
    return {
        "published": True,
        "event_id": str(getattr(event, "event_id", "") or ""),
        "kind": str(getattr(event, "kind", "") or "action_proof_feedback"),
        "dedupe_key": str(getattr(event, "dedupe_key", "") or ""),
    }


__all__ = [
    "OWNER",
    "publish_smart_loop_feedback_brain_event",
    "publish_staged_roi_brain_event",
]
