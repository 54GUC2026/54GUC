"""Staged ROI window and parent-state owner for proof/referee closure."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.action_grading_policy import (
    delayed_action_feedback_horizons_s,
    staged_roi_grade_delay_s,
    staged_roi_grade_policy,
)
from arch4.interfaces import LeverAction

OWNER = "arch4.brain.proof_referee_window_owner"
STAGED_ROI_PARENT_TERMINAL_STATUS = "proof_closed"


def staged_roi_grade_horizon_s(action: Any) -> int:
    try:
        horizon = int(round(float(staged_roi_grade_delay_s(action) or 0.0)))
    except (TypeError, ValueError):
        horizon = 0
    return horizon if horizon > 0 else 0


def staged_roi_parent_terminal_reason(action_record: Any) -> str:
    if action_record is None:
        return "missing_action_record"
    meta_raw = getattr(action_record, "objective_metadata", {}) or {}
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    if str(meta.get("staged_roi_parent_status", "") or "") == STAGED_ROI_PARENT_TERMINAL_STATUS:
        return "staged_roi_parent_status"
    if str(getattr(action_record, "objective_status", "") or "") == STAGED_ROI_PARENT_TERMINAL_STATUS:
        return "objective_status"
    if str(getattr(action_record, "learning_commit_status", "") or "") == STAGED_ROI_PARENT_TERMINAL_STATUS:
        return "learning_commit_status"
    if meta.get("staged_roi_final_closed_ts") is not None:
        return "staged_roi_final_closed_ts"
    return ""


def staged_roi_parent_is_terminal(action_record: Any) -> bool:
    return bool(staged_roi_parent_terminal_reason(action_record))


def assert_staged_roi_parent_open(action_record: Any, *, source: str) -> None:
    reason = staged_roi_parent_terminal_reason(action_record)
    if reason:
        raise RuntimeError(f"staged ROI parent is terminal in {source}: {reason}")


def register_staged_roi_windows(
    *,
    action_record: Any,
    action: LeverAction,
) -> None:
    if action_record is None or action is None:
        return
    assert_staged_roi_parent_open(action_record, source="register_staged_roi_windows")
    params = dict(getattr(action, "params", {}) or {})
    horizon = staged_roi_grade_horizon_s(action)
    if horizon <= 0:
        return
    meta = dict(getattr(action_record, "objective_metadata", {}) or {})
    if getattr(action_record, "success", None) is not True:
        meta["staged_roi_parent_status"] = (
            "not_opened_execution_unsuccessful"
        )
        meta["staged_roi_nonreward_reason"] = (
            "execution_unsuccessful_is_lifecycle_truth"
        )
        meta["staged_roi_windows_s"] = []
        meta["smart_loop_feedback_windows_s"] = []
        action_record.objective_metadata = meta
        return
    done: set[int] = set()
    for raw_done in meta.get("staged_roi_done_s", []) or []:
        try:
            done_horizon = int(raw_done or 0)
        except (TypeError, ValueError):
            continue
        if done_horizon > 0:
            done.add(done_horizon)
    if horizon in done:
        raise RuntimeError(f"staged ROI horizon already closed before parent proof close: {horizon}s")
    existing_windows: list[int] = []
    for raw_window in meta.get("staged_roi_windows_s", []) or []:
        try:
            existing_horizon = int(raw_window or 0)
        except (TypeError, ValueError):
            continue
        if existing_horizon > 0:
            existing_windows.append(existing_horizon)
    if existing_windows:
        if sorted(set(existing_windows)) != [horizon]:
            raise RuntimeError(
                "competing staged ROI horizons for one action parent: "
                f"existing={sorted(set(existing_windows))} new={horizon}"
            )
        meta.setdefault("staged_roi_parent_status", "pending")
        meta.setdefault("staged_roi_done_s", [])
        action_record.objective_metadata = meta
        return
    meta["staged_roi_windows_s"] = [horizon]
    meta["staged_roi_grade_delay_s"] = horizon
    meta["staged_roi_grade_policy"] = staged_roi_grade_policy(action)
    meta["selected_action_command_id"] = str(params.get("_selected_action_command_id") or "")
    meta["selected_action_proof_id"] = str(params.get("_selected_action_proof_id") or "")
    meta["smart_loop_reason_ref"] = str(params.get("_smart_loop_reason_ref") or "")
    meta["smart_loop_context_key"] = str(params.get("_smart_loop_context_key") or "")
    action_transaction = params.get("_action_transaction_key")
    if not isinstance(action_transaction, Mapping):
        raise RuntimeError("staged_roi_action_transaction_key_missing")
    meta["action_transaction"] = dict(action_transaction)
    meta["action_root_id"] = str(action_transaction.get("root_id") or "")
    if not meta["action_root_id"]:
        raise RuntimeError("staged_roi_action_root_id_missing")
    feedback_windows = list(delayed_action_feedback_horizons_s(action))
    if feedback_windows:
        existing_feedback = _positive_ints(meta.get("smart_loop_feedback_windows_s", []))
        if existing_feedback and sorted(set(existing_feedback)) != feedback_windows:
            raise RuntimeError(
                "competing SmartLoop feedback horizons for one action parent: "
                f"existing={sorted(set(existing_feedback))} new={feedback_windows}"
            )
        meta["smart_loop_feedback_windows_s"] = feedback_windows
        meta.setdefault("smart_loop_feedback_done_s", [])
    ts = float(getattr(action_record, "timestamp", 0.0) or 0.0)
    if ts > 0.0:
        meta["staged_roi_grade_due_at_ts"] = round(ts + horizon, 3)
    meta.setdefault("staged_roi_done_s", [])
    meta["staged_roi_action_shape"] = str(params.get("_tsar_candidate_action") or "")
    meta["staged_roi_parent_status"] = "pending"
    meta["staged_roi_requires_valid_data"] = True
    action_record.objective_metadata = meta


def _positive_ints(values: Any) -> list[int]:
    output: list[int] = []
    for raw in values or []:
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            output.append(value)
    return output


__all__ = [
    "OWNER",
    "STAGED_ROI_PARENT_TERMINAL_STATUS",
    "assert_staged_roi_parent_open",
    "register_staged_roi_windows",
    "staged_roi_grade_horizon_s",
    "staged_roi_parent_is_terminal",
    "staged_roi_parent_terminal_reason",
]
