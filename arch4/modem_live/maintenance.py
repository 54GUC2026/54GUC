"""Decide when slow Modem Live maintenance may access the modem.

This module has no ``arch4.*`` imports. Acquisition makes backend calls and
updates caches. These helpers only decide when maintenance is allowed.
"""

from __future__ import annotations

from typing import Any


def maintenance_refresh_lock_policy(contract: Any) -> str:
    return "backend_force" if bool(getattr(contract, "maintenance_holds_io_lock", False)) else "backend"


def neighbor_read_lock_policy(contract: Any) -> str:
    return "backend_force" if bool(getattr(contract, "neighbor_reads_hold_io_lock", False)) else "backend"


def neighbor_scan_due(
    *,
    contract: Any,
    now: float,
    last_neighbor_scan: Any,
    neighbor_scan_interval: Any,
) -> bool:
    if not bool(getattr(contract, "generic_neighbor_scan_enabled", True)):
        return False
    try:
        last_ts = float(last_neighbor_scan or 0.0)
    except Exception:
        last_ts = 0.0
    try:
        interval_s = float(neighbor_scan_interval or 0.0)
    except Exception:
        interval_s = 0.0
    return float(now) - last_ts > interval_s


def live_register_has_maintenance_margin(
    *,
    clocked_register_enabled: bool,
    contract: Any,
    sequence: Any,
    updated_at: Any,
    refreshing: bool,
    now: float,
    live_register_interval_s: Any,
    required_margin_s: Any = 0.5,
    completion_ordered_steps: bool = False,
) -> bool:
    if not bool(clocked_register_enabled):
        return True
    maintenance_serializes_fast = bool(
        getattr(contract, "maintenance_holds_io_lock", False)
        or getattr(contract, "io_serial_required", False)
    )
    if not (
        maintenance_serializes_fast
        and bool(getattr(contract, "has_explicit_actionable_snapshot", False))
    ):
        return True
    try:
        seq = int(sequence or 0)
    except Exception:
        seq = 0
    try:
        updated_ts = float(updated_at or 0.0)
    except Exception:
        updated_ts = 0.0
    if bool(refreshing) or seq <= 0 or updated_ts <= 0.0:
        return False
    try:
        interval_s = float(live_register_interval_s or 2.0)
    except Exception:
        interval_s = 2.0
    try:
        required_s = max(0.0, float(required_margin_s or 0.0))
    except Exception:
        required_s = 0.5
    age_s = max(0.0, float(now) - updated_ts)
    remaining_s = max(0.0, interval_s - age_s)
    if completion_ordered_steps:
        if not bool(getattr(contract, "maintenance_stepwise", False)):
            raise ValueError("completion_ordered_maintenance_requires_steps")
        # Buffered ordinary reads yield between bounded wire requests. They
        # must not start ahead of due fast work, but need not reserve an entire
        # worst-case timeout inside a shorter target polling interval.
        return remaining_s > 0.0
    return remaining_s >= required_s
