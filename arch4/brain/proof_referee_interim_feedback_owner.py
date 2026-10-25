"""Non-terminal SmartLoop action-feedback owner.

The Proof/Referee remains the only grading authority. These receipts make the
declared 1-minute and 6-minute observations visible to the originating
SmartLoop proof without closing the parent or admitting durable ROI.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from typing import Any, Optional

from arch4.brain.memory_io import dict_to_record
from arch4.brain.proof_memory_store import record_action_proof_feedback
from arch4.brain.proof_referee_brain_event_owner import (
    publish_smart_loop_feedback_brain_event,
)
from arch4.brain.proof_referee_evidence import (
    staged_roi_action_outcome_contract,
    staged_roi_label,
)
from arch4.brain.proof_feedback_owner import (
    build_referee_layered_feedback,
    layered_action_outcome_label,
    terminal_layered_action_feedback_is_valid,
)
from arch4.brain.proof_referee_action_lifecycle_projection_owner import (
    project_referee_grade_to_action_lifecycle,
)
from arch4.brain.proof_referee_context_owner import delayed_reward_proof_identity
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.brain.reward_ledger import (
    complete_trajectory_projection,
    pending_trajectory_projection_entries,
    trajectory_projection_is_completed,
)



OWNER = "arch4.brain.proof_referee_interim_feedback_owner"


def record_due_smart_loop_feedback(
    *,
    memory: Any,
    action_transaction_authority: Any,
    brain_event_inbox: Any,
    original: Any,
    meta: dict[str, Any],
    done: set[int],
    horizon: int,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    extra: dict[str, Any],
    incident_assessment: Optional[dict[str, Any]],
    ts: float,
) -> dict[str, Any]:
    label, reason, evidence = staged_roi_label(
        original=original,
        modem_now=modem_now,
        computed_now=computed_now,
        extra=extra,
        incident_assessment=incident_assessment,
    )
    evidence_label = str(label or "neutral")
    layered_feedback = build_referee_layered_feedback(
        label=evidence_label,
        reason=reason,
        evidence=evidence,
        horizon_s=int(horizon),
    )
    label = layered_action_outcome_label(layered_feedback)
    contract = staged_roi_action_outcome_contract(label, reason)
    referee_code = str(contract.get("code", "") or "")
    done.add(int(horizon))
    meta["smart_loop_feedback_done_s"] = sorted(done)
    feedback_rows = meta.get("smart_loop_feedback", [])
    if not isinstance(feedback_rows, list):
        feedback_rows = []
    feedback_rows.append(
        {
            "horizon_s": int(horizon),
            "label": str(label or "neutral"),
            "evidence_label": evidence_label,
            "reason": str(reason or ""),
            "code": referee_code,
            "recorded_ts": round(float(ts), 3),
            "terminal": False,
            "memory_admitted": False,
            "evidence_valid": bool(evidence.get("grade_evidence_valid", False)),
            "evidence": dict(evidence),
            "layered_action_feedback": dict(layered_feedback),
        }
    )
    meta["smart_loop_feedback"] = feedback_rows[-8:]
    original.objective_metadata = meta
    persistence = record_action_proof_feedback(
        memory,
        original,
        ts=float(ts),
        horizon=int(horizon),
    )
    if not bool(persistence.get("recorded", False)):
        done.discard(int(horizon))
        meta["smart_loop_feedback_done_s"] = sorted(done)
        meta["smart_loop_feedback"] = feedback_rows[:-1][-8:]
        original.objective_metadata = meta
        return {
            "horizon_s": int(horizon),
            "terminal": False,
            "memory_admitted": False,
            "recorded": False,
            "reason": str(persistence.get("reason") or "proof_feedback_not_persisted"),
        }
    event = _publish_persisted_feedback(
        memory=memory,
        action_transaction_authority=action_transaction_authority,
        brain_event_inbox=brain_event_inbox,
        trajectory_key=str(persistence.get("trajectory_key") or ""),
        original=original,
        horizon=horizon,
        evidence=evidence,
        layered_feedback=layered_feedback,
        ts=round(float(ts), 3),
    )
    return {
        "horizon_s": int(horizon),
        "label": str(label or "neutral"),
        "evidence_label": evidence_label,
        "reason": str(reason or ""),
        "code": referee_code,
        "terminal": False,
        "memory_admitted": False,
        "recorded": True,
        "brain_event": event,
        "trajectory_key": str(persistence.get("trajectory_key") or ""),
        "action_root_id": str(event.get("action_root_id") or ""),
        "grade": int(event.get("grade", 0) or 0),
        "grade_receipt": dict(event.get("grade_receipt") or {}),
    }


def resume_pending_smart_loop_feedback(
    *,
    memory: Any,
    action_transaction_authority: Any,
    brain_event_inbox: Any,
    ts: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trajectory in pending_trajectory_projection_entries(memory):
        payload = (
            dict(trajectory.get("projection_payload") or {})
            if isinstance(trajectory.get("projection_payload"), dict)
            else {}
        )
        record_raw = payload.get("record")
        feedback = (
            dict(payload.get("feedback") or {})
            if isinstance(payload.get("feedback"), dict)
            else {}
        )
        if not isinstance(record_raw, dict) or not feedback:
            continue
        try:
            original = dict_to_record(record_raw)
        except (TypeError, ValueError):
            continue
        results.append(
            _publish_persisted_feedback(
                memory=memory,
                action_transaction_authority=action_transaction_authority,
                brain_event_inbox=brain_event_inbox,
                trajectory_key=str(
                    trajectory.get("trajectory_key") or ""
                ),
                original=original,
                horizon=int(trajectory.get("horizon_s", 0) or 0),
                evidence=dict(feedback.get("evidence") or {}),
                layered_feedback=dict(
                    feedback.get("layered_action_feedback") or {}
                ),
                ts=float(trajectory.get("recorded_ts") or ts),
            )
        )
    return results


def _publish_persisted_feedback(
    *,
    memory: Any,
    action_transaction_authority: Any,
    brain_event_inbox: Any,
    trajectory_key: str,
    original: Any,
    horizon: int,
    evidence: dict[str, Any],
    layered_feedback: dict[str, Any],
    ts: float,
) -> dict[str, Any]:
    proof_identity = delayed_reward_proof_identity(
        original=original,
        evidence=evidence,
    )
    if not terminal_layered_action_feedback_is_valid(
        layered_feedback,
        horizon_s=int(horizon),
    ):
        return {
            "trajectory_key": str(trajectory_key or ""),
            "recorded": True,
            "published": False,
            "reason": "persisted_layered_feedback_invalid",
            "action_root_id": str(
                proof_identity.get("action_root_id") or ""
            ),
        }
    label = layered_action_outcome_label(layered_feedback)
    reason = str(layered_feedback.get("reason") or "")
    referee_code = str(
        staged_roi_action_outcome_contract(label, reason).get("code") or ""
    )
    grade_receipt = {
        "owner": "arch4.brain.proof_feedback_owner",
        "horizon_s": int(horizon),
        "label": str(label or "neutral"),
        "reason": str(reason or ""),
        "referee_code": str(referee_code or ""),
        "layered_action_feedback": dict(layered_feedback),
        "evidence": dict(evidence),
    }
    brain_completed = trajectory_projection_is_completed(
        memory,
        trajectory_key=trajectory_key,
        projection="brain",
    )
    event: dict[str, Any] = {"published": True, "duplicate": True}
    if not brain_completed:
        event = publish_smart_loop_feedback_brain_event(
            proof_owner=OWNER,
            brain_event_inbox=brain_event_inbox,
            original=original,
            horizon=horizon,
            evidence=evidence,
            layered_feedback=layered_feedback,
            ts=ts,
        )
        if not bool(event.get("published", False)):
            return {
                "trajectory_key": str(trajectory_key or ""),
                "recorded": True,
                "published": False,
                "reason": str(
                    event.get("reason") or "feedback_event_not_published"
                ),
                "action_root_id": str(
                    proof_identity.get("action_root_id") or ""
                ),
                "grade": int(
                    layered_feedback.get("memory_grade_score", 0) or 0
                ),
                "grade_receipt": grade_receipt,
            }
        complete_trajectory_projection(
            memory,
            trajectory_key=trajectory_key,
            projection="brain",
            receipt=event,
            ts=ts,
        )

    action_projection: dict[str, Any] = {
        "completed": True,
        "duplicate": True,
    }
    if not trajectory_projection_is_completed(
        memory,
        trajectory_key=trajectory_key,
        projection="action_lifecycle",
    ):
        action_projection = project_referee_grade_to_action_lifecycle(
            authority=action_transaction_authority,
            root_id=str(proof_identity.get("action_root_id") or ""),
            horizon_s=int(horizon),
            grade=int(layered_feedback.get("memory_grade_score", 0) or 0),
            reason_receipt=grade_receipt,
            graded_at=float(ts),
        )
        complete_trajectory_projection(
            memory,
            trajectory_key=trajectory_key,
            projection="action_lifecycle",
            receipt=action_projection,
            ts=ts,
        )
    return {
        "trajectory_key": str(trajectory_key or ""),
        "recorded": True,
        "published": True,
        "projection_completed": True,
        "brain_event": event,
        "action_lifecycle_projection": action_projection,
        "action_root_id": str(proof_identity.get("action_root_id") or ""),
        "grade": int(layered_feedback.get("memory_grade_score", 0) or 0),
        "grade_receipt": grade_receipt,
    }


__all__ = [
    "OWNER",
    "record_due_smart_loop_feedback",
    "resume_pending_smart_loop_feedback",
]
