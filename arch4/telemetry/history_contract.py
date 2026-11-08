"""
Canonical telemetry_history row construction.

The logger owns file IO and retention. This module owns the fact-only JSONL
tick schema so replay, imports, GUI monitoring, and forensics do not depend on
private logger internals.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from arch4.controller_frames import (
    AcceptedControllerGeneration,
    project_controller_read_model,
)
from arch4.canonical_scheduler_display import build_scheduler_display_contract, build_scheduler_display_surface
from arch4.evidence_scope import FormulaVector, evidence_scope_from_mapping
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH
from arch4.rat_model import build_rat_snapshot


TELEMETRY_FORMULA_EPOCH = "arch_production_signal_v1"


def telemetry_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
            "telemetry": TELEMETRY_FORMULA_EPOCH,
        }
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _optional_float(value: Any, *, valid: bool) -> Optional[float]:
    if not valid:
        return None
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _quality_percent(value: Any, *, valid: bool) -> Optional[float]:
    parsed = _optional_float(value, valid=valid)
    if parsed is None:
        return None
    if parsed <= 1.0:
        return max(0.0, min(100.0, parsed * 100.0))
    return max(0.0, min(100.0, parsed))


def _rtt_jitter_display_valid(facts: Mapping[str, Any], jitter_pct: float) -> bool:
    if bool(facts.get("rtt_dl_jitter_valid", False)):
        return True
    rtt_ms = _safe_float(facts.get("rtt_ms", 0.0), 0.0)
    rtt_avg = _safe_float(facts.get("rtt_sliding_avg", 0.0), 0.0)
    return rtt_ms > 0.0 and rtt_avg > 0.0 and jitter_pct >= 0.0


def _gb_from_bytes(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        raw = float(value)
    except Exception:
        return None
    if raw < 0.0:
        return None
    return raw / (1024.0 ** 3)


def build_telemetry_history_fact_row(
    *,
    tick_input: AcceptedControllerGeneration,
    tick_count: int,
    state_name: str,
    action_lever: str,
    action_reason: str,
    action_text: str,
    row_ts: float | None = None,
) -> dict[str, Any]:
    facts = project_controller_read_model(tick_input)
    sample_ts = float(facts['ts'])
    write_ts = float(row_ts) if row_ts is not None else sample_ts
    engine_tick_ts = float(facts.get("engine_tick_ts", 0.0) or 0.0)
    if engine_tick_ts <= 0.0:
        engine_tick_ts = write_ts
    sample_age_s = _safe_float(facts.get("sample_age_s", 0.0), 0.0)
    if sample_age_s <= 0.0 and engine_tick_ts > 0.0 and sample_ts > 0.0:
        sample_age_s = max(0.0, engine_tick_ts - sample_ts)
    tick_input_diagnostics = facts.get("input_diagnostics", None)
    if not isinstance(tick_input_diagnostics, dict):
        tick_input_diagnostics = {}
    modem_sample_interval_s = _safe_float(
        tick_input_diagnostics.get("modem_sample_interval_s", 0.0), 0.0
    )
    modem_sample_expected_interval_s = _safe_float(
        tick_input_diagnostics.get("modem_sample_expected_interval_s", 0.0), 0.0
    )
    register_intake_interval_s = _safe_float(
        tick_input_diagnostics.get("register_intake_interval_s", 0.0), 0.0
    )
    register_intake_expected_interval_s = _safe_float(
        tick_input_diagnostics.get("register_intake_expected_interval_s", 0.0), 0.0
    )
    ca_stat_pct = _safe_float(facts.get('ca_stat_pct', 0.0), 0.0)
    ca_stat_ld_pct = _safe_float(facts.get('ca_stat_ld_pct', 0.0), 0.0)
    cart = int(facts.get('cart', 0) or 0)
    cast = _optional_float(facts.get('cast', ca_stat_pct), valid=True)
    cart_ld = int(facts.get('cart_ld', 0) or 0)
    cart_ld_valid = bool(facts.get('cart_ld_valid', False))
    cast_ld_valid = bool(facts.get('cast_ld_valid', False))
    cast_ld = _optional_float(facts.get('cast_ld', ca_stat_ld_pct), valid=cast_ld_valid)
    cart_activity = _optional_float(facts.get('cart_activity_per_hour', 0.0), valid=True)
    cart_ld_activity = _optional_float(facts.get('cart_ld_activity', 0.0), valid=cast_ld_valid)
    delivered_capacity_valid = bool(facts.get('delivered_capacity_valid', False))
    dl_bottleneck_missing = facts.get('dl_bottleneck_missing_evidence', ()) or ()
    dl_bottleneck_rejected = facts.get('dl_bottleneck_rejected_explanations', ()) or ()
    dl_bottleneck_evidence = facts.get('dl_bottleneck_evidence', {}) or {}
    ca_signature = str(facts['ca_signature'] or "")
    configured_pool_signature = str(facts.get("configured_band_pool_signature", "") or "")
    traffic_day_used_bytes = int(facts.get("traffic_day_used_bytes", 0) or 0)
    volume_used_today_gb = _gb_from_bytes(traffic_day_used_bytes)
    traffic_quota_limit_bytes = facts.get("traffic_quota_limit_bytes", None)
    rtt_dl_jitter_pct = _safe_float(facts.get('rtt_dl_jitter_pct', 0.0), 0.0)
    rtt_dl_jitter_display_valid = _rtt_jitter_display_valid(facts, rtt_dl_jitter_pct)
    cq_display_valid = bool(facts.get("cq_cell_display_valid", facts['cq_cell_valid']))
    cq_display_value = facts.get("cq_cell_display", facts['cq_cell'])
    cq_display_held = bool(facts.get("cq_cell_display_held", False))
    cq_display_pct = _quality_percent(cq_display_value, valid=cq_display_valid)
    scheduler_surface = build_scheduler_display_surface(
        build_scheduler_display_contract(
            {
                "radio_connected": bool(facts.get("radio_connected", False)),
                "ri": facts.get("ri", 0),
                "ri_valid": facts.get("ri_valid", False),
                "ri_source": facts.get("ri_source", ""),
                "mcs_dl0": facts.get("mcs_dl0", 0) or facts.get("mcs_dl", 0),
                "mcs_dl1": facts.get("mcs_dl1", 0),
                "mcs_ul": facts.get("mcs_ul", 0),
                "cqi0": facts.get("cqi0", 0) or facts.get("cqi", 0),
                "cqi1": facts.get("cqi1", 0),
                "qam_dl0": facts.get("qam_dl0", ""),
                "qam_dl1": facts.get("qam_dl1", ""),
                "qam_ul": facts.get("qam_ul", ""),
            }
        )
    )
    rsrq_delta_display_valid = bool(facts.get("rsrq_delta_display_valid", False))
    rsrq_delta_display_db = _optional_float(
        facts.get("rsrq_delta_display_db", 0.0),
        valid=rsrq_delta_display_valid,
    )
    if rsrq_delta_display_db is None and bool(facts.get('rsrq_gap_valid', False)):
        rsrq_delta_display_db = _optional_float(facts.get('rsrq_gap', 0.0), valid=True)
        rsrq_delta_display_valid = rsrq_delta_display_db is not None
    row = {
        "record_type": "tick",
        "ts": write_ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(write_ts)),
        "engine_tick_ts": engine_tick_ts,
        "engine_tick_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(engine_tick_ts)),
        "sample_ts": sample_ts,
        "sample_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(sample_ts)),
        "sample_fresh": bool(facts.get("sample_fresh", True)),
        "sample_sequence": int(facts.get("sample_sequence", 0) or 0),
        "sample_age_s": round(float(sample_age_s), 3),
        "modem_live_io_quiet_active": bool(
            facts.get("modem_live_io_quiet_active", False)
        ),
        "modem_live_io_quiet_remaining_s": round(
            float(facts.get("modem_live_io_quiet_remaining_s", 0.0) or 0.0),
            3,
        ),
        "live_register_quiet_hold": bool(
            facts.get("live_register_quiet_hold", False)
        ),
        "live_register_hold_reason": str(
            facts.get("live_register_hold_reason", "") or ""
        ),
        "modem_sample_interval_s": round(float(modem_sample_interval_s), 3),
        "modem_sample_expected_interval_s": round(float(modem_sample_expected_interval_s), 3),
        "modem_sample_slo_exceeded": bool(
            tick_input_diagnostics.get("modem_sample_slo_exceeded", False)
        ),
        "register_intake_interval_s": round(float(register_intake_interval_s), 3),
        "register_intake_expected_interval_s": round(float(register_intake_expected_interval_s), 3),
        "register_intake_slo_exceeded": bool(
            tick_input_diagnostics.get("register_intake_slo_exceeded", False)
        ),
        "register_intake_sequence_gap": int(
            tick_input_diagnostics.get("register_intake_sequence_gap", 0) or 0
        ),
        "engine_intake_mode": str(tick_input_diagnostics.get("engine_intake_mode", "") or ""),
        "engine_intake_drained_samples": int(
            tick_input_diagnostics.get("engine_intake_drained_samples", 0) or 0
        ),
        "engine_intake_missing_samples": int(
            tick_input_diagnostics.get("engine_intake_missing_samples", 0) or 0
        ),
        "engine_intake_history_depth": int(
            tick_input_diagnostics.get("engine_intake_history_depth", 0) or 0
        ),
        "engine_intake_missing_total": int(
            tick_input_diagnostics.get("engine_intake_missing_total", 0) or 0
        ),
        "sample_source": str(facts.get("sample_source", "") or ""),
        "tick": int(tick_count),
        "provider_name": str(facts.get("provider_name", "") or ""),
        "provider_plmn": str(facts.get("provider_plmn", "") or ""),
        "access_type": str(facts.get("access_type", "") or ""),
        "provider": str(facts.get("provider_name", "") or ""),
        "plmn": str(facts.get("provider_plmn", "") or ""),
        "model": str(facts.get("model", "") or ""),
        "firmware": str(facts.get("firmware", "") or ""),
        "software_version": str(facts.get("software_version", "") or ""),
        "webui": str(facts.get("webui", "") or ""),
        "sinr_db": float(facts['sinr_db']),
        "rsrp_dbm": float(facts['rsrp_dbm']),
        "rsrq_db": float(facts['rsrq_db']),
        "rsrq_source": str(facts.get("rsrq_source", "reported") or "reported"),
        "rssi_dbm": float(facts['rssi_dbm']),
        "mcs_dl": int(facts['mcs_dl']),
        "mcs_ul": int(facts['mcs_ul']),
        "cqi": int(facts['cqi']),
        "cqi_source": str(facts.get("cqi_source", "reported") or "reported"),
        "mcs_dl0": int(facts.get("mcs_dl0", 0) or 0),
        "mcs_dl1": int(facts.get("mcs_dl1", 0) or 0),
        "cqi0": int(facts.get("cqi0", 0) or 0),
        "cqi1": int(facts.get("cqi1", 0) or 0),
        "qam_dl0": facts.get("qam_dl0", ""),
        "qam_dl1": facts.get("qam_dl1", ""),
        "qam_ul": facts.get("qam_ul", ""),
        "qam_dl_primary": int(facts.get("qam_dl_primary", 0) or 0),
        "qam_dl_secondary": int(facts.get("qam_dl_secondary", 0) or 0),
        "qm_dl0": int(facts.get("qam_dl_primary", 0) or 0),
        "qm_dl1": int(facts.get("qam_dl_secondary", 0) or 0),
        "qm_ul": int(
            facts.get("qam_ul", 0)
            if isinstance(facts.get("qam_ul", 0), (int, float))
            else 0
        ),
        "tx_power": facts.get("tx_power", ""),
        **scheduler_surface,
        "ri": int(facts['ri']),
        "ri_valid": bool(facts.get("ri_valid", int(facts['ri'] or 0) > 0)),
        "ri_source": str(facts.get("ri_source", "") or ""),
        "ri_stability_pct": _optional_float(
            facts.get('ri_stability_pct', 0.0),
            valid=bool(facts.get('ri_stability_valid', False)),
        ),
        "ri_activity_per_hour": _optional_float(
            facts.get('ri_activity_per_hour', 0.0),
            valid=bool(facts.get('ri_stability_valid', False)),
        ),
        "ri_stability_valid": bool(facts.get('ri_stability_valid', False)),
        "ca_active": bool(cart),
        "ca_raw_state": int(facts.get('ca_raw_state', 1 if bool(facts['ca_active']) else 0) or 0),
        "cart": cart,
        "cast": cast,
        "cart_ld": cart_ld,
        "cart_ld_valid": cart_ld_valid,
        "cart_ld_display": cart_ld if cart_ld_valid else None,
        "cart_ld_display_valid": cart_ld_valid,
        "cast_ld": cast_ld,
        "cast_ld_valid": cast_ld_valid,
        "cast_ld_pct": cast_ld,
        "cart_activity_per_hour": cart_activity,
        "cart_ld_activity": cart_ld_activity,
        "ca_realtime_state": cart,
        "ca_stat_pct": cast,
        "ca_stat_scope_key": str(facts.get('ca_stat_scope_key', "") or ""),
        "ca_realtime_ld_state": cart_ld,
        "ca_realtime_ld_valid": cart_ld_valid,
        "ca_stat_ld_pct": cast_ld,
        "ca_stat_ld_valid": cast_ld_valid,
        "ca_ld_pct": cast_ld,
        "ca_change_per_hour": cart_activity,
        "ca_ld_change_per_hour": cart_ld_activity,
        "ca_signature": ca_signature,
        "ca_band_signature": ca_signature,
        "ca_component_band_set": list(facts.get("ca_component_band_set", ()) or []),
        "ca_component_band_signature": str(facts.get("ca_component_band_signature", "") or ""),
        "ca_component_count": int(facts.get("ca_component_count", 0) or 0),
        "ca_composition_signature": str(facts.get("ca_composition_signature", "") or ""),
        "ca_source": str(facts.get("ca_source", "") or ""),
        "primary_band": int(facts['primary_band'] or 0),
        "serving_band": int(facts['serving_band'] or 0),
        "pband": int(facts['serving_band'] or facts['primary_band'] or 0),
        "band": int(facts['serving_band'] or facts['primary_band'] or 0),
        "serving_band_signature": str(facts['serving_band_signature'] or ""),
        "configured_band_pool": list(facts.get("configured_band_pool", ()) or []),
        "configured_band_pool_signature": configured_pool_signature,
        "configured_band_pool_source": str(facts.get("configured_band_pool_source", "") or ""),
        "configured_ca_pool_display": configured_pool_signature,
        "selected_band_set": list(facts['selected_band_set'] or []),
        "selected_band_set_signature": str(facts['selected_band_set_signature'] or ""),
        "active_band_set": list(facts['active_band_set'] or []),
        "active_band_set_signature": str(facts['active_band_set_signature'] or ""),
        "ca_state_text": str(facts['ca_state_text'] or ""),
        "radio_state_text": str(facts['radio_state_text'] or ""),
        "pci": int(facts['pci'] or 0),
        "earfcn": int(facts['earfcn'] or 0),
        "earfcn_dl": int(facts['earfcn'] or 0),
        "earfcn_ul": int(facts.get("earfcn_ul", 0) or 0),
        "enb_id": int(facts['enb_id'] or 0),
        "cell_id": int(facts['cell_id'] or 0),
        "tac": int(facts.get("tac", 0) or 0),
        "rrc": str(facts.get("rrc", "") or ""),
        "trans_mode": str(facts.get("trans_mode", "") or ""),
        "network_type": str(facts.get("network_type", "") or ""),
        "network_type_ex": str(facts.get("network_type_ex", "") or ""),
        "nsa_active": bool(facts.get("nsa_active", False)),
        "sa_active": bool(facts.get("sa_active", False)),
        "nr_band": int(facts.get("nr_band", 0) or 0),
        "nr_pci": int(facts.get("nr_pci", 0) or 0),
        "nr_arfcn": int(facts.get("nr_arfcn", 0) or 0),
        "nr_cell_id": int(facts.get("nr_cell_id", 0) or 0),
        "nr_rsrp_dbm": _optional_float(facts.get("nr_rsrp_dbm", 0.0), valid=bool(facts.get("nr_band", 0))),
        "nr_rsrq_db": _optional_float(facts.get("nr_rsrq_db", 0.0), valid=bool(facts.get("nr_band", 0))),
        "nr_sinr_db": _optional_float(facts.get("nr_sinr_db", 0.0), valid=bool(facts.get("nr_band", 0))),
        "nr_bandwidth_mhz": _optional_float(facts.get("nr_bandwidth_mhz", 0.0), valid=bool(facts.get("nr_bandwidth_mhz", 0.0))),
        "nr_scs_khz": _optional_float(facts.get("nr_scs_khz", 0.0), valid=bool(facts.get("nr_scs_khz", 0.0))),
        "nr_cells": [dict(cell) for cell in facts.get("nr_cells", ()) if isinstance(cell, dict)],
        "nr_mode": str(facts.get("nr_mode", "") or ""),
        "nr_state_text": str(facts.get("nr_state_text", "") or ""),
        "nr_secondary_active": bool(facts.get("nr_secondary_active", False)),
        "nr_transition_state": str(facts.get("nr_transition_state", "") or ""),
        "nr_transition_dirty": bool(facts.get("nr_transition_dirty", False)),
        "nr_identity_completeness": str(facts.get("nr_identity_completeness", "absent") or "absent"),
        "nsa_snapshot": dict(facts.get("nsa_snapshot") or {}) if isinstance(facts.get("nsa_snapshot"), Mapping) else None,
        "nr_capacity_evidence": dict(facts.get("nr_capacity_evidence") or {}) if isinstance(facts.get("nr_capacity_evidence"), Mapping) else None,
        "radio_connected": bool(facts['radio_connected']),
        "session_uptime_s": float(facts['session_uptime_s'] or 0.0),
        "session_uptime_source": str(facts.get("session_uptime_source", "") or ""),
        "dl_bandwidth": str(facts.get("dl_bandwidth", "") or ""),
        "ul_bandwidth": str(facts.get("ul_bandwidth", "") or ""),
        "dl_bandwidth_mhz": float(facts.get("dl_bandwidth_mhz", 0.0) or 0.0),
        "ul_bandwidth_mhz": float(facts.get("ul_bandwidth_mhz", 0.0) or 0.0),
        "dl_freq_mhz": float(facts.get("dl_freq_mhz", 0.0) or 0.0),
        "ul_freq_mhz": float(facts.get("ul_freq_mhz", 0.0) or 0.0),
        "dl_mbps": float(facts['dl_mbps']),
        "ul_mbps": float(facts['ul_mbps']),
        "has_traffic": bool(facts['has_traffic']),
        "traffic_current_download_rate_bps": max(0.0, float(facts['dl_mbps'] or 0.0) * 1_000_000.0 / 8.0),
        "traffic_current_upload_rate_bps": max(0.0, float(facts['ul_mbps'] or 0.0) * 1_000_000.0 / 8.0),
        "traffic_day_download_bytes": int(facts.get("traffic_day_download_bytes", 0) or 0),
        "traffic_day_upload_bytes": int(facts.get("traffic_day_upload_bytes", 0) or 0),
        "traffic_day_used_bytes": traffic_day_used_bytes,
        "volume_used_today_gb": volume_used_today_gb,
        "traffic_month_download_bytes": int(facts.get("traffic_month_download_bytes", 0) or 0),
        "traffic_month_upload_bytes": int(facts.get("traffic_month_upload_bytes", 0) or 0),
        "traffic_total_download_bytes": int(facts.get("traffic_total_download_bytes", 0) or 0),
        "traffic_total_upload_bytes": int(facts.get("traffic_total_upload_bytes", 0) or 0),
        "traffic_session_download_bytes": int(facts.get("traffic_session_download_bytes", 0) or 0),
        "traffic_session_upload_bytes": int(facts.get("traffic_session_upload_bytes", 0) or 0),
        "traffic_device_download_counter_bytes": facts.get("traffic_device_download_counter_bytes", None),
        "traffic_device_upload_counter_bytes": facts.get("traffic_device_upload_counter_bytes", None),
        "traffic_accounting_source": str(facts.get("traffic_accounting_source", "") or ""),
        "traffic_accounting_status": str(facts.get("traffic_accounting_status", "") or ""),
        "traffic_accounting_month_key": str(facts.get("traffic_accounting_month_key", "") or ""),
        "traffic_accounting_day_key": str(facts.get("traffic_accounting_day_key", "") or ""),
        "traffic_volume_poll_interval_s": float(facts.get("traffic_volume_poll_interval_s", 0.0) or 0.0),
        "traffic_volume_last_sample_ts": float(facts.get("traffic_volume_last_sample_ts", 0.0) or 0.0),
        "traffic_volume_next_sample_ts": float(facts.get("traffic_volume_next_sample_ts", 0.0) or 0.0),
        "traffic_volume_age_s": facts.get("traffic_volume_age_s", None),
        "traffic_rate_source": str(facts.get("traffic_rate_source", "") or ""),
        "traffic_quota_start_day": facts.get("traffic_quota_start_day", None),
        "traffic_quota_limit_label": str(facts.get("traffic_quota_limit_label", "") or ""),
        "traffic_quota_left_bytes": facts.get("traffic_quota_left_bytes", None),
        "traffic_quota_limit_bytes": traffic_quota_limit_bytes,
        "traffic_quota_month_threshold_pct": facts.get("traffic_quota_month_threshold_pct", None),
        "traffic_quota_day_threshold_pct": facts.get("traffic_quota_day_threshold_pct", None),
        "delivered_capacity_valid": delivered_capacity_valid,
        "delivered_capacity_ratio": _optional_float(
            facts.get('delivered_capacity_ratio', 0.0),
            valid=delivered_capacity_valid,
        ),
        "delivered_capacity_best_dl_mbps": _optional_float(
            facts.get('delivered_capacity_best_dl_mbps', 0.0),
            valid=delivered_capacity_valid,
        ),
        "delivered_capacity_total_bw_mhz": _optional_float(
            facts.get('delivered_capacity_total_bw_mhz', 0.0),
            valid=bool(facts.get('delivered_capacity_total_bw_mhz', 0.0)),
        ),
        "delivered_capacity_ceiling_mbps": _optional_float(
            facts.get('delivered_capacity_ceiling_mbps', 0.0),
            valid=delivered_capacity_valid,
        ),
        "delivered_capacity_sample_count": int(facts.get('delivered_capacity_sample_count', 0) or 0),
        "delivered_capacity_source": str(
            facts.get('delivered_capacity_source', "needs_controlled_speed_tests")
            or "needs_controlled_speed_tests"
        ),
        "dl_bottleneck_state": str(facts.get('dl_bottleneck_state', "unknown") or "unknown"),
        "dl_bottleneck_reason": str(
            facts.get('dl_bottleneck_reason', "missing_controlled_speedtest_evidence")
            or "missing_controlled_speedtest_evidence"
        ),
        "dl_bottleneck_proof_level": str(
            facts.get('dl_bottleneck_proof_level', "unknown") or "unknown"
        ),
        "dl_bottleneck_missing_evidence": list(dl_bottleneck_missing)
        if isinstance(dl_bottleneck_missing, (list, tuple))
        else [],
        "dl_bottleneck_rejected_explanations": list(dl_bottleneck_rejected)
        if isinstance(dl_bottleneck_rejected, (list, tuple))
        else [],
        "dl_bottleneck_evidence": dict(dl_bottleneck_evidence)
        if isinstance(dl_bottleneck_evidence, dict)
        else {},
        "dl_bottleneck_owner": str(
            facts.get('dl_bottleneck_owner', "brain.dl_bottleneck")
            or "brain.dl_bottleneck"
        ),
        "dl_bottleneck_schema_version": str(
            facts.get('dl_bottleneck_schema_version', "dl_bottleneck_v1")
            or "dl_bottleneck_v1"
        ),
        "rtt_ms": float(facts['rtt_ms'] or 0.0),
        "rtt_sliding_avg": float(facts['rtt_sliding_avg'] or 0.0),
        "rtt_baseline": float(facts.get('rtt_baseline', 0.0) or 0.0),
        "rtt_quiet_delta": float(facts.get("rtt_quiet_delta", 0.0) or 0.0),
        "rtt_quiet_held": bool(facts.get("rtt_quiet_held", False)),
        "rtt_delta": _optional_float(
            facts.get('rtt_delta', 0.0),
            valid=bool(facts.get('rtt_delta_valid', False)),
        ),
        "rtt_delta_valid": bool(facts.get('rtt_delta_valid', False)),
        "rtt_delta_display_ms": _optional_float(
            facts.get("rtt_delta_display_ms", 0.0),
            valid=bool(facts.get("rtt_delta_display_valid", False)),
        ),
        "rtt_delta_display_valid": bool(facts.get("rtt_delta_display_valid", False)),
        "rtt_delta_display_held": bool(facts.get("rtt_delta_display_held", False)),
        "rtt_delta_display_live_ts": float(facts.get("rtt_delta_display_live_ts", 0.0) or 0.0),
        "rtt_active_traffic_class": str(
            facts.get('rtt_active_traffic_class', "unavailable") or "unavailable"
        ),
        "rtt_active_load_mbps": float(facts.get('rtt_active_load_mbps', 0.0) or 0.0),
        "rtt_low_load_latency_valid": bool(
            facts.get('rtt_low_load_latency_valid', False)
        ),
        "rtt_low_load_latency_reason": str(
            facts.get('rtt_low_load_latency_reason', "") or ""
        ),
        "rtt_low_load_latency_mbps": float(
            facts.get('rtt_low_load_latency_mbps', 0.0) or 0.0
        ),
        "rtt_active_jitter_mad_ms": _optional_float(
            facts.get('rtt_active_jitter_mad_ms', 0.0),
            valid=bool(facts.get('rtt_active_jitter_valid', False)),
        ),
        "rtt_active_jitter_step_p95_ms": _optional_float(
            facts.get('rtt_active_jitter_step_p95_ms', 0.0),
            valid=bool(facts.get('rtt_active_jitter_valid', False)),
        ),
        "rtt_active_jitter_samples": int(facts.get('rtt_active_jitter_samples', 0) or 0),
        "rtt_active_jitter_valid": bool(facts.get('rtt_active_jitter_valid', False)),
        "rtt_dl_jitter_bucket": str(facts.get('rtt_dl_jitter_bucket', "") or ""),
        "rtt_dl_jitter_trimmed_ms": _optional_float(
            facts.get('rtt_dl_jitter_trimmed_ms', 0.0),
            valid=bool(facts.get('rtt_dl_jitter_valid', False)),
        ),
        "rtt_dl_jitter_baseline_ms": _optional_float(
            facts.get('rtt_dl_jitter_baseline_ms', 0.0),
            valid=bool(facts.get('rtt_dl_jitter_valid', False)),
        ),
        "rtt_dl_jitter_delta_ms": _optional_float(
            facts.get('rtt_dl_jitter_delta_ms', 0.0),
            valid=bool(facts.get('rtt_dl_jitter_valid', False)),
        ),
        "rtt_dl_jitter_pct": rtt_dl_jitter_pct,
        "rtt_jitter_pct": rtt_dl_jitter_pct,
        "jitter_pct": rtt_dl_jitter_pct,
        "rtt_dl_jitter_valid": bool(facts.get('rtt_dl_jitter_valid', False)),
        "rtt_dl_jitter_display_valid": rtt_dl_jitter_display_valid,
        "rtt_delta_block_reason": str(facts.get('rtt_delta_block_reason', "") or ""),
        "rtt_elevated": bool(facts['rtt_elevated']),
        "cq_cell": float(facts['cq_cell']),
        "cq_cell_valid": bool(facts['cq_cell_valid']),
        "cq_cell_source": str(facts['cq_cell_source'] or ""),
        "cq_cell_display": _optional_float(cq_display_value, valid=cq_display_valid),
        "cq_cell_display_valid": cq_display_valid,
        "cq_cell_display_held": cq_display_held,
        "cq_norm_pct": cq_display_pct,
        "cq_display_pct": cq_display_pct,
        "conn_quality": cq_display_pct,
        "bq_cell": _optional_float(
            facts.get('bq_cell', 0.0),
            valid=bool(facts.get('bq_cell_valid', False)),
        ),
        "bq_cell_valid": bool(facts.get('bq_cell_valid', False)),
        "bq_cell_source": str(facts.get('bq_cell_source', "") or ""),
        "cq_cqi_source": str(facts.get("cq_cqi_source", "none") or "none"),
        "cqi_mcs_gap_smoothed": _optional_float(
            facts.get("cqi_mcs_gap_smoothed", 0.0),
            valid=bool(facts.get("cqi_mcs_gap_valid", False)),
        ),
        "cqi_mcs_gap_valid": bool(facts.get("cqi_mcs_gap_valid", False)),
        # LEGACY - no structured downstream consumer found in crystal_ball,
        # GUI snapshot, or reports. Retained for external tooling that may
        # read telemetry_history JSONL directly. Safe to keep; cost is minimal.
        "cqi_mcs_delta_pct": _optional_float(
            facts.get("cqi_mcs_gap_idx", 0.0),
            valid=bool(facts.get("cqi_mcs_gap_valid", False)),
        ),
        "congestion_pct": _optional_float(
            facts.get('congestion_pct', 0.0),
            valid=bool(facts.get('congestion_pct_valid', False)),
        ),
        "congestion_pct_valid": bool(facts.get('congestion_pct_valid', False)),
        "congestion_display_pct": _optional_float(
            facts.get("congestion_display_pct", 0.0),
            valid=bool(facts.get("congestion_display_valid", False)),
        ),
        "congestion_display_valid": bool(
            facts.get("congestion_display_valid", False)
        ),
        "congestion_supported": bool(facts.get('congestion_supported', True)),
        "congestion_support_reason": str(facts.get('congestion_support_reason', "") or ""),
        "congestion_bucket": (
            str(facts.get('congestion_bucket', "") or "")
            if bool(facts.get('congestion_pct_valid', False))
            else ""
        ),
        "congestion_state_bucket": (
            str(facts.get('congestion_state_bucket', "") or "")
            if bool(facts.get('congestion_pct_valid', False))
            else ""
        ),
        "congestion_state_avg_pct": _optional_float(
            facts.get('congestion_state_avg_pct', 0.0),
            valid=bool(facts.get('congestion_pct_valid', False)),
        ),
        "congestion_kind_hint": str(facts.get('congestion_kind_hint', "") or ""),
        # LEGACY - unclamped diagnostic value; no structured consumer reads
        # this field from JSONL. Retained for external forensic tooling.
        "congestion_raw_pct": _optional_float(
            facts.get('congestion_raw_pct', 0.0),
            valid=bool(facts.get('congestion_pct_valid', False)),
        ),
        "congestion_source": (
            str(facts.get('congestion_source', "") or "")
            if bool(facts.get('congestion_pct_valid', False))
            else ""
        ),
        "congestion_internal_bucket": str(facts.get('congestion_internal_bucket', "") or ""),
        "congestion_qam_gap": _optional_float(
            facts.get('congestion_qam_gap', 0.0),
            valid=bool(facts.get('congestion_pct_valid', False)),
        ),
        "congestion_slope": _optional_float(
            facts.get('congestion_slope', 0.0),
            valid=bool(facts.get('congestion_pct_valid', False)),
        ),
        "congestion_rising": bool(facts.get('congestion_rising', False)),
        "congestion_trend_display": str(facts.get("congestion_trend_display", "") or ""),
        "congestion_flags_valid": bool(facts.get('congestion_flags_valid', False)),
        "congestion_bad_now": bool(facts.get('congestion_bad_now', False)),
        "congestion_pressure_high": bool(facts.get('congestion_pressure_high', False)),
        "congestion_pressure_moderate": bool(facts.get('congestion_pressure_moderate', False)),
        "congestion_fast_mode": str(facts.get('congestion_fast_mode', "") or ""),
        "congestion_hold_mode": str(facts.get('congestion_hold_mode', "") or ""),
        "congestion_trusted_rtt": bool(facts.get('congestion_trusted_rtt', False)),
        "congestion_rtt_base_pct": _optional_float(
            facts.get('congestion_rtt_base_pct', 0.0),
            valid=bool(facts.get('congestion_trusted_rtt', False)),
        ),
        "congestion_trusted_rsrq": bool(facts.get('congestion_trusted_rsrq', False)),
        "congestion_rsrq_available": bool(facts.get('congestion_rsrq_available', False)),
        "congestion_rsrq_contributes": bool(facts.get('congestion_rsrq_contributes', False)),
        "congestion_rsrq_reason": str(facts.get('congestion_rsrq_reason', "") or ""),
        "congestion_rsrq_pct": _optional_float(
            facts.get('congestion_rsrq_pct', 0.0),
            valid=bool(facts.get('congestion_rsrq_available', False)),
        ),
        "congestion_rsrq_gap_db": _optional_float(
            facts.get('congestion_rsrq_gap_db', 0.0),
            valid=bool(facts.get('congestion_rsrq_available', False)),
        ),
        "congestion_rsrq_gap_threshold_db": _optional_float(
            facts.get('congestion_rsrq_gap_threshold_db', 0.0),
            valid=True,
        ),
        "congestion_trusted_active_jitter": bool(facts.get('congestion_trusted_active_jitter', False)),
        "congestion_active_jitter_pct": _optional_float(
            facts.get('congestion_active_jitter_pct', 0.0),
            valid=bool(facts.get('congestion_trusted_active_jitter', False)),
        ),
        "mcs_collapse": bool(facts['mcs_collapse']),
        "state": str(state_name or ""),
        "action_lever": str(action_lever or ""),
        "action_reason": str(action_reason or ""),
        "action_text": str(action_text or ""),
        "rsrq_baseline": float(facts.get('rsrq_baseline', 0.0) or 0.0),
        "rsrq_baseline_valid": bool(facts.get('rsrq_baseline_valid', False)),
        "rsrq_gap": _optional_float(
            facts.get('rsrq_gap', 0.0),
            valid=bool(facts.get('rsrq_gap_valid', False)),
        ),
        "rsrq_gap_valid": bool(facts.get('rsrq_gap_valid', False)),
        "rsrq_delta_display_db": rsrq_delta_display_db,
        "rsrq_delta_display_valid": rsrq_delta_display_valid,
        "rsrq_delta_display_held": bool(facts.get("rsrq_delta_display_held", False)),
        "rsrq_delta_display_live_ts": float(facts.get("rsrq_delta_display_live_ts", 0.0) or 0.0),
        "rsrq_baseline_display_db": _optional_float(
            facts.get("rsrq_baseline_display_db", 0.0),
            valid=bool(facts.get("rsrq_baseline_display_valid", False)),
        ),
        "rsrq_baseline_display_valid": bool(facts.get("rsrq_baseline_display_valid", False)),
        "rsrq_baseline_display_held": bool(facts.get("rsrq_baseline_display_held", False)),
        "rsrq_baseline_display_live_ts": float(facts.get("rsrq_baseline_display_live_ts", 0.0) or 0.0),
    }
    row["rat_snapshot"] = build_rat_snapshot(row, profile=row)
    row.update(
        {
            "identity_code": str(facts.get("identity_code", "") or ""),
            "identity_status": str(facts.get("identity_status", "") or ""),
            "identity_catalog_version": str(
                facts.get("identity_catalog_version", "") or ""
            ),
            "identity_catalog_checksum": str(
                facts.get("identity_catalog_checksum", "") or ""
            ),
            "access_type_id": str(
                facts.get("access_type_id", "") or ""
            ),
            "profile_id": str(facts.get("profile_id", "") or ""),
            "session_scope_key": str(
                facts.get("session_scope_key", "") or ""
            ),
            "source_epoch": str(facts.get("source_epoch", "") or ""),
            "source_sequence": int(facts.get("sample_sequence", 0) or 0),
        }
    )
    try:
        row["evidence_scope"] = evidence_scope_from_mapping(
            row,
            formula_vector=telemetry_formula_vector(),
            observed_at=sample_ts,
        ).as_dict()
    except (TypeError, ValueError) as exc:
        row["evidence_quarantine"] = {
            "schema": "EvidenceQuarantine.v1",
            "owner": "telemetry_history_contract",
            "reason": str(exc),
            "identity_code": str(row.get("identity_code") or ""),
            "session_scope_key": str(row.get("session_scope_key") or ""),
            "source_epoch": str(row.get("source_epoch") or ""),
            "source_sequence": int(row.get("source_sequence", 0) or 0),
        }
    return row
