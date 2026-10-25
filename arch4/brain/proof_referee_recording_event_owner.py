"""Logger event owner for delayed ROI proof recording surfaces."""

from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger("arch4.brain.proof_referee_recording_event_owner")

OWNER = "arch4.brain.proof_referee_recording_event_owner"


def log_staged_roi_parent_close_event(
    *,
    logger: Any,
    original: Any,
    horizon: int,
    reason: str,
    referee_code: str,
    level: str,
) -> None:
    if logger is None:
        return
    try:
        logger.log_event(
            "action_proof_close",
            decision_trace_id=str(getattr(original, "decision_trace_id", "") or ""),
            lever=str(getattr(getattr(original, "lever", None), "name", getattr(original, "lever", "")) or ""),
            bands=dict(getattr(original, "action_params", {}) or {}).get("bands", []),
            grade_level=str(level or "neutral"),
            grade_code=str(referee_code or ""),
            grade_authority="referee",
            grade_kind="staged_roi_parent_close",
            proof_status="closed",
            roi_horizon_s=int(horizon),
            roi_reason=str(reason or ""),
            learning_ltm_applied=False,
        )
    except Exception as exc:
        log.debug("staged ROI parent close event suppressed: %s", exc)


def log_action_roi_evidence_event(
    *,
    logger: Any,
    original: Any,
    record: Any,
    params: Mapping[str, Any],
    horizon: int,
    reason: str,
    evidence: Mapping[str, Any],
    reward_ledger_key: str = "",
) -> dict[str, Any]:
    if logger is None:
        return {"recorded": False, "reason": "logger_unavailable"}
    try:
        payload = dict(
            decision_trace_id=str(getattr(original, "decision_trace_id", "") or ""),
            lever=str(getattr(getattr(original, "lever", None), "name", getattr(original, "lever", "")) or ""),
            bands=params.get("bands", params.get("dl_bands", [])),
            grade_level=record.referee_outcome_level,
            grade_code=record.referee_outcome_code,
            grade_authority="referee",
            grade_kind="staged_roi",
            final_outcome=True,
            parent_final_outcome_event="action_proof_close",
            proof_status="closed",
            roi_horizon_s=int(horizon),
            roi_reason=reason,
            grade_context_status=str(evidence.get("grade_context_status", "") or ""),
            grade_evidence_valid=bool(evidence.get("grade_evidence_valid", False)),
        )
        projection_key = str(reward_ledger_key or "")
        if projection_key:
            once = getattr(logger, "log_event_once", None)
            if not callable(once):
                return {"recorded": False, "reason": "logger_idempotent_event_boundary_missing"}
            return dict(
                once(
                    f"delayed_reward:evidence:{projection_key}",
                    "action_roi_evidence",
                    reward_ledger_key=projection_key,
                    **payload,
                )
                or {}
            )
        logger.log_event("action_roi_evidence", **payload)
        return {"recorded": True, "duplicate": False}
    except Exception as exc:
        log.debug("staged ROI action_grade log suppressed: %s", exc)
        return {"recorded": False, "reason": type(exc).__name__}


__all__ = [
    "OWNER",
    "log_action_roi_evidence_event",
    "log_staged_roi_parent_close_event",
]
