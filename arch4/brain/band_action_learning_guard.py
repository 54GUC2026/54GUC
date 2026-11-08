"""Canonical learned band-action guard.

This module keeps the public guard API stable. The only production path that
translates Memory band-learning storage into decisions is
``learning_memory_projection``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from arch4.brain.learning_memory_projection import (
    LearnedBandActionDecision,
    band_suppressed_by_memory,
    exploration_band_eligible_from_memory,
    historical_throughput_top_quartile_from_memory,
    normalize_band_list,
    probe_exhausted_by_memory,
    screen_band_action_from_memory,
)


def screen_band_action(
    memory: Any,
    *,
    hour: int,
    bands: Iterable[Any],
    now: Optional[float] = None,
    current_sinr: float = 0.0,
) -> LearnedBandActionDecision:
    """Return the canonical learned-memory allow/block decision for bands."""

    return screen_band_action_from_memory(
        memory,
        hour=hour,
        bands=bands,
        now=now,
        current_sinr=current_sinr,
    )


def exploration_band_eligible(memory: Any, band: Any) -> LearnedBandActionDecision:
    """Return whether a single band is eligible for explicit exploration."""

    return exploration_band_eligible_from_memory(memory, band)


def band_blacklisted(memory: Any, band: Any) -> bool:
    """Return whether learned evidence marks a band as blacklisted."""

    return band_suppressed_by_memory(memory, band)


def probe_exhausted(
    memory: Any,
    *,
    hour: int,
    bands: Iterable[Any],
) -> LearnedBandActionDecision:
    """Return whether this exploration target is exhausted for the hour."""

    return probe_exhausted_by_memory(memory, hour=hour, bands=bands)


def historical_throughput_top_quartile(historical: Any, *, hour: int) -> Optional[float]:
    """Return the learned top-quartile throughput hold threshold.

    Historical owns the raw `band_perf` store; projection owns the read.
    """

    return historical_throughput_top_quartile_from_memory(historical, hour=hour)


__all__ = [
    "LearnedBandActionDecision",
    "band_blacklisted",
    "exploration_band_eligible",
    "historical_throughput_top_quartile",
    "normalize_band_list",
    "probe_exhausted",
    "screen_band_action",
]
