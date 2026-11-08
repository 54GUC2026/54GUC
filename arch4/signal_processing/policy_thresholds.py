"""Canonical modem-neutral thresholds consumed by CGF signal processing."""

from __future__ import annotations

from typing import Any, Dict

_FLOAT_THRESHOLDS: dict[str, float] = {
    "sinr_feasibility_gate_db": 0.0,
    "rtt_warning_ms": 0.0,
    "rtt_warning_clear_ms": 0.0,
    "rtt_cliff_ms": 0.0,
    "v5_cq_bad_threshold": 0.0,
    "v5_cq_good_threshold": 0.0,
    "emergency_sinr_floor": 0.0,
    "emergency_radio_down_persist_s": 0.0,
    "emergency_sinr_persist_s": 0.0,
    "emergency_congestion_persist_s": 0.0,
    "emergency_timeout_s": 0.0,
    "emergency_recovery_stable_s": 0.0,
    "emergency_reentry_guard_s": 0.0,
    "degraded_reentry_guard_s": 0.0,
    "min_action_interval_s": 0.0,
    "ca_lost_window_s": 0.0,
    "ca_lost_on_threshold": 0.0,
    "ca_lost_off_threshold": 0.0,
    "ca_lost_eval_guard_s": 0.0,
}


def policy_threshold_authority(cfg: Any) -> Dict[str, Dict[str, Any]]:
    """Return only thresholds used by the active modem-neutral CGF path."""
    hard = {
        name.removeprefix("v5_"): float(getattr(cfg, name, default) or default)
        for name, default in _FLOAT_THRESHOLDS.items()
    }
    return {"hard": hard}


__all__ = ("policy_threshold_authority",)
