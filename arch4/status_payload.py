"""
arch4/status_payload.py

Runtime-neutral status payload shaping shared by engine and boundary projections.

This module produces the canonical status payload dict used by the runtime.
Compatibility aliases for replay/export consumers are added explicitly at the
export edge, not mixed into the canonical builder.

FIELD GROUPS
============

BAND SIGNATURES
  serving_band         : int    - PCell band number
  serving_band_signature : str  - "B3" format
  selected_band_set    : list   - Operator-selected bands (from modem lock)
  selected_band_set_signature : str - "B1+B3" format
  active_band_set      : list   - Currently active bands (PCell + SCells)
  active_band_set_signature : str - "B1+B3" format
  band_set             : list   - Same as active_band_set (operator display)
  band_set_signature   : str    - Same as active_band_set_signature
  ca_state_text        : str    - "B1+B3" or "B3" (human-readable)

TRAFFIC
  has_traffic          : bool   - True if DL/UL > 0.01 Mbps
  traffic_active       : bool   - Legacy alias for has_traffic

CELL IDENTITY
  pci                  : int    - Physical Cell ID
  earfcn_dl            : int    - DL EARFCN
  enb_id               : int    - eNodeB ID
  cell_id              : int    - Modem-reported LTE Cell ID; derived only if missing

RF SIGNALS
  sinr_db              : float  - Signal quality (dB)
  rsrp_dbm             : float  - Reference signal power (dBm)
  rsrq_db              : float  - Reference signal quality (dB)
  rssi_dbm             : float  - Received signal strength (dBm)

MODULATION
  mcs_dl, mcs_ul       : int    - Modulation/Coding Scheme
  cqi                  : int    - Channel Quality Indicator (0-15)
  ri                   : int    - Rank Indicator (MIMO layers)
  qam_dl_primary/secondary : int - QAM level (16/64/256)

THROUGHPUT
  dl_mbps, ul_mbps     : float  - Current throughput
  rx_bytes_per_sec     : float  - Raw byte counters
  tx_bytes_per_sec     : float

RTT & LATENCY
  rtt_ms               : float  - Raw RTT measurement
  rtt_sliding_avg      : float  - Smoothed RTT
  rtt_baseline         : float  - Learned quiet-time RTT
  rtt_effective_baseline : float - Baseline used for delta
  rtt_delta            : float  - Current - baseline
  rtt_delta_valid      : bool   - True if delta is meaningful

CELL QUALITY (CQ)
  cq_cell              : float  - Live connection quality score (0-1)
  cq_cell_valid        : bool   - True if score is reliable

BAND QUALITY (BQ)
  bq_cell              : float  - Structural band/cell quality score (0-1)
  bq_cell_valid        : bool   - True if BQ has enough samples

CONGESTION SIGNALS
  congestion_pct       : float  - Overall congestion (0-100)
  rsrq_baseline        : float  - Learned good RSRQ for this cell
  rsrq_gap             : float  - Current RSRQ delta from baseline
  rsrq_gap_valid       : bool   - True if gap is meaningful

DELIVERED CAPACITY
  delivered_capacity_ratio : float - best controlled DL speed-test / conservative LTE ceiling
  delivered_capacity_valid : bool  - True only after controlled speed-test evidence

LEGACY ALIASES (compat/export edge only)
  band                 : int    - Same as serving_band
  primary_band         : int    - Same as serving_band
  traffic_active       : bool   - Same as has_traffic
"""

from __future__ import annotations

from arch4.controller_frames import (
    AcceptedControllerGeneration,
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
    project_controller_read_model,
)

from typing import Any, Dict, Mapping, Optional
import time

from arch4.interfaces import (
    Constraints,
    band_signature,
    canonical_state_name,
    normalize_band_list,
    normalize_band_fields,
    parse_band_signature,
)
from arch4.runtime_identity import coerce_provider_plmn_text
from arch4.runtime_sidecar_health import format_runtime_iso_utc
from arch4.modem.live_translation import (
    apply_scheduler_field_trust,
    scheduler_field_trust as modem_live_scheduler_field_trust,
)
from arch4.canonical_scheduler_display import (
    build_scheduler_display_contract,
    build_scheduler_display_surface,
)
from arch4.brain.referee_publication import (
    merge_operator_summary_into_brain_referee_publication,
)
from arch4.operator_projection import build_operator_projection
from arch4.coerce import (
    safe_float as _safe_float,
    safe_int as _safe_int,
    safe_getattr as _safe_getattr,
)
import logging

log = logging.getLogger("arch4.status_payload")


_LEGACY_STATUS_KEYS = {
    "provider",
    "plmn",
    "band",
    "pband",
    "primary_band",
    "traffic_active",
    "ca_active",
    "ca_realtime_state",
    "ca_stat_pct",
    "ca_realtime_ld_state",
    "ca_realtime_ld_valid",
    "ca_stat_ld_pct",
    "ca_stat_ld_valid",
    "ca_change_per_hour",
    "ca_ld_change_per_hour",
    "ca_active_pct",
    "ca_active_session_pct",
    "nr5g_active",
    "nr5g_band",
    "nr5g_pci",
    "nr5g_arfcn",
    "nr5g_rsrp_dbm",
    "nr5g_rsrq_db",
    "nr5g_sinr_db",
}


