"""Congestion profile and precursor owner for Memory."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, Dict, List, Optional

from arch4.brain.memory_learning_gate_journal_owner import (
    store_learning_gate_allowed as memory_store_learning_gate_allowed,
)


OWNER = "arch4.brain.memory_congestion_profile_owner"


def update_congestion_profile(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    *,
    learning_gate: Dict[str, Any],
) -> None:
    cong = float(getattr(computed.quality, 'congestion_pct', 0.0) or 0.0)
    if cong <= 0.0 or not bool(getattr(modem.cellular, 'radio_connected', False)):
        return
    band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
    cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
    earfcn = int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0)
    if band <= 0:
        return
    gate_allowed = memory_store_learning_gate_allowed(
        memory,
        learning_gate,
        surface="congestion_memory",
        context={
            "cell_id": cell_id,
            "band": band,
            "earfcn": earfcn,
            "congestion_pct": cong,
        },
    )
    if not gate_allowed:
        return
    ctx = _cell_context_key(modem)
    hour = time.localtime().tm_hour
    hourly_root = getattr(memory, "_congestion_hourly", {})
    if ctx not in hourly_root:
        hourly_root[ctx] = {}
    hourly = hourly_root[ctx]
    if hour not in hourly:
        hourly[hour] = {"ema": cong, "samples": 1}
        return
    slot = hourly[hour]
    alpha = float(getattr(memory, "_congestion_hourly_alpha", 0.05) or 0.05)
    slot["ema"] = (1 - alpha) * slot["ema"] + alpha * cong
    slot["samples"] = slot["samples"] + 1


def get_congestion_forecast(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    *,
    hours_ahead: int = 0,
) -> Optional[float]:
    hourly = getattr(memory, "_congestion_hourly", {}).get(_cell_context_key(modem))
    if not hourly:
        return None
    target_hour = (time.localtime().tm_hour + int(hours_ahead or 0)) % 24
    slot = hourly.get(target_hour)
    if not slot or slot.get("samples", 0) < 5:
        return None
    return float(slot["ema"])


def get_congestion_profile_summary(memory: Any, modem: CellularIntelligenceGeneration) -> Dict[int, float]:
    hourly = getattr(memory, "_congestion_hourly", {}).get(_cell_context_key(modem))
    if not hourly:
        return {}
    return {
        int(hour): float(slot["ema"])
        for hour, slot in hourly.items()
        if slot.get("samples", 0) >= 5
    }


def get_least_congested_band_for_hour(
    memory: Any,
    hour: int,
    candidate_bands: List[int],
    cell_id: int = 0,
    earfcn: int = 0,
) -> Optional[tuple]:
    best_band = None
    best_cong = 999.0
    hourly_root = getattr(memory, "_congestion_hourly", {})
    for band in candidate_bands:
        if cell_id > 0 and earfcn > 0:
            ctx = f"{band}|{cell_id}|{earfcn}"
            hourly = hourly_root.get(ctx)
            if hourly:
                slot = hourly.get(hour)
                if slot and slot.get("samples", 0) >= 5:
                    if slot["ema"] < best_cong:
                        best_cong = slot["ema"]
                        best_band = band
                    continue
        for ctx, hourly in hourly_root.items():
            if ctx.startswith(f"{band}|"):
                slot = hourly.get(hour)
                if slot and slot.get("samples", 0) >= 5:
                    if slot["ema"] < best_cong:
                        best_cong = slot["ema"]
                        best_band = band
    if best_band is not None:
        return (best_band, best_cong)
    return None


def record_congestion_precursor(memory: Any, modem: CellularIntelligenceGeneration, fingerprint: Dict[str, Any]) -> None:
    if not fingerprint:
        return
    memory._congestion_precursors.append(
        {
            "ctx": _cell_context_key(modem),
            "hour": time.localtime().tm_hour,
            "fingerprint": dict(fingerprint),
            "ts": time.time(),
        }
    )
    max_count = int(getattr(memory, "_congestion_precursors_max", 500) or 500)
    while len(memory._congestion_precursors) > max_count:
        memory._congestion_precursors.pop(0)
    memory._mark_dirty()


def match_congestion_precursor(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    current_signals: Dict[str, float],
    *,
    tolerance: float = 0.35,
) -> Optional[float]:
    if not getattr(memory, "_congestion_precursors", None) or not current_signals:
        return None
    if not bool(current_signals.get("congestion_rising", False)):
        return None
    ctx = _cell_context_key(modem)
    hour = time.localtime().tm_hour
    ranges = {
        "congestion_pct": 100.0,
        "congestion_slope": 15.0,
    }
    best_score = None
    for precursor in memory._congestion_precursors:
        if precursor.get("ctx") != ctx:
            continue
        p_hour = int(precursor.get("hour", -99))
        if abs((hour - p_hour + 12) % 24 - 12) > 2:
            continue
        fp = precursor.get("fingerprint", {})
        if not fp:
            continue
        diffs = []
        for key, norm_range in ranges.items():
            cur_val = float(current_signals.get(key, 0.0) or 0.0)
            fp_val = float(fp.get(key, 0.0) or 0.0)
            if norm_range > 0:
                diffs.append(abs(cur_val - fp_val) / norm_range)
        if not diffs:
            continue
        avg_dist = sum(diffs) / len(diffs)
        if avg_dist <= tolerance:
            score = 1.0 - (avg_dist / tolerance)
            if best_score is None or score > best_score:
                best_score = score
    return best_score


def _cell_context_key(modem: CellularIntelligenceGeneration) -> str:
    band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
    cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
    earfcn = int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0)
    return f"{band}|{cell_id}|{earfcn}"


__all__ = [
    "OWNER",
    "get_congestion_forecast",
    "get_congestion_profile_summary",
    "get_least_congested_band_for_hour",
    "match_congestion_precursor",
    "record_congestion_precursor",
    "update_congestion_profile",
]
