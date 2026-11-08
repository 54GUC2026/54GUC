"""
Calibration collection-attempt public state record owner.
"""

from __future__ import annotations

import time
from typing import Any


CALIBRATION_COLLECTION_ATTEMPT_OWNER = "calibration_collection_attempt"


def build_collection_attempt_record(
    *,
    accepted: bool,
    reason: str,
    candidate_id: str = "",
    ts: float | None = None,
    **details: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "accepted": bool(accepted),
        "reason": str(reason or ""),
        "candidate_id": str(candidate_id or ""),
        "ts": float(time.time() if ts is None else ts),
    }
    for key, value in details.items():
        if value in (None, ""):
            continue
        record[str(key)] = value
    return record


def build_collection_attempt_status_fields(record: Any) -> dict[str, Any]:
    attempt = dict(record) if isinstance(record, dict) else {}
    return {
        "last_collection_attempt": dict(attempt),
        "last_collection_accepted": bool(attempt.get("accepted", False)),
        "last_collection_reason": str(attempt.get("reason", "") or ""),
        "last_collection_candidate_id": str(attempt.get("candidate_id", "") or ""),
        "last_collection_ts": float(attempt.get("ts", 0.0) or 0.0),
    }


__all__ = [
    "CALIBRATION_COLLECTION_ATTEMPT_OWNER",
    "build_collection_attempt_record",
    "build_collection_attempt_status_fields",
]
