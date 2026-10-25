"""Action-history Memory owner.

Memory is the durable container. This owner builds owner-stamped
``ActionRecord`` rows, appends them to the action-history store, journals them,
and answers action-history similarity queries.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import math
from typing import Any, Dict, Iterable, List, Optional

from arch4.brain.memory_calibration_context_owner import signature_compact_str
from arch4.brain.memory_delivered_load_owner import dlst_from_modem_computed
from arch4.brain.memory_io import record_to_dict
from arch4.evidence_scope import (
    EvidenceScope,
    FormulaVector,
    evidence_scope_from_mapping,
)
from arch4.interfaces import (
    ARCH_QUALITY_FORMULA_EPOCH,
    ActionRecord,
    LeverType,
)
from arch4.learning_gate_contract import LearningGateDecision
from arch4.policy.incident_action_policy import incident_class_name, incident_next_step

OWNER = "arch4.brain.memory_action_history_owner"
ACTION_ROI_FORMULA_EPOCH = "layered_roi_minus3_plus3_v1"


def action_roi_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "action_roi": ACTION_ROI_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def _action_evidence_scope(
    modem: CellularIntelligenceGeneration,
    *,
    lever: LeverType,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    return evidence_scope_from_mapping(
        {
            "identity_code": str(getattr(modem.identity, 'identity_code', "") or ""),
            "identity_status": str(getattr(modem.identity, 'identity_status', "") or ""),
            "identity_catalog_version": str(
                getattr(modem.identity, 'identity_catalog_version', "") or ""
            ),
            "identity_catalog_checksum": str(
                getattr(modem.identity, 'identity_catalog_checksum', "") or ""
            ),
            "session_scope_key": str(
                getattr(modem.generation, 'session_scope_key', "") or ""
            ),
            "source_epoch": str(getattr(modem.generation, 'source_epoch', "") or ""),
            "source_sequence": int(getattr(modem.generation, 'generation', 0) or 0),
            "sample_ts": float(getattr(modem.generation, 'sample_ts', 0.0) or 0.0),
            "sample_age_s": float(getattr(modem.generation, 'sample_age_s', 0.0) or 0.0),
            "provider_plmn": str(getattr(modem.identity, 'provider_plmn', "") or ""),
            "access_type": str(getattr(modem.identity, 'access_type', "") or ""),
            "serving_band": int(getattr(modem.cellular, 'serving_band', 0) or 0),
            "earfcn": int(getattr(modem.cellular, 'earfcn_dl', 0) or 0),
            "pci": int(getattr(modem.cellular, 'pci', 0) or 0),
            "action_shape_key": (
                f"{lever.name}:"
                f"{sorted((str(key), repr(value)) for key, value in params.items())}"
            ),
        },
        formula_vector=action_roi_formula_vector(),
    ).as_dict()


def iter_action_history_reversed(memory: Any) -> Iterable[ActionRecord]:
    """Read action history through the canonical owner/store boundary."""

    if memory is None:
        return ()
    store = getattr(memory, "_action_history_store", None)
    if store is None:
        return ()
    current_raw = getattr(memory, "_current_action_evidence_scope", {})
    try:
        current = EvidenceScope.from_mapping(dict(current_raw or {}))
    except (TypeError, ValueError):
        return ()
    return (
        record
        for record in store.reversed()
        if _record_matches_runtime_scope(record, current)
    )


def action_history_records(memory: Any, *, newest_first: bool = False) -> List[ActionRecord]:
    """Return a bounded store snapshot without exposing Memory's container field."""

    if memory is None:
        return []
    store = getattr(memory, "_action_history_store", None)
    if store is None:
        return []
    records = list(iter_action_history_reversed(memory))
    records.reverse()
    if newest_first:
        records.reverse()
    return records


