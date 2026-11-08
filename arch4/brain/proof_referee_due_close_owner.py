"""Staged-ROI due-window close owner."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.brain.proof_referee_brain_event_owner import publish_staged_roi_brain_event
from arch4.brain.proof_referee_evidence import staged_roi_label
from arch4.brain.proof_referee_parent_close_owner import close_staged_roi_parent
from arch4.brain.proof_referee_record_builder_owner import build_staged_roi_record
from arch4.brain.proof_referee_projection_outbox_owner import project_reward_outbox_entry
from arch4.brain.proof_referee_recording_event_owner import log_action_roi_evidence_event
from arch4.brain.proof_referee_secondary_learning import commit_staged_roi_secondary_learning
from arch4.brain.proof_referee_window_owner import assert_staged_roi_parent_open
from arch4.brain.proof_memory_store import record_staged_roi_action_record
from arch4.brain.proof_memory_store import (
    update_staged_roi_projection_record,
)
from arch4.brain.reward_ledger import reward_ledger_entry


OWNER = "arch4.brain.proof_referee_due_close_owner"


def close_due_staged_roi_window(
    *,
    logger: Any,
    memory: Any,
    action_transaction_authority: Any,
    historical: Any,
    crystal_ball: Any,
    brain_event_inbox: Any,
    bandit_state_store: Any = None,
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
    record, outcome_contract = build_staged_roi_record(
        proof_owner=OWNER,
        original=original,
        modem_now=modem_now,
        horizon=horizon,
        label=label,
        reason=reason,
        evidence=evidence,
        ts=ts,
    )
    evidence_label = str(label or "neutral")
    label = str(
        outcome_contract.get("learning_outcome_label") or "neutral"
    )
    referee_code = str(outcome_contract.get("code", "") or "")
    child_persistence = record_staged_roi_action_record(memory, record)
    if not bool(child_persistence.get("stored", False)):
        return {
            "horizon_s": int(horizon),
            "label": label,
            "reason": str(
                child_persistence.get("reason")
                or "terminal_child_not_persisted"
            ),
            "code": referee_code,
            "terminal": True,
            "reward_admitted": False,
            "parent_closed": False,
        }
    secondary = commit_staged_roi_secondary_learning(
        proof_owner=OWNER,
        logger=logger,
        memory=memory,
        historical=historical,
        crystal_ball=crystal_ball,
        record=record,
        original=original,
        modem_now=modem_now,
        computed_now=computed_now,
        label=label,
        reason=reason,
        referee_code=referee_code,
        evidence=evidence,
        ts=ts,
        learning_gate=dict(getattr(record, "learning_gate", {}) or {}),
    )
    ledger_key = str(secondary.get("reward_ledger_key") or "")
    ledger_row = reward_ledger_entry(memory, ledger_key)
    if ledger_row:
        projection = project_reward_outbox_entry(
            memory=memory,
            historical=historical,
            crystal_ball=crystal_ball,
            brain_event_inbox=brain_event_inbox,
            logger=logger,
            action_transaction_authority=action_transaction_authority,
            bandit_state_store=bandit_state_store,
            ledger_entry=ledger_row,
            original=original,
            record=record,
            ts=ts,
        )
    else:
        brain_event = publish_staged_roi_brain_event(
            proof_owner=OWNER,
            brain_event_inbox=brain_event_inbox,
            original=original,
            record=record,
            horizon=horizon,
            label=label,
            reason=reason,
            referee_code=referee_code,
            secondary=secondary,
            evidence=evidence,
            ts=ts,
        )
        evidence_event = log_action_roi_evidence_event(
            logger=logger,
            original=original,
            record=record,
            params=dict(getattr(original, "action_params", {}) or {}),
            horizon=horizon,
            reason=reason,
            evidence=evidence,
        )
        projection = {
            "complete": False,
            "reason": "terminal_reward_not_admitted",
            "brain_event": brain_event,
            "evidence_event": evidence_event,
        }
    if isinstance(projection.get("secondary_learning"), dict):
        secondary = dict(projection["secondary_learning"])
        record.objective_metadata = {
            **dict(getattr(record, "objective_metadata", {}) or {}),
            "secondary_learning": secondary,
        }
    update_staged_roi_projection_record(memory, record, ts=ts)
    projection_complete = bool(projection.get("complete", False))
    done.add(int(horizon))
    meta["staged_roi_done_s"] = sorted(done)
    original.objective_metadata = meta
    assert_staged_roi_parent_open(
        original, source="close_staged_roi_parent"
    )
    close_staged_roi_parent(
        proof_owner=OWNER,
        logger=logger,
        memory=memory,
        original=original,
        horizon=horizon,
        label=label,
        reason=reason,
        referee_code=referee_code,
        evidence=evidence,
        ts=ts,
    )
    result = {
        "horizon_s": int(horizon),
        "label": label,
        "evidence_label": evidence_label,
        "reason": reason,
        "code": referee_code,
        "terminal": True,
        "reward_admitted": bool(
            secondary.get("reward_admitted", False)
        ),
        "parent_closed": True,
        "reward_projection_complete": projection_complete,
        "action_root_id": str(
            dict(
                dict(getattr(record, "objective_metadata", {}) or {}).get(
                    "delayed_reward_proof_identity"
                )
                or {}
            ).get("action_root_id")
            or ""
        ),
        "grade": int(
            dict(
                dict(getattr(record, "objective_metadata", {}) or {}).get(
                    "layered_action_feedback"
                )
                or {}
            ).get("memory_grade_score", 0)
            or 0
        ),
        "grade_receipt": {
            "owner": "arch4.brain.proof_feedback_owner",
            "horizon_s": int(horizon),
            "label": str(label or "neutral"),
            "reason": str(reason or ""),
            "referee_code": referee_code,
            "layered_action_feedback": dict(
                dict(getattr(record, "objective_metadata", {}) or {}).get(
                    "layered_action_feedback"
                )
                or {}
            ),
            "evidence": dict(evidence),
        },
    }
    surface_present = _secondary_surface_present(
        brain_event_inbox=brain_event_inbox,
        historical=historical,
        crystal_ball=crystal_ball,
    )
    if surface_present:
        result["secondary_learning"] = secondary
        result["reward_projection"] = projection
    brain_event = projection.get("brain_event")
    if not isinstance(brain_event, dict):
        brain_event = dict((projection.get("receipts") or {}).get("brain") or {})
    if isinstance(brain_event, dict) and brain_event.get("published") is True:
        result["brain_event"] = dict(brain_event)
    return result


def _secondary_surface_present(*, brain_event_inbox: Any, historical: Any, crystal_ball: Any) -> bool:
    return bool(
        brain_event_inbox is not None
        or historical is not None
        or crystal_ball is not None
    )


__all__ = ["OWNER", "close_due_staged_roi_window"]
