"""
arch4/brain/memory_io.py
───────────────────────────────────────────────────────────────────────────────
Pure serialization helpers for Memory persistence.

Extracted from memory.py to reduce its size and make these functions
independently importable by tests and tooling.

All functions are stateless: they take typed objects and return dicts,
or take dicts and return typed objects. No side effects.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Any, Dict

from arch4.interfaces import ActionRecord, BandKnowledge, CellKnowledge, LeverType
from arch4.brain.delivered_load_memory import normalize_dlst


def record_to_dict(r: ActionRecord) -> dict:
    """Serialize an ActionRecord to a JSON-safe dict."""
    return {
        "timestamp": r.timestamp,
        "sinr_db": r.sinr_db, "rsrq_db": r.rsrq_db,
        "mcs_dl": r.mcs_dl, "ca_signature": r.ca_signature,
        "rtt_sliding_avg": r.rtt_sliding_avg, "rtt_ms": r.rtt_ms,
        "lever": r.lever.name, "action_params": r.action_params,
        "success": r.success,
        "throughput_before_mbps": r.throughput_before_mbps,
        "throughput_after_mbps": r.throughput_after_mbps,
        "sinr_after": r.sinr_after,
        "ca_signature_after": r.ca_signature_after,
        "improvement": r.improvement,
        "provider_plmn": getattr(r, "provider_plmn", ""),
        "provider_name": getattr(r, "provider_name", ""),
        "outcome_label": getattr(r, "outcome_label", ""),
        "hard_fail": bool(getattr(r, "hard_fail", False)),
        "calibration_marker": getattr(r, "calibration_marker", ""),
        "calibration_signature": getattr(r, "calibration_signature", ""),
        "incident_class": getattr(r, "incident_class", ""),
        "incident_next_step": getattr(r, "incident_next_step", ""),
        "incident_self_induced": bool(getattr(r, "incident_self_induced", False)),
        "learning_ltm_applied": bool(getattr(r, "learning_ltm_applied", True)),
        "learning_policy_reason": getattr(r, "learning_policy_reason", ""),
        "verification_status": getattr(r, "verification_status", ""),
        "verification_note": getattr(r, "verification_note", ""),
        "verification_stage": getattr(r, "verification_stage", ""),
        "verification_authority": getattr(r, "verification_authority", ""),
        "verification_triggered_by": getattr(r, "verification_triggered_by", ""),
        "bouncer_intent_level": getattr(r, "bouncer_intent_level", ""),
        "bouncer_intent_code": getattr(r, "bouncer_intent_code", ""),
        "intent_verdict_stage": getattr(r, "intent_verdict_stage", ""),
        "intent_verdict_authority": getattr(r, "intent_verdict_authority", ""),
        "intent_verdict_triggered_by": getattr(r, "intent_verdict_triggered_by", ""),
        "outcome_verdict_stage": getattr(r, "outcome_verdict_stage", ""),
        "outcome_verdict_authority": getattr(r, "outcome_verdict_authority", ""),
        "outcome_verdict_triggered_by": getattr(r, "outcome_verdict_triggered_by", ""),
        "learning_commit_stage": getattr(r, "learning_commit_stage", ""),
        "learning_commit_authority": getattr(r, "learning_commit_authority", ""),
        "learning_commit_triggered_by": getattr(r, "learning_commit_triggered_by", ""),
        "learning_commit_status": getattr(r, "learning_commit_status", ""),
        "learning_gate": dict(getattr(r, "learning_gate", {}) or {}),
        "objective_stage": getattr(r, "objective_stage", ""),
        "objective_status": getattr(r, "objective_status", ""),
        "objective_until_ts": float(getattr(r, "objective_until_ts", 0.0) or 0.0),
        "objective_metadata": dict(getattr(r, "objective_metadata", {}) or {}),
        "decision_trace_id": getattr(r, "decision_trace_id", ""),
        "proposal_intent": getattr(r, "proposal_intent", ""),
        "proposal_objective_id": getattr(r, "proposal_objective_id", ""),
        "proposal_reason": getattr(r, "proposal_reason", ""),
        "proposal_score": float(getattr(r, "proposal_score", 0.0) or 0.0),
        "proposal_action_origin": str(
            getattr(r, "proposal_action_origin", "") or ""
        ),
        "proposal_stage": getattr(r, "proposal_stage", ""),
        "proposal_authority": getattr(r, "proposal_authority", ""),
        "proposal_triggered_by": getattr(r, "proposal_triggered_by", ""),
        "execution_status": getattr(r, "execution_status", ""),
        "execution_error": getattr(r, "execution_error", ""),
        "execution_stage": getattr(r, "execution_stage", ""),
        "execution_authority": getattr(r, "execution_authority", ""),
        "execution_triggered_by": getattr(r, "execution_triggered_by", ""),
        "referee_outcome_level": getattr(r, "referee_outcome_level", ""),
        "referee_outcome_code": getattr(r, "referee_outcome_code", ""),
        "cq_cell": float(getattr(r, "cq_cell", 0.0) or 0.0),
        "roi_horizon_s": int(getattr(r, "roi_horizon_s", 0) or 0),
        "roi_parent_trace_id": getattr(r, "roi_parent_trace_id", ""),
        "rat_context": dict(getattr(r, "rat_context", {}) or {}),
        "DLST": normalize_dlst(getattr(r, "DLST", None)),
        "baseline_evidence_scope": dict(
            getattr(r, "baseline_evidence_scope", {}) or {}
        ),
        "evidence_scope": dict(getattr(r, "evidence_scope", {}) or {}),
    }


def dict_to_record(d: dict) -> ActionRecord:
    """Deserialize a durable action record after rejecting old provenance."""

    if "proposal_manual" in d:
        raise ValueError("action_history_boolean_provenance_deleted")
    return ActionRecord(
        timestamp=d["timestamp"],
        sinr_db=d["sinr_db"], rsrq_db=d["rsrq_db"],
        mcs_dl=d["mcs_dl"], ca_signature=d["ca_signature"],
        rtt_sliding_avg=d.get("rtt_sliding_avg", 0.0), rtt_ms=d["rtt_ms"],
        lever=LeverType[d["lever"]],
        action_params=d.get("action_params", {}),
        success=d["success"],
        throughput_before_mbps=d["throughput_before_mbps"],
        throughput_after_mbps=d["throughput_after_mbps"],
        sinr_after=d["sinr_after"],
        ca_signature_after=d["ca_signature_after"],
        improvement=d["improvement"],
        provider_plmn=d.get("provider_plmn", ""),
        provider_name=d.get("provider_name", ""),
        outcome_label=d.get("outcome_label", ""),
        hard_fail=bool(d.get("hard_fail", False)),
        calibration_marker=d.get("calibration_marker", ""),
        calibration_signature=d.get("calibration_signature", ""),
        incident_class=d.get("incident_class", ""),
        incident_next_step=d.get("incident_next_step", ""),
        incident_self_induced=bool(d.get("incident_self_induced", False)),
        learning_ltm_applied=bool(d.get("learning_ltm_applied", True)),
        learning_policy_reason=d.get("learning_policy_reason", ""),
        verification_status=d.get("verification_status", ""),
        verification_note=d.get("verification_note", ""),
        verification_stage=d.get("verification_stage", ""),
        verification_authority=d.get("verification_authority", ""),
        verification_triggered_by=d.get("verification_triggered_by", ""),
        bouncer_intent_level=d.get("bouncer_intent_level", ""),
        bouncer_intent_code=d.get("bouncer_intent_code", ""),
        intent_verdict_stage=d.get("intent_verdict_stage", ""),
        intent_verdict_authority=d.get("intent_verdict_authority", ""),
        intent_verdict_triggered_by=d.get("intent_verdict_triggered_by", ""),
        outcome_verdict_stage=d.get("outcome_verdict_stage", ""),
        outcome_verdict_authority=d.get("outcome_verdict_authority", ""),
        outcome_verdict_triggered_by=d.get("outcome_verdict_triggered_by", ""),
        learning_commit_stage=d.get("learning_commit_stage", ""),
        learning_commit_authority=d.get("learning_commit_authority", ""),
        learning_commit_triggered_by=d.get("learning_commit_triggered_by", ""),
        learning_commit_status=d.get("learning_commit_status", ""),
        learning_gate=dict(d.get("learning_gate") or {}),
        objective_stage=d.get("objective_stage", ""),
        objective_status=d.get("objective_status", ""),
        objective_until_ts=float(d.get("objective_until_ts", 0.0) or 0.0),
        objective_metadata=dict(d.get("objective_metadata") or {}),
        decision_trace_id=d.get("decision_trace_id", ""),
        proposal_intent=d.get("proposal_intent", ""),
        proposal_objective_id=d.get("proposal_objective_id", ""),
        proposal_reason=d.get("proposal_reason", ""),
        proposal_score=float(d.get("proposal_score", 0.0) or 0.0),
        proposal_action_origin=str(d.get("proposal_action_origin") or ""),
        proposal_stage=d.get("proposal_stage", ""),
        proposal_authority=d.get("proposal_authority", ""),
        proposal_triggered_by=d.get("proposal_triggered_by", ""),
        execution_status=d.get("execution_status", ""),
        execution_error=d.get("execution_error", ""),
        execution_stage=d.get("execution_stage", ""),
        execution_authority=d.get("execution_authority", ""),
        execution_triggered_by=d.get("execution_triggered_by", ""),
        referee_outcome_level=d.get("referee_outcome_level", ""),
        referee_outcome_code=d.get("referee_outcome_code", ""),
        cq_cell=float(d.get("cq_cell", 0.0) or 0.0),
        roi_horizon_s=int(d.get("roi_horizon_s", 0) or 0),
        roi_parent_trace_id=d.get("roi_parent_trace_id", ""),
        rat_context=dict(d.get("rat_context") or {}),
        DLST=normalize_dlst(d.get("DLST")),
        baseline_evidence_scope=dict(d.get("baseline_evidence_scope") or {}),
        evidence_scope=dict(d.get("evidence_scope") or {}),
    )


def band_to_dict(bk: BandKnowledge) -> dict:
    """Serialize a BandKnowledge entry."""
    return {
        "times_tried": bk.times_tried,
        "times_appeared_in_ca": bk.times_appeared_in_ca,
        "last_tried": bk.last_tried,
        "last_sinr": bk.last_sinr,
        "last_ca_signature_with": bk.last_ca_signature_with,
        "avg_throughput_with": bk.avg_throughput_with,
        "never_works": bk.never_works,
        "times_primary_attach": bk.times_primary_attach,
        "times_fallback_attach": bk.times_fallback_attach,
        "times_unusable_attach": bk.times_unusable_attach,
        "last_outcome": bk.last_outcome,
        "DLST": normalize_dlst(getattr(bk, "DLST", None)),
    }


def cell_to_dict(ck: CellKnowledge) -> dict:
    """Serialize a CellKnowledge entry."""
    return {
        "band": ck.band, "earfcn": ck.earfcn,
        "times_served": ck.times_served,
        "avg_sinr": ck.avg_sinr, "avg_throughput": ck.avg_throughput,
        "avg_mcs": ck.avg_mcs,
        "avg_bq_cell": ck.avg_bq_cell,
        "avg_cq_cell": ck.avg_cq_cell,
        "best_ca_signature": ck.best_ca_signature,
        "last_seen": ck.last_seen,
        "observed_bw_mhz": ck.observed_bw_mhz,
        "nr_attach_count": ck.nr_attach_count,
        "nr_total_count": ck.nr_total_count,
        "nr_sinr_ema": ck.nr_sinr_ema,
        "DLST": normalize_dlst(getattr(ck, "DLST", None)),
    }
