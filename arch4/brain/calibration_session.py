"""Calibration session and candidate lineage contracts."""

from __future__ import annotations

from typing import Any, Dict

from arch4.brain.calibration_profile import (
    BACKGROUND_MIN_DURATION_S,
    BOOTSTRAP_MIN_DURATION_S,
    COMPARATIVE_MIN_DURATION_S,
)
from arch4.brain.calibration_workflow import (
    PHASE_ANCHOR_SURVEY,
    PHASE_BACKGROUND,
    PHASE_BOOTSTRAP,
    PHASE_COMPARATIVE,
)

DEFAULT_FIRST_SETUP_TIMEOUT_S = 15 * 60
DEFAULT_COLLECTION_TIMEOUT_S = 30 * 60


def normalize_workflow_mode(
    workflow_mode: str,
    *,
    active_profile_id: str = "",
    active_profile_version: int = 0,
) -> str:
    raw = str(workflow_mode or "").strip().lower()
    if raw in {
        PHASE_BOOTSTRAP,
        "bootstrap",
        "first_time",
        "first_time_calibration",
    }:
        return PHASE_BOOTSTRAP
    if raw in {
        PHASE_COMPARATIVE,
        "comparative",
        "compare",
        "antenna_compare",
    }:
        return PHASE_COMPARATIVE
    if raw in {
        PHASE_ANCHOR_SURVEY,
        "survey",
        "anchor_survey",
        "single_band_survey",
        "single_band_anchor_survey",
    }:
        return PHASE_ANCHOR_SURVEY
    if raw in {
        PHASE_BACKGROUND,
        "background",
        "self_calibration",
    }:
        return PHASE_BACKGROUND
    if str(active_profile_id or "") in {"", "C0"} or int(active_profile_version or 0) <= 1:
        return PHASE_BOOTSTRAP
    return PHASE_COMPARATIVE


def build_collection_session(
    *,
    cfg: Any,
    workflow_mode: str,
    reason: str,
    now: float,
    active_profile_id: str,
    active_profile_version: int,
) -> Dict[str, Any]:
    """Build the persisted calibration collection-session contract."""

    base_min = max(1, int(getattr(cfg, "calibration_min_samples", 30) or 30))
    mode = normalize_workflow_mode(
        workflow_mode,
        active_profile_id=str(active_profile_id or ""),
        active_profile_version=int(active_profile_version or 0),
    )
    if mode == PHASE_BOOTSTRAP:
        min_samples = max(base_min, 20)
        target_samples = max(min_samples, 36)
        min_duration_s = BOOTSTRAP_MIN_DURATION_S
        target_duration_s = max(min_duration_s, 90.0)
        sample_interval_s = 2.0
        recommended_hold_s = 30.0
        timeout_s = float(
            getattr(cfg, "calibration_first_setup_timeout_s", DEFAULT_FIRST_SETUP_TIMEOUT_S)
            or DEFAULT_FIRST_SETUP_TIMEOUT_S
        )
    elif mode == PHASE_ANCHOR_SURVEY:
        min_samples = max(10, min(base_min, 18))
        target_samples = max(min_samples, 24)
        min_duration_s = max(COMPARATIVE_MIN_DURATION_S, 45.0)
        target_duration_s = max(min_duration_s, 75.0)
        sample_interval_s = 3.0
        recommended_hold_s = 30.0
        timeout_s = float(
            getattr(cfg, "calibration_collection_timeout_s", DEFAULT_COLLECTION_TIMEOUT_S)
            or DEFAULT_COLLECTION_TIMEOUT_S
        )
    elif mode == PHASE_BACKGROUND:
        min_samples = max(12, min(base_min, 20))
        target_samples = max(min_samples, 24)
        min_duration_s = BACKGROUND_MIN_DURATION_S
        target_duration_s = max(min_duration_s, 180.0)
        sample_interval_s = 5.0
        recommended_hold_s = 0.0
        timeout_s = 0.0
    else:
        min_samples = max(8, min(base_min, 16))
        target_samples = max(min_samples, 20)
        min_duration_s = COMPARATIVE_MIN_DURATION_S
        target_duration_s = max(min_duration_s, 45.0)
        sample_interval_s = 2.0
        recommended_hold_s = 25.0
        timeout_s = float(
            getattr(cfg, "calibration_collection_timeout_s", DEFAULT_COLLECTION_TIMEOUT_S)
            or DEFAULT_COLLECTION_TIMEOUT_S
        )
    return {
        "session_id": f"{mode}:{int(now)}",
        "workflow_mode": str(mode),
        "reason": str(reason),
        "started_ts": float(now),
        "last_sample_ts": 0.0,
        "min_samples": int(min_samples),
        "target_samples": int(target_samples),
        "min_duration_s": float(min_duration_s),
        "target_duration_s": float(target_duration_s),
        "sample_interval_s": float(sample_interval_s),
        "recommended_hold_s": float(recommended_hold_s),
        "timeout_s": max(0.0, float(timeout_s)),
    }


def build_candidate_lineage(
    *,
    reason: str,
    workflow_mode: str,
    opened_ts: float,
    active_profile_id: str,
    active_profile_version: int,
) -> Dict[str, Any]:
    """Build the persisted candidate lineage contract."""

    reason_text = str(reason or "")
    if reason_text.startswith("auto_"):
        origin = "automatic_startup"
    elif str(workflow_mode or "") == PHASE_BACKGROUND:
        origin = "automatic_background"
    else:
        origin = "manual_operator"
    active_id = str(active_profile_id or "")
    active_version = int(active_profile_version or 0)
    return {
        "opened_reason": reason_text,
        "opened_ts": float(opened_ts),
        "workflow_mode": str(workflow_mode or ""),
        "origin": str(origin),
        "reference_profile_id": active_id,
        "reference_profile_version": int(active_version),
        "reference_marker": f"{active_id}@v{active_version}" if active_id else "",
    }


__all__ = [
    "DEFAULT_COLLECTION_TIMEOUT_S",
    "DEFAULT_FIRST_SETUP_TIMEOUT_S",
    "build_candidate_lineage",
    "build_collection_session",
    "normalize_workflow_mode",
]
