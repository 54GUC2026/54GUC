"""Effective policy snapshot builder.

This is a low-level policy/debug surface used by adapter support and reporting.
Keeping it outside engine_reporting prevents adapter helpers from importing the
reporting stack.
"""

from __future__ import annotations

from typing import Any, Iterable

from arch4.interfaces import Arch4Config
from arch4.operator_profiles import family_label, family_support_text, infer_backend_family


def build_effective_policy_snapshot(
    cfg: Arch4Config,
    deployed_bands: Iterable[int],
) -> dict[str, Any]:
    """Compact resolved threshold snapshot for deterministic tuning/debug."""
    keys = [
        "rtt_warning_ms",
        "rtt_cliff_ms",
        "sinr_feasibility_gate_db",
        "emergency_sinr_floor",
        "emergency_reentry_guard_s",
        "emergency_recovery_stable_s",
        "soft_congestion_enabled",
        "soft_congestion_lq_threshold",
        "soft_congestion_rtt_min_ms",
        "soft_congestion_entry_ticks",
        "soft_congestion_clear_ticks",
        "soft_congestion_min_dl_mbps",
        "v5_congestion_enabled",
        "v5_cq_window_samples",
        "v5_cq_min_samples",
        "v5_cq_coherence_min",
        "v5_cq_mcs_positive_ratio_min",
        "v5_cq_bad_threshold",
        "v5_cq_good_threshold",
        "congestion_qualifier_gap_high",
        "congestion_qualifier_gap_mod",
        "congestion_qualifier_rtt_high_ms",
        "congestion_qualifier_rtt_mod_ms",
        "congestion_qualifier_window_ticks",
        "congestion_qualifier_persist_hits",
        "congestion_qualifier_clear_ticks",
        "congestion_qualifier_rsrq_gap_db",
        "v5_rtt_congestion_window",
        "v5_rtt_recent_window",
        "v5_low_rf_sinr_gate_db",
        "v5_good_dl_hold_enabled",
        "v5_good_dl_hold_mbps",
        "min_action_interval_s",
        "autonomous_connect_settle_s",
        "radio_change_settle_s",
        "band_action_fail_limit",
        "band_action_cooldown_s",
        "restore_ca_retry_after_s",
        "ca_lost_window_s",
        "ca_lost_on_threshold",
        "ca_lost_off_threshold",
        "learning_enabled",
        "learning_stm_min_attempts",
        "learning_stm_bad_rate_threshold",
        "learning_ltm_min_attempts",
        "learning_ltm_bad_rate_threshold",
        "learning_retry_min_elapsed_s",
        "learning_retry_sinr_improvement_db",
        "a1_night_antithrash_max_band_actions",
        "a1_night_antithrash_hold_s",
        "mikrotik_priority_rotate_max_in_window",
        "mikrotik_priority_rotate_window_s",
        "health_checks_enabled",
        "health_ping_fail_threshold",
        "health_ping_recover_threshold",
        "health_hold_down_s",
        "health_multi_target_enabled",
        "calibration_enabled",
    ]
    out: dict[str, Any] = {k: getattr(cfg, k, None) for k in keys}
    out["bouncer_stable_cq_threshold"] = getattr(
        cfg,
        "bouncer_stable_cq_threshold",
        getattr(cfg, "bouncer_stable_lq_threshold", None),
    )
    out["bouncer_stable_lq_threshold"] = getattr(
        cfg,
        "bouncer_stable_cq_threshold",
        getattr(cfg, "bouncer_stable_lq_threshold", None),
    )
    out["bouncer_stable_sinr_db"] = getattr(cfg, "bouncer_stable_sinr_db", None)
    out["deployed_bands"] = sorted({int(b) for b in deployed_bands})
    backend = str(getattr(cfg, "modem_backend", "") or "").strip().lower()
    family = infer_backend_family(backend) if backend else "unknown"
    if backend:
        out["modem_backend"] = backend
    if family and family != "unknown":
        out["backend_family"] = family
        label = family_label(family)
        if label:
            out["family_label"] = label
        support = family_support_text(family)
        if support:
            out["family_support_text"] = support
    return out
