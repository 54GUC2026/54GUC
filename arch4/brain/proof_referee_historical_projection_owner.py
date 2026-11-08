"""Historical projection owner for staged ROI proof/referee closure."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping

from arch4.brain.proof_referee_context_owner import (
    int_or_zero,
)

OWNER = "arch4.brain.proof_referee_historical_projection_owner"


def commit_staged_roi_historical_projection(
    *,
    historical: Any,
    record: Any,
    original: Any,
    evidence: dict[str, Any],
    ts: float,
    contract_label: str,
    referee_code: str,
    referee_level: str,
    reason: str,
    proof_identity: Mapping[str, Any],
    reward_entry: Mapping[str, Any],
    reward_ledger_key: str = "",
) -> dict[str, Any]:
    record_history = getattr(historical, "record_terminal_reward_outcome", None)
    if not callable(record_history):
        if historical is not None:
            return {
                "historical_committed": False,
                "historical_error": "missing_historical_classified_projection",
            }
        return {"historical_committed": False}

    identity = dict(proof_identity or {})
    if not bool(identity.get("exact_identity_complete", False)):
        return {"historical_committed": False, "historical_error": "terminal_proof_identity_incomplete"}
    bands = positive_band_list(
        identity.get("target_post_action_bands") or []
    )
    if not bands:
        return {"historical_committed": False, "historical_error": "target_post_action_band_missing"}
    record_meta = dict(getattr(record, "objective_metadata", {}) or {})
    layered_feedback = (
        dict(record_meta.get("layered_action_feedback") or {})
        if isinstance(record_meta.get("layered_action_feedback"), dict)
        else {}
    )
    meta = record_history(
        reward_entry=dict(reward_entry or {}),
        action_record=record,
        action=_HistoricalActionProjection(
            params={"bands": positive_band_list(bands)}
        ),
        provider_plmn=str(getattr(record, "provider_plmn", "") or ""),
        provider_name=str(getattr(record, "provider_name", "") or ""),
        emergency_class=str(getattr(record, "emergency_class", "") or ""),
        hour=time.localtime(float(ts)).tm_hour,
        throughput_after=float(evidence.get("tp_now", 0.0) or 0.0),
        success=str(contract_label or "") == "good",
        source="staged_roi",
        metadata_extra={
            "decision_trace_id": str(getattr(record, "roi_parent_trace_id", "") or ""),
            "roi_child_trace_id": str(getattr(record, "decision_trace_id", "") or ""),
            "roi_horizon_s": int(getattr(record, "roi_horizon_s", 0) or 0),
            "referee_code": str(referee_code or ""),
            "referee_level": str(referee_level or ""),
            "reason": str(reason or ""),
            "grade_context_status": str(evidence.get("grade_context_status", "") or ""),
            "grade_evidence_valid": bool(evidence.get("grade_evidence_valid", False)),
            "parent_objective_id": str(getattr(original, "proposal_objective_id", "") or ""),
            "terminal_grade_score": int_or_zero(layered_feedback.get("memory_grade_score")),
            "proof_identity": dict(identity),
            "target_post_action_bands": list(bands),
            "post_action_band": int(identity.get("post_action_band", 0) or 0),
            "reward_ledger_key": str(reward_ledger_key or ""),
        },
    )
    return {
        "historical_committed": bool((meta or {}).get("recorded", False)),
        "historical_reason": str((meta or {}).get("reason", "") or ""),
        "historical_duplicate": bool((meta or {}).get("duplicate", False)),
        "reward_ledger_key": str(reward_ledger_key or ""),
    }


@dataclass(frozen=True, slots=True)
class _HistoricalActionProjection:
    params: Mapping[str, Any]


def positive_band_list(values: Any) -> list[int]:
    bands: list[int] = []
    for raw in values or []:
        band = int_or_zero(raw)
        if band > 0 and band not in bands:
            bands.append(band)
    return bands


__all__ = [
    "OWNER",
    "commit_staged_roi_historical_projection",
    "positive_band_list",
]