def add_legacy_status_aliases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add compatibility aliases at the final export edge only."""
    out = dict(payload or {})
    normalize_throughput_rate_aliases(out)
    _apply_customer_display_derivatives(out)
    out["provider"] = str(out.get("provider_name", "") or "")
    out["plmn"] = str(out.get("provider_plmn", "") or "")
    out["band"] = _safe_int(out.get("serving_band", 0), 0)
    out["pband"] = _safe_int(out.get("serving_band", 0), 0)
    out["primary_band"] = _safe_int(out.get("serving_band", 0), 0)
    out["traffic_active"] = bool(out.get("has_traffic", False))
    cart = _safe_int(out.get("cart", 0), 0)
    cast = max(0.0, min(100.0, _safe_float(out.get("cast", 0.0), 0.0)))
    cart_ld = _safe_int(out.get("cart_ld", 0), 0)
    cart_ld_valid = bool(out.get("cart_ld_valid", False))
    cart_ld_display_raw = out.get("cart_ld_display")
    cart_ld_display_valid_raw = out.get("cart_ld_display_valid")
    cart_ld_display = _safe_int(
        cart_ld_display_raw if cart_ld_display_raw not in (None, "") else cart_ld,
        0,
    )
    cart_ld_display_valid = (
        bool(cart_ld_display_valid_raw)
        if cart_ld_display_valid_raw is not None
        else cart_ld_valid
    )
    cast_ld = max(0.0, min(100.0, _safe_float(out.get("cast_ld", 0.0), 0.0)))
    cast_ld_valid = bool(out.get("cast_ld_valid", False))
    ca_stat_ld = max(0.0, min(100.0, _safe_float(out.get("ca_stat_ld_pct", 0.0), 0.0)))
    ca_stat_ld_valid = bool(out.get("ca_stat_ld_valid", False))
    cart_activity = max(
        0.0,
        _safe_float(out.get("cart_activity_per_hour", 0.0), 0.0),
    )
    cart_ld_activity = max(
        0.0,
        _safe_float(out.get("cart_ld_activity", 0.0), 0.0),
    )
    out["cart"] = cart
    out["cast"] = cast
    out["cart_ld"] = cart_ld
    out["cart_ld_valid"] = cart_ld_valid
    out["cart_ld_display"] = cart_ld_display
    out["cart_ld_display_valid"] = cart_ld_display_valid
    out["cast_ld"] = cast_ld
    out["cast_ld_valid"] = cast_ld_valid
    out["cart_activity_per_hour"] = cart_activity
    out["cart_ld_activity"] = cart_ld_activity
    out["ri_stability_pct"] = max(0.0, min(100.0, _safe_float(out.get("ri_stability_pct", 0.0), 0.0)))
    out["ri_activity_per_hour"] = max(0.0, _safe_float(out.get("ri_activity_per_hour", 0.0), 0.0))
    out["ri_stability_valid"] = bool(out.get("ri_stability_valid", False))
    if "ca_active" in out and out.get("ca_active") not in (None, ""):
        out["ca_active"] = bool(out.get("ca_active"))
    else:
        out["ca_active"] = bool(cart)
    out["ca_realtime_state"] = cart
    out["ca_stat_pct"] = cast
    out["ca_realtime_ld_state"] = cart_ld
    out["ca_realtime_ld_valid"] = cart_ld_valid
    out["ca_stat_ld_pct"] = ca_stat_ld
    out["ca_stat_ld_valid"] = ca_stat_ld_valid
    out["ca_change_per_hour"] = cart_activity
    out["ca_ld_change_per_hour"] = cart_ld_activity
    out["ca_active_pct"] = cast
    out["ca_active_session_pct"] = cast

    nr_band = _safe_int(out.get("nr_band", 0), 0)
    nr_active = bool(out.get("nr_secondary_active", False))
    out["nr5g_active"] = nr_active
    out["nr5g_band"] = nr_band
    out["nr5g_pci"] = _safe_int(out.get("nr_pci", 0), 0)
    out["nr5g_arfcn"] = _safe_int(out.get("nr_arfcn", 0), 0)
    out["nr5g_rsrp_dbm"] = _safe_float(out.get("nr_rsrp_dbm", 0.0), 0.0)
    out["nr5g_rsrq_db"] = _safe_float(out.get("nr_rsrq_db", 0.0), 0.0)
    out["nr5g_sinr_db"] = _safe_float(out.get("nr_sinr_db", 0.0), 0.0)
    out["nr5g_bandwidth_mhz"] = _safe_float(out.get("nr_bandwidth_mhz", 0.0), 0.0)
    out["nr5g_scs_khz"] = _safe_float(out.get("nr_scs_khz", 0.0), 0.0)
    return out


def normalize_throughput_rate_aliases(payload: Dict[str, Any]) -> None:
    """Keep Mbps and byte-per-second current-rate aliases consistent."""
    if not isinstance(payload, dict):
        return
    if payload.get("traffic_valid") is False:
        return

    rx_bps = max(0.0, _safe_float(payload.get("rx_bytes_per_sec"), 0.0))
    tx_bps = max(0.0, _safe_float(payload.get("tx_bytes_per_sec"), 0.0))
    dl_mbps = max(0.0, _safe_float(payload.get("dl_mbps"), 0.0))
    ul_mbps = max(0.0, _safe_float(payload.get("ul_mbps"), 0.0))
    current_rx_bps = max(0.0, _safe_float(payload.get("traffic_current_download_rate_bps"), 0.0))
    current_tx_bps = max(0.0, _safe_float(payload.get("traffic_current_upload_rate_bps"), 0.0))

    if rx_bps <= 0.0 and dl_mbps > 0.0:
        rx_bps = dl_mbps * 1_000_000.0 / 8.0
        payload["rx_bytes_per_sec"] = rx_bps
    if tx_bps <= 0.0 and ul_mbps > 0.0:
        tx_bps = ul_mbps * 1_000_000.0 / 8.0
        payload["tx_bytes_per_sec"] = tx_bps
    if dl_mbps <= 0.0 and rx_bps > 0.0:
        dl_mbps = rx_bps * 8.0 / 1_000_000.0
        payload["dl_mbps"] = dl_mbps
    if ul_mbps <= 0.0 and tx_bps > 0.0:
        ul_mbps = tx_bps * 8.0 / 1_000_000.0
        payload["ul_mbps"] = ul_mbps

    if current_rx_bps <= 0.0 and rx_bps > 0.0:
        payload["traffic_current_download_rate_bps"] = rx_bps
    if current_tx_bps <= 0.0 and tx_bps > 0.0:
        payload["traffic_current_upload_rate_bps"] = tx_bps


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


def _apply_customer_display_derivatives(payload: Dict[str, Any]) -> None:
    """Publish customer-facing derivatives from canonical modem/status truth.

    These fields are intentionally derived at the status boundary so telemetry,
    current_state, and GUI consumers do not each rebuild their own CA-pool or
    traffic-volume truth.
    """
    if not isinstance(payload, dict):
        return

    configured_pool = normalize_band_list(payload.get("configured_band_pool"))
    configured_sig = str(payload.get("configured_band_pool_signature") or "").strip()
    configured_display = configured_sig or band_signature(configured_pool)
    if configured_display:
        payload["configured_ca_pool_display"] = configured_display

    day_used_bytes = payload.get("traffic_day_used_bytes")
    day_used_gb = _gb_from_bytes(day_used_bytes)
    if day_used_gb is not None:
        payload["volume_used_today_gb"] = day_used_gb

def _carrier_rows(modem: CellularIntelligenceGeneration) -> list[dict]:
    try:
        active = [
            c
            for c in (modem.cellular.carriers or [])
            if getattr(c, "is_pcell", False) or int(getattr(c, "state", 0) or 0) >= 1
        ]
        active.sort(
            key=lambda c: (
                0 if getattr(c, "is_pcell", False) else 1,
                int(getattr(c, "band", 0) or 0),
            )
        )
        rows = []
        for c in active[:3]:
            bw = getattr(c, "bandwidth_mhz", None)
            try:
                bw_val = float(bw) if bw is not None else None
            except Exception as exc:
                log.debug("_carrier_rows: bw coerce suppressed %s", exc)
                bw_val = None
            rows.append(
                {
                    "band": int(getattr(c, "band", 0) or 0),
                    "dl_mhz": bw_val,
                    "ul_mhz": bw_val,
                    "pcell": bool(getattr(c, "is_pcell", False)),
                }
            )
        return rows
    except Exception as exc:
        log.debug("_carrier_rows: suppressed %s", exc)
        return []


def _carrier_details(modem: CellularIntelligenceGeneration) -> list[dict]:
    def optional(carrier, field, convert):
        value=getattr(carrier,field,None)
        return convert(value) if value is not None else None
    try:
        active = [
            c
            for c in (modem.cellular.carriers or [])
            if getattr(c, "is_pcell", False) or int(getattr(c, "state", 0) or 0) >= 1
        ]
        active.sort(
            key=lambda c: (
                0 if getattr(c, "is_pcell", False) else 1,
                int(getattr(c, "band", 0) or 0),
            )
        )
        out = []
        for c in active[:3]:
            bw = getattr(c, "bandwidth_mhz", None)
            try:
                bw_val = float(bw) if bw is not None else None
            except Exception as exc:
                log.debug("_carrier_details: bw coerce suppressed %s", exc)
                bw_val = None
            out.append(
                {
                    "band": int(getattr(c, "band", 0) or 0),
                    "earfcn": optional(c,"earfcn",int),
                    "pci": optional(c,"pci",int),
                    "bandwidth_mhz": bw_val,
                    "sinr_db": optional(c,"sinr_db",float),
                    "rsrp_dbm": optional(c,"rsrp_dbm",float),
                    "state": int(getattr(c, "state", 0) or 0),
                    "is_pcell": bool(getattr(c, "is_pcell", False)),
                }
            )
        return out
    except Exception as exc:
        log.debug("_carrier_details: suppressed %s", exc)
        return []


def _active_bands_signature(modem: CellularIntelligenceGeneration) -> str:
    """
    Derive live active-band signature from carrier telemetry.

    Keep PCC always and include SCC when modem marks it active (state >= 1).
    This keeps GUI aligned with modem-reported active CA composition.
    """
    try:
        bands: set[int] = set()
        for c in (modem.cellular.carriers or []):
            try:
                band = int(getattr(c, "band", 0) or 0)
            except Exception as exc:
                log.debug("_active_bands_signature: band coerce suppressed %s", exc)
                band = 0
            if band <= 0:
                continue
            is_pcell = bool(getattr(c, "is_pcell", False))
            state = int(getattr(c, "state", 0) or 0)
            if is_pcell:
                bands.add(band)
                continue
            if state >= 1:
                bands.add(band)
        if bands:
            return "+".join(f"B{b}" for b in sorted(bands))
    except Exception as exc:
        log.debug("_active_bands_signature: suppressed %s", exc)
    return ""


def _selected_band_set(modem: Any, base_status: Dict[str, Any]) -> list[int]:
    bands = normalize_band_list(list(getattr(modem.cellular, 'selected_band_set', []) or []))
    if bands:
        return bands
    return normalize_band_list(base_status.get("selected_band_set"))


def _configured_band_pool(
    modem: Any,
    base_status: Dict[str, Any],
) -> list[int]:
    bands = normalize_band_list(list(getattr(modem.identity, 'configured_band_pool', []) or []))
    if not bands:
        bands = normalize_band_list(base_status.get("configured_band_pool"))
    if not bands:
        bands = normalize_band_list(list(getattr(modem.cellular, 'selected_band_set', []) or []))
    if not bands:
        bands = normalize_band_list(base_status.get("selected_band_set"))
    return list(bands or [])


def _configured_band_pool_signature(
    modem: Any,
    base_status: Dict[str, Any],
    bands: list[int],
) -> str:
    sig = str(getattr(modem.identity, 'configured_band_pool_signature', "") or "")
    if not sig:
        sig = str(base_status.get("configured_band_pool_signature", "") or "")
    if not sig:
        sig = str(getattr(modem.cellular, 'selected_band_set_signature', "") or "")
    if not sig:
        sig = str(base_status.get("selected_band_set_signature", "") or "")
    if sig:
        return sig
    return band_signature(bands) or ""


def _configured_band_pool_source(
    modem: Any,
    base_status: Dict[str, Any],
    bands: list[int],
) -> str:
    source = str(getattr(modem.identity, 'configured_band_pool_source', "") or "")
    if not source:
        source = str(base_status.get("configured_band_pool_source", "") or "")
    if not source and bands:
        source = "selected_band_set_fallback"
    return source


def _configured_band_pool_status(base_status: Dict[str, Any]) -> str:
    return str(base_status.get("configured_band_pool_status", "") or "").strip()


def _configured_band_pool_age_s(base_status: Dict[str, Any]) -> Optional[float]:
    value = base_status.get("configured_band_pool_age_s")
    try:
        if value is None:
            return None
        return max(0.0, float(value))
    except Exception:
        return None


def _configured_band_pool_last_sample_ts(base_status: Dict[str, Any]) -> float:
    value = base_status.get("configured_band_pool_last_sample_ts", 0.0)
    return max(0.0, _safe_float(value, 0.0))


def _active_band_set(modem: Any) -> list[int]:
    bands = normalize_band_list(list(getattr(modem.cellular, 'active_band_set', []) or []))
    if bands:
        return bands
    bands = normalize_band_list([row.get("band", 0) for row in _carrier_rows(modem)])
    if bands:
        return bands
    return []


def _neighbors(modem: CellularIntelligenceGeneration) -> list[dict]:
    neigh = getattr(modem.cellular, 'neighbors', None) or []
    return [
        {
            "pci": int(getattr(n, "pci", 0)),
            "band": int(getattr(n, "band", 0)),
            "earfcn": int(getattr(n, "earfcn", 0)),
            "rsrp_dbm": float(getattr(n, "rsrp_dbm", -999)),
            "rsrq_db": float(getattr(n, "rsrq_db", -999)),
        }
        for n in neigh
    ]


def _build_operator_summary(
    *,
    modem: CellularIntelligenceGeneration,
    computed: Any,
    state_name: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the plain-English brain status summary for the operator.

    Operator model:
      The brain drives the modem most of the time.
      The operator sets the course and steps in when needed.

      This summary tells the operator three things:
        1. operator_status    — what is the brain doing right now (always shown)
        2. pilot_recommendation — what the brain thinks the pilot should consider
                                  (empty string = nothing needed, brain has it)
        3. pilot_recommendation_kind — machine-readable category for GUI routing

      Recommendation kinds:
        "none"            — brain has everything under control
        "calibration"     — consider running calibration (antenna drift, new install)
        "manual_band"     — brain suggests a specific band set the pilot can confirm
        "check_hardware"  — something the brain cannot fix (cable, antenna, ISP)
        "review_incident" — unusual pattern, brain is uncertain, pilot should look

    Language rules:
      - No jargon: "signal quality" not "SINR", "network is busy" not "congestion_pressure"
      - Active voice: "Brain switched to B20" not "B20 was switched to"
      - Short: one sentence for status, one sentence for recommendation
      - Honest uncertainty: "Brain is not sure why…" is better than false confidence
    """
    status_parts = []
    recommendation = ""
    rec_kind = "none"

    canonical_action_outcome = (
        payload.get("canonical_action_outcome") if isinstance(payload.get("canonical_action_outcome"), dict) else {}
    )
    canonical_incident_state = (
        payload.get("canonical_incident_state") if isinstance(payload.get("canonical_incident_state"), dict) else {}
    )
    operator_projection = (
        dict(payload.get("canonical_operator_projection"))
        if isinstance(payload.get("canonical_operator_projection"), dict)
        and payload.get("canonical_operator_projection")
        else build_operator_projection(
            action_outcome=canonical_action_outcome,
            incident_state=canonical_incident_state,
        )
    )
    incident_class = str(canonical_incident_state.get("incident_class") or "healthy")
    probable_cause = str(canonical_incident_state.get("probable_cause") or "none")
    brain_health_message = str(canonical_incident_state.get("user_message") or "").strip()
    brain_health_status = str(canonical_incident_state.get("health_status") or "").strip().lower()
    brain_health_confidence = float(canonical_incident_state.get("health_confidence") or 0.0)
    brain = payload.get("brain") or {}
    decision_context = (
        brain.get("decision_context") if isinstance(brain.get("decision_context"), dict) else {}
    )
    execution_outcome = (
        brain.get("execution_outcome") if isinstance(brain.get("execution_outcome"), dict) else {}
    )
    warning_class = str((decision_context.get("warning_class", brain.get("warning_class", ""))) or "")
    warning_source = str((decision_context.get("warning_source", brain.get("warning_source", ""))) or "")
    warning_mechanism = str((decision_context.get("warning_mechanism", brain.get("warning_mechanism", ""))) or "")
    emergency_origin_class = str(brain.get("emergency_origin_class", "") or "")
    calibration = payload.get("calibration") or {}
    gap_score = float(calibration.get("gap_score", 0.0) or 0.0)
    needs_calib = bool(calibration.get("needs_calibration", False))
    drift_detected = bool(calibration.get("baseline_drift_detected", False))
    drift_summary = str(calibration.get("baseline_drift_summary", "") or "")
    recalibration_recommended = bool(calibration.get("recalibration_recommended", False))
    threshold_hint_db = float(calibration.get("threshold_adjust_hint_db", 0.0) or 0.0)
    action_text = str(
        payload.get("action_text")
        or execution_outcome.get("action_reason")
        or ""
    )
    state = canonical_state_name(state_name)
    load_trend = str(brain.get("load_trend", "") or "")
    cliff_ahead = bool(brain.get("cliff_ahead", False))
    radio_connected = bool(modem.cellular.radio_connected)
    sinr = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
    rtt = float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0)
    congestion_pct = float(getattr(computed.quality, 'congestion_pct', 0.0) or 0.0)
    congestion_bucket = str(getattr(computed.quality, 'congestion_bucket', "") or "").strip().lower()
    congestion_fast_high = bool(
        bool(getattr(computed.quality, 'congestion_pct_valid', False))
        and (congestion_bucket == "high" or congestion_pct >= 60.0)
    )
    projection_status_text = str(operator_projection.get("status_text", "") or "").strip()

    # ── Autopilot status sentence ────────────────────────────────────────
    if projection_status_text:
        status_parts.append(projection_status_text)
    elif (
        brain_health_message
        and incident_class != "healthy"
        and brain_health_status in {"confirmed", "investigating"}
        and brain_health_confidence >= 0.50
    ):
        status_parts.append(brain_health_message)
    elif state == "E_RADIO_DOWN" and emergency_origin_class == "E_SESSION_RESET":
        status_parts.append("Modem control session reset observed. Brain is collecting evidence while the link stabilizes.")
    elif not radio_connected:
        status_parts.append("Radio not connected. Brain is waiting for the modem to reconnect.")
    elif incident_class == "radio_disconnected":
        status_parts.append("Radio disconnected. Brain is attempting to recover the connection.")
    elif incident_class == "rf_event_spike":
        status_parts.append("Sudden signal drop detected. Brain is switching to emergency recovery.")
    elif incident_class == "rf_collapse":
        status_parts.append("RF cliff detected. Brain is in low-action RF recovery hold.")
    elif incident_class == "data_path_degraded":
        status_parts.append("Modem signal is still present, but the data path is failing. Brain is holding radio changes.")
    elif incident_class == "radio_ca_loss":
        status_parts.append("Carrier aggregation dropped under load. Brain is evaluating radio recovery.")
    elif incident_class == "ca_capacity_reduced_observe":
        status_parts.append("Carrier aggregation is reduced, but traffic and signal still look usable. Brain is observing.")
    elif warning_class == "congestion" and warning_source == "local_traffic":
        status_parts.append("Heavy local traffic is filling the pipe. Brain is avoiding radio churn.")
    elif state in {"WORKING", "OPTIMIZING"}:
        if congestion_fast_high:
            status_parts.append("Network delay is high. Brain is confirming congestion before changing radio setup.")
        elif cliff_ahead:
            status_parts.append("Signal is good. Brain is preparing for expected congestion in the next hour.")
        else:
            status_parts.append("Signal is good. Autopilot is running normally.")
    elif state == "DEGRADED":
        status_parts.append("Signal quality has dropped. Brain is investigating the cause.")
    elif state == "CONGESTED":
        status_parts.append("Network is busy. Brain is adapting carefully.")
    elif state == "E_RADIO_DOWN":
        status_parts.append("Radio connection is down. Brain is attempting recovery.")
    elif state == "BAD_RF":
        status_parts.append("RF conditions are poor. Brain is in low-action hold and monitoring for recovery.")
    elif state == "E_SESSION_RESET":
        status_parts.append("Modem session reset detected. Brain is stabilizing the connection.")
    elif state == "E_CONGESTION_STORM":
        status_parts.append("Cell congestion is severe. Brain is holding recovery actions carefully.")
    else:
        status_parts.append("Autopilot active.")

    # Append what the brain just did, if anything
    if action_text and "none" not in action_text.lower():
        # Translate technical action text to plain English
        action_plain = _action_text_to_plain(action_text)
        if action_plain:
            status_parts.append(action_plain)

    # ── Pilot recommendation ─────────────────────────────────────────────
    if drift_detected and recalibration_recommended:
        recommendation = (
            drift_summary
            or "RF baseline appears to have shifted from the learned norm. "
               "Consider recalibration when conditions are quiet."
        )
        if threshold_hint_db > 0.0:
            recommendation += f" Suggested temporary tolerance shift: about {threshold_hint_db:.1f} dB."
        rec_kind = "calibration"
    elif needs_calib and gap_score >= 0.65:
        recommendation = (
            "Calibration recommended: signal has drifted from baseline. "
            "Run a calibration survey to update the antenna position profile."
        )
        rec_kind = "calibration"

    elif incident_class in {"rf_collapse", "rf_event_spike"} and sinr < 3.0:
        recommendation = (
            "Signal is very weak. Check the antenna cable and modem position. "
            "The brain cannot fix a physical hardware issue."
        )
        rec_kind = "check_hardware"

    elif probable_cause == "uncertain" and rtt > 200.0:
        recommendation = (
            "Brain is not sure what is causing the slow network. "
            "If this persists, consider checking the ISP connection or restarting the modem manually."
        )
        rec_kind = "review_incident"

    return {
        "status": " ".join(status_parts),
        "pilot_recommendation": recommendation,
        "pilot_recommendation_kind": rec_kind,
        # Machine-readable extras for GUI routing
        "incident_class": incident_class,
        "probable_cause": probable_cause,
        "warning_class": warning_class,
        "warning_source": warning_source,
        "warning_mechanism": warning_mechanism,
        "cliff_ahead": cliff_ahead,
        "load_trend": load_trend,
        "canonical_action_scope": str(canonical_action_outcome.get("action_scope", "") or ""),
        "canonical_action_semantic": str(canonical_action_outcome.get("semantic", "") or ""),
        "canonical_projection_class": str(operator_projection.get("projection_class", "") or ""),
    }


