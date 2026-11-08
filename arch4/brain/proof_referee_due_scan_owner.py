"""Staged-ROI due-window scan owner."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from arch4.brain.memory_action_history_owner import action_history_records
from arch4.brain.proof_referee_context_owner import int_or_zero
from arch4.brain.proof_referee_window_owner import staged_roi_parent_is_terminal

OWNER = "arch4.brain.proof_referee_due_scan_owner"


@dataclass(frozen=True)
class DueStagedRoiWindow:
    original: Any
    meta: dict[str, Any]
    done: set[int]
    horizon: int


@dataclass(frozen=True)
class DueSmartLoopFeedbackWindow:
    original: Any
    meta: dict[str, Any]
    done: set[int]
    horizon: int


def due_staged_roi_windows(*, memory: Any, now: float | None = None) -> list[DueStagedRoiWindow]:
    """Return at most one due staged-ROI window per open parent record."""
    if memory is None:
        return []
    ts = time.time() if now is None else float(now)
    output: list[DueStagedRoiWindow] = []
    for original in action_history_records(memory):
        if staged_roi_parent_is_terminal(original):
            continue
        meta = dict(getattr(original, "objective_metadata", {}) or {})
        feedback_windows = set(_positive_ints(meta.get("smart_loop_feedback_windows_s", [])))
        feedback_done = set(_positive_ints(meta.get("smart_loop_feedback_done_s", [])))
        if feedback_windows - feedback_done:
            continue
        windows = _positive_ints(meta.get("staged_roi_windows_s", []))
        if not windows:
            continue
        done = set(_positive_ints(meta.get("staged_roi_done_s", [])))
        age_s = max(0.0, ts - float(getattr(original, "timestamp", 0.0) or 0.0))
        due = [h for h in sorted(windows) if h not in done and age_s >= h]
        if due:
            output.append(
                DueStagedRoiWindow(
                    original=original,
                    meta=meta,
                    done=done,
                    horizon=int(due[0]),
                )
            )
    return output


def due_smart_loop_feedback_windows(
    *, memory: Any, now: float | None = None
) -> list[DueSmartLoopFeedbackWindow]:
    """Return all due, non-terminal SmartLoop Referee feedback horizons."""

    if memory is None:
        return []
    ts = time.time() if now is None else float(now)
    output: list[DueSmartLoopFeedbackWindow] = []
    for original in action_history_records(memory):
        if staged_roi_parent_is_terminal(original):
            continue
        meta = dict(getattr(original, "objective_metadata", {}) or {})
        windows = _positive_ints(meta.get("smart_loop_feedback_windows_s", []))
        if not windows:
            continue
        done = set(_positive_ints(meta.get("smart_loop_feedback_done_s", [])))
        age_s = max(0.0, ts - float(getattr(original, "timestamp", 0.0) or 0.0))
        for horizon in sorted(set(windows)):
            if horizon in done or age_s < horizon:
                continue
            output.append(
                DueSmartLoopFeedbackWindow(
                    original=original,
                    meta=meta,
                    done=done,
                    horizon=int(horizon),
                )
            )
    return output


def _positive_ints(values: Any) -> list[int]:
    output: list[int] = []
    for raw in values or []:
        value = int_or_zero(raw)
        if value > 0:
            output.append(value)
    return output


__all__ = [
    "OWNER",
    "DueSmartLoopFeedbackWindow",
    "DueStagedRoiWindow",
    "due_smart_loop_feedback_windows",
    "due_staged_roi_windows",
]
