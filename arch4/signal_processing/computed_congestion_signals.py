"""Lower signal-producer adapter for mutable ``ComputedSignals``."""

from __future__ import annotations

from arch4.signal_processing import congestion_signal_math as _math
from arch4.interfaces import ComputedSignals


QUIET_ENTRY_RTT_DELTA_MOD_MS = _math.QUIET_ENTRY_RTT_DELTA_MOD_MS
QUIET_ENTRY_RTT_DELTA_HIGH_MS = _math.QUIET_ENTRY_RTT_DELTA_HIGH_MS


def quiet_rtt_avg(computed: ComputedSignals) -> float:
    return _math.quiet_rtt_avg(computed)


def congestion_rtt_delta_ready(computed: ComputedSignals) -> bool:
    return _math.congestion_rtt_delta_ready(computed)


def congestion_rtt_ref_ms(
    computed: ComputedSignals,
    *,
    baseline_ms: float,
) -> float:
    return _math.congestion_rtt_ref_ms(computed, baseline_ms=baseline_ms)


def congestion_rtt_mod_ms(
    *,
    using_delta: bool,
    baseline_mod_ms: float,
) -> float:
    return float(
        QUIET_ENTRY_RTT_DELTA_MOD_MS if using_delta else baseline_mod_ms
    )


def congestion_rtt_high_ms(
    *,
    using_delta: bool,
    baseline_high_ms: float,
) -> float:
    return float(
        QUIET_ENTRY_RTT_DELTA_HIGH_MS if using_delta else baseline_high_ms
    )


def congestion_rtt_level(
    computed: ComputedSignals,
    *,
    baseline_level: str,
) -> str:
    return _math.congestion_rtt_level(
        computed,
        baseline_level=baseline_level,
    )


def canonical_congestion_pct(computed: ComputedSignals) -> float:
    return _math.canonical_congestion_pct(computed)


def canonical_rtt_delta(
    computed: ComputedSignals,
) -> tuple[bool, float]:
    return _math.canonical_rtt_delta(computed)


def authority_congestion_flags(
    computed: ComputedSignals,
) -> tuple[bool, bool, bool]:
    return _math.authority_congestion_flags(computed, computed)


__all__ = [
    "QUIET_ENTRY_RTT_DELTA_HIGH_MS",
    "QUIET_ENTRY_RTT_DELTA_MOD_MS",
    "authority_congestion_flags",
    "canonical_congestion_pct",
    "canonical_rtt_delta",
    "congestion_rtt_delta_ready",
    "congestion_rtt_high_ms",
    "congestion_rtt_level",
    "congestion_rtt_mod_ms",
    "congestion_rtt_ref_ms",
    "quiet_rtt_avg",
]