def _action_text_to_plain(action_text: str) -> str:
    """Translate technical brain action text to a short plain-English sentence."""
    t = action_text.lower()
    if not t or t.startswith("none"):
        return ""
    if "ltm score" in t or "explore: add" in t or "explore:" in t:
        # Extract band info if possible
        import re
        bands = re.findall(r"b(\d+)", t)
        if bands:
            band_str = " and B".join(bands) if len(bands) > 1 else f"B{bands[0]}"
            return f"Brain is testing {band_str} to see if it improves performance."
        return "Brain is testing an alternative band configuration."
    if "cliff ahead" in t or "cliff" in t:
        return "Brain is switching configuration to prepare for expected network load."
    if "restore" in t and "ca" in t:
        return "Brain is restoring the multi-band (CA) configuration."
    if "emergency" in t or "recovery" in t:
        return "Brain is executing emergency recovery."
    if "neighbor" in t or "cell" in t and "lock" in t:
        return "Brain is trying a nearby cell with a stronger signal."
    if "night" in t or "probe" in t:
        return "Brain is running a quiet-hours band survey."
    return ""


def _learning_status(
    historical: Any,
    memory: Any,
    *,
    durable_memory_runtime_status: Mapping[str, Any],
) -> Dict[str, Any]:
    total_hist_samples = 0
    historical_state: Dict[str, Any] = {}
    if historical:
        try:
            total_hist_samples = int(historical.total_samples())
        except Exception as exc:
            log.debug("_learning_status: total_samples suppressed %s", exc)
            historical_state = historical.get_state_for_save()
            hours = historical_state.get("hours", {})
            if isinstance(hours, dict):
                for h in hours.values():
                    if isinstance(h, dict):
                        total_hist_samples += int(h.get("samples", 0) or 0)
    out: Dict[str, Any] = {
        "memory_actions": len(getattr(memory, "action_history", []) or []),
        "memory_cells": len(getattr(memory, "cells", {}) or {}),
        "memory_baselines": len(getattr(memory, "baselines", {}) or {}),
        "historical_total_samples": total_hist_samples,
        "historical_journal_hours": _historical_journal_hours_display(
            historical_state=historical_state,
            historical=historical,
        ),
    }
    if durable_memory_runtime_status:
        out["persistence"] = dict(durable_memory_runtime_status)
    if historical is not None and hasattr(historical, "get_runtime_status"):
        try:
            runtime = historical.get_runtime_status()
            if isinstance(runtime, dict):
                out["historical_persistence"] = runtime
        except Exception as exc:
            log.debug("_learning_status: historical runtime suppressed %s", exc)
    return out