def record_action_history(
    memory: Any,
    modem_before: CellularIntelligenceGeneration,
    computed_before: DerivedSignalFrames,
    lever: LeverType,
    params: Dict[str, Any],
    success: bool,
    modem_after: Optional[CellularIntelligenceGeneration],
    computed_after: Optional[DerivedSignalFrames],
    *,
    provider_plmn: str = "",
    provider_name: str = "",
    incident_assessment: Optional[Dict[str, Any]] = None,
    verification_status: str = "",
    verification_note: str = "",
    verification_stage: str = "verification_result",
    verification_authority: str = "engine_verifier",
    verification_triggered_by: str = "engine",
    bouncer_intent: Optional[Dict[str, Any]] = None,
    referee_outcome: Optional[Dict[str, Any]] = None,
    learning_commit_stage: str = "learning_commit",
    learning_commit_authority: str = "memory_store",
    learning_commit_triggered_by: str = "engine",
    learning_commit_status: str = "recorded",
    learning_gate: Optional[Dict[str, Any]] = None,
    proposal_reason: str = "",
    proposal_intent: str = "",
    proposal_objective_id: str = "",
    proposal_score: float = 0.0,
    proposal_action_origin: str,
    proof_observed_at: float,
    proposal_stage: str = "proposal",
    proposal_authority: str = "brain",
    proposal_triggered_by: str = "engine",
    execution_status: str = "",
    execution_error: str = "",
    execution_stage: str = "execution_result",
    execution_authority: str = "engine_executor",
    execution_triggered_by: str = "engine",
    decision_trace_id: str = "",
    defer_learning_commit: bool = False,
) -> ActionRecord:
    """Build and store one action-history row through the legal owner."""

    try:
        proof_timestamp = float(proof_observed_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("action_history_proof_timestamp_invalid") from exc
    if not math.isfinite(proof_timestamp) or proof_timestamp <= 0.0:
        raise ValueError("action_history_proof_timestamp_required")

    action_origin = str(proposal_action_origin or "").strip()
    if action_origin not in {
        "smart_loop",
        "fault_recovery",
        "operator_ngui",
        "operator_sms",
        "schedule",
        "watchdog",
        "lease_expiry",
    }:
        raise ValueError(
            f"action_history_proposal_origin_invalid:{action_origin or 'missing'}"
        )

    tp_before = float(modem_before.traffic.dl_mbps or 0.0)
    tp_after = float(modem_after.traffic.dl_mbps or 0.0) if modem_after else 0.0
    sinr_after = modem_after.cellular.sinr_db if modem_after else 0.0
    ca_after = modem_after.cellular.ca_band_signature if modem_after else "none"
    dlst_after = dlst_from_modem_computed(memory, modem_after, computed_after)
    incident_assessment = dict(incident_assessment or {})
    bouncer_intent = dict(bouncer_intent or {})
    referee_outcome = dict(referee_outcome or {})
    gate_decision = _learning_gate_decision(lever, learning_gate)
    learning_gate_dict = gate_decision.as_dict()
    scope_error = ""
    baseline_scope: Dict[str, Any] = {}
    outcome_scope: Dict[str, Any] = {}
    try:
        baseline_scope = _action_evidence_scope(
            modem_before,
            lever=lever,
            params=params,
        )
        outcome_scope = _action_evidence_scope(
            modem_after or modem_before,
            lever=lever,
            params=params,
        )
    except (TypeError, ValueError) as exc:
        scope_error = str(exc)
    if outcome_scope:
        memory._current_action_evidence_scope = dict(outcome_scope)

    record = ActionRecord(
        timestamp=proof_timestamp,
        sinr_db=modem_before.cellular.sinr_db,
        rsrq_db=modem_before.cellular.rsrq_db,
        mcs_dl=modem_before.cellular.mcs_dl,
        ca_signature=modem_before.cellular.ca_band_signature,
        rtt_sliding_avg=computed_before.latency.rtt_sliding_avg,
        rtt_ms=computed_before.latency.rtt_ms,
        lever=lever,
        action_params=params,
        success=success,
        throughput_before_mbps=tp_before,
        throughput_after_mbps=tp_after,
        sinr_after=sinr_after,
        ca_signature_after=ca_after,
        improvement=tp_after - tp_before,
        provider_plmn=provider_plmn or memory.current_provider_context(),
        provider_name=provider_name or "",
        outcome_label="",
        hard_fail=False,
        calibration_marker=str(memory.current_calibration_marker or "C0"),
        calibration_signature=signature_compact_str(memory.current_calibration_signature),
        incident_class=incident_class_name(incident_assessment),
        incident_next_step=incident_next_step(incident_assessment),
        incident_self_induced=bool(incident_assessment.get("self_induced", False)),
        learning_ltm_applied=False,
        learning_policy_reason="pending_referee_classification",
        verification_status=str(verification_status or ""),
        verification_note=str(verification_note or ""),
        verification_stage=str(verification_stage or "verification_result"),
        verification_authority=str(verification_authority or "engine_verifier"),
        verification_triggered_by=str(verification_triggered_by or "engine"),
        bouncer_intent_level=str(bouncer_intent.get("level", "") or ""),
        bouncer_intent_code=str(bouncer_intent.get("code", "") or ""),
        intent_verdict_stage=str(bouncer_intent.get("stage", "intent_verdict") or "intent_verdict"),
        intent_verdict_authority=str(bouncer_intent.get("authority", "bouncer") or "bouncer"),
        intent_verdict_triggered_by=str(bouncer_intent.get("triggered_by", "engine") or "engine"),
        referee_outcome_level=str(referee_outcome.get("level", "") or ""),
        referee_outcome_code=str(referee_outcome.get("code", "") or ""),
        outcome_verdict_stage=str(referee_outcome.get("stage", "outcome_verdict") or "outcome_verdict"),
        outcome_verdict_authority=str(referee_outcome.get("authority", "referee") or "referee"),
        outcome_verdict_triggered_by=str(referee_outcome.get("triggered_by", "engine") or "engine"),
        learning_commit_stage=str(learning_commit_stage or "learning_commit"),
        learning_commit_authority=str(learning_commit_authority or "memory_store"),
        learning_commit_triggered_by=str(learning_commit_triggered_by or "engine"),
        learning_commit_status="pending",
        learning_gate=learning_gate_dict,
        proposal_reason=str(proposal_reason or ""),
        proposal_intent=str(proposal_intent or ""),
        proposal_objective_id=str(proposal_objective_id or ""),
        proposal_score=float(proposal_score or 0.0),
        proposal_action_origin=action_origin,
        proposal_stage=str(proposal_stage or "proposal"),
        proposal_authority=str(proposal_authority or "brain"),
        proposal_triggered_by=str(proposal_triggered_by or "engine"),
        execution_status=str(execution_status or ""),
        execution_error=str(execution_error or ""),
        execution_stage=str(execution_stage or "execution_result"),
        execution_authority=str(execution_authority or "engine_executor"),
        execution_triggered_by=str(execution_triggered_by or "engine"),
        decision_trace_id=str(decision_trace_id or ""),
        cq_cell=float(getattr(computed_after.quality, 'cq_cell', 0.0) or 0.0) if computed_after else 0.0,
        DLST=dlst_after,
        baseline_evidence_scope=baseline_scope,
        evidence_scope=outcome_scope,
        objective_metadata={
            "delayed_reward_baseline": {
                "schema": "delayed_reward_baseline.v1",
                "owner": OWNER,
                "cq_before": float(getattr(computed_before.quality, 'cq_cell', 0.0) or 0.0),
                "congestion_before": float(
                    getattr(computed_before.quality, 'congestion_pct', 0.0) or 0.0
                ),
                "congestion_before_valid": bool(
                    getattr(computed_before.quality, 'congestion_pct_valid', False)
                ),
            }
            ,
            "evidence_scope_admission": {
                "accepted": not bool(scope_error),
                "reason": scope_error,
            },
        },
    )
    admitted = bool(memory._append_action_history_record(record))
    if not admitted:
        return record
    memory._append_journal_event(
        {
            "kind": "action_record",
            "ts": float(record.timestamp),
            "record": record_to_dict(record),
        }
    )
    memory._mark_dirty(events=1)
    return record


def get_similar_actions(
    memory: Any,
    lever: LeverType,
    sinr_range: tuple = (-999, 999),
    ca_sig: Optional[str] = None,
    limit: int = 10,
) -> List[ActionRecord]:
    """Find past actions under similar conditions."""

    results: List[ActionRecord] = []
    provider = memory.current_provider_context()
    for rec in iter_action_history_reversed(memory):
        if provider and getattr(rec, "provider_plmn", ""):
            if rec.provider_plmn != provider:
                continue
        if rec.lever != lever:
            continue
        if not (sinr_range[0] <= rec.sinr_db <= sinr_range[1]):
            continue
        if ca_sig and rec.ca_signature != ca_sig:
            continue
        results.append(rec)
        if len(results) >= limit:
            break
    return results


def _record_matches_runtime_scope(
    record: ActionRecord,
    current: EvidenceScope,
) -> bool:
    try:
        baseline = EvidenceScope.from_mapping(
            dict(getattr(record, "baseline_evidence_scope", {}) or {})
        )
        outcome = EvidenceScope.from_mapping(
            dict(getattr(record, "evidence_scope", {}) or {})
        )
    except (TypeError, ValueError):
        return False
    return bool(
        baseline.same_runtime_namespace(current)
        and outcome.same_runtime_namespace(current)
    )


def _learning_gate_decision(
    lever: LeverType,
    learning_gate: Optional[Dict[str, Any]],
) -> LearningGateDecision:
    if isinstance(learning_gate, dict) and learning_gate:
        return LearningGateDecision.from_mapping(learning_gate)
    if lever == LeverType.BAND_SELECTION:
        return LearningGateDecision(
            learning_allowed=False,
            block_reason="learning_gate_blocked",
            gate_input_complete=False,
            missing_inputs=("learning_gate",),
        )
    return LearningGateDecision(learning_allowed=True)


__all__ = [
    "OWNER",
    "action_roi_formula_vector",
    "action_history_records",
    "get_similar_actions",
    "iter_action_history_reversed",
    "record_action_history",
]
