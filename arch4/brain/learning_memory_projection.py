"""Canonical projection boundary for learned band-action memory.

Memory owns persistence and query primitives. This module owns the runtime
projection that turns those primitives into action-governor and learning-quality
signals. Production smartness code must not call Memory's band-learning query
methods directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from arch4.brain.memory_band_learning_query_store import (
    best_bandset_for_hour as query_best_bandset_for_hour,
    is_probe_exhausted as query_is_probe_exhausted,
    ranked_bandsets_for_hour as query_ranked_bandsets_for_hour,
    learned_bandsets_for_hour as query_learned_bandsets_for_hour,
    should_block_band_action as query_should_block_band_action,
    should_try_band as query_should_try_band,
)
from arch4.brain.memory_band_operational_history_owner import (
    is_band_blacklisted as operational_is_band_blacklisted,
)

OWNER = "arch4.brain.learning_memory_projection"


@dataclass(frozen=True)
class LearnedBandActionDecision:
    allowed: bool
    reason: str = ""
    source: str = OWNER

    @property
    def blocked(self) -> bool:
        return not bool(self.allowed)


def normalize_band_list(bands: Iterable[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    if bands is None:
        return out
    if not isinstance(bands, (list, tuple, set)):
        bands = [bands]
    for raw in bands or []:
        try:
            band = int(raw)
        except (TypeError, ValueError):
            continue
        if band <= 0 or band in seen:
            continue
        seen.add(band)
        out.append(band)
    return out


def screen_band_action_from_memory(
    memory: Any,
    *,
    hour: int,
    bands: Iterable[Any],
    now: Optional[float] = None,
    current_sinr: float = 0.0,
) -> LearnedBandActionDecision:
    normalized = normalize_band_list(bands)
    if not normalized:
        return LearnedBandActionDecision(False, "empty_band_target")
    if memory is None:
        return LearnedBandActionDecision(True, "")
    blocked, reason = query_should_block_band_action(
        memory,
        int(hour or 0),
        normalized,
        now=time.time() if now is None else float(now),
        current_sinr=float(current_sinr or 0.0),
    )
    return LearnedBandActionDecision(
        allowed=not bool(blocked),
        reason=str(reason or ""),
    )


def band_suppressed_by_memory(memory: Any, band: Any) -> bool:
    try:
        value = int(band)
    except (TypeError, ValueError):
        return False
    if value <= 0 or memory is None:
        return False
    return bool(operational_is_band_blacklisted(memory, value))


def exploration_band_eligible_from_memory(memory: Any, band: Any) -> LearnedBandActionDecision:
    try:
        value = int(band)
    except (TypeError, ValueError):
        return LearnedBandActionDecision(False, "invalid_band")
    if value <= 0:
        return LearnedBandActionDecision(False, "invalid_band")
    if memory is None:
        return LearnedBandActionDecision(True, "")
    if band_suppressed_by_memory(memory, value):
        return LearnedBandActionDecision(False, "band_blacklisted")
    if not query_should_try_band(memory, value):
        return LearnedBandActionDecision(False, "band_try_cooldown")
    return LearnedBandActionDecision(True, "")


def probe_exhausted_by_memory(
    memory: Any,
    *,
    hour: int,
    bands: Iterable[Any],
) -> LearnedBandActionDecision:
    normalized = normalize_band_list(bands)
    if not normalized or memory is None:
        return LearnedBandActionDecision(True, "")
    if query_is_probe_exhausted(memory, int(hour or 0), normalized):
        return LearnedBandActionDecision(False, "probe_exhausted")
    return LearnedBandActionDecision(True, "")


def ranked_bandsets_from_memory(
    memory: Any,
    hour: int,
    candidate_sets: Iterable[Iterable[Any]],
    *,
    min_attempts: float = 2.0,
    rtt_pressure_level: str = "normal",
    rtt_delta: float = 0.0,
    rtt_delta_valid: bool = False,
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
) -> list[tuple]:
    if memory is None:
        return []
    normalized_sets: list[list[int]] = []
    for candidate in candidate_sets or []:
        bands = normalize_band_list(candidate)
        if bands:
            normalized_sets.append(bands)
    if not normalized_sets:
        return []
    return list(
        query_ranked_bandsets_for_hour(
            memory,
            int(hour),
            normalized_sets,
            min_attempts=float(min_attempts),
            rtt_pressure_level=str(rtt_pressure_level or "normal"),
            rtt_delta=float(rtt_delta or 0.0),
            rtt_delta_valid=bool(rtt_delta_valid),
            rsrq_gap=float(rsrq_gap or 0.0),
            rsrq_gap_valid=bool(rsrq_gap_valid),
        )
        or []
    )


def learned_bandset_candidates_from_memory(
    memory: Any,
    *,
    hour: int,
    min_attempts: float = 2.0,
) -> list[list[int]]:
    """Project evidenced candidate shapes without exposing Memory stores."""

    if memory is None:
        return []
    return [
        normalize_band_list(bands)
        for bands in query_learned_bandsets_for_hour(
            memory,
            hour=int(hour),
            min_attempts=float(min_attempts),
        )
        if normalize_band_list(bands)
    ]


def best_bandset_from_memory(
    memory: Any,
    hour: int,
    candidate_sets: Iterable[Iterable[Any]],
    min_attempts: float = 2.0,
    min_score: float = 0.55,
    rtt_pressure_level: str = "normal",
    rtt_delta: float = 0.0,
    rtt_delta_valid: bool = False,
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
) -> Optional[list[int]]:
    if memory is None:
        return None
    normalized_sets: list[list[int]] = []
    for candidate in candidate_sets or []:
        bands = normalize_band_list(candidate)
        if bands:
            normalized_sets.append(bands)
    if not normalized_sets:
        return None
    value = query_best_bandset_for_hour(
        memory,
        int(hour),
        normalized_sets,
        min_attempts=float(min_attempts),
        min_score=float(min_score),
        rtt_pressure_level=str(rtt_pressure_level or "normal"),
        rtt_delta=float(rtt_delta or 0.0),
        rtt_delta_valid=bool(rtt_delta_valid),
        rsrq_gap=float(rsrq_gap or 0.0),
        rsrq_gap_valid=bool(rsrq_gap_valid),
    )
    return normalize_band_list(value) or None


def should_try_band_from_memory(memory: Any, band: Any) -> bool:
    try:
        value = int(band)
    except (TypeError, ValueError):
        return False
    if value <= 0 or memory is None:
        return False
    return bool(query_should_try_band(memory, value))


def historical_throughput_top_quartile_from_memory(historical: Any, *, hour: int) -> Optional[float]:
    if historical is None:
        return None
    perf = getattr(historical, "band_perf", {}).get(int(hour or 0), {})
    if not perf:
        return None
    all_tps: list[float] = []
    for _sig, values in perf.items():
        try:
            total, count = values
            if int(count or 0) >= 3:
                all_tps.append(float(total or 0.0) / float(count or 1))
        except Exception:
            continue
    if len(all_tps) < 3:
        return None
    all_tps.sort()
    idx = int(len(all_tps) * 0.75)
    return all_tps[min(idx, len(all_tps) - 1)]


__all__ = [
    "LearnedBandActionDecision",
    "OWNER",
    "band_suppressed_by_memory",
    "best_bandset_from_memory",
    "exploration_band_eligible_from_memory",
    "historical_throughput_top_quartile_from_memory",
    "learned_bandset_candidates_from_memory",
    "normalize_band_list",
    "probe_exhausted_by_memory",
    "ranked_bandsets_from_memory",
    "screen_band_action_from_memory",
    "should_try_band_from_memory",
]