def _historical_journal_hours_display(
    *,
    historical_state: Any,
    historical: Any,
) -> Dict[int, Dict[str, Any]]:
    state = historical_state if isinstance(historical_state, dict) else {}
    hours = state.get("hours", {})
    if not isinstance(hours, dict) or not hours:
        hours = getattr(historical, "hours", {}) if historical is not None else {}
    if not isinstance(hours, dict):
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for h_str, hp in hours.items():
        try:
            hour = int(h_str) % 24
        except (TypeError, ValueError):
            continue
        if isinstance(hp, dict):
            samples = hp.get("samples", 0)
            avg_sinr = hp.get("avg_sinr", 0.0)
            avg_rtt = hp.get("avg_rtt", 0.0)
            avg_throughput = hp.get("avg_throughput_mbps", 0.0)
        else:
            samples = getattr(hp, "samples", 0)
            avg_sinr = getattr(hp, "avg_sinr", 0.0)
            avg_rtt = getattr(hp, "avg_rtt", 0.0)
            avg_throughput = getattr(hp, "avg_throughput_mbps", 0.0)
        out[hour] = {
            "samples": int(samples or 0),
            "avg_sinr": float(avg_sinr or 0.0),
            "avg_rtt": float(avg_rtt or 0.0),
            "avg_throughput_mbps": float(avg_throughput or 0.0),
        }
    return out


