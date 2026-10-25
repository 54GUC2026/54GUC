"""Shared helpers for congestion-specific signal interpretation.

These helpers read the RTT delta from the congestion combiner.
The RTT processor handles traffic-aware baselines via crystal ball,
so delta is meaningful at any traffic level.  When the crystal ball
has no data for the current traffic bucket, the processor suppresses
delta entirely (valid=False), so no garbage values leak through.

Emergency override: if rtt_sliding_avg > 600 ms, the raw value is used
regardless — bufferbloat alone cannot produce that.
"""

from __future__ import annotations

from arch4.coerce import CONGESTION_HIGH_ENTER_PCT, CONGESTION_MILD_ENTER_PCT
from arch4.controller_frames import DerivedSignalFrames
from arch4.signal_processing import congestion_signal_math as _math


QUIET_ENTRY_RTT_DELTA_MOD_MS = _math.QUIET_ENTRY_RTT_DELTA_MOD_MS
QUIET_ENTRY_RTT_DELTA_HIGH_MS = _math.QUIET_ENTRY_RTT_DELTA_HIGH_MS


def _quiet_rtt_delta(computed: DerivedSignalFrames) -> float:
    """Return the quiet-window RTT delta, or raw delta if extreme."""
    return _math.quiet_rtt_delta(computed.latency)


def quiet_rtt_avg(computed: DerivedSignalFrames) -> float:
    """RTT sliding avg adjusted for quiet window.

    During self-traffic, rtt_sliding_avg is inflated by bufferbloat.
    This returns baseline + quiet_delta instead — the true network RTT.
    If rtt_sliding_avg >= 600ms, returns raw (extreme override).
    """
    return _math.quiet_rtt_avg(computed.latency)


def quiet_rtt_elevated(
    computed: DerivedSignalFrames, warning_ms: float
) -> bool:
    """Like rtt_elevated but using quiet-window RTT."""
    return quiet_rtt_avg(computed) > warning_ms


def congestion_rtt_delta_ready(computed: DerivedSignalFrames) -> bool:
    return _math.congestion_rtt_delta_ready(computed.latency)


def congestion_rtt_ref_ms(
    computed: DerivedSignalFrames, *, baseline_ms: float
) -> float:
    return _math.congestion_rtt_ref_ms(
        computed.latency,
        baseline_ms=baseline_ms,
    )


def congestion_rtt_mod_ms(*, using_delta: bool, legacy_mod_ms: float) -> float:
    return float(QUIET_ENTRY_RTT_DELTA_MOD_MS if using_delta else legacy_mod_ms)


def congestion_rtt_high_ms(*, using_delta: bool, legacy_high_ms: float) -> float:
    return float(QUIET_ENTRY_RTT_DELTA_HIGH_MS if using_delta else legacy_high_ms)


def congestion_rtt_level(
    computed: DerivedSignalFrames, *, baseline_level: str
) -> str:
    return _math.congestion_rtt_level(
        computed.latency,
        baseline_level=baseline_level,
    )


def canonical_congestion_pct(computed: DerivedSignalFrames) -> float:
    return _math.canonical_congestion_pct(computed.quality)


def canonical_rsrq_gap(
    computed: DerivedSignalFrames,
) -> tuple[bool, float]:
    return _math.canonical_rsrq_gap(computed.quality)


def canonical_rtt_delta(
    computed: DerivedSignalFrames,
) -> tuple[bool, float]:
    return _math.canonical_rtt_delta(computed.latency)


def canonical_congestion_flags(
    computed: DerivedSignalFrames,
    *,
    moderate_pct: float = CONGESTION_MILD_ENTER_PCT,
    high_pct: float = CONGESTION_HIGH_ENTER_PCT,
    rsrq_moderate_db: float = 4.0,
    rsrq_high_db: float = 6.0,
) -> tuple[bool, bool, bool]:
    """
    Canonical congestion classification driven only by:
      - combined congestion_pct
      - RTT delta vs quiet/traffic-aware baseline
      - RSRQ gap vs idle baseline

    Returns (bad_now, high_pressure, moderate_pressure).
    """
    return _math.canonical_congestion_flags(
        computed.latency,
        computed.quality,
        moderate_pct=moderate_pct,
        high_pct=high_pct,
        rsrq_moderate_db=rsrq_moderate_db,
        rsrq_high_db=rsrq_high_db,
    )


def authority_congestion_flags(
    computed: DerivedSignalFrames,
) -> tuple[bool, bool, bool]:
    """Return explicit authority flags when available.

    Falls back to canonical congestion classification using the current
    authority thresholds if older payloads did not provide the explicit fields.
    """
    return _math.authority_congestion_flags(
        computed.latency,
        computed.quality,
    )
