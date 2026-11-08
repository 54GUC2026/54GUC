"""
HiLink-family priority-rotate policy helpers.

These helpers keep HiLink-family rotate confidence and lifecycle windows out of
the generic decision mixins.
"""

from __future__ import annotations

from typing import Any

from arch4.brain.memory_band_operational_history_owner import (
    band_has_ca_throughput_evidence,
)


def _soft_or_cfg_float(engine: Any, soft_key: str, cfg_name: str, default: float) -> float:
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    raw = soft.get(soft_key, getattr(engine.cfg, cfg_name, default))
    if raw is None:
        return float(default)
    return float(raw)


def allow_hilink_priority_rotate(engine: Any, *, now: float) -> bool:
    return not (
        float(now) < engine.cooldowns.get_until("hilink_rotate")
        or float(now) < float((engine._get_hilink_rotate_success_quiet_until() if hasattr(engine, "_get_hilink_rotate_success_quiet_until") else getattr(engine, "_hilink_rotate_success_quiet_until", 0.0)) or 0.0)
    )


def hilink_rotation_has_confidence(engine: Any, target_band: int) -> bool:
    """
    Require some prior evidence before forcing HiLink anchor rotation.

    Accept when the target band has appeared in CA with non-trivial throughput
    history. Broad learned bandset truth belongs to self-learning/TSAR, not to
    this profile policy.
    """
    return band_has_ca_throughput_evidence(
        engine.memory,
        int(target_band),
        min_avg_throughput_mbps=0.5,
    )


def note_hilink_priority_rotate_stable(
    engine: Any,
    *,
    target_band: int,
    now: float,
) -> None:
    quiet_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "hilink_priority_rotate_success_quiet_s",
            "hilink_priority_rotate_success_quiet_s",
            getattr(engine.cfg, "hilink_priority_rotate_success_quiet_s", 900.0),
        ),
    )
    until = float(now) + quiet_s
    if hasattr(engine, "_set_hilink_rotate_success_quiet_until"):
        engine._set_hilink_rotate_success_quiet_until(until)
    else:
        engine._hilink_rotate_success_quiet_until = until
    engine._emit_runtime_event(
        "hilink_priority_rotate_stable_hold",
        target_band=int(target_band),
        quiet_s=int(quiet_s),
    )


def note_hilink_priority_rotate_reverted(
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
            "hilink_priority_rotate_fail_cooldown_s",
            "hilink_priority_rotate_fail_cooldown_s",
            getattr(engine.cfg, "hilink_priority_rotate_fail_cooldown_s", 1200.0),
        ),
    )
    engine.cooldowns.set("hilink_rotate", hold_s=cooldown_s, now=now)
    engine._emit_runtime_event(
        "hilink_priority_rotate_reverted",
        target_band=int(target_band),
        current_band=int(current_band),
        cooldown_s=int(cooldown_s),
    )