def attach_learning_status(
    payload: Dict[str, Any],
    historical: Optional[Any],
    memory: Optional[Any],
    *,
    durable_memory_runtime_status: Mapping[str, Any],
) -> Dict[str, Any]:
    if historical is None or memory is None:
        return payload
    try:
        learning = _learning_status(
            historical,
            memory,
            durable_memory_runtime_status=durable_memory_runtime_status,
        )
        if "brain" in payload and isinstance(payload["brain"], dict):
            payload["brain"]["learning"] = learning
        else:
            payload["brain"] = {"learning": learning}
    except Exception as exc:
        log.debug("attach_learning_status: suppressed %s", exc)
    return payload


def _apply_identity_connection_fields(
    out: Dict[str, Any],
    *,
    raw_base_status: Dict[str, Any],
    modem: CellularIntelligenceGeneration,
    constraints: Optional[Constraints],
    state_name: str,
) -> float:
    """Project identity and connection fields into the canonical payload."""
    provider_name = modem.identity.provider_name
    provider_plmn = coerce_provider_plmn_text(modem.identity.provider_plmn)
    if constraints:
        provider_name = provider_name or (constraints.provider_name or "")
        provider_plmn = provider_plmn or coerce_provider_plmn_text(constraints.provider_plmn)

    out["provider_name"] = str(provider_name or "")
    out["provider_plmn"] = coerce_provider_plmn_text(provider_plmn)
    now_ts = _safe_float(modem.generation.sample_ts, 0.0) or time.time()
    out["ts"] = now_ts
    out["iso"] = format_runtime_iso_utc(now_ts)
    out["backend"] = str(raw_base_status.get("backend", "") or "")
    out["vendor"] = str(modem.identity.vendor or "")
    out["model"] = str(modem.identity.model or "")
    out["firmware"] = str(
        modem.identity.firmware or modem.identity.software_version or ""
    )
    out["software_version"] = str(modem.identity.software_version or "")
    out["webui"] = str(modem.identity.webui or "")
    out["hardware_version"] = str(modem.identity.hardware_version or "")
    for identity_key in ("serial", "imei", "imsi", "config_version", "lan_mac", "wan_ip"):
        out[identity_key] = str(raw_base_status.get(identity_key, "") or "")
    provider_full_name = str(
        raw_base_status.get("provider_full_name")
        or raw_base_status.get("network_provider_fullname")
        or ""
    )
    if provider_full_name:
        out["network_provider_fullname"] = provider_full_name
    out["radio_connected"] = bool(modem.cellular.radio_connected)
    out["state"] = canonical_state_name(state_name)
    out["network_type"] = str(modem.identity.network_type or "")
    out["network_type_ex"] = str(modem.identity.network_type_ex or "")
    out["rrc"] = str(modem.cellular.rrc or "")
    out["trans_mode"] = str(modem.cellular.trans_mode or "")
    out["ims"] = str(raw_base_status.get("ims", "") or "")
    out["lte_band_hex"] = str(raw_base_status.get("lte_band_hex", "") or "")
    return now_ts


