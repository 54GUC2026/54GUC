"""
MikroTik priority-rotate policy helpers.

These helpers keep MikroTik-specific rotate gating and lifecycle windows out of
the generic decision-state mixin.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Iterable




def _soft_or_cfg_float(engine: Any, soft_key: str, cfg_name: str, default: float) -> float:
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    raw = soft.get(soft_key, getattr(engine.cfg, cfg_name, default))
    if raw is None:
        return float(default)
    return float(raw)


def _soft_or_cfg_int(engine: Any, soft_key: str, cfg_name: str, default: int) -> int:
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    raw = soft.get(soft_key, getattr(engine.cfg, cfg_name, default))
    if raw is None:
        return int(default)
    return int(raw)


def _normalize_bands(bands: Iterable[int]) -> list[int]:
    normalized: list[int] = []
    seen = set()
    for band in bands:
        try:
            band_i = int(band)
        except (TypeError, ValueError):
            continue
        if band_i <= 0 or band_i in seen:
            continue
        seen.add(band_i)
        normalized.append(band_i)
    return normalized


def allow_mikrotik_priority_rotate(
    engine: Any,
    *,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    now: float,
) -> bool:
    if now < engine.cooldowns.get_until("mikrotik_rotate"):
        return False
    if now < float((engine._get_routeros_rotate_success_quiet_until() if hasattr(engine, "_get_routeros_rotate_success_quiet_until") else getattr(engine, "_mikrotik_rotate_success_quiet_until", 0.0)) or 0.0):
        return False

    min_gap = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_min_interval_s",
            "routeros_priority_rotate_min_interval_s",
            getattr(engine.cfg, "mikrotik_priority_rotate_min_interval_s", 300.0),
        ),
    )
    last_attempt_at = float(getattr(engine, "_mikrotik_rotate_last_attempt_at", 0.0) or 0.0)
    if last_attempt_at > 0.0 and (now - last_attempt_at) < min_gap:
        return False

    window_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_window_s",
            "routeros_priority_rotate_window_s",
            getattr(engine.cfg, "mikrotik_priority_rotate_window_s", 3600.0),
        ),
    )
    max_in_window = max(
        1,
        _soft_or_cfg_int(
            engine,
            "mikrotik_priority_rotate_max_in_window",
            "routeros_priority_rotate_max_in_window",
            getattr(engine.cfg, "mikrotik_priority_rotate_max_in_window", 2),
        ),
    )
    engine._mikrotik_rotate_attempt_times = [
        float(ts)
        for ts in (getattr(engine, "_mikrotik_rotate_attempt_times", []) or [])
        if (now - float(ts)) <= window_s
    ]
    if len(engine._mikrotik_rotate_attempt_times) >= max_in_window:
        return False

    if float(modem.traffic.dl_mbps) < float(
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_min_dl_mbps",
            "routeros_priority_rotate_min_dl_mbps",
            getattr(engine.cfg, "mikrotik_priority_rotate_min_dl_mbps", 5.0),
        )
    ):
        return False
    if float(modem.cellular.sinr_db) < float(
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_sinr_gate_db",
            "routeros_priority_rotate_sinr_gate_db",
            getattr(engine.cfg, "mikrotik_priority_rotate_sinr_gate_db", 12.0),
        )
    ):
        return False

    return int(getattr(modem.cellular, 'primary_band', 0) or 0) != 3


def mikrotik_priority_rotate_order(active_bands: list[int], current_primary: int) -> list[int]:
    normalized = _normalize_bands(active_bands)
    if 3 in normalized and int(current_primary or 0) != 3:
        return [3] + [band for band in normalized if band != 3]
    return [band for band in normalized if band != int(current_primary or 0)] + [
        band for band in normalized if band == int(current_primary or 0)
    ]


def record_mikrotik_priority_rotate_attempt(engine: Any, *, now: float) -> None:
    engine._mikrotik_rotate_last_attempt_at = float(now)
    engine._mikrotik_rotate_attempt_times.append(float(now))


def note_mikrotik_priority_rotate_stable(
    engine: Any,
    *,
    target_band: int,
    now: float,
) -> None:
    quiet_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_success_quiet_s",
            "routeros_priority_rotate_success_quiet_s",
            getattr(engine.cfg, "mikrotik_priority_rotate_success_quiet_s", 900.0),
        ),
    )
    until = float(now) + quiet_s
    if hasattr(engine, "_set_routeros_rotate_success_quiet_until"):
        engine._set_routeros_rotate_success_quiet_until(until)
    else:
        engine._mikrotik_rotate_success_quiet_until = until
    engine._emit_runtime_event(
        "mikrotik_priority_rotate_stable_hold",
        target_band=int(target_band),
        quiet_s=int(quiet_s),
    )


def note_mikrotik_priority_rotate_reverted(
    engine: Any,
    *,
    target_band: int,
    current_band: int,
    now: float,
) -> None:
    cooldown_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "mikrotik_priority_rotate_fail_cooldown_s",
            "routeros_priority_rotate_fail_cooldown_s",
            getattr(engine.cfg, "mikrotik_priority_rotate_fail_cooldown_s", 1200.0),
        ),
    )
    engine.cooldowns.set("mikrotik_rotate", hold_s=cooldown_s, now=now)
    engine._emit_runtime_event(
        "mikrotik_priority_rotate_reverted",
        target_band=int(target_band),
        current_band=int(current_band),
        cooldown_s=int(cooldown_s),
    )
