"""Staged-ROI proof record construction owner."""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Mapping, Optional

from arch4.brain.proof_feedback_owner import (
    build_referee_layered_feedback,
    layered_action_outcome_label,
)
from arch4.brain.memory_action_history_owner import _action_evidence_scope
from arch4.brain.proof_referee_context_owner import delayed_reward_proof_identity
from arch4.brain.proof_referee_evidence import (
    staged_roi_action_outcome_contract,
    staged_roi_learning_grade_actionable,
)
OWNER = "arch4.brain.proof_referee_record_builder_owner"


def build_staged_roi_record(
    *,
    proof_owner: str,
    original: Any,
    modem_now: Optional[CellularIntelligenceGeneration],
    horizon: int,
    label: str,
    reason: str,
    evidence: Mapping[str, Any],
    ts: float,
) -> tuple[Any, dict[str, Any]]:
    params = dict(getattr(original, "action_params", {}) or {})
    params["_roi_parent_trace_id"] = str(getattr(original, "decision_trace_id", "") or "")
    params["_roi_horizon_s"] = int(horizon)
    layered_feedback = build_referee_layered_feedback(
        label=str(label or "neutral"),
        reason=reason,
        evidence=evidence,
        horizon_s=int(horizon),
    )
    canonical_label = layered_action_outcome_label(layered_feedback)
    outcome_contract = staged_roi_action_outcome_contract(
        canonical_label,
        reason,
    )
    contract_label = str(
        outcome_contract.get("learning_outcome_label", "") or "neutral"
    )
    params["_layered_feedback_schema"] = str(layered_feedback.get("schema") or "")
    params["_layered_feedback_owner"] = str(layered_feedback.get("owner") or "")
    params["_layered_feedback_learning_label"] = str(layered_feedback.get("learning_label") or "")
    params["_layered_feedback_grade_score"] = int(layered_feedback.get("memory_grade_score", 0) or 0)
    proof_identity = delayed_reward_proof_identity(
        original=original,
        modem_now=modem_now,
        evidence=evidence,
    )
    if not params.get("bands") and proof_identity.get("target_post_action_bands"):
        params["bands"] = list(proof_identity["target_post_action_bands"])
    strict_positive = bool(
        params.get("_ca_pool_keep_rule")
        in {"keep_only_if_all_grade_windows_positive", "keep_only_if_single_grade_positive"}
        or params.get("_ca_pool_expansion_trial")
    )
    learning_success = (contract_label == "good") if strict_positive else (contract_label != "bad")
    referee_code = str(outcome_contract.get("code", "") or "")
    referee_level = str(outcome_contract.get("level", "") or "")
    outcome_scope: dict[str, Any] = {}
    if modem_now is not None:
        try:
            outcome_scope = _action_evidence_scope(
                modem_now,
                lever=getattr(original, "lever", None),
                params=params,
            )
        except (TypeError, ValueError):
            outcome_scope = {}
    record = type(original)(
        timestamp=ts,
        sinr_db=float(getattr(original, "sinr_db", 0.0) or 0.0),
        rsrq_db=float(getattr(original, "rsrq_db", 0.0) or 0.0),
        mcs_dl=int(getattr(original, "mcs_dl", 0) or 0),
        ca_signature=str(getattr(original, "ca_signature", "") or ""),
        rtt_sliding_avg=float(getattr(original, "rtt_sliding_avg", 0.0) or 0.0),
        rtt_ms=float(getattr(original, "rtt_ms", 0.0) or 0.0),
        lever=getattr(original, "lever", None),
        action_params=params,
        success=learning_success,
        throughput_before_mbps=float(getattr(original, "throughput_before_mbps", 0.0) or 0.0),
        throughput_after_mbps=float(evidence.get("tp_now", 0.0) or 0.0),
        sinr_after=float(evidence.get("sinr_now", 0.0) or 0.0),
        ca_signature_after=(
            str(modem_now.cellular.ca_band_signature or "")
            if modem_now is not None
            else ""
        ),
        improvement=float(evidence.get("tp_now", 0.0) or 0.0)
        - float(getattr(original, "throughput_before_mbps", 0.0) or 0.0),
        provider_plmn=str(getattr(original, "provider_plmn", "") or ""),
        provider_name=str(getattr(original, "provider_name", "") or ""),
        outcome_label=contract_label,
        hard_fail=bool(outcome_contract.get("hard_fail", False)),
        calibration_marker=str(getattr(original, "calibration_marker", "") or ""),
        calibration_signature=str(getattr(original, "calibration_signature", "") or ""),
        incident_class=str(evidence.get("incident_class", "") or ""),
        incident_next_step="",
        incident_self_induced=bool(evidence.get("self_induced", False)),
        learning_policy_reason=f"staged_roi:{horizon}s:{reason}",
        referee_outcome_level=referee_level,
        referee_outcome_code=referee_code,
        outcome_verdict_stage="staged_roi",
        outcome_verdict_authority="referee",
        outcome_verdict_triggered_by=proof_owner,
        learning_commit_stage="learning_commit",
        learning_commit_authority="memory_store",
        learning_commit_triggered_by=proof_owner,
        learning_ltm_applied=staged_roi_learning_grade_actionable(outcome_contract),
        learning_commit_status=(
            "classified"
            if staged_roi_learning_grade_actionable(outcome_contract)
            else "skipped"
        ),
        objective_metadata={
            "staged_roi_evidence_label": str(label or "neutral"),
            "staged_roi_evidence": dict(evidence),
            "layered_action_feedback": dict(layered_feedback),
            "delayed_reward_proof_identity": dict(proof_identity),
            "selected_action_command_id": str(proof_identity.get("selected_action_command_id") or ""),
            "selected_action_proof_id": str(proof_identity.get("selected_action_proof_id") or ""),
            "smart_loop_reason_ref": str(proof_identity.get("smart_loop_reason_ref") or ""),
            "smart_loop_context_key": str(proof_identity.get("smart_loop_context_key") or ""),
            "staged_roi_grade_policy": str(
                dict(getattr(original, "objective_metadata", {}) or {}).get("staged_roi_grade_policy", "") or ""
            ),
        },
        decision_trace_id=f"{getattr(original, 'decision_trace_id', '')}:roi:{horizon}",
        proposal_intent=str(getattr(original, "proposal_intent", "") or ""),
        proposal_objective_id=str(getattr(original, "proposal_objective_id", "") or ""),
        proposal_reason=str(getattr(original, "proposal_reason", "") or ""),
        proposal_score=float(getattr(original, "proposal_score", 0.0) or 0.0),
        proposal_action_origin=str(
            getattr(original, "proposal_action_origin", "") or ""
        ),
        proposal_stage=str(getattr(original, "proposal_stage", "") or ""),
        proposal_authority=str(getattr(original, "proposal_authority", "") or ""),
        proposal_triggered_by=str(getattr(original, "proposal_triggered_by", "") or ""),
        execution_status="staged_roi",
        execution_stage="staged_roi",
        execution_authority=proof_owner,
        execution_triggered_by=proof_owner,
        cq_cell=float(evidence.get("cq_now", 0.0) or 0.0),
        learning_gate=dict(getattr(original, "learning_gate", {}) or {}),
        roi_horizon_s=int(horizon),
        roi_parent_trace_id=str(getattr(original, "decision_trace_id", "") or ""),
        baseline_evidence_scope=dict(
            getattr(original, "baseline_evidence_scope", {}) or {}
        ),
        evidence_scope=outcome_scope,
    )
    record.objective_metadata["delayed_reward_proof_identity"] = delayed_reward_proof_identity(
        original=original,
        record=record,
        modem_now=modem_now,
        evidence=evidence,
    )
    return record, outcome_contract




__all__ = ["OWNER", "build_staged_roi_record"]
