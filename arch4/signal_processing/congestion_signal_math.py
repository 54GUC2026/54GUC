"""Pure congestion math shared by Core and Intelligence adapters."""

from __future__ import annotations

from typing import Protocol

from arch4.coerce import CONGESTION_HIGH_ENTER_PCT, CONGESTION_MILD_ENTER_PCT


QUIET_ENTRY_RTT_DELTA_MOD_MS = 40.0
QUIET_ENTRY_RTT_DELTA_HIGH_MS = 75.0
RTT_EXTREME_OVERRIDE_MS = 600.0


class LatencySignalView(Protocol):
    rtt_delta: float
    rtt_quiet_delta: float
    rtt_sliding_avg: float
    rtt_quiet_held: bool
    rtt_baseline: float
    rtt_delta_valid: bool


class QualitySignalView(Protocol):
    congestion_pct: float
    rsrq_gap_valid: bool
    rsrq_gap: float
    congestion_flags_valid: bool
    congestion_pressure_moderate: bool
    congestion_pressure_high: bool
    congestion_bad_now: bool


def quiet_rtt_delta(latency: LatencySignalView) -> float:
    raw = float(latency.rtt_delta or 0.0)
    quiet = float(latency.rtt_quiet_delta or 0.0)
    sliding = float(latency.rtt_sliding_avg or 0.0)
    if sliding >= RTT_EXTREME_OVERRIDE_MS:
        return raw
    if bool(latency.rtt_quiet_held):
        return quiet
    if quiet > 0.0:
        return quiet
    return raw


def quiet_rtt_avg(latency: LatencySignalView) -> float:
    sliding = float(latency.rtt_sliding_avg or 0.0)
    if sliding >= RTT_EXTREME_OVERRIDE_MS:
        return sliding
    if bool(latency.rtt_quiet_held):
        return float(latency.rtt_baseline or 0.0) + float(
            latency.rtt_quiet_delta or 0.0
        )
    return sliding


def congestion_rtt_delta_ready(latency: LatencySignalView) -> bool:
    return bool(latency.rtt_delta_valid) and quiet_rtt_delta(latency) > 0.0


def congestion_rtt_ref_ms(
    latency: LatencySignalView,
    *,
    baseline_ms: float,
) -> float:
    if congestion_rtt_delta_ready(latency):
        return quiet_rtt_delta(latency)
    return float(baseline_ms or 0.0)


def congestion_rtt_level(
    latency: LatencySignalView,
    *,
    baseline_level: str,
) -> str:
    if congestion_rtt_delta_ready(latency):
        delta = quiet_rtt_delta(latency)
        if delta >= QUIET_ENTRY_RTT_DELTA_HIGH_MS:
            return "alarm"
        if delta >= QUIET_ENTRY_RTT_DELTA_MOD_MS:
            return "busy"
        return "normal"
    return str(baseline_level or "normal")


def canonical_congestion_pct(quality: QualitySignalView) -> float:
    value = float(quality.congestion_pct or 0.0)
    return max(0.0, min(100.0, value))


def canonical_rsrq_gap(
    quality: QualitySignalView,
) -> tuple[bool, float]:
    return bool(quality.rsrq_gap_valid), float(quality.rsrq_gap or 0.0)


def canonical_rtt_delta(
    latency: LatencySignalView,
) -> tuple[bool, float]:
    if not congestion_rtt_delta_ready(latency):
        return False, 0.0
    return True, congestion_rtt_ref_ms(latency, baseline_ms=0.0)


def canonical_congestion_flags(
    latency: LatencySignalView,
    quality: QualitySignalView,
    *,
    moderate_pct: float = CONGESTION_MILD_ENTER_PCT,
    high_pct: float = CONGESTION_HIGH_ENTER_PCT,
    rsrq_moderate_db: float = 4.0,
    rsrq_high_db: float = 6.0,
) -> tuple[bool, bool, bool]:
    pct = canonical_congestion_pct(quality)
    rtt_valid, rtt_delta_ms = canonical_rtt_delta(latency)
    rsrq_valid, rsrq_gap_db = canonical_rsrq_gap(quality)
    high = bool(
        pct >= float(high_pct)
        or (
            rtt_valid
            and rtt_delta_ms >= QUIET_ENTRY_RTT_DELTA_HIGH_MS
        )
        or (rsrq_valid and rsrq_gap_db >= float(rsrq_high_db))
    )
    moderate = bool(
        high
        or pct >= float(moderate_pct)
        or (
            rtt_valid
            and rtt_delta_ms >= QUIET_ENTRY_RTT_DELTA_MOD_MS
        )
        or (rsrq_valid and rsrq_gap_db >= float(rsrq_moderate_db))
    )
    return moderate, high, moderate


def authority_congestion_flags(
    latency: LatencySignalView,
    quality: QualitySignalView,
) -> tuple[bool, bool, bool]:
    if bool(quality.congestion_flags_valid):
        return (
            bool(quality.congestion_bad_now),
            bool(quality.congestion_pressure_high),
            bool(quality.congestion_pressure_moderate),
        )
    return canonical_congestion_flags(latency, quality)


__all__ = [
    "QUIET_ENTRY_RTT_DELTA_HIGH_MS",
    "QUIET_ENTRY_RTT_DELTA_MOD_MS",
    "RTT_EXTREME_OVERRIDE_MS",
    "authority_congestion_flags",
    "canonical_congestion_flags",
    "canonical_congestion_pct",
    "canonical_rsrq_gap",
    "canonical_rtt_delta",
    "congestion_rtt_delta_ready",
    "congestion_rtt_level",
    "congestion_rtt_ref_ms",
    "quiet_rtt_avg",
    "quiet_rtt_delta",
]
