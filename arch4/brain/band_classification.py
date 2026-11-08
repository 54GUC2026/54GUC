"""
Shared band-probe classification and calibration sample building.

Single production authority for band-probe classification and calibration
sample construction.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from arch4.coerce import safe_bool


def classify_band_probe_outcome(
    *,
    cfg: Any,
    modem_after: Any,
    computed_after: Any,
    appeared: bool,
    is_primary: bool,
) -> tuple[str, bool]:
    """Classify band-selection result for learning/training.

    B818 may legally attach to a fallback band when the requested band is not
    reachable/active. That must be learned as a network reachability result,
    not misclassified as a modem fault.

    Returns (classification_label, weak_unusable_flag).
    """
    rtt_avg = computed_after.latency.rtt_sliding_avg if computed_after else 0.0
    weak_unusable = (
        modem_after.cellular.sinr_db <= cfg.night_probe_trigger_bad_rf_sinr_db
        or modem_after.cellular.rsrp_dbm <= cfg.night_probe_trigger_bad_rsrp_dbm
        or (rtt_avg > 0 and rtt_avg > cfg.rtt_cliff_ms)
    )

    if not modem_after.cellular.radio_connected:
        return "action_failed_or_modem_fault", False

    if appeared and is_primary:
        return ("weak_attach_unusable", True) if weak_unusable else ("requested_and_serving", False)
    if appeared:
        return ("weak_attach_unusable", True) if weak_unusable else ("requested_present_not_anchor", False)

    if modem_after.cellular.radio_connected:
        return "requested_absent_fallback_attach", weak_unusable

    return "action_failed_or_modem_fault", False


def build_calibration_candidate_sample(
    *,
    modem: Any,
    computed: Any,
    calibration_status: Dict[str, Any],
    health_status: Optional[Dict[str, Any]],
    state_name: str,
    now: float,
    learning_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a calibration candidate observation sample.

    Used by both live engine and sim replay to construct the sample dict
    that feeds calibration authority evaluation.
    """
    sample = {
        "ts": float(now),
        "sample_ts": float(getattr(modem.generation, 'sample_ts', 0.0) or now),
        "engine_tick_ts": float(getattr(modem.generation, 'accepted_ts', 0.0) or now),
        "sample_fresh": safe_bool(getattr(modem.generation, 'sample_fresh', True), True),
        "sample_sequence": int(getattr(modem.generation, 'generation', 0) or 0),
        "sample_age_s": float(getattr(modem.generation, 'sample_age_s', 0.0) or 0.0),
        "sample_source": str(getattr(modem.generation, 'sample_source', "") or ""),
        "state": str(state_name),
        "sinr_db": float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
        "rsrq_db": float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0),
        "rsrq_source": str(getattr(modem.cellular, 'rsrq_source', "reported") or "reported"),
        "rsrp_dbm": float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0),
        "rssi_dbm": float(getattr(modem.cellular, 'rssi_dbm', 0.0) or 0.0),
        "rtt_ms": float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0),
        "rtt_sliding_avg": float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0),
        "rtt_dl_jitter_pct": float(
            getattr(computed.latency, 'rtt_dl_jitter_pct', 0.0) or 0.0
        ),
        "rtt_dl_jitter_valid": bool(
            getattr(computed.latency, 'rtt_dl_jitter_valid', False)
        ),
        "ca_stat_ld_pct": float(getattr(computed.quality, 'ca_stat_ld_pct', 0.0) or 0.0),
        "ca_stat_ld_valid": bool(getattr(computed.quality, 'ca_stat_ld_valid', False)),
        "throughput_dl_mbps": float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0),
        "throughput_ul_mbps": float(getattr(modem.traffic, 'ul_mbps', 0.0) or 0.0),
        "ca_active": bool(getattr(modem.cellular, 'ca_active', False)),
        "carrier_count": int(getattr(modem.cellular, 'carrier_count', 0) or 0),
        "primary_band": int(getattr(modem.cellular, 'primary_band', 0) or 0),
        "serving_band": int(getattr(modem.cellular, 'serving_band', 0) or 0),
        "pci": int(getattr(modem.cellular, 'pci', 0) or 0),
        "earfcn": int(
            getattr(modem.cellular, 'earfcn_dl', 0)
            or getattr(modem.cellular, 'earfcn_dl', 0)
            or 0
        ),
        "radio_connected": bool(getattr(modem.cellular, 'radio_connected', False)),
        "ri": int(getattr(modem.cellular, 'ri', 0) or 0),
        "band_signature": str(
            getattr(modem.cellular, 'band_set_signature', "")
            or getattr(modem.cellular, 'ca_band_signature', "")
            or ""
        ),
        "ca_persistence_ratio": float(calibration_status.get("ca_persistence_ratio", 0.0) or 0.0),
        "network_load_biased": safe_bool(calibration_status.get("network_load_biased", False), False),
        "network_load_summary": str(calibration_status.get("network_load_summary", "") or ""),
        "network_load_reason": str(calibration_status.get("network_load_reason", "") or ""),
        "offpeak_window": safe_bool(calibration_status.get("offpeak_window", False), False),
        "gap_score": float(calibration_status.get("gap_score", 0.0) or 0.0),
        "baseline_ready": safe_bool(calibration_status.get("baseline_ready", False), False),
        "needs_calibration": safe_bool(calibration_status.get("needs_calibration", False), False),
        "baseline_drift_detected": safe_bool(calibration_status.get("baseline_drift_detected", False), False),
        "recalibration_recommended": safe_bool(calibration_status.get("recalibration_recommended", False), False),
        "threshold_adjust_hint_db": float(calibration_status.get("threshold_adjust_hint_db", 0.0) or 0.0),
        "health_ok": safe_bool(calibration_status.get("health_ok", True), True),
        "health_overall_ok": safe_bool((health_status or {}).get("overall_ok", True), True),
        "health_stage": str((health_status or {}).get("stage", "") or ""),
    }
    if isinstance(learning_gate, dict) and learning_gate:
        gate = dict(learning_gate)
        sample["learning_gate"] = gate
        sample["learning_allowed"] = safe_bool(gate.get("learning_allowed", True), True)
        sample["learning_block_reason"] = str(gate.get("block_reason", "") or "")
        sample["sample_stale"] = safe_bool(gate.get("sample_stale", False), False)
    return sample
