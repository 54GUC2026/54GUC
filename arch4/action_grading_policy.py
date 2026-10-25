"""
Shared action grading policy.

Radio changes need physical settle time. A grade emitted before that window is a
status update, not a final learning outcome.
"""

from __future__ import annotations

import time
from typing import Any

from arch4.interfaces import LeverType

RADIO_ACTION_GRADE_MIN_SETTLE_S = 60.0
NORMAL_DELAYED_ACTION_FEEDBACK_HORIZONS_S = (60, 360)
NORMAL_RADIO_ACTION_TERMINAL_GRADE_S = 600.0
EMERGENCY_RESTART_TERMINAL_GRADE_S = 90.0

_RADIO_GRADE_LEVERS = {
    LeverType.BAND_SELECTION,
    LeverType.BAND_LOCK,
    LeverType.CELL_LOCK,
    LeverType.PBAND,
    LeverType.NR_BAND_LOCK,
    LeverType.RESTART,
}


def _action_params(action: Any) -> dict[str, Any]:
    params = getattr(action, "params", {}) if action is not None else {}
    return dict(params or {}) if isinstance(params, dict) else {}


def is_radio_action_for_delayed_grading(action: Any) -> bool:
    lever = getattr(action, "lever", None)
    return lever in _RADIO_GRADE_LEVERS


def staged_roi_grade_policy(action: Any) -> str:
    """Return the single delayed-ROI policy for a radio action."""
    if not is_radio_action_for_delayed_grading(action):
        return ""
    params = _action_params(action)
    if getattr(action, "lever", None) == LeverType.RESTART and params.get("emergency_fast") is True:
        return "emergency_restart_terminal_grade"
    selected_action = str(params.get("_tsar_candidate_action", "") or "").strip().lower()
    if (
        bool(params.get("_ca_pool_expansion_trial"))
        or bool(params.get("_ca_pool_reduction"))
        or selected_action in {"stabilise_ca_pool", "stabilize_ca_pool", "reduce_ca_pool"}
    ):
        return "normal_radio_terminal_grade"
    lever = getattr(action, "lever", None)
    if lever in _RADIO_GRADE_LEVERS:
        return "normal_radio_terminal_grade"
    return ""


def staged_roi_grade_delay_s(action: Any) -> float:
    """Return the one terminal Referee horizon for a delayed radio action.

    Normal cellular/radio mutations share the 10-minute terminal horizon.  A
    caller-provided parameter cannot silently create a second 5/6-minute final
    grader.  The only shorter terminal is the explicit emergency-fast restart
    path, whose purpose is outage recovery rather than normal radio tuning.
    """
    if not is_radio_action_for_delayed_grading(action):
        return 0.0
    if staged_roi_grade_policy(action) == "emergency_restart_terminal_grade":
        return EMERGENCY_RESTART_TERMINAL_GRADE_S
    return NORMAL_RADIO_ACTION_TERMINAL_GRADE_S


def delayed_action_feedback_horizons_s(action: Any) -> tuple[int, ...]:
    """Return the non-terminal Referee horizons for one delayed action.

    The final staged-ROI horizon remains the only memory-admitting grade. A
    horizon equal to the final grade is intentionally omitted here because the
    final closure itself is the feedback for that horizon.
    """

    if not is_radio_action_for_delayed_grading(action):
        return ()
    final_horizon = int(round(staged_roi_grade_delay_s(action) or 0.0))
    return tuple(
        int(horizon)
        for horizon in NORMAL_DELAYED_ACTION_FEEDBACK_HORIZONS_S
        if 0 < int(horizon) < final_horizon
    )


def action_record_age_s(action_record: Any, *, now: float | None = None) -> float:
    if action_record is None:
        return 0.0
    ts = float(getattr(action_record, "timestamp", 0.0) or 0.0)
    if ts <= 0:
        return 0.0
    return max(0.0, float(time.time() if now is None else now) - ts)


def radio_action_grade_settle_remaining_s(action: Any, action_record: Any, *, now: float | None = None) -> float:
    if not is_radio_action_for_delayed_grading(action):
        return 0.0
    if action_record is None or float(getattr(action_record, "timestamp", 0.0) or 0.0) <= 0.0:
        return RADIO_ACTION_GRADE_MIN_SETTLE_S
    return max(0.0, RADIO_ACTION_GRADE_MIN_SETTLE_S - action_record_age_s(action_record, now=now))


def should_delay_radio_action_grade(action: Any, action_record: Any, *, now: float | None = None) -> bool:
    if action_record is not None and getattr(action_record, "success", None) is not True:
        return False
    return radio_action_grade_settle_remaining_s(action, action_record, now=now) > 0.0