def build_canonical_status_payload(
    base_status: Optional[Dict[str, Any]],
    accepted: AcceptedControllerGeneration,
    constraints: Optional[Constraints],
    state_name: str,
    decision: Optional[Any] = None,
    historical: Optional[Any] = None,
    memory: Optional[Any] = None,
    ops_monitors: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build canonical 54GUC status payload.

    Field Groups:
    ─────────────
    IDENTITY & CONNECTION
        provider_name, provider (alias), provider_plmn, plmn (alias),
        radio_connected, state

    BAND & CA STATUS
        cart, cast, cart_ld, cast_ld, ca_signature,
        serving_band, serving_band_signature,
        selected_band_set, selected_band_set_signature,
        active_band_set, active_band_set_signature, active_bands_signature,
        band_set, band_set_signature, ca_state_text, radio_state_text

    CELL IDENTITY
        pci, earfcn_dl, enb_id, cell_id, tac

    RF SIGNALS
        sinr_db, rsrp_dbm, rsrq_db, rssi_dbm

    MODULATION
        mcs_dl, mcs_ul, mcs_dl0, mcs_dl1, mcs_ul_raw,
        qm_dl0, qm_dl1, qm_ul, mod0, mod1,
        qam_dl_primary, qam_dl_secondary, qam_ul, qam_source, qam_confidence,
        cqi, ri

    THROUGHPUT
        dl_mbps, ul_mbps, rx_bytes_per_sec, tx_bytes_per_sec,
        has_traffic, traffic_active (alias), session_uptime_s

    RTT & LATENCY
        rtt_ms, rtt_sliding_avg, rtt_congestion_avg, rtt_recent_median,
        rtt_baseline, rtt_effective_baseline, rtt_delta, rtt_delta_valid,
        rtt_quiet_delta, rtt_quiet_held, rtt_probe_ok, rtt_pressure_level

    CELL QUALITY (CQ)
        cq_cell, cq_cell_valid, cq_cell_source,
        cq_coverage, cq_vector_a_valid

    BAND QUALITY (BQ)
        bq_cell, bq_cell_valid, bq_cell_source

    CONGESTION SIGNALS
        cqi_mcs_expected_mcs, cqi_mcs_observed_mcs, cqi_mcs_gap_idx,
        cqi_mcs_gap_ratio, cqi_mcs_gap_smoothed, cqi_mcs_gap_valid,
        congestion_qam_gap, congestion_qualifier_high, congestion_qualifier_mod,
        rsrq_baseline, rsrq_baseline_valid, rsrq_baseline_samples,
        rsrq_gap, rsrq_gap_valid,
        congestion_pct, congestion_bucket, congestion_source

    CARRIER & NEIGHBOR DETAILS
        carrier_count, carrier_rows, carrier_details, neighbors

    SUBSYSTEMS
        brain (dict), brain_referee (dict), ops_monitors (dict)

    This builder is canonical-only. Legacy aliases are added separately by
    add_legacy_status_aliases() at the export edge.
    """
    if not isinstance(accepted, AcceptedControllerGeneration):
        raise TypeError(
            "status_payload_requires_accepted_controller_generation"
        )
    modem = accepted.intelligence
    computed = accepted.derived
    presentation = accepted.presentation

    # Source contract:
    # 1) Start from backend public/canonical status (`base_status`).
    # 2) Drop any legacy alias keys that leaked in from compatibility paths.
    # 3) Override canonical runtime metrics from `modem`/`computed`.
    raw_base_status = dict(base_status or {})
    out = dict(raw_base_status)
    for key in _LEGACY_STATUS_KEYS:
        out.pop(key, None)
    out.update(project_controller_read_model(accepted))
    for key in _LEGACY_STATUS_KEYS:
        out.pop(key, None)

    # ── IDENTITY & CONNECTION ─────────────────────────────────────────────
    now_ts = _apply_identity_connection_fields(
        out,
        raw_base_status=raw_base_status,
        modem=modem,
        constraints=constraints,
        state_name=state_name,
    )
    # ── BAND & CA STATUS ──────────────────────────────────────────────────
    cart = _safe_int(getattr(computed.quality, 'cart', 0), 0)
    cast = max(
        0.0,
        min(100.0, _safe_float(getattr(computed.quality, 'cast', 0.0), 0.0)),
    )
    cart_ld = _safe_int(getattr(computed.quality, 'cart_ld', 0), 0)
    cart_ld_valid = bool(getattr(computed.quality, 'cart_ld_valid', False))
    cart_ld_display_raw = presentation.cart_ld_display
    cart_ld_display_valid_raw = presentation.cart_ld_display_valid
    cart_ld_display = _safe_int(
        cart_ld_display_raw if cart_ld_display_raw not in (None, "") else cart_ld,
        0,
    )
    cart_ld_display_valid = (
        bool(cart_ld_display_valid_raw)
        if cart_ld_display_valid_raw is not None
        else cart_ld_valid
    )
    cast_ld = max(
        0.0,
        min(100.0, _safe_float(getattr(computed.quality, 'cast_ld', 0.0), 0.0)),
    )
    cast_ld_valid = bool(getattr(computed.quality, 'cast_ld_valid', False))
    ca_stat_ld = max(
        0.0,
        min(100.0, _safe_float(getattr(computed.quality, 'ca_stat_ld_pct', 0.0), 0.0)),
    )
    ca_stat_ld_valid = bool(getattr(computed.quality, 'ca_stat_ld_valid', False))
    cart_activity_per_hour = _safe_float(
        getattr(computed.quality, 'cart_activity_per_hour', 0.0),
        0.0,
    )
    cart_ld_activity = _safe_float(
        getattr(computed.quality, 'cart_ld_activity', 0.0),
        0.0,
    )
    ri_stability_pct = max(
        0.0,
        min(100.0, _safe_float(getattr(computed.quality, 'ri_stability_pct', 0.0), 0.0)),
    )
    ri_activity_per_hour = _safe_float(getattr(computed.quality, 'ri_activity_per_hour', 0.0), 0.0)
    ri_stability_valid = bool(getattr(computed.quality, 'ri_stability_valid', False))
    modem_ca_active = bool(getattr(modem.cellular, 'ca_active', False))

    # Use unified band normalization
    nbf = normalize_band_fields(
        serving_band=out.get("serving_band")
        or modem.cellular.serving_band
        or modem.cellular.primary_band,
        selected_band_set=out.get("selected_band_set") or _selected_band_set(modem, out),
        active_band_set=_active_band_set(modem),
        ca_active=modem_ca_active,
        serving_band_signature=modem.cellular.serving_band_signature,
        selected_band_set_signature=modem.cellular.selected_band_set_signature,
        active_band_set_signature=modem.cellular.active_band_set_signature,
        ca_state_text=presentation.ca_state_text,
    )
    serving_band = nbf.serving_band
    selected_band_set = nbf.selected_band_set
    active_band_set = nbf.active_band_set
    selected_band_set_signature = nbf.selected_band_set_signature
    active_band_set_signature = nbf.active_band_set_signature
    serving_band_signature = nbf.serving_band_signature
    band_set = nbf.band_set
    band_set_signature = nbf.band_set_signature
    ca_state_text = nbf.ca_state_text

    configured_band_pool = _configured_band_pool(modem, out)
    ca_signature = _configured_band_pool_signature(
        modem,
        out,
        configured_band_pool,
    )
    out["ca_signature"] = ca_signature
    out["ca_band_signature"] = ca_signature
    out["carrier_count"] = len(configured_band_pool)
    out["ca_raw_state"] = _safe_int(getattr(computed.quality, 'ca_raw_state', cart), 0)
    out["cart"] = cart
    out["cast"] = cast
    out["cart_ld"] = cart_ld
    out["cart_ld_valid"] = cart_ld_valid
    out["cart_ld_display"] = cart_ld_display
    out["cart_ld_display_valid"] = cart_ld_display_valid
    out["cast_ld"] = cast_ld
    out["cast_ld_valid"] = cast_ld_valid
    out["ca_realtime_state"] = cart
    out["ca_stat_pct"] = cast
    out["ca_realtime_ld_state"] = cart_ld
    out["ca_realtime_ld_valid"] = cart_ld_valid
    out["ca_stat_ld_pct"] = ca_stat_ld
    out["ca_stat_ld_valid"] = ca_stat_ld_valid
    out["cart_activity_per_hour"] = max(0.0, cart_activity_per_hour)
    out["cart_ld_activity"] = max(0.0, cart_ld_activity)
    out["ca_change_per_hour"] = max(0.0, cart_activity_per_hour)
    out["ca_ld_change_per_hour"] = max(0.0, cart_ld_activity)
    out["ca_active_pct"] = cast
    out["ca_active_session_pct"] = cast
    out["ri_stability_pct"] = ri_stability_pct
    out["ri_activity_per_hour"] = max(0.0, ri_activity_per_hour)
    out["ri_stability_valid"] = ri_stability_valid
    out["ca_active"] = modem_ca_active
    out["ca_stat_scope_key"] = str(getattr(computed.quality, 'ca_stat_scope_key', "") or "")
    out["ca_component_band_set"] = list(
        raw_base_status.get(
            "ca_component_band_set",
            getattr(modem.cellular, 'ca_component_band_set', []) or [],
        )
        or []
    )
    out["ca_component_band_signature"] = str(
        raw_base_status.get(
            "ca_component_band_signature",
            getattr(modem.cellular, 'ca_component_band_signature', "") or "",
        )
        or ""
    )
    out["ca_component_count"] = _safe_int(
        raw_base_status.get("ca_component_count", getattr(modem.cellular, 'ca_component_count', 0)),
        0,
    )
    out["ca_composition_signature"] = str(
        raw_base_status.get(
            "ca_composition_signature",
            getattr(modem.cellular, 'ca_composition_signature', "") or "",
        )
        or ""
    )
    out["ca_source"] = str(raw_base_status.get("ca_source", getattr(modem.cellular, 'ca_source', "") or "") or "")

    scheduler_field_trust = modem_live_scheduler_field_trust(
        raw_base_status=raw_base_status,
        modem=modem.cellular,
    )
    apply_scheduler_field_trust(out, scheduler_field_trust)
    # RTT/CQ/congestion canonical and held display fields were copied above
    # from the one accepted-generation read-model projection. They are not
    # reconstructed from mutable ComputedSignals at this boundary.
    delivered_capacity_valid = bool(getattr(computed.quality, 'delivered_capacity_valid', False))
    out["delivered_capacity_valid"] = delivered_capacity_valid
    out["delivered_capacity_ratio"] = (
        max(0.0, min(1.0, _safe_float(getattr(computed.quality, 'delivered_capacity_ratio', 0.0), 0.0)))
        if delivered_capacity_valid
        else None
    )
    out["delivered_capacity_best_dl_mbps"] = (
        max(0.0, _safe_float(getattr(computed.quality, 'delivered_capacity_best_dl_mbps', 0.0), 0.0))
        if delivered_capacity_valid
        else None
    )
    out["delivered_capacity_total_bw_mhz"] = max(
        0.0,
        _safe_float(getattr(computed.quality, 'delivered_capacity_total_bw_mhz', 0.0), 0.0),
    )
    out["delivered_capacity_ceiling_mbps"] = (
        max(0.0, _safe_float(getattr(computed.quality, 'delivered_capacity_ceiling_mbps', 0.0), 0.0))
        if delivered_capacity_valid
        else None
    )
    out["delivered_capacity_sample_count"] = max(
        0,
        _safe_int(getattr(computed.quality, 'delivered_capacity_sample_count', 0), 0),
    )
    out["delivered_capacity_source"] = str(
        getattr(computed.quality, 'delivered_capacity_source', "needs_controlled_speed_tests")
        or "needs_controlled_speed_tests"
    )
    out["dl_bottleneck_state"] = str(getattr(computed.quality, 'dl_bottleneck_state', "unknown") or "unknown")
    out["dl_bottleneck_reason"] = str(
        getattr(computed.quality, 'dl_bottleneck_reason', "missing_controlled_speedtest_evidence")
        or "missing_controlled_speedtest_evidence"
    )
    out["dl_bottleneck_proof_level"] = str(
        getattr(computed.quality, 'dl_bottleneck_proof_level', "unknown") or "unknown"
    )
    missing_evidence = getattr(computed.quality, 'dl_bottleneck_missing_evidence', ()) or ()
    out["dl_bottleneck_missing_evidence"] = list(missing_evidence) if isinstance(missing_evidence, (list, tuple)) else []
    rejected = getattr(computed.quality, 'dl_bottleneck_rejected_explanations', ()) or ()
    out["dl_bottleneck_rejected_explanations"] = list(rejected) if isinstance(rejected, (list, tuple)) else []
    evidence = getattr(computed.quality, 'dl_bottleneck_evidence', {}) or {}
    out["dl_bottleneck_evidence"] = dict(evidence) if isinstance(evidence, dict) else {}
    out["dl_bottleneck_owner"] = str(getattr(computed.quality, 'dl_bottleneck_owner', "brain.dl_bottleneck") or "brain.dl_bottleneck")
    out["dl_bottleneck_schema_version"] = str(
        getattr(computed.quality, 'dl_bottleneck_schema_version', "dl_bottleneck_v1") or "dl_bottleneck_v1"
    )

    # ── MODEM-FAMILY DETAILS ─────────────────────────────────────────────
    # Generic carrier_count was ambiguous across modem families. Keep raw
    # rows/details only for low-level diagnostics; CART/CAST and the configured
    # pool are the public CA truth.
    out["carrier_rows"] = _carrier_rows(modem)
    out["carrier_details"] = _carrier_details(modem)
    out["neighbors"] = _neighbors(modem)

    # ── BAND SIGNATURES (finalized) ───────────────────────────────────────
    out["serving_band"] = serving_band
    out["serving_band_signature"] = serving_band_signature
    out["selected_band_set"] = list(selected_band_set)
    out["selected_band_set_signature"] = selected_band_set_signature
    out["configured_band_pool"] = configured_band_pool
    out["configured_band_pool_signature"] = _configured_band_pool_signature(
        modem,
        out,
        configured_band_pool,
    )
    out["configured_band_pool_source"] = _configured_band_pool_source(
        modem,
        out,
        configured_band_pool,
    )
    out["configured_band_pool_status"] = _configured_band_pool_status(out)
    out["configured_band_pool_age_s"] = _configured_band_pool_age_s(out)
    out["configured_band_pool_last_sample_ts"] = (
        _configured_band_pool_last_sample_ts(out)
    )
    _apply_customer_display_derivatives(out)
    out["active_band_set"] = list(active_band_set)
    out["active_band_set_signature"] = active_band_set_signature
    out["band_set"] = list(band_set)
    out["band_set_signature"] = str(band_set_signature or "")
    out["active_bands_signature"] = active_band_set_signature or _active_bands_signature(modem)
    out["ca_state_text"] = str(presentation.ca_state_text or ca_state_text)
    out["radio_state_text"] = str(
        presentation.radio_state_text
        or (
            "Connected"
            if bool(modem.cellular.radio_connected)
            else "Disconnected"
        )
    )
    apply_scheduler_field_trust(out, scheduler_field_trust)
    scheduler_surface = build_scheduler_display_surface(
        build_scheduler_display_contract(out, field_trust=scheduler_field_trust)
    )
    for target, source in (
        ("me0", "scheduler_me0_display"),
        ("me1", "scheduler_me1_display"),
        ("me_ul", "scheduler_me_ul_display"),
    ):
        out[target] = scheduler_surface[source]
    if _safe_int(out.get("ri"), 0) == 1:
        out["cqi1"] = None
        out["mcs_dl1"] = None
        out["me1"] = None
        out["qam_dl1"] = ""

    # ── SUBSYSTEMS ────────────────────────────────────────────────────────
    if decision:
        try:
            brain_status = decision.get_runtime_status()
            if brain_status:
                out["brain"] = brain_status
        except Exception as exc:
            log.debug("build_status_payload: brain status suppressed %s", exc)
    out = attach_learning_status(
        out,
        historical,
        memory,
        durable_memory_runtime_status=(
            dict(out.get("durable_memory_runtime_status") or {})
            if isinstance(out.get("durable_memory_runtime_status"), Mapping)
            else {}
        ),
    )
    if isinstance(ops_monitors, dict):
        out["ops_monitors"] = ops_monitors

    # Operator summary — plain-English brain status for the human pilot.
    # This is merged into brain_referee so the exported operator surface
    # stays single-owner instead of splitting across parallel summary objects.
    try:
        summary = _build_operator_summary(
            modem=modem,
            computed=computed,
            state_name=state_name,
            payload=out,
        )
        out = merge_operator_summary_into_brain_referee_publication(out, summary)
    except Exception as exc:
        log.debug("build_status_payload: operator summary suppressed %s", exc)

    return out


def build_status_payload(
    base_status: Optional[Dict[str, Any]],
    accepted: AcceptedControllerGeneration,
    constraints: Optional[Constraints],
    state_name: str,
    decision: Optional[Any] = None,
    historical: Optional[Any] = None,
    memory: Optional[Any] = None,
    ops_monitors: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build export-facing status payload with compatibility aliases."""
    payload = build_canonical_status_payload(
        base_status=base_status,
        accepted=accepted,
        constraints=constraints,
        state_name=state_name,
        decision=decision,
        historical=historical,
        memory=memory,
        ops_monitors=ops_monitors,
    )
    return add_legacy_status_aliases(payload)
