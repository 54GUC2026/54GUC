"""
Calibration active-profile public projection owner.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any, Mapping

from arch4.brain.calibration_profile import CalibrationProfile


CALIBRATION_ACTIVE_PROFILE_OWNER = "calibration_active_profile"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def active_profile_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(state)
    profile_id = str(raw.get("active_profile_id", "") or "")
    profiles = _mapping(raw.get("profiles"))
    profile = profiles.get(profile_id, {})
    return copy.deepcopy(profile) if isinstance(profile, dict) else {}


def build_active_metadata(state: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(state)
    return {
        "active_profile_id": str(raw.get("active_profile_id", "") or ""),
        "active_profile_version": int(raw.get("active_profile_version", 0) or 0),
        "new_calibration_available": bool(raw.get("new_calibration_available", False)),
        "candidate_profile_id": str(raw.get("candidate_profile_id", "") or ""),
        "collection_enabled": bool(raw.get("collection_enabled", True)),
        "last_updated_ts": float(raw.get("last_updated_ts", 0.0) or 0.0),
    }


def build_calibration_profile(
    *,
    profile_id: str,
    version: int,
    state: str,
    values: Mapping[str, Any],
    now: float,
    approved_by: str = "",
) -> dict[str, Any]:
    return asdict(
        CalibrationProfile(
            profile_id=str(profile_id),
            version=int(version or 0),
            state=str(state or ""),
            values=dict(values or {}),
            raw_samples=[],
            analysis_report={},
            created_ts=float(now),
            updated_ts=float(now),
            approved_by=str(approved_by or ""),
        )
    )


__all__ = [
    "CALIBRATION_ACTIVE_PROFILE_OWNER",
    "active_profile_from_state",
    "build_active_metadata",
    "build_calibration_profile",
]
