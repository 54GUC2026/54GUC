"""Retired Memory calibration-sample writer.

CalibrationAuthority now owns admitted observations and their JSONL/SQLite
archive.  This hard-failing tombstone prevents old callers from recreating the
former duplicate writer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

OWNER = "arch4.brain.memory_calibration_sample_owner"


def record_calibration_sample(
    memory: Any,
    modem: Any,
    computed: Any,
    calibration_status: Any,
    state_name: str = "",
    now: Optional[float] = None,
    *,
    learning_gate: Dict[str, Any],
) -> None:
    raise RuntimeError(
        "illegal_calibration_sample_writer:use "
        "CalibrationAuthority.consume_calibration_observation"
    )
__all__ = [
    "OWNER",
    "record_calibration_sample",
]
