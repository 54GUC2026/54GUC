"""Brain owner for proposal and intent ActionRecord contract stamping."""

from __future__ import annotations

from typing import Any

from arch4.brain.tick_contract import ProposalOutcome
from arch4.action_admission import selected_action_context_from_action
from arch4.interfaces import LeverAction

OWNER = "arch4.brain.proposal_contract_owner"


def build_proposal_result(
    *,
    decision_trace_id: str,
    action: LeverAction,
) -> dict:
    """Build proposal result dict for extra contract."""
    return ProposalOutcome.from_runtime(
        decision_trace_id=decision_trace_id,
        action=action,
    ).to_dict()


def apply_intent_contract_to_action_record(
    *,
    action_record: Any,
    intent_verdict: dict,
) -> None:
    """Stamp intent-verdict fields onto an ActionRecord."""
    action_record.bouncer_intent_level = str(intent_verdict.get("level", "") or "")
    action_record.bouncer_intent_code = str(intent_verdict.get("code", "") or "")
    action_record.intent_verdict_stage = str(intent_verdict.get("stage", "") or "")
    action_record.intent_verdict_authority = str(intent_verdict.get("authority", "") or "")
    action_record.intent_verdict_triggered_by = str(intent_verdict.get("triggered_by", "") or "")


def apply_proposal_contract_to_action_record(
    *,
    action_record: Any,
    action: Any,
    proposal_result: dict,
) -> None:
    """Stamp proposal fields onto an ActionRecord from action + proposal result."""
    action_record.proposal_reason = str(getattr(action, "reason", "") or "")
    action_record.proposal_intent = str(getattr(action, "intent", "") or "")
    action_record.proposal_objective_id = str(getattr(action, "objective_id", "") or "")
    action_record.proposal_score = float(getattr(action, "score", 0.0) or 0.0)
    action_record.proposal_action_origin = (
        selected_action_context_from_action(action).origin.kind.value
    )
    action_record.proposal_stage = str(proposal_result.get("stage", "") or "")
    action_record.proposal_authority = str(proposal_result.get("authority", "") or "")
    action_record.proposal_triggered_by = str(proposal_result.get("triggered_by", "") or "")


__all__ = [
    "OWNER",
    "apply_intent_contract_to_action_record",
    "apply_proposal_contract_to_action_record",
    "build_proposal_result",
]
