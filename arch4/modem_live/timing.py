"""Select Modem Live read and maintenance timing.

This module has no ``arch4.*`` imports. Acquisition supplies configuration,
defaults, the access contract, profile, and clock cycle.
"""

from __future__ import annotations

from typing import Any


def _present(value: Any) -> bool:
    return value not in (None, 0, 0.0, "")


def _float_or(value: Any, default_value: float) -> float:
    try:
        return float(value if value is not None else default_value)
    except Exception:
        return float(default_value)


def resolve_live_register_timing(
    *,
    contract: Any,
    access_profile: Any,
    cfg_interval: Any,
    cfg_stale: Any,
    default_interval: float,
    default_stale: float,
    clock_ms: int,
) -> tuple[float, float, int]:
    profile_interval = getattr(access_profile, "fast_live_interval_s", None)
    profile_stale = getattr(access_profile, "fast_live_stale_s", None)
    if not _present(profile_interval) or not _present(profile_stale):
        raise ValueError("modem live timing requires a ModemLiveAccessProfile owner")

    interval_s = max(0.5, float(profile_interval))
    stale_s = max(2.0, float(profile_stale), interval_s)

    clock_s = max(0.001, float(clock_ms or 100) / 1000.0)
    cadence_cycles = max(1, int(round(interval_s / clock_s)))
    return interval_s, stale_s, cadence_cycles


def resolve_live_maintenance_interval(
    *,
    initialized: bool,
    current_interval_s: Any,
    cfg_interval: Any,
    default_interval: float,
    access_profile: Any,
) -> tuple[float, str]:
    if not initialized:
        return max(2.0, _float_or(current_interval_s or 10.0, 10.0)), "uninitialized"

    profile_interval = getattr(access_profile, "slow_live_interval_s", None)
    if not _present(profile_interval):
        raise ValueError("modem live maintenance timing requires a ModemLiveAccessProfile owner")
    return max(2.0, float(profile_interval)), "access_profile"
