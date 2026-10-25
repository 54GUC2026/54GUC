"""Shared delivered-capacity estimator for controlled speed-test evidence.

This module intentionally does not read live traffic counters. Delivered
capacity is only meaningful when fed controlled speed-test samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from arch4.core_contracts.signals import SignalSamplePort


_DEFAULT_LTE_BITS_PER_HZ = 6.0
_MIN_CONTROLLED_SAMPLES = 3


@dataclass(frozen=True)
class DeliveredCapacityEstimate:
    valid: bool
    best_dl_mbps: float = 0.0
    total_bw_mhz: float = 0.0
    ceiling_mbps: float = 0.0
    ratio: float = 0.0
    sample_count: int = 0
    source: str = "unavailable"


def estimate_lte_total_bw_mhz(
    modem: SignalSamplePort,
    *,
    ca_active: bool,
) -> float:
    """Estimate LTE BW for delivered-capacity math.

    Prefer reported carrier bandwidth sum. If carrier detail is missing but CA
    is active, use a conservative 20 MHz LTE floor as discussed for V5.35.
    """
    carriers_raw = getattr(modem, "carriers", []) or []
    if isinstance(carriers_raw, int):
        carriers = []
    else:
        try:
            carriers = list(carriers_raw)
        except TypeError:
            carriers = []
    total = 0.0
    for carrier in carriers:
        try:
            bw = float(getattr(carrier, "bandwidth_mhz", 0.0) or 0.0)
        except (TypeError, ValueError):
            bw = 0.0
        if bw > 0.0:
            total += bw
    if total > 0.0:
        return total

    for attr in ("primary_bw_mhz", "dl_bandwidth_mhz", "dl_bw_mhz"):
        try:
            bw = float(getattr(modem, attr, 0.0) or 0.0)
        except (TypeError, ValueError):
            bw = 0.0
        if bw > 0.0:
            break
    else:
        bw = 0.0

    try:
        carrier_count = int(getattr(modem, "carrier_count", 0) or 0)
    except (TypeError, ValueError):
        carrier_count = 0
    if bool(ca_active) or carrier_count >= 2:
        return max(bw, 20.0)
    return bw


def estimate_lte_delivered_capacity(
    modem: SignalSamplePort,
    dl_samples_mbps: Iterable[float],
    *,
    ca_active: bool,
    bits_per_hz: float = _DEFAULT_LTE_BITS_PER_HZ,
) -> DeliveredCapacityEstimate:
    """Estimate delivered-capacity ratio from controlled DL speed-test samples."""
    samples: list[float] = []
    for sample in dl_samples_mbps:
        try:
            value = float(sample)
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            samples.append(value)

    total_bw = estimate_lte_total_bw_mhz(modem, ca_active=ca_active)
    try:
        bph = float(bits_per_hz)
    except (TypeError, ValueError):
        bph = _DEFAULT_LTE_BITS_PER_HZ
    if bph <= 0.0:
        bph = _DEFAULT_LTE_BITS_PER_HZ

    if len(samples) < _MIN_CONTROLLED_SAMPLES or total_bw <= 0.0:
        return DeliveredCapacityEstimate(
            valid=False,
            total_bw_mhz=max(0.0, total_bw),
            sample_count=len(samples),
            source="needs_controlled_speed_tests",
        )

    best = max(samples)
    ceiling = total_bw * bph
    ratio = max(0.0, min(1.0, best / ceiling)) if ceiling > 0.0 else 0.0
    return DeliveredCapacityEstimate(
        valid=True,
        best_dl_mbps=best,
        total_bw_mhz=total_bw,
        ceiling_mbps=ceiling,
        ratio=ratio,
        sample_count=len(samples),
        source="controlled_speedtest_best_of_3",
    )
