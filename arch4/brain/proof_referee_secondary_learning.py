"""Secondary-learning adapter for delayed proof/referee closure."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
from typing import Any, Mapping, Optional

from arch4.action_outcome_contract import action_outcome_contract_from_staged_roi
from arch4.brain.delivered_load_memory import build_dlst_from_computed
from arch4.brain.proof_feedback_owner import terminal_layered_action_feedback_is_valid
from arch4.brain.proof_referee_context_owner import delayed_reward_proof_identity, int_or_zero
from arch4.brain.secondary_learning_receipt import secondary_learning_receipt_from_mapping
from arch4.brain.reward_ledger import admit_terminal_reward
from arch4.terminal_reward_observation import TerminalRewardObservation


log = logging.getLogger("arch4.brain.proof_referee_secondary_learning")

OWNER = "arch4.brain.proof_referee_secondary_learning"


def commit_staged_roi_secondary_learning(
    *,
    proof_owner: str,
    logger: Any,
    memory: Any,
    historical: Any,
    crystal_ball: Any,
    record: Any,
    original: Any,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    label: str,
    reason: str,
    referee_code: str,
    evidence: dict[str, Any],
    ts: float,
    learning_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit secondary learning surfaces for one already graded ROI proof."""

    outcome_contract = action_outcome_contract_from_staged_roi(label=label, reason=reason)
    referee_level = str(outcome_contract.get("level", "") or "")
    result: dict[str, Any] = {
        "schema": "staged_roi_secondary_learning.v1",
        "owner": OWNER,
        "proof_owner": str(proof_owner or ""),
        "label": str(label or ""),
        "reason": str(reason or ""),
        "referee_code": str(referee_code or ""),
        "referee_level": referee_level,
        "memory_committed": False,
        "historical_committed": False,
        "crystal_ball_updated": False,
        "crystal_ball_regraded": False,
    }
    record_meta = dict(getattr(record, "objective_metadata", {}) or {})
    layered_feedback = dict(record_meta.get("layered_action_feedback") or {})
    terminal_grade_score = int_or_zero(layered_feedback.get("memory_grade_score"))
    result["terminal_grade_score"] = max(-3, min(3, terminal_grade_score))
    if not terminal_layered_action_feedback_is_valid(
        layered_feedback,
        horizon_s=int_or_zero(getattr(record, "roi_horizon_s", 0)),
    ):
        result.update({"committed": False, "reason": "terminal_layered_reward_package_invalid"})
        return _store_secondary_receipt(record, result)
    if layered_feedback.get("context_clean") is not True:
        result.update({"committed": False, "reason": "terminal_layered_reward_context_quarantined"})
        return _store_secondary_receipt(record, result)
    proof_identity = delayed_reward_proof_identity(
        original=original,
        record=record,
        modem_now=modem_now,
        evidence=evidence,
    )
    result["proof_identity"] = dict(proof_identity)
    if not bool(proof_identity.get("exact_identity_complete", False)):
        result.update({"committed": False, "reason": "terminal_proof_identity_incomplete"})
        return _store_secondary_receipt(record, result)
    if not _learning_gate_allowed(learning_gate):
        result.update(
            {
                "committed": False,
                "reason": str((learning_gate or {}).get("block_reason") or "learning_gate_blocked"),
                "learning_gate": dict(learning_gate or {}),
            }
        )
        return _store_secondary_receipt(record, result)
    try:
        terminal_observation = TerminalRewardObservation.capture(
            modem_now,
            computed_now,
        )
    except (TypeError, ValueError) as exc:
        result.update(
            {
                "committed": False,
                "reason": f"terminal_observation_invalid:{exc}",
            }
        )
        return _store_secondary_receipt(record, result)
    record.DLST = build_dlst_from_computed(
        modem_now,
        computed_now,
        provider_plmn=str(modem_now.identity.provider_plmn or ""),
        now=float(ts),
    )

    reward_receipt = admit_terminal_reward(
        memory,
        record=record,
        proof_identity=proof_identity,
        terminal_observation=terminal_observation.as_dict(),
        layered_feedback=layered_feedback,
        evidence=evidence,
        ts=ts,
    )
    result.update(
        {
            "reward_admitted": bool(reward_receipt.get("admitted", False)),
            "reward_duplicate": bool(reward_receipt.get("duplicate", False)),
            "reward_ledger_key": str(reward_receipt.get("ledger_key") or ""),
            "reward_ledger_reason": str(reward_receipt.get("reason") or ""),
            "reward_ledger_error": str(reward_receipt.get("error") or ""),
        }
    )
    record_meta["reward_ledger_receipt"] = dict(reward_receipt)
    record.objective_metadata = record_meta
    canonical_admitted = bool(
        reward_receipt.get("admitted", False) or reward_receipt.get("duplicate", False)
    )
    result["reward_admitted"] = canonical_admitted
    result["reward_delivery_new"] = bool(reward_receipt.get("admitted", False))
    if not canonical_admitted:
        # A rejected entry may have a deterministic candidate hash for
        # diagnostics, but RewardLedger is the only owner of a canonical
        # reward_ledger_key.  Brain terminal-rejection receipts must not carry
        # an unadmitted key.
        result["reward_ledger_key"] = ""
        record.learning_ltm_applied = False
        record.learning_commit_status = (
            "duplicate_reward" if reward_receipt.get("duplicate") is True else "blocked"
        )
        result.update(
            {
                "committed": False,
                "reason": str(reward_receipt.get("reason") or "terminal_reward_not_admitted"),
            }
        )
        return _store_secondary_receipt(record, result)
    if not _learning_grade_actionable(outcome_contract):
        record.learning_ltm_applied = False
        record.learning_commit_status = "reward_committed"
    result.update(
        {
            "committed": True,
            "reason": "reward_projection_pending",
        }
    )
    return _store_secondary_receipt(record, result)


def _store_secondary_receipt(record: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    receipt = secondary_learning_receipt_from_mapping(result)
    meta_raw = getattr(record, "objective_metadata", {}) or {}
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    meta["secondary_learning"] = dict(receipt)
    record.objective_metadata = meta
    return receipt


def _learning_gate_allowed(learning_gate: Mapping[str, Any]) -> bool:
    return bool(isinstance(learning_gate, Mapping) and learning_gate.get("learning_allowed") is True)


def _learning_grade_actionable(outcome_contract: Mapping[str, Any]) -> bool:
    contract = outcome_contract if isinstance(outcome_contract, Mapping) else {}
    label_norm = str(contract.get("learning_outcome_label", "") or "").strip().lower()
    return bool(contract.get("learning_ltm_allowed", False)) and label_norm in {"good", "bad"}


__all__ = ["OWNER", "commit_staged_roi_secondary_learning"]
