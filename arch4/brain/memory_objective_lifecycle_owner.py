"""Objective-lifecycle mutation owner for Memory action history."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from arch4.evidence_scope import EvidenceScope, FormulaVector, validated_evidence_scope
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH, ActionRecord, LeverType
from arch4.brain.memory_action_history_owner import ACTION_ROI_FORMULA_EPOCH

OWNER = "arch4.brain.memory_objective_lifecycle_owner"


def record_objective_lifecycle(
    memory: Any,
    *,
    stage: str,
    status: str,
    objective_id: str = "",
    lever: str = "",
    intent: str = "",
    reason: str = "",
    decision_trace_id: str = "",
    until_ts: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
) -> Dict[str, Any]:
    record = record_lifecycle_event(
        memory,
        stage=stage,
        status=status,
        objective_id=objective_id,
        lever=lever,
        intent=intent,
        reason=reason,
        decision_trace_id=decision_trace_id,
        until_ts=until_ts,
        metadata=metadata,
        provider_plmn=provider_plmn,
        provider_name=provider_name,
    )
    return lifecycle_record_to_dict(record)


def get_recent_objective_lifecycle(memory: Any, limit: int = 200) -> list[Dict[str, Any]]:
    store = getattr(memory, "_action_history_store", None)
    if store is None:
        return []
    return store.recent_lifecycle(limit=limit)


def record_lifecycle_event(
    memory: Any,
    *,
    stage: str,
    status: str,
    objective_id: str = "",
    lever: str = "",
    intent: str = "",
    reason: str = "",
    decision_trace_id: str = "",
    until_ts: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
    append_journal: bool = True,
    evidence_scope: Optional[Dict[str, Any]] = None,
) -> ActionRecord:
    lever_type = LeverType.BAND_SELECTION
    try:
        lever_type = LeverType[str(lever)] if lever else LeverType.BAND_SELECTION
    except (KeyError, ValueError):
        pass

    lifecycle_scope = _lifecycle_evidence_scope(
        memory,
        supplied=evidence_scope,
        lever=lever_type,
        stage=stage,
    )
    record = ActionRecord(
        timestamp=time.time(),
        sinr_db=0.0,
        rsrq_db=0.0,
        mcs_dl=0,
        ca_signature="",
        rtt_sliding_avg=0.0,
        rtt_ms=0.0,
        lever=lever_type,
        action_params={},
        success=False,
        throughput_before_mbps=0.0,
        throughput_after_mbps=0.0,
        sinr_after=0.0,
        ca_signature_after="",
        improvement=0.0,
        provider_plmn=str(provider_plmn or getattr(memory, "_current_provider_plmn", "") or ""),
        provider_name=str(provider_name or ""),
        execution_status="lifecycle_event",
        proposal_intent=str(intent or ""),
        proposal_objective_id=str(objective_id or ""),
        proposal_reason=str(reason or ""),
        objective_stage=str(stage or ""),
        objective_status=str(status or ""),
        objective_until_ts=float(until_ts or 0.0),
        objective_metadata=dict(metadata or {}),
        decision_trace_id=str(decision_trace_id or ""),
        baseline_evidence_scope=dict(lifecycle_scope),
        evidence_scope=dict(lifecycle_scope),
    )
    admitted = _append_action_history_record(memory, record)
    if append_journal and admitted:
        _append_lifecycle_journal(memory, record, lever=lever, metadata=metadata)
    return record


def apply_lifecycle_journal_record(memory: Any, record: Dict[str, Any]) -> ActionRecord:
    return record_lifecycle_event(
        memory,
        stage=str(record.get("stage", "") or ""),
        status=str(record.get("status", "") or ""),
        objective_id=str(record.get("objective_id", "") or ""),
        lever=str(record.get("lever", "") or ""),
        intent=str(record.get("intent", "") or ""),
        reason=str(record.get("reason", "") or ""),
        decision_trace_id=str(record.get("decision_trace_id", "") or ""),
        until_ts=float(record.get("until_ts", 0.0) or 0.0),
        metadata=dict(record.get("metadata") or {}),
        provider_plmn=str(record.get("provider_plmn", "") or ""),
        provider_name=str(record.get("provider_name", "") or ""),
        append_journal=False,
        evidence_scope=(
            dict(record.get("evidence_scope") or {})
            if isinstance(record.get("evidence_scope"), dict)
            else None
        ),
    )


def lifecycle_record_to_dict(record: ActionRecord) -> Dict[str, Any]:
    return {
        "ts": float(record.timestamp),
        "stage": str(record.objective_stage or ""),
        "status": str(record.objective_status or ""),
        "objective_id": str(record.proposal_objective_id or ""),
        "lever": str(record.lever.name if hasattr(record.lever, "name") else record.lever),
        "intent": str(record.proposal_intent or ""),
        "reason": str(record.proposal_reason or ""),
        "decision_trace_id": str(record.decision_trace_id or ""),
        "until_ts": float(record.objective_until_ts or 0.0),
        "provider_plmn": str(record.provider_plmn or ""),
        "provider_name": str(record.provider_name or ""),
        "metadata": dict(record.objective_metadata or {}),
    }


def _append_action_history_record(memory: Any, record: ActionRecord) -> bool:
    return bool(memory._append_action_history_record(record))


def _lifecycle_evidence_scope(
    memory: Any,
    *,
    supplied: Optional[Dict[str, Any]],
    lever: LeverType,
    stage: str,
) -> Dict[str, Any]:
    for candidate in (
        supplied,
        getattr(memory, "_current_action_evidence_scope", {}),
        getattr(memory, "_current_memory_evidence_scope", {}),
    ):
        try:
            source = validated_evidence_scope(candidate or {})
        except (TypeError, ValueError):
            continue
        scope = EvidenceScope(
            identity_code=source.identity_code,
            identity_status=source.identity_status,
            identity_catalog_version=source.identity_catalog_version,
            identity_catalog_checksum=source.identity_catalog_checksum,
            session_scope_key=source.session_scope_key,
            source_epoch=source.source_epoch,
            source_sequence=source.source_sequence,
            observed_at=source.observed_at,
            evidence_age_s=source.evidence_age_s,
            confidence=source.confidence,
            evidence_count=source.evidence_count,
            formula_vector=FormulaVector.build(
                {
                    "action_roi": ACTION_ROI_FORMULA_EPOCH,
                    "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
                }
            ),
            portability=source.portability,
            access_type_id=source.access_type_id,
            model_family_id=source.model_family_id,
            provider_plmn=source.provider_plmn,
            sim_scope_hash=source.sim_scope_hash,
            site_scope_id=source.site_scope_id,
            cell_scope_key=source.cell_scope_key,
            serving_band=source.serving_band,
            action_shape_key=f"{lever.name}:lifecycle:{str(stage or '')}",
        )
        return scope.as_dict()
    return {}


def _append_lifecycle_journal(
    memory: Any,
    record: ActionRecord,
    *,
    lever: str,
    metadata: Optional[Dict[str, Any]],
) -> None:
    memory._append_journal_event(
        {
            "kind": "lifecycle_event",
            "ts": float(record.timestamp),
            "record": {
                "stage": str(record.objective_stage or ""),
                "status": str(record.objective_status or ""),
                "objective_id": str(record.proposal_objective_id or ""),
                "lever": str(lever or ""),
                "intent": str(record.proposal_intent or ""),
                "reason": str(record.proposal_reason or ""),
                "decision_trace_id": str(record.decision_trace_id or ""),
                "until_ts": float(record.objective_until_ts or 0.0),
                "metadata": dict(metadata or {}),
                "provider_plmn": str(record.provider_plmn or ""),
                "provider_name": str(record.provider_name or ""),
                "evidence_scope": dict(record.evidence_scope or {}),
            },
        }
    )
    memory._mark_dirty(events=1)


__all__ = [
    "OWNER",
    "apply_lifecycle_journal_record",
    "get_recent_objective_lifecycle",
    "lifecycle_record_to_dict",
    "record_lifecycle_event",
    "record_objective_lifecycle",
]
