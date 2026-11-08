"""Band-confidence mutation and ranking owner for Memory."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from arch4.brain.delivered_load_memory import attach_dlst, empty_dlst, normalize_dlst

OWNER = "arch4.brain.memory_band_confidence_owner"


def update_band_confidence(
    memory: Any,
    band_signature: str,
    *,
    outcome: str,
    sinr_db: float = 0.0,
    rtt_ms: float = 0.0,
    dlst: Optional[Dict[str, Any]] = None,
    now: float | None = None,
) -> None:
    ts = float(time.time() if now is None else now)
    key = str(band_signature or "").strip()
    if not key:
        return

    entry = memory.band_confidence_ltm.get(key)
    if entry is None:
        entry = {
            "chosen_count": 0,
            "good_count": 0,
            "bad_count": 0,
            "neutral_count": 0,
            "sinr_sum": 0.0,
            "rtt_sum": 0.0,
            "score": 0.5,
            "last_updated_ts": ts,
            "first_seen_ts": ts,
            "DLST": empty_dlst(),
        }
        memory.band_confidence_ltm[key] = entry
    attach_dlst(entry, normalize_dlst(dlst))

    age_days = max(0.0, (ts - float(entry.get("last_updated_ts", ts))) / 86400.0)
    decay = 0.97 ** age_days
    for count_key in ("chosen_count", "good_count", "bad_count", "neutral_count"):
        entry[count_key] = int(round(entry.get(count_key, 0) * decay))
    entry["sinr_sum"] = float(entry.get("sinr_sum", 0.0)) * decay
    entry["rtt_sum"] = float(entry.get("rtt_sum", 0.0)) * decay

    entry["chosen_count"] = int(entry.get("chosen_count", 0)) + 1
    entry["sinr_sum"] = float(entry.get("sinr_sum", 0.0)) + float(sinr_db)
    entry["rtt_sum"] = float(entry.get("rtt_sum", 0.0)) + float(rtt_ms)

    if outcome == "good":
        entry["good_count"] = int(entry.get("good_count", 0)) + 1
    elif outcome == "bad":
        entry["bad_count"] = int(entry.get("bad_count", 0)) + 1
    else:
        entry["neutral_count"] = int(entry.get("neutral_count", 0)) + 1

    old_score = float(entry.get("score", 0.5))
    if outcome == "good":
        entry["score"] = round(min(1.0, old_score + 0.05), 4)
    elif outcome == "bad":
        entry["score"] = round(max(0.0, old_score - 0.05), 4)
    entry["last_updated_ts"] = ts


def get_ranked_band_configs(
    memory: Any,
    *,
    min_observations: int = 3,
    limit: int = 10,
) -> list[dict]:
    results = []
    for signature, entry in memory.band_confidence_ltm.items():
        if int(entry.get("chosen_count", 0)) < min_observations:
            continue
        chosen = max(1, int(entry.get("chosen_count", 1)))
        results.append(
            {
                "band_signature": signature,
                "score": float(entry.get("score", 0.5)),
                "chosen_count": int(entry.get("chosen_count", 0)),
                "good_count": int(entry.get("good_count", 0)),
                "bad_count": int(entry.get("bad_count", 0)),
                "neutral_count": int(entry.get("neutral_count", 0)),
                "avg_sinr_db": round(float(entry.get("sinr_sum", 0.0)) / chosen, 2),
                "avg_rtt_ms": round(float(entry.get("rtt_sum", 0.0)) / chosen, 2),
                "last_updated_ts": float(entry.get("last_updated_ts", 0.0)),
                "DLST": normalize_dlst(entry.get("DLST")),
            }
        )
    return sorted(results, key=lambda row: row["score"], reverse=True)[:limit]


__all__ = ["OWNER", "get_ranked_band_configs", "update_band_confidence"]
