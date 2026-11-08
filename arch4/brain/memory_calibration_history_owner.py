"""Calibration DB history owner for Memory."""

from __future__ import annotations

from typing import Any, Optional

from arch4.brain.memory_calibration_marker_owner import resolve_marker_alias
from arch4.brain.memory_calibration_store import get_hourly_ranges, reassign_marker_history

OWNER = "arch4.brain.memory_calibration_history_owner"


def reassign_calibration_marker_history(memory: Any, src_marker: str, dst_marker: str) -> None:
    reassign_marker_history(
        memory._calibration_db_path,
        memory._db_lock,
        enabled=bool(memory.learning_cfg.calibration_db_enabled),
        src_marker=src_marker,
        dst_marker=dst_marker,
    )


def get_calibration_hourly_ranges(
    memory: Any,
    marker: str,
    lookback_hours: int = 24,
    now: Optional[float] = None,
) -> list:
    mk = resolve_marker_alias(memory, str(marker or "C0"))
    return get_hourly_ranges(
        memory._calibration_db_path,
        memory._db_lock,
        enabled=bool(memory.learning_cfg.calibration_db_enabled),
        marker=mk,
        lookback_hours=lookback_hours,
        now=now,
    )


__all__ = [
    "OWNER",
    "get_calibration_hourly_ranges",
    "reassign_calibration_marker_history",
]
