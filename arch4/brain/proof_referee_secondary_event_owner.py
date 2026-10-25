"""Secondary-learning event publication owner for delayed ROI proof closure."""

from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger("arch4.brain.proof_referee_secondary_event_owner")

OWNER = "arch4.brain.proof_referee_secondary_event_owner"


def log_staged_roi_secondary_learning_event(
    *,
    logger: Any,
    record: Any,
    label: str,
    reason: str,
    referee_code: str,
    referee_level: str,
    result: Mapping[str, Any],
    reward_ledger_key: str = "",
) -> dict[str, Any]:
    if logger is None:
        return {"recorded": False, "reason": "logger_unavailable"}
    try:
        payload = dict(
            decision_trace_id=str(getattr(record, "roi_parent_trace_id", "") or ""),
            roi_child_trace_id=str(getattr(record, "decision_trace_id", "") or ""),
            grade_level=str(referee_level or ""),
            grade_code=str(referee_code or ""),
            label=str(label or ""),
            reason=str(reason or ""),
            memory_committed=bool(result.get("memory_committed", False)),
            historical_committed=bool(result.get("historical_committed", False)),
            crystal_ball_updated=bool(result.get("crystal_ball_updated", False)),
            crystal_ball_regraded=bool(result.get("crystal_ball_regraded", False)),
        )
        projection_key = str(reward_ledger_key or "")
        if projection_key:
            once = getattr(logger, "log_event_once", None)
            if not callable(once):
                return {"recorded": False, "reason": "logger_idempotent_event_boundary_missing"}
            return dict(
                once(
                    f"delayed_reward:secondary:{projection_key}",
                    "staged_roi_secondary_learning",
                    reward_ledger_key=projection_key,
                    **payload,
                )
                or {}
            )
        logger.log_event("staged_roi_secondary_learning", **payload)
        return {"recorded": True, "duplicate": False}
    except Exception as exc:
        log.debug("staged ROI secondary learning event suppressed: %s", exc)
        return {"recorded": False, "reason": type(exc).__name__}


__all__ = ["OWNER", "log_staged_roi_secondary_learning_event"]
