"""
Brain calibration-learning runtime-clock lane owner.

Runtime clock triggers this lane; Brain/calibration owns the due marker and
status surface consumed by the calibration learning worker.
"""

from __future__ import annotations

from typing import Any

from arch4.runtime_clock import CALIBRATION_LEARNING_JOB_KEY, ClockedJobOutcome


def mark_calibration_learning_lane_due(
    engine: Any,
    *,
    due_tick: int,
    job: Any,
    now: float,
) -> ClockedJobOutcome:
    engine._calibration_learning_lane_due = True
    engine._calibration_learning_clock_status = {
        "owner": "brain",
        "lane": "slow_learning_calibration",
        "job_key": CALIBRATION_LEARNING_JOB_KEY,
        "due_tick": int(due_tick or 0),
        "due_ts": float(now),
        "cadence_ticks": int(getattr(job, "cadence_ticks", 0) or 0),
        "budget_ms": float(getattr(job, "budget_ms", 0.0) or 0.0),
    }
    return ClockedJobOutcome(ran=True)


__all__ = ["mark_calibration_learning_lane_due"]
