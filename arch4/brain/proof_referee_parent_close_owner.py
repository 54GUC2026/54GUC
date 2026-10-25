"""Staged-ROI parent proof-close owner."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.brain.proof_memory_store import close_action_proof_record
from arch4.brain.proof_referee_context_owner import int_or_zero
from arch4.brain.proof_referee_evidence import staged_roi_action_outcome_contract
from arch4.brain.proof_referee_recording_event_owner import log_staged_roi_parent_close_event

OWNER = "arch4.brain.proof_referee_parent_close_owner"


def close_staged_roi_parent(
    *,
    proof_owner: str,
    logger: Any,
    memory: Any,
    original: Any,
    horizon: int,
    label: str,
    reason: str,
    referee_code: str,
    evidence: dict[str, Any],
    ts: float,
) -> None:
    if original is None:
        return
    contract = staged_roi_action_outcome_contract(label, reason)
    level = str(contract.get("level", "") or "neutral")
    contract_label = str(contract.get("learning_outcome_label", "") or "neutral")
    meta = dict(getattr(original, "objective_metadata", {}) or {})
    closures = meta.get("staged_roi_closures", [])
    if not isinstance(closures, list):
        closures = []
    for existing in closures:
        if not isinstance(existing, Mapping):
            continue
        if int_or_zero(existing.get("horizon_s")) == int(horizon):
            raise RuntimeError(f"staged ROI parent already closed for horizon: {int(horizon)}s")
    closures.append(
        {
            "horizon_s": int(horizon),
            "label": str(label or "neutral"),
            "reason": str(reason or ""),
            "code": str(referee_code or ""),
            "level": level,
            "closed_ts": round(float(ts), 3),
            "evidence_valid": bool(evidence.get("grade_evidence_valid", False)),
            "context_status": str(evidence.get("grade_context_status", "") or ""),
        }
    )
    meta["staged_roi_closures"] = closures
    meta["staged_roi_parent_status"] = "proof_closed"
    meta["staged_roi_final_horizon_s"] = int(horizon)
    meta["staged_roi_final_label"] = str(label or "neutral")
    meta["staged_roi_final_reason"] = str(reason or "")
    meta["staged_roi_final_code"] = str(referee_code or "")
    meta["staged_roi_final_level"] = level
    meta["staged_roi_final_closed_ts"] = round(float(ts), 3)
    meta["staged_roi_final_evidence"] = dict(evidence or {})
    original.objective_metadata = meta
    original.objective_stage = "staged_roi"
    original.objective_status = "proof_closed"
    original.outcome_label = contract_label
    original.hard_fail = bool(contract.get("hard_fail", False))
    original.referee_outcome_level = level
    original.referee_outcome_code = str(referee_code or "")
    original.outcome_verdict_stage = "staged_roi"
    original.outcome_verdict_authority = "referee"
    original.outcome_verdict_triggered_by = proof_owner
    original.learning_ltm_applied = False
    original.learning_commit_status = "proof_closed"
    original.learning_policy_reason = _append_reason(
        str(getattr(original, "learning_policy_reason", "") or ""),
        f"staged_roi_parent_closed:{int(horizon)}s:{reason}",
    )

    close_action_proof_record(memory, original, ts=float(ts))

    log_staged_roi_parent_close_event(
        logger=logger,
        original=original,
        horizon=horizon,
        reason=reason,
        referee_code=referee_code,
        level=level,
    )


def _append_reason(existing: str, addition: str) -> str:
    existing = str(existing or "").strip(";")
    addition = str(addition or "").strip(";")
    if not addition:
        return existing
    if not existing:
        return addition
    parts = [p for p in existing.split(";") if p]
    if addition in parts:
        return existing
    return f"{existing};{addition}"


__all__ = ["OWNER", "close_staged_roi_parent"]
