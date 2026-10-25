"""
arch4/interfaces.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Shared data types and interfaces

Every module imports from here. These types define the standard
API boundaries between modules. Changing a type here means
updating all consumers — that's the point.

Maps directly to Foundation Document:
  - ModemInput  → Chapter 2.1 (what cell sends us)
  - ModemOutput → Chapter 2.3 (five dynamic levers)
  - SystemState → Chapter 4.1 (signal-driven states)
  - Constraints → Chapter 5.4 (constraint map)
═══════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path as _Path
from typing import Optional, List, Dict, Any, Final
import os
import sys
import time

from arch4.app_info import (
    ARCH_SW_LABEL,
    read_runtime_build_date,
    read_runtime_release_channel,
    read_runtime_version,
)

DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S: Final[float] = 15.0

INPUT_DIAGNOSTIC_KEYS: tuple[str, ...] = (
    "modem_sample_interval_s",
    "modem_sample_expected_interval_s",
    "modem_sample_slo_exceeded",
    "register_intake_interval_s",
    "register_intake_expected_interval_s",
    "register_intake_slo_exceeded",
    "register_intake_sequence_gap",
    "engine_intake_mode",
    "engine_intake_drained_samples",
    "engine_intake_missing_samples",
    "engine_intake_history_depth",
    "engine_intake_missing_total",
)


def input_diagnostics(modem: Any) -> Dict[str, Any]:
    """Return Modem Live-owned diagnostics without treating them as modem facts."""

    payload = getattr(modem, "input_diagnostics", None)
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def set_input_diagnostics(modem: Any, values: Dict[str, Any]) -> None:
    if modem is None:
        return
    payload = dict(input_diagnostics(modem))
    for key, value in dict(values or {}).items():
        if key in INPUT_DIAGNOSTIC_KEYS:
            payload[key] = value
    modem.input_diagnostics = payload

# ─────────────────────────────────────────────────────────────
# Version + regional band map (F1)
# ─────────────────────────────────────────────────────────────

def _read_version() -> str:
    """Read version from VERSION file or bundle manifest across layouts."""
    return read_runtime_version(
        module_file=__file__,
        executable=sys.executable,
        argv0=getattr(sys, "argv", [""])[0] if getattr(sys, "argv", None) else "",
        env_root=os.environ.get("ARCH_APP_ROOT", ""),
        cwd=_Path.cwd(),
    )


def _read_build_date() -> str:
    """Read build date from BUILD_DATE file, bundle manifest, or VERSION mtime."""
    return read_runtime_build_date(
        module_file=__file__,
        executable=sys.executable,
        argv0=getattr(sys, "argv", [""])[0] if getattr(sys, "argv", None) else "",
        env_root=os.environ.get("ARCH_APP_ROOT", ""),
        cwd=_Path.cwd(),
    )


def _read_release_channel() -> str:
    return read_runtime_release_channel(
        module_file=__file__,
        executable=sys.executable,
        argv0=getattr(sys, "argv", [""])[0] if getattr(sys, "argv", None) else "",
        env_root=os.environ.get("ARCH_APP_ROOT", ""),
        cwd=_Path.cwd(),
    )


LUC_VERSION = _read_version()
LUC_BUILD_DATE = _read_build_date()
LUC_RELEASE_CHANNEL = _read_release_channel()
LUC_SHORT_LABEL = LUC_VERSION
LUC_FULL_LABEL = LUC_SHORT_LABEL
# Backward-compatible alias used across existing modules.
ARCH4_VERSION = ARCH_SW_LABEL

# Congestion contract defaults. Lower layers may receive manual/runtime values,
# but this contract owns the product default.
DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB: Final[float] = 1.0

# Canonical runtime folders (54GUC portable layout).
ARCH_RUNTIME_OUTPUT_DIR = "guc_output"
ARCH_RUNTIME_MEMORY_DIR = "guc_memory"
ARCH_RUNTIME_OUTPUT_MODEL_DIR = "guc_output_model"
ARCH_RUNTIME_MEMORY_MODEL_DIR = "guc_memory_model"
ARCH_RUNTIME_TELEMETRY_HISTORY_BASENAME = "telemetry_history"
ARCH_RUNTIME_HEALTH_FILENAME = "runtime_health.json"
ARCH_NGUI_PAGE_TRUTH_FILENAME = "ngui_page_truth.json"
ARCH_SIGNAL_STATE_FILENAME = "signal_state.json"
ARCH_CURRENT_STATE_FILENAME = "current_state.json"

ARCH_QUALITY_FORMULA_EPOCH = "bq_cq_split_v1"

# Regional LTE bands — verified via operator spectrum review Nov 2025.
# All three operators (Yettel 22001, MTS 22003, A1 22005) share
# identical LTE band allocations.
DEPLOYED_BANDS = frozenset({1, 3, 20})      # Confirmed working, deployed since 2015-2016

# Provider PLMN map
PROVIDER_PLMN = {
    "22001": "Yettel",
    "22003": "MTS",
    "22005": "A1",
}

POLICY_FAMILY_GENERIC = "GENERIC"
POLICY_FAMILY_PROVIDER = "PROVIDER_SPECIFIC"


@dataclass(frozen=True)
class PolicyProfile:
    """Provider+modem policy selection scaffold (behavior flags only)."""
    name: str
    # Stable identifiers for profile routing (names can stay human-readable).
    policy_key: str = "GENERIC"
    policy_family: str = POLICY_FAMILY_GENERIC
    provider_plmn: str = ""
    provider_name: str = "Unknown"
    modem_backend: str = ""
    # Enable DecisionEngine on B818 (conservative path; device monitor still runs)
    b818_use_full_brain: bool = False
    # Keep B818 device monitor active for morning sequence / modem-down reboot
    b818_keep_device_monitor: bool = True
    # Whether nighttime CA should be treated as optional/bonus in future logic
    night_ca_bonus_only: bool = False
    # Placeholder for future provider-specific night policy
    enable_night_band_training: bool = False
    # On powerful modems (B818), allow full state tracking but suppress daytime
    # auto band changes unless a provider-specific night policy is active.
    b818_daytime_auto_band_actions: bool = True
    # Modified full-brain on powerful modems may still require slower probing
    night_probe_dwell_s: float = 900.0
    night_probe_cooldown_s: float = 600.0
    # Historical A1+B818 special-profile flag. The old midnight forced schedule
    # is retired, but this flag still identifies the profile for restricted
    # night logic and the morning sequence.
    a1_overnight_recovery: bool = False


def select_policy_profile(provider_plmn: str, modem_backend: str) -> PolicyProfile:
    """
    Select a behavior profile by provider + modem type.

    Uses backend family (from operator_profiles) instead of raw vendor names.
    Night band training is available for any backend that supports band selection.
    """
    plmn = (provider_plmn or "").strip()
    backend = (modem_backend or "").strip().lower()
    provider = PROVIDER_PLMN.get(plmn, "Unknown")

    from arch4.runtime_sidecar_identity import infer_sidecar_identity_backend_family
    from arch4.modem_live.profile_catalog import modem_backend_capability_flags
    family = infer_sidecar_identity_backend_family(backend) or "unknown"
    _caps = modem_backend_capability_flags(backend)
    supports_band_select = bool(_caps.get("supports_band_selection", False))
    is_hilink = family == "huawei_hilink_like"

    # ── Non-HiLink backends (MikroTik, ZTE, Zyxel, OpenWrt, model, etc.) ──
    if not is_hilink:
        return PolicyProfile(
            name=f"{provider or 'Unknown'}-{backend or 'modem'}",
            policy_key="GENERIC",
            policy_family=POLICY_FAMILY_GENERIC,
            provider_plmn=plmn,
            provider_name=provider,
            modem_backend=backend,
            b818_use_full_brain=False,
            b818_keep_device_monitor=False,
            night_ca_bonus_only=False,
            enable_night_band_training=supports_band_select,
            a1_overnight_recovery=(plmn == "22005"),
        )

    # ── HiLink-like — provider-specific tuning ──
    # B818 has the most production hours; other HiLink models get the same
    # family behavior but without the B818 device monitor (which uses B818-specific
    # morning sequence / reboot logic).
    has_device_monitor = backend == "b818"

    if plmn == "22005":  # A1
        return PolicyProfile(
            name=f"A1-{backend}-conservative",
            policy_key="A1_B818_FORCED" if backend == "b818" else "A1_HUAWEI",
            policy_family=POLICY_FAMILY_PROVIDER,
            provider_plmn=plmn,
            provider_name=provider,
            modem_backend=backend,
            b818_use_full_brain=True,
            b818_keep_device_monitor=has_device_monitor,
            night_ca_bonus_only=True,
            enable_night_band_training=True,
            b818_daytime_auto_band_actions=True,
            night_probe_dwell_s=900.0,
            night_probe_cooldown_s=600.0,
            a1_overnight_recovery=True,
        )

    if plmn in {"22003", "22001"}:  # MTS / Yettel
        return PolicyProfile(
            name=f"{provider}-{backend}-conservative",
            policy_key="GENERIC_B818" if backend == "b818" else "GENERIC_HUAWEI",
            policy_family=POLICY_FAMILY_GENERIC,
            provider_plmn=plmn,
            provider_name=provider,
            modem_backend=backend,
            b818_use_full_brain=True,
            b818_keep_device_monitor=has_device_monitor,
            night_ca_bonus_only=False,
            enable_night_band_training=True,
            b818_daytime_auto_band_actions=False,
            night_probe_dwell_s=900.0,
            night_probe_cooldown_s=600.0,
        )

    # ── HiLink-like, other/unknown providers ──
    return PolicyProfile(
        name=f"{provider or 'Unknown'}-{backend}-conservative",
        policy_key="GENERIC_B818" if backend == "b818" else "GENERIC_HUAWEI",
        policy_family=POLICY_FAMILY_GENERIC,
        provider_plmn=plmn,
        provider_name=provider,
        modem_backend=backend,
        b818_use_full_brain=True,
        b818_keep_device_monitor=has_device_monitor,
        night_ca_bonus_only=False,
        enable_night_band_training=True,
        b818_daytime_auto_band_actions=False,
        night_probe_dwell_s=900.0,
        night_probe_cooldown_s=600.0,
    )

# ─────────────────────────────────────────────────────────────
# Chapter 2.1 / 2.3: Modem Inputs (what we observe)
# ─────────────────────────────────────────────────────────────

@dataclass
class CarrierInfo:
    """One reported PCell/SCell; absent optional measurements remain None."""
    band: int                    # e.g. 1, 3, 7, 20
    earfcn: Optional[int]         # carrier frequency number
    pci: Optional[int]            # physical cell ID; zero is valid
    bandwidth_mhz: Optional[float] # channel bandwidth
    sinr_db: Optional[float]      # direct dB; zero is valid when measured
    rsrp_dbm: Optional[float]     # reference signal power
    state: int                   # 0=not configured, 1+=active SCell
    is_pcell: bool = False


@dataclass
class NeighborCell:
    """One neighbor cell from AT+QENG='neighbourcell'."""
    band: int
    earfcn: int
    pci: int
    rsrp_dbm: float
    rsrq_db: float


def is_neighbor_placeholder(neighbor: NeighborCell) -> bool:
    """Return True if a neighbor cell has placeholder/sentinel values.

    Huawei HiLink firmware reports neighbor PCIs but with sentinel signal
    values (rsrp=-999, rsrq=-999, earfcn=0). These are advisory — the PCI
    is real but the measurement is not usable for scoring or comparison.
    """
    if neighbor.rsrp_dbm <= -900.0 or neighbor.rsrq_db <= -900.0:
        return True
    if neighbor.earfcn == 0 and neighbor.rsrp_dbm == 0.0:
        return True
    return False


def filter_usable_neighbors(neighbors: List["NeighborCell"]) -> List["NeighborCell"]:
    """Return only neighbors with real measurement data (not placeholders)."""
    return [n for n in neighbors if not is_neighbor_placeholder(n)]


def normalize_band_list(values: Any) -> List[int]:
    if not isinstance(values, list):
        return []
    out: List[int] = []
    for raw in values:
        try:
            band = int(raw or 0)
        except Exception:
            band = 0
        if band > 0 and band not in out:
            out.append(band)
    out.sort()
    return out


def band_signature(values: List[int]) -> str:
    return "+".join(f"B{b}" for b in values) if values else ""


def parse_band_signature(value: Any) -> List[int]:
    text = str(value or "").strip()
    if not text:
        return []
    bands: List[int] = []
    for token in text.replace(" ", "").split("+"):
        token = token.strip().upper()
        if token.startswith("B"):
            token = token[1:]
        if token in {"", "CA"}:
            continue
        try:
            band = int(token)
        except Exception:
            continue
        if band > 0 and band not in bands:
            bands.append(band)
    bands.sort()
    return bands


@dataclass
class NormalizedBandFields:
    """Canonical band field values after normalization.

    This is the single source of truth for band signature derivation.
    Used by modem/base.py, status_payload.py, and NGUI runtime projection.
    """
    serving_band: int = 0
    serving_band_signature: str = ""
    selected_band_set: List[int] = field(default_factory=list)
    selected_band_set_signature: str = ""
    active_band_set: List[int] = field(default_factory=list)
    active_band_set_signature: str = ""
    band_set: List[int] = field(default_factory=list)
    band_set_signature: str = ""
    ca_state_text: str = ""


def normalize_band_fields(
    *,
    serving_band: Any = 0,
    selected_band_set: Any = None,
    active_band_set: Any = None,
    band_set: Any = None,
    ca_active: bool = False,
    # Pre-computed signatures (use if already available, otherwise derive)
    serving_band_signature: str = "",
    selected_band_set_signature: str = "",
    active_band_set_signature: str = "",
    band_set_signature: str = "",
    ca_state_text: str = "",
) -> NormalizedBandFields:
    """Unified band field normalization — single source of truth.

    Derives all band signatures with consistent fallback logic:
      1. serving_band_signature from serving_band
      2. selected_band_set_signature from selected_band_set
      3. active_band_set_signature from active_band_set
      4. band_set from selected || active || [serving]
      5. band_set_signature from band_set || serving_band_signature

    Call this instead of duplicating the fallback logic in multiple modules.
    """
    # Normalize inputs
    try:
        sb = int(serving_band or 0)
    except (TypeError, ValueError):
        sb = 0

    sel = normalize_band_list(selected_band_set) if selected_band_set else []
    act = normalize_band_list(active_band_set) if active_band_set else []
    bs = normalize_band_list(band_set) if band_set else []

    # PBAND/serving-band signature is single-band truth. A multi-band value is
    # an allowed/selected pool and must not be carried as PBAND.
    parsed_serving_sig = parse_band_signature(serving_band_signature)
    if sb > 0:
        sb_sig = f"B{sb}"
    elif len(parsed_serving_sig) == 1:
        sb = int(parsed_serving_sig[0])
        sb_sig = f"B{sb}"
    else:
        sb_sig = ""
    sel_sig = selected_band_set_signature or band_signature(sel)
    act_sig = active_band_set_signature or band_signature(act)

    # band_set fallback: active || selected || [serving]
    if not bs:
        bs = act or sel or ([sb] if sb > 0 else [])

    bs_sig = band_set_signature or band_signature(bs) or sb_sig

    # CA state text
    ca_txt = ca_state_text or ("MULTI-BAND ACTIVE" if ca_active else "SINGLE BAND ACTIVE")

    return NormalizedBandFields(
        serving_band=sb,
        serving_band_signature=sb_sig,
        selected_band_set=sel,
        selected_band_set_signature=sel_sig,
        active_band_set=act,
        active_band_set_signature=act_sig,
        band_set=bs,
        band_set_signature=bs_sig,
        ca_state_text=ca_txt,
    )


@dataclass
class ModemInput:
    """
    Standard modem observation — everything the brain can see.

    Populated by the modem abstraction layer each sample.
    Brain code ONLY reads this struct. Never calls RouterOS/AT directly.

    Maps to Foundation Doc Chapter 2.3 (observable signals).
    """
    # ── RF measurements (from CRS — Input #5) ──
    sinr_db: float               # SINR in direct dB
    rsrp_dbm: float              # reference signal received power
    rsrq_db: float               # reference signal received quality
    rssi_dbm: float              # total wideband received power

    # ── Scheduling feedback (from DCI — Input #1) ──
    mcs_dl: int                  # downlink MCS assigned by eNodeB (0-28)
    mcs_ul: int                  # uplink MCS
    cqi: int                     # channel quality indicator (0-15)
    ri: int                      # rank indicator (1 or 2 for 2x2 MIMO)

    # ── CA status (from RRC Reconfig — Input #3) ──
    carriers: List[CarrierInfo]  # all active carriers (PCell + SCells)
    ca_active: bool              # True if any SCell active
    ca_band_signature: str       # configured CA Pool, e.g. "B1+B3+B20"

    # ── Serving cell identity ──
    primary_band: int            # PCell band number
    pci: int                     # serving physical cell ID
    earfcn: int                  # compat alias; canonical shared access is earfcn_dl
    enb_id: int                  # eNodeB identity

    # ── Session health ──
    radio_connected: bool        # True if RRC_CONNECTED
    session_uptime_s: float      # seconds since last RRC establishment

    # ── Traffic counters (from RouterOS interface monitor) ──
    rx_bytes_per_sec: float      # downlink throughput
    tx_bytes_per_sec: float      # uplink throughput
    session_uptime_source: str = ""  # reported|maintenance_pending|held|synthetic|unsupported
    traffic_valid: bool = True
    traffic_state_detail: str = ""
    traffic_invalid_reason: str = ""
    packet_session_connected: Optional[bool] = None
    connection_status_code: str = ""
    traffic_day_download_bytes: int = 0
    traffic_day_upload_bytes: int = 0
    traffic_day_used_bytes: int = 0
    traffic_month_download_bytes: int = 0
    traffic_month_upload_bytes: int = 0
    traffic_total_download_bytes: int = 0
    traffic_total_upload_bytes: int = 0
    traffic_session_download_bytes: int = 0
    traffic_session_upload_bytes: int = 0
    traffic_device_download_counter_bytes: Optional[int] = None
    traffic_device_upload_counter_bytes: Optional[int] = None
    traffic_counter_period_started_at: Optional[float] = None
    traffic_counter_interface_started_at: Optional[float] = None
    traffic_accounting_source: str = ""
    traffic_accounting_status: str = ""
    traffic_accounting_month_key: str = ""
    traffic_accounting_day_key: str = ""
    traffic_volume_poll_interval_s: float = 0.0
    traffic_volume_last_sample_ts: float = 0.0
    traffic_volume_next_sample_ts: float = 0.0
    traffic_volume_age_s: Optional[float] = None
    traffic_rate_source: str = ""
    traffic_quota_start_day: Optional[int] = None
    traffic_quota_limit_label: str = ""
    traffic_quota_left_bytes: Optional[int] = None
    traffic_quota_limit_bytes: Optional[int] = None
    traffic_quota_month_threshold_pct: Optional[float] = None
    traffic_quota_day_threshold_pct: Optional[float] = None
    engine_tick_ts: float = 0.0
    sample_fresh: bool = True
    sample_sequence: int = 0
    sample_age_s: float = 0.0
    sample_source: str = ""
    modem_live_io_quiet_active: bool = False
    modem_live_io_quiet_remaining_s: float = 0.0
    live_register_quiet_hold: bool = False
    live_register_hold_reason: str = ""
    input_diagnostics: Dict[str, Any] = field(default_factory=dict)
    lte_interface_disabled: bool = False  # Router-side lte1 disabled flag (MikroTik)
    cell_id: int = 0             # E-UTRAN Cell Identity (stable, globally unique)
    selected_band_set: List[int] = field(default_factory=list)  # modem-selected / allowed LTE bands
    configured_band_pool: List[int] = field(default_factory=list)  # backend-configured LTE mask / pool
    configured_band_pool_signature: str = ""
    configured_band_pool_source: str = ""
    configured_band_pool_status: str = ""
    configured_band_pool_age_s: Optional[float] = None
    configured_band_pool_last_sample_ts: float = 0.0
    ca_component_band_set: List[int] = field(default_factory=list)  # diagnostic SCC bands from modem/parser
    ca_component_band_signature: str = ""
    ca_component_count: int = 0
    ca_composition_signature: str = ""  # diagnostic PCell+SCell signature, keeps same-band CA distinct
    ca_source: str = ""

    # ── Neighbors (from AT+QENG — optional, on-demand) ──
    neighbors: List[NeighborCell] = field(default_factory=list)

    # ── Live radio detail (backend-native, optional) ──
    # These fields are part of the canonical modem contract so GUI/runtime
    # consumers do not need to reach into backend-private side channels.
    dl_bandwidth: str = ""
    ul_bandwidth: str = ""
    dl_bandwidth_mhz: float = 0.0
    ul_bandwidth_mhz: float = 0.0
    dl_freq_mhz: float = 0.0
    ul_freq_mhz: float = 0.0
    earfcn_ul: int = 0
    tac: int = 0
    rrc: str = ""
    trans_mode: str = ""
    network_type: str = ""
    network_type_ex: str = ""
    provider_name: str = ""
    provider_plmn: str = ""
    provider_full_name: str = ""
    access_type: str = ""
    cqi0: int = 0
    cqi1: int = 0
    ri_valid: bool = False
    ri_source: str = ""
    qam_dl0: Any = ""
    qam_dl1: Any = ""
    mcs_ul_supported: bool = True
    tbs_dl0: int = 0
    tbs_dl1: int = 0
    tbs_ul: int = 0
    mcs_dl_retx: bool = False
    mcs_dl1_retx: bool = False
    mcs_ul_retx: bool = False
    tx_power: Any = ""
    ims: str = ""
    antenna: str = ""
    antenna_mode: str = ""
    antenna_mode_source: str = ""
    antenna_switch_api_supported: Optional[bool] = None
    external_antenna_ports_present: Optional[bool] = None
    lte_band_hex: str = ""

    # ── Timestamp ──
    timestamp: float = field(default_factory=time.time)
    # Optional per-codeword scheduling detail (when backend exposes it).
    # MOD uses LTE modulation order Qm values: 2,4,6,8.
    mcs_dl0: int = 0
    mcs_dl1: int = 0
    # Compatibility aliases for per-codeword LTE modulation order Qm.
    mod0: int = 0
    mod1: int = 0
    # Normalized modulation contract (all backends).
    qam_dl_primary: int = 0
    qam_dl_secondary: int = 0
    qam_ul: int = 0
    qam_source: str = "unknown"  # reported | inferred | unknown
    qam_confidence: float = 0.0

    # ── 5G/NR readiness stubs (Tier 3, Item 11) ───────────────────────────
    # Stubbed at 0/False for all existing LTE backends.
    # 5G-capable backends (future: MikroTik RBL60, B535 5G, etc.) will
    # populate these fields. All existing code paths are unaffected because
    # the LTE signal path does not read these fields.
    nr_rsrp_dbm: float = 0.0         # NR RSRP (dBm)
    nr_sinr_db: float = 0.0          # NR SINR (dB)
    nr_rsrq_db: float = 0.0          # NR RSRQ (dB)
    nr_band: int = 0                  # NR serving band (0 = unavailable)
    nr_pci: int = 0                   # NR physical cell ID (0 = unavailable)
    nr_arfcn: int = 0                 # NR ARFCN/NR-ARFCN (0 = unavailable)
    nr_cell_id: int = 0               # NR cell identity (0 = unavailable)
    nsa_active: bool = False          # 5G NSA: LTE anchor + NR secondary cell
    sa_active: bool = False           # 5G SA: NR standalone
    nr_mode: str = ""                # NSA | SA | ""
    nr_state_text: str = ""          # human-readable NR state text
    nr_cells: List[Dict[str, Any]] = field(default_factory=list)
    nr_bandwidth_mhz: float = 0.0
    nr_scs_khz: float = 0.0
    # ── Temperature (ZTE MC888/MC801A only, 0.0 = not reported) ──
    temperature_modem_5g_c: float = 0.0  # 5G modem die temperature (°C)

    # ── Wi-Fi status (read-only, None = not reported by backend) ──
    wifi_enabled: Optional[bool] = None        # any Wi-Fi radio active
    wifi_24ghz_enabled: Optional[bool] = None  # 2.4 GHz radio enabled
    wifi_5ghz_enabled: Optional[bool] = None   # 5 GHz radio enabled
    wifi_6ghz_enabled: Optional[bool] = None   # 6 GHz radio enabled

    # ── Runtime modem identity (from backend public status) ──
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    software_version: str = ""
    webui: str = ""
    serial: str = ""
    imei: str = ""
    imsi: str = ""
    hardware_version: str = ""
    config_version: str = ""
    lan_mac: str = ""
    wan_ip: str = ""

    # ── Signal provenance (how each value was obtained) ──
    # Values: "reported" = modem-native, "estimated" = host-reconstructed,
    #         "unavailable" = inputs insufficient.
    rsrq_source: str = "reported"
    cqi_source: str = "reported"
    # 54GUC-owned runtime keepalive lane. This is not a modem-native fact;
    # it reflects whether the generic TRAFFIC_PRESENCE worker is active.
    traffic_presence_active: bool = False
    traffic_presence_interval_s: float = 0.0

    @property
    def earfcn_dl(self) -> int:
        """Canonical shared LTE DL EARFCN access path."""
        try:
            return int(self.earfcn or 0)
        except (ValueError, TypeError):
            return 0

    @earfcn_dl.setter
    def earfcn_dl(self, value: int) -> None:
        try:
            self.earfcn = int(value or 0)
        except (ValueError, TypeError):
            self.earfcn = 0

    @property
    def provider(self) -> str:
        return str(self.provider_name or "")

    @property
    def plmn(self) -> str:
        return str(self.provider_plmn or "")

    @property
    def throughput_dl_mbps(self) -> float:
        return self.rx_bytes_per_sec * 8 / 1_000_000

    @property
    def throughput_ul_mbps(self) -> float:
        return self.tx_bytes_per_sec * 8 / 1_000_000

    @property
    def carrier_count(self) -> int:
        return len(normalize_band_list(self.configured_band_pool))

    def _carrier_items(self) -> List[CarrierInfo]:
        carriers = self.carriers
        if isinstance(carriers, list):
            return carriers
        if isinstance(carriers, tuple):
            return list(carriers)
        return []

    @property
    def has_traffic(self) -> bool:
        """True if meaningful traffic flowing (backward-compat alias for traffic_state != idle)."""
        return bool(self.traffic_valid) and self.traffic_state != "idle"

    @classmethod
    def safe_defaults(cls, **overrides: Any) -> "ModemInput":
        """Build a conservative ModemInput with null-safe defaults.

        This is intended for new backend bring-up, replay helpers, and
        monitoring-only devices that do not expose the full shared contract
        yet. Callers override only the fields they actually know.
        """
        defaults: Dict[str, Any] = {
            "sinr_db": 0.0,
            "rsrp_dbm": 0.0,
            "rsrq_db": 0.0,
            "rssi_dbm": 0.0,
            "mcs_dl": 0,
            "mcs_ul": 0,
            "cqi": 0,
            "ri": 0,
            "carriers": [],
            "ca_active": False,
            "ca_band_signature": "",
            "primary_band": 0,
            "pci": 0,
            "earfcn": 0,
            "enb_id": 0,
            "radio_connected": False,
            "session_uptime_s": 0.0,
            "rx_bytes_per_sec": 0.0,
            "tx_bytes_per_sec": 0.0,
        }
        defaults.update(overrides)
        return cls(**defaults)

    @property
    def traffic_state(self) -> str:
        """Three-state traffic classification.

        idle     — DL < 400 kbps AND UL < 400 kbps.
                   Brain may probe freely. No user session active.

        download — DL ≥ 400 kbps AND UL < 400 kbps.
                   Video stream, software update, or ONE-SIDED video call
                   (remote party has no camera). Treat as has_traffic.
                   Probe adds resistance but can be overridden when urgency
                   pressure accumulates enough.

        upload   — UL ≥ 400 kbps AND DL < 400 kbps.
                   Upload-heavy or uplink-only session. Still meaningful
                   traffic and must not be treated as idle.

        active   — Both DL and UL significant (each ≥ 400 kbps).
                   Two-way session: VoIP, video call both sides, gaming,
                   upload. Highest probe resistance — only probe when
                   accumulated pressure exceeds resistance threshold.

        Both thresholds are 50,000 bytes/s = 400 kbps. This is the existing
        meaningful-session classification, not an "any data proves NET" check.
        """
        if not bool(self.traffic_valid):
            return "invalid"
        dl = float(self.rx_bytes_per_sec)
        ul = float(self.tx_bytes_per_sec)
        if dl >= 50_000 and ul >= 50_000:   # both directions active
            return "active"
        if dl >= 50_000:                     # download only (50 kB/s = 400 kbps)
            return "download"
        if ul >= 50_000:                     # upload only
            return "upload"
        return "idle"

    @property
    def primary_bw_mhz(self) -> float:
        """Primary (anchor) carrier bandwidth in MHz.

        Always available from every modem — the primary band always reports
        its BW. Returns 0.0 if no primary carrier is identified.
        This is the most important single metric for prime band quality:
        a primary with 20 MHz is always preferable to one with 10 MHz,
        all else being equal.
        """
        for c in self._carrier_items():
            if bool(getattr(c, "is_pcell", False)):
                bw = float(getattr(c, "bandwidth_mhz", 0.0) or 0.0)
                if bw > 0:
                    return bw
        # Fallback: if no pcell flagged, check primary_band match
        for c in self._carrier_items():
            if int(getattr(c, "band", 0) or 0) == int(self.primary_band or 0):
                bw = float(getattr(c, "bandwidth_mhz", 0.0) or 0.0)
                if bw > 0:
                    return bw
        return 0.0

    @property
    def serving_band(self) -> int:
        return int(self.primary_band or 0)

    @property
    def serving_band_signature(self) -> str:
        return f"B{self.serving_band}" if self.serving_band > 0 else ""

    @property
    def active_band_set(self) -> List[int]:
        bands: List[int] = []
        for carrier in self._carrier_items():
            try:
                band = int(getattr(carrier, "band", 0) or 0)
            except Exception:
                band = 0
            if band <= 0:
                continue
            is_pcell = bool(getattr(carrier, "is_pcell", False))
            state = int(getattr(carrier, "state", 0) or 0)
            if is_pcell or state >= 1:
                if band not in bands:
                    bands.append(band)
        bands.sort()
        return bands

    @property
    def active_band_set_signature(self) -> str:
        return band_signature(self.active_band_set)

    @property
    def selected_band_set_signature(self) -> str:
        return band_signature(normalize_band_list(self.selected_band_set))

    @property
    def qm_dl0(self) -> int:
        """LTE modulation order Qm for downlink codeword 0."""
        return int(self.mod0 or self.qam_dl_primary or 0)

    @property
    def qm_dl1(self) -> int:
        """LTE modulation order Qm for downlink codeword 1."""
        return int(self.mod1 or self.qam_dl_secondary or 0)

    @property
    def qm_ul(self) -> int:
        """LTE uplink modulation order Qm."""
        return int(self.qam_ul or 0)

    @property
    def band_set(self) -> List[int]:
        active = self.active_band_set
        if active:
            return active
        selected = normalize_band_list(self.selected_band_set)
        if selected:
            return selected
        if self.serving_band > 0:
            return [self.serving_band]
        return []

    @property
    def band_set_signature(self) -> str:
        active = self.active_band_set
        if active:
            active_sig = band_signature(active)
            has_secondary_detail = any(
                int(getattr(carrier, "band", 0) or 0) > 0
                and not bool(getattr(carrier, "is_pcell", False))
                and int(getattr(carrier, "state", 0) or 0) >= 1
                for carrier in self._carrier_items()
            )
            return active_sig
        selected = self.selected_band_set_signature
        if selected:
            return selected
        return self.serving_band_signature

    @property
    def ca_state_text(self) -> str:
        return "MULTI-BAND ACTIVE" if bool(self.ca_active) else "SINGLE BAND ACTIVE"

    @property
    def radio_state_text(self) -> str:
        return "Connected" if bool(self.radio_connected) else "Disconnected"


# ─────────────────────────────────────────────────────────────
# Brain-computed signals (not from modem, computed by brain)
# ─────────────────────────────────────────────────────────────

@dataclass
class ComputedSignals:
    """
    Signals the brain computes from ModemInput + its own measurements.

    Maps to Foundation Doc Chapter 2.3.3 (brain measurement).

    F1.6+ REWORK: congestion/distance_to_red replaced with:
      - rtt_sliding_avg: always-on canary (scheduling grant delay)
      - rtt_elevated: boolean — RTT pressure above warning threshold
      - rtt_alarm: boolean — RTT pressure above severe/cliff threshold
      - rtt_probe_ok: boolean — fresh RTT sample was collected this sample
      - rtt_pressure_level: `normal` | `busy` | `alarm` | `probe_missing`
      - cq_cell: live connection quality score
    """
    # ── RTT (always-on, every tick) ──
    rtt_ms: float = 0.0              # ICMP RTT to probe target (this sample)
    rtt_ema: float = 0.0             # smoothed RTT (fast EMA)
    rtt_sliding_avg: float = 0.0     # 30-tick sliding window average
    rtt_baseline: float = 0.0        # learned idle floor (per-context, with ratchet)
    rtt_effective_baseline: float = 0.0  # quiet/idle baseline actually used for congestion RTT delta
    rtt_delta: float = 0.0           # sliding_avg - baseline (0 if below baseline)
    rtt_delta_valid: bool = False
    rtt_elevated: bool = False       # True if sliding_avg > warning threshold
    rtt_alarm: bool = False          # True if sliding_avg > severe/cliff threshold
    rtt_probe_ok: bool = True        # True if this sample produced a fresh RTT sample
    rtt_pressure_level: str = "normal"  # normal | busy | alarm | probe_missing
    # Quiet/low-load RTT: operational delta is qualified through 10 Mbps DL
    # and 5 Mbps UL; baseline learning retains its stricter idle gate.
    rtt_quiet_delta: float = 0.0     # RTT delta from last quiet window
    rtt_quiet_held: bool = False     # True = held from previous quiet window, not live
    rtt_delta_block_reason: str = ""  # "" | traffic_busy | ul_contamination | probe_missing
    rtt_delta_display_ms: float = 0.0
    rtt_delta_display_valid: bool = False
    rtt_delta_display_held: bool = False
    rtt_delta_display_live_ts: float = 0.0

    # ── Band Quality (structural) ──
    # Vendor-neutral structural score for the currently serving band/cell:
    #   SINR + RSRP + RI + CA + BW
    # This is the stable structural lane consumed by calibration/learning.
    quality_model: str = "unclassified"
    quality_capacity_fraction: Optional[float] = None
    quality_total_bw_mhz: Optional[float] = None
    quality_radio_inputs: dict = field(default_factory=dict)
    quality_valid: bool = False
    quality_reason: str = ""
    bq_cell: float = 0.0
    bq_cell_valid: bool = False
    bq_cell_source: str = "warming"     # warming | live | hold | restored | stale

    # ── Connection Quality (CQ) ──
    # Live connection usability now:
    #   CQ = smoothed BQ * (1 - congestion penalty)
    # Canonical CQ lane (live connection quality)
    cq_cell: float = 0.0              # live connection quality 0..1
    cq_cell_source: str = "warming"   # warming | live | hold | restored | stale
    cq_coverage: float = 0.0          # valid CQ samples / CQ window
    cq_vector_a_valid: bool = False   # strict evidence validity gate
    cq_cell_display: float = 0.0
    cq_cell_display_valid: bool = False
    cq_cell_display_held: bool = False

    # Intelligence communication quality without the PBAND-bandwidth factor.
    # Core owns this fact; New Horizon grades it without recalculating BQ/CQ.
    cqb_cell: float = 0.0
    cqb_cell_valid: bool = False
    cqb_cell_source: str = "warming"
    cqb_coverage: float = 0.0

    # CA telemetry. Canonical names are CART/CAST. Public ca_* compatibility
    # names are derived at status/UI/telemetry publisher boundaries.
    ca_raw_state: int = 0               # diagnostic raw modem CA state before filtering
    cart: int = 0                       # CART: filtered current CA state, 1 when active else 0
    cast: float = 0.0                   # CAST: CART active ratio over the rolling wall-clock window
    ca_stat_scope_key: str = ""         # context key for CAST reset boundary
    cart_ld: int = 0                    # CART_LD: CART only on DL-load-qualified ticks
    cart_ld_valid: bool = False         # True when current tick has enough DL load for CA to matter
    cart_ld_display: int = 0            # CART_LD display value; mirrors canonical CART_LD gate
    cart_ld_display_valid: bool = False # True while canonical CART_LD gate is valid
    cast_ld: float = 0.0                # CAST_LD: CART active ratio over DL-load-qualified samples
    cast_ld_valid: bool = False         # True after this CAST scope has at least one DL-load sample
    cart_activity_per_hour: float = 0.0 # CART activity: CART changes per wall-clock hour
    cart_ld_activity: float = 0.0       # CART_LD activity: CART changes per qualified-hour
    ri_stability_pct: float = 0.0       # RI stability: RI>=2 ratio over CA scope window
    ri_activity_per_hour: float = 0.0   # RI activity: RI state changes per wall-clock hour
    ri_stability_valid: bool = False
    ca_stat_pct: float = 0.0
    ca_stat_ld_pct: float = 0.0
    ca_stat_ld_valid: bool = False

    # V5 RTT split (probe-gated, not traffic-gated)
    rtt_congestion_avg: float = 0.0   # dedicated RTT window for congestion path
    rtt_recent_median: float = 0.0    # median(last-3) anti-tail confirmation
    rtt_low_load_latency_valid: bool = False
    rtt_low_load_latency_reason: str = ""
    rtt_low_load_latency_mbps: float = 0.0
    rtt_active_traffic_class: str = "unavailable"  # qualified | unavailable
    rtt_active_load_mbps: float = 0.0
    rtt_active_jitter_mad_ms: float = 0.0
    rtt_active_jitter_step_p95_ms: float = 0.0
    rtt_active_jitter_samples: int = 0
    rtt_active_jitter_valid: bool = False
    rtt_dl_jitter_bucket: str = ""
    rtt_dl_jitter_trimmed_ms: float = 0.0
    rtt_dl_jitter_baseline_ms: float = 0.0
    rtt_dl_jitter_delta_ms: float = 0.0
    rtt_dl_jitter_pct: float = 0.0
    rtt_dl_jitter_valid: bool = False
    rtt_dl_jitter_reason: str = ""

    # Congestion qualifier contract (canonical in LUC8 signal block).
    # Current pipeline computes these from the same detector lane that still
    # exposes Vector-B compatibility aliases.
    congestion_qam_gap: float = 0.0
    congestion_qualifier_high: bool = False
    congestion_qualifier_mod: bool = False
    # Compatibility provenance for older CQI/CQ exports. Scheduler signals no
    # longer feed CQ; current CQ is structural BQ with congestion overlay.
    cq_cqi_source: str = "none"

    # Vector-B compatibility telemetry retained only for still-exported fields.
    cqi_mcs_gap_ratio: float = 0.0    # raw CQI-vs-observed DL MCS gap ratio
    cqi_mcs_gap_smoothed: float = 0.0 # smoothed CQI/MCS gap ratio
    cqi_mcs_gap_valid: bool = False   # current tick produced a valid gap sample

    # ── CQI-MCS compatibility telemetry ──
    # Scheduler efficiency is GUI/export-only. Congestion authority is RTT +
    # RSRQ; these fields remain so old logs/readers do not break.
    cqi_mcs_expected_mcs: int = 0         # ideal MCS estimate for averaged CQI
    cqi_mcs_observed_mcs: int = 0         # actual MCS from modem
    cqi_mcs_gap_idx: int = 0              # congestion percent (0..100)
    # Ratio/smoothed/valid are defined above in the compat block.

    # ── Scheduler compatibility telemetry ──
    # GUI/export-only; not a Brain decision or learning input.
    mcs_fast_ema: float = 0.0        # fast MCS tracker (seconds)
    mcs_slow_ema: float = 0.0        # slow MCS tracker (tens of seconds)
    mcs_collapse: bool = False       # fast dropped below slow significantly

    # ── RSRQ trend (channel/propagation signal) ──
    # Prediction-reserved for future Brain layers (ARCH6v1+):
    # intentionally computed/logged now, not yet a direct policy gate.
    rsrq_slope: float = 0.0          # RSRQ trend (2-5 min window)
    rsrq_degrading: bool = False     # True if RSRQ trending down

    # ── RSRQ baseline and gap ──────────────────────────────────────────────
    # Per-context learned idle RSRQ — native reported RSRQ when available,
    # otherwise a same-context RSRP-RSSI fallback when RSSI is trustworthy.
    # Context key: band|cell|earfcn|bandwidth.
    # RSRQ owns an idle baseline/floor per RF context. Calibration settlement
    # corrects stale optimistic floors downward; ratchet moves the floor toward
    # 0 only after sustained better quiet evidence.
    rsrq_baseline: float = 0.0           # learned per-context idle RSRQ
    rsrq_baseline_valid: bool = False    # True once enough samples collected
    rsrq_baseline_samples: int = 0       # qualifying samples accumulated
    # rsrq_gap = rsrq_idle_baseline - smoothed_live_idle_rsrq
    rsrq_gap: float = 0.0               # gap between idle baseline and live RSRQ
    rsrq_gap_valid: bool = False         # False until baseline is trustworthy
    rsrq_display_gap: float = 0.0        # canonical display gap (may be held)
    rsrq_display_gap_valid: bool = False
    rsrq_display_gap_held: bool = False
    rsrq_delta_display_db: float = 0.0
    rsrq_delta_display_valid: bool = False
    rsrq_delta_display_held: bool = False
    rsrq_delta_display_live_ts: float = 0.0
    rsrq_baseline_display_db: float = 0.0
    rsrq_baseline_display_valid: bool = False
    rsrq_baseline_display_held: bool = False
    rsrq_baseline_display_live_ts: float = 0.0
    # ── Combined congestion signal ────────────────────────────────────────
    # Dedicated congestion authority owns these outputs.
    # - congestion_pct: fast internal congestion truth (0-100)
    # - congestion_display_pct: operator-facing value stepped toward fast truth
    # - congestion_internal_bucket: fast bucket with local hysteresis
    # - congestion_bucket: stable user-facing bucket for exported/UI surfaces
    # - congestion_state_avg_pct/state_bucket: slower state-confirmation lane
    congestion_supported: bool = True         # False when required support inputs are unavailable
    congestion_support_reason: str = ""      # canonical unsupported reason when congestion is unavailable
    congestion_pct: float = 0.0              # 0-100, clamped
    congestion_pct_valid: bool = False       # False when both source lanes expired and fast congestion is indeterminate
    congestion_display_pct: float = 0.0      # operator display, max 5 percentage points per authority tick
    congestion_display_valid: bool = False   # display validity follows the canonical congestion authority
    congestion_internal_bucket: str = ""     # fast "normal" | "mild" | "high"
    congestion_bucket: str = ""              # stable user-facing bucket
    congestion_state_avg_pct: float = 0.0    # averaged pct for state confirmation
    congestion_state_bucket: str = ""        # slower "normal" | "mild" | "high"
    congestion_raw_pct: float = 0.0          # unclamped, for logs (can exceed 100)
    congestion_source: str = ""              # "rtt" | "rsrq" | "" (dominant contributor)
    congestion_rsrq_held: bool = False       # True when RSRQ contribution is from hold, not live
    congestion_rsrq_available: bool = False  # True when RSRQ lane had a valid live/held input
    congestion_rsrq_contributes: bool = False  # True when RSRQ entered the weighted congestion mix
    congestion_rsrq_reason: str = ""         # why RSRQ contributed or was excluded
    congestion_rsrq_pct: float = 0.0         # normalized RSRQ congestion projection before weighting
    congestion_rsrq_gap_db: float = 0.0      # live/held RSRQ gap considered by the combiner
    congestion_rsrq_gap_threshold_db: float = 0.0    # positive RSRQ gap threshold required before live RSRQ contributes
    congestion_fast_mode: str = ""           # live | signal_hold | combined_hold | invalid
    congestion_hold_mode: str = ""           # none | rtt | rsrq | multi | combined
    congestion_trusted_rtt: bool = False     # fast lane accepted RTT as live evidence this sample
    congestion_rtt_base_pct: float = 0.0     # normalized RTT pressure before bounded jitter addition
    congestion_trusted_rsrq: bool = False    # fast lane accepted RSRQ as live evidence this sample
    # Low-load jitter may strengthen the existing RTT congestion lane.  These
    # are diagnostics for that modifier, never a third weighted lane.
    congestion_trusted_active_jitter: bool = False
    congestion_active_jitter_pct: float = 0.0  # bounded RTT-pressure addition in percentage points
    congestion_slope: float = 0.0            # rate of change: pct-points per minute (positive = rising)
    congestion_rising: bool = False          # True if slope > 3 pct/min sustained over window
    congestion_trend_display: str = ""       # CongestionAuthority-owned display trend: UP | DOWN | STABLE
    congestion_flags_valid: bool = False     # True when authority pressure flags were computed
    congestion_bad_now: bool = False         # canonical state-consumer bad/moderate gate
    congestion_pressure_high: bool = False   # high congestion pressure
    congestion_pressure_moderate: bool = False  # moderate-or-higher congestion pressure
    congestion_kind_hint: str = ""           # authority hint: local_traffic | attached_cell

    # ── Delivered capacity evidence ──────────────────────────────────────
    # Nullable Beta evidence from controlled speed tests only. This is not a
    # scheduler/QAM proof and must not be derived from ordinary live traffic.
    delivered_capacity_valid: bool = False
    delivered_capacity_ratio: float = 0.0
    delivered_capacity_best_dl_mbps: float = 0.0
    delivered_capacity_total_bw_mhz: float = 0.0
    delivered_capacity_ceiling_mbps: float = 0.0
    delivered_capacity_sample_count: int = 0
    delivered_capacity_source: str = "needs_controlled_speed_tests"
    dl_bottleneck_state: str = "unknown"
    dl_bottleneck_reason: str = "missing_controlled_speedtest_evidence"
    dl_bottleneck_proof_level: str = "unknown"
    dl_bottleneck_missing_evidence: tuple[str, ...] = field(
        default_factory=lambda: ("controlled_speedtest_dl_samples",)
    )
    dl_bottleneck_rejected_explanations: tuple[str, ...] = field(default_factory=tuple)
    dl_bottleneck_evidence: dict[str, Any] = field(default_factory=dict)
    dl_bottleneck_owner: str = "brain.dl_bottleneck"
    dl_bottleneck_schema_version: str = "dl_bottleneck_v1"

    # ── Cell structural change detection ──────────────────────────────────
    # True while a serving-cell identity change is still settling: new PCI,
    # EARFCN, primary band, or a CA signature change that survived the dwell
    # filter.
    # Signals that calibration baselines may be invalidated (tower change,
    # provider reconfig, handover, etc.).
    cell_structural_change: bool = False
    cell_structural_change_fresh: bool = False  # True only on the first observed change tick.
    cell_structural_change_kind: str = ""  # "pci" | "earfcn" | "band" | "ca_signature"
    cell_structural_change_class: str = ""  # "ca_only" | "same_band_cell" | "anchor_move" | "service_loss_shift"
    cell_structural_change_service_impact: bool = False

    # Before-state snapshot for self-switch capture (Task 3).
    # Populated by _detect_structural_change() with the values from the
    # previous tick, so engine.py can log a before/after episode.
    # Only valid when cell_structural_change_fresh=True.
    cell_change_pci_before: int = 0
    cell_change_earfcn_before: int = 0
    cell_change_band_before: int = 0
    cell_change_ca_sig_before: str = ""

    # ── Input freeze detection ─────────────────────────────────────────────
    # Counts consecutive ticks where the modem-side fingerprint appears stale.
    # This is observation-only and should stay conservative: stable RF alone
    # must not be treated as a frozen modem feed.
    input_freeze_samples: int = 0

    # ── 5G/NR readiness stubs (Tier 3, Item 11) ───────────────────────────
    # Stubbed at 0/False for all existing LTE backends.
    # These fields unlock future 5G NSA/SA backend support without breaking
    # the existing LTE pipeline. Set by 5G-capable backends only.
    nr_rsrp_dbm: float = 0.0         # NR RSRP (dBm), 0.0 = unavailable
    nr_sinr_db: float = 0.0          # NR SINR (dB), 0.0 = unavailable
    nr_rsrq_db: float = 0.0          # NR RSRQ (dB), 0.0 = unavailable
    nr_band: int = 0                  # NR serving band number (0 = unavailable)
    nsa_active: bool = False          # True if 5G NSA (LTE anchor + NR secondary)
    sa_active: bool = False           # True if 5G SA (NR standalone)
    nr_available: bool = False        # True only for a stable NSA secondary
    nr_secondary_active: bool = False
    nr_transition_state: str = "absent"
    nr_transition_dirty: bool = False
    nr_identity_completeness: str = "absent"

    # ── 5G/NR derived semantic flags ──────────────────────────────────────
    # These are the fields the brain, incident assessor, and learning system
    # consume — never raw nr_rsrp_dbm directly.
    #
    # nr_rsrp_ema: short EMA of nr_rsrp_dbm sampled only from non-zero ticks.
    #   Alpha is tuned to preserve roughly ~15-20s smoothing at the current
    #   2s controller tick.
    #   Resets to 0.0 when nsa_active transitions True (clean attach).
    #   Only fed by non-zero nr_rsrp_dbm values (zero = modem not reporting NR).
    #
    # nr_rsrp_ema_valid: True after min 3 non-zero NR RSRP samples accumulated.
    #
    # nr_recovering: nsa_active=False but EMA is valid and above the minimum
    #   viable threshold for NR SCell addition (−100 dBm for n78, derived from
    #   3GPP B1 event operator data). Means: NR signal present, re-attach likely.
    #   Brain must suppress learning/incident classification while this is True.
    #
    # nr_just_dropped: nsa_active flipped False this sample (was True last tick).
    #   Brain must suppress incident classification on this sample specifically.
    nr_rsrp_ema: float = 0.0
    nr_rsrp_ema_valid: bool = False
    nr_recovering: bool = False
    nr_just_dropped: bool = False
    nsa_snapshot: dict[str, Any] = field(default_factory=dict)
    nr_capacity_evidence: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
# Chapter 2.3: Modem Outputs (five dynamic levers)
# ─────────────────────────────────────────────────────────────

class LeverType(Enum):
    """Cellular and traffic-control levers owned by Brain/Controller."""
    BAND_SELECTION = auto()    # Lever 1
    BAND_LOCK = auto()         # Single-band restriction / hard lock
    CELL_LOCK = auto()         # Lever 2
    TRAFFIC_PRESENCE = auto()  # Lever 3
    TRAFFIC_VOLUME = auto()    # Lever 4
    UE_CAPABILITY = auto()     # Lever 5
    RESTART = auto()           # Modem restart (B818)
    PBAND = auto()             # Primary user-facing anchor/change control
    NR_BAND_LOCK = auto()      # NR band preference (ZTE MC888 goform, NSA only)
    WIFI_RADIO = auto()        # Device Wi-Fi radio enable/disable
    NETWORK_SETTINGS = auto()  # Explicit SIM roaming/operator/APN request
    MOBILE_DATA = auto()  # Device remains reachable while cellular data is off
    RADIO_MODE = auto()  # Guarded LTE-only / previously selected NSA mode


@dataclass
class LeverAction:
    """
    A command from the brain to the modem.

    Every brain decision produces zero or one LeverAction.
    The modem abstraction layer translates this to hardware-specific
    commands (RouterOS API, AT commands, etc).
    """
    lever: LeverType
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""                # human-readable decision rationale
    intent: str = ""                # semantic objective type (e.g., restore_ca)
    objective_id: str = ""          # stable objective key for lifecycle tracking

    # ── Lever 1: Band Selection ──
    # params: {"bands": [1, 3, 20]} — allowed band list

    # ── Lever 2: Cell Lock ──
    # params: {"pci": 303, "earfcn": 1795} — lock to specific cell
    # params may include {"lock_duration_s": 3600} for timed manual locks
    # params: {"unlock": True} — release cell lock

    # ── Lever 3: Traffic Presence ──
    # params: {"keepalive_interval_s": 5} — adjust ping rate

    # ── Lever 4: Traffic Volume ──
    # params: {"background_mbps": 1.0} — background traffic rate
    # params: {"sqm_limit_mbps": 8.0} — upload SQM limit

    # ── Lever 5: UE Capability ──
    # params: {"dl_256qam": True, "ul_64qam": True}
    # params: {"restart": True} — requires modem reboot


@dataclass
class LeverResult:
    """Result of executing a LeverAction on the modem."""
    success: bool
    lever: LeverType
    executed_at: float = field(default_factory=time.time)
    error: str = ""
    radio_drop_detected: bool = False  # True if radio went down during action
    recovery_time_s: float = 0.0       # how long until radio came back
    details: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
# Chapter 4: System States
# ─────────────────────────────────────────────────────────────

class SystemState(Enum):
    """
    Signal-driven system states (ARCH7 model).

    Canonical runtime states:
      WORKING
      DEGRADED
      CONGESTED
      E_RADIO_DOWN
      BAD_RF
      E_SESSION_RESET
      E_CONGESTION_STORM

    Historical state-name aliases:
      OPTIMIZING -> WORKING
      EXPLORING  -> WORKING
    """
    WORKING = auto()
    DEGRADED = auto()
    CONGESTED = auto()
    E_RADIO_DOWN = auto()
    BAD_RF = auto()
    E_RF_COLLAPSE = BAD_RF
    E_SESSION_RESET = auto()
    E_CONGESTION_STORM = auto()

    # Legacy aliases
    OPTIMIZING = WORKING
    EXPLORING = WORKING


def canonical_state_name(state: SystemState | str | None) -> str:
    """Return the canonical exported runtime state name.

    Runtime truth is now:
      WORKING, DEGRADED, CONGESTED, fault states

    Compatibility handling:
      - legacy string CONFIRMED_CONGESTION -> CONGESTED
      - legacy string SUSPECTED_CONGESTION -> WORKING
      - legacy OPTIMIZING / EXPLORING -> WORKING
    """
    if isinstance(state, SystemState):
        return str(state.name or "")
    txt = str(state or "").strip().upper()
    if txt in {"", "NONE"}:
        return ""
    if txt in {"OPTIMIZING", "EXPLORING", "SUSPECTED_CONGESTION"}:
        return "WORKING"
    if txt == "CONFIRMED_CONGESTION":
        return "CONGESTED"
    return txt


EMERGENCY_STATES = {
    SystemState.E_RADIO_DOWN,
    SystemState.E_SESSION_RESET,
    SystemState.E_CONGESTION_STORM,
}

HARD_FAULT_STATES = {
    SystemState.E_RADIO_DOWN,
}

FAULT_STATES = {
    SystemState.BAD_RF,
    *EMERGENCY_STATES,
}


def is_emergency_state(state: Optional[SystemState]) -> bool:
    return state in EMERGENCY_STATES


def is_hard_fault_state(state: Optional[SystemState]) -> bool:
    return state in HARD_FAULT_STATES


def is_fault_state(state: Optional[SystemState]) -> bool:
    return state in FAULT_STATES


def emergency_state_class(
    state: Optional[SystemState],
    emergency_class: str = "",
) -> str:
    if state in {
        SystemState.E_RADIO_DOWN,
        SystemState.E_SESSION_RESET,
        SystemState.E_CONGESTION_STORM,
    }:
        return str(state.name)
    emergency_class = str(emergency_class or "")
    if emergency_class in {"E_RADIO_DOWN", "E_SESSION_RESET", "E_CONGESTION_STORM"}:
        return emergency_class
    return ""


def fault_state_class(
    state: Optional[SystemState],
    emergency_class: str = "",
) -> str:
    if state == SystemState.BAD_RF:
        return "BAD_RF"
    if state in EMERGENCY_STATES:
        return str(state.name)
    emergency_class = str(emergency_class or "")
    if emergency_class in {"BAD_RF", "E_RF_COLLAPSE"}:
        return "BAD_RF"
    if emergency_class in {"E_RADIO_DOWN", "E_SESSION_RESET", "E_CONGESTION_STORM"}:
        return emergency_class
    return ""


# ─────────────────────────────────────────────────────────────
# Chapter 5.4: Constraint Map
# ─────────────────────────────────────────────────────────────

@dataclass
class Constraints:
    """
    What this hardware can and cannot do.
    Populated by startup checklist, read by brain at runtime.

    Maps to Foundation Doc Chapter 5.4.
    """
    ttl_ipv4: bool = False
    ttl_ipv6: bool = False
    mss_ipv4: bool = False
    mss_ipv6: bool = False
    keepalive: bool = False
    dl_256qam: bool = False
    ul_64qam: bool = False
    imei_phone: bool = False
    pci_lock_available: bool = False
    band_select_available: bool = True
    band_lock_available: bool = False
    band_priority_available: bool = True
    ue_category: int = 0
    ca_combos_confirmed: List[str] = field(default_factory=list)
    provider_plmn: str = ""           # e.g. "22003"
    provider_name: str = ""           # e.g. "MTS"

    def lever_available(self, lever: LeverType) -> bool:
        """Check if a lever is usable on this hardware."""
        if lever == LeverType.BAND_SELECTION:
            return self.band_select_available
        if lever == LeverType.BAND_LOCK:
            return self.band_lock_available
        if lever == LeverType.CELL_LOCK:
            return self.pci_lock_available
        # All other levers are always available
        return True


# ─────────────────────────────────────────────────────────────
# Modem capabilities (backend feature matrix)
# ─────────────────────────────────────────────────────────────

@dataclass
class ModemCapabilities:
    """Static or probed hardware/firmware capabilities.

    Defaults stay conservative for control levers, but traffic support
    defaults to True by policy until a backend is explicitly proven not
    to expose usable traffic counters. Bare construction keeps PBAND
    optimistic for local orchestration tests, while real backend truth still
    comes from the catalog via ``get_capabilities(backend)``.
    """
    supports_band_selection: bool = False
    supports_band_lock: bool = False
    supports_cell_lock: bool = False
    supports_traffic_presence: bool = True
    supports_traffic_volume: bool = True
    supports_ue_capability: bool = False
    supports_restart: bool = False
    supports_neighbors: bool = False
    supports_sms: bool = False
    supports_ca_details: bool = False
    supports_band_priority: bool = False
    supports_plmn: bool = False
    supports_pband: bool = True
    supports_nr_band_lock: bool = False
    supports_wifi_control: bool = False
    supports_network_settings: bool = False
    supports_mobile_data: bool = False
    supports_radio_mode: bool = False
    traffic_presence_owner: str = "runtime_54guc"
    pband_control_mode: str = "none"
    ca_pool_control_mode: str = "none"
    cell_lock_control_mode: str = "none"


def get_capabilities(backend: str) -> ModemCapabilities:
    """Capability matrix by modem backend name."""
    from arch4.modem_live.profile_catalog import (
        modem_backend_action_semantics,
        modem_backend_capability_flags,
    )

    caps = modem_backend_capability_flags(backend)
    caps.setdefault("supports_pband", True)
    semantics = modem_backend_action_semantics(backend)
    caps["traffic_presence_owner"] = semantics.traffic_presence_mode
    caps["pband_control_mode"] = semantics.pband_control_mode
    caps["ca_pool_control_mode"] = semantics.ca_pool_control_mode
    caps["cell_lock_control_mode"] = semantics.cell_lock_control_mode
    return ModemCapabilities(**caps)


# ─────────────────────────────────────────────────────────────
# Memory types (Chapter 4.4 — learned baselines)
# ─────────────────────────────────────────────────────────────

@dataclass
class ActionRecord:
    """
    One action→result pair stored in memory.

    Signal conditions at time of action + what we did + what happened.
    This is how the brain learns without time zones.
    """
    timestamp: float

    # Signal conditions when action was taken
    sinr_db: float
    rsrq_db: float
    mcs_dl: int
    ca_signature: str
    rtt_sliding_avg: float
    rtt_ms: float

    # What we did
    lever: LeverType
    action_params: Dict[str, Any]

    # What happened (measured after the action settlement window)
    success: bool
    throughput_before_mbps: float
    throughput_after_mbps: float
    sinr_after: float
    ca_signature_after: str
    improvement: float  # throughput_after - throughput_before
    provider_plmn: str = ""
    provider_name: str = ""
    outcome_label: str = ""  # good|bad|neutral (band-learning classifier)
    hard_fail: bool = False
    calibration_marker: str = ""
    calibration_signature: str = ""
    incident_class: str = ""
    incident_next_step: str = ""
    incident_self_induced: bool = False
    learning_ltm_applied: bool = True
    learning_policy_reason: str = ""
    # v5.0 verification/bouncer/referee/learning fields
    verification_status: str = ""
    verification_note: str = ""
    verification_stage: str = ""
    verification_authority: str = ""
    verification_triggered_by: str = ""
    bouncer_intent_level: str = ""
    bouncer_intent_code: str = ""
    intent_verdict_stage: str = ""
    intent_verdict_authority: str = ""
    intent_verdict_triggered_by: str = ""
    referee_outcome_level: str = ""
    referee_outcome_code: str = ""
    outcome_verdict_stage: str = ""
    outcome_verdict_authority: str = ""
    outcome_verdict_triggered_by: str = ""
    learning_commit_stage: str = ""
    learning_commit_authority: str = ""
    learning_commit_triggered_by: str = ""
    learning_commit_status: str = ""
    learning_gate: Dict[str, Any] = field(default_factory=dict)
    proposal_reason: str = ""
    proposal_intent: str = ""
    proposal_objective_id: str = ""
    objective_stage: str = ""
    objective_status: str = ""
    objective_until_ts: float = 0.0
    objective_metadata: Dict[str, Any] = field(default_factory=dict)
    decision_trace_id: str = ""
    proposal_score: float = 0.0
    proposal_action_origin: str = ""
    proposal_stage: str = ""
    proposal_authority: str = ""
    proposal_triggered_by: str = ""
    execution_status: str = ""
    execution_error: str = ""
    execution_stage: str = ""
    execution_authority: str = ""
    execution_triggered_by: str = ""
    cq_cell: float = 0.0
    roi_horizon_s: int = 0
    roi_parent_trace_id: str = ""
    rat_context: Dict[str, Any] = field(default_factory=dict)
    DLST: Dict[str, Any] = field(default_factory=dict)
    baseline_evidence_scope: Dict[str, Any] = field(default_factory=dict)
    evidence_scope: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BandKnowledge:
    """
    What the brain knows about a specific band from experience.
    """
    band: int
    times_tried: int = 0
    times_appeared_in_ca: int = 0      # actually showed up as SCell
    last_tried: float = 0.0
    last_sinr: float = 0.0
    last_ca_signature_with: str = ""   # best CA seen with this band
    avg_throughput_with: float = 0.0   # average DL when this band active
    never_works: bool = False          # True if tried N times, never appeared
    times_primary_attach: int = 0      # requested band became PCell
    times_fallback_attach: int = 0     # requested band missing, modem attached elsewhere
    times_unusable_attach: int = 0     # attached but scored as weak/unusable
    last_outcome: str = ""             # last probe outcome classification
    DLST: Dict[str, Any] = field(default_factory=dict)  # nullable delivered-load-speed-test evidence


@dataclass
class CellKnowledge:
    """
    What the brain knows about a specific cell (PCI) from experience.

    LTE quality fields (all four cases):
      avg_bq_cell   — structural band/cell quality EMA (RF + CA/RI/BW)
      avg_cq_cell   — live connection quality EMA (legacy field; reset on epoch change)
      avg_sinr      — raw SINR EMA, secondary reference
      avg_throughput — DL throughput EMA, load-dependent, kept for compatibility
      avg_mcs       — MCS EMA

    NR fields (Cases 3+4 only, zero/inactive on 4G modem or 4G SIM):
      nr_attach_count — ticks where nsa_active=True on this PCI
      nr_total_count  — total ticks observed on this PCI (for attach rate)
      nr_sinr_ema     — EMA of nr_sinr_db when stable NSA is active

    nr_attach_rate = nr_attach_count / nr_total_count when nr_total_count >= 5.
    All NR fields stay zero on 4G-only hardware — no special branch needed.
    """
    pci: int
    band: int
    earfcn: int
    cell_id: int = 0              # E-UTRAN Cell Identity (stable)
    times_served: int = 0
    avg_sinr: float = 0.0
    avg_throughput: float = 0.0
    avg_mcs: float = 0.0
    avg_bq_cell: float = 0.0      # structural quality — per band/cell memory
    avg_cq_cell: float = 0.0      # composite quality — primary cell score
    best_ca_signature: str = ""
    last_seen: float = 0.0
    observed_bw_mhz: float = 0.0  # primary carrier bandwidth observed on this PCI
    # ── NR fields ──────────────────────────────────────────────────────────
    nr_attach_count: int = 0       # ticks with nsa_active=True on this PCI
    nr_total_count: int = 0        # total ticks observed (denominator for attach rate)
    nr_sinr_ema: float = 0.0       # EMA of nr_sinr_db when NR is attached
    DLST: Dict[str, Any] = field(default_factory=dict)  # nullable delivered-load-speed-test evidence


# ─────────────────────────────────────────────────────────────
# Axis 2: Aggressiveness types
# ─────────────────────────────────────────────────────────────

@dataclass
class AggressivenessProfile:
    """Per-state aggressiveness profile — how hard the brain pushes."""
    foreground_budget: float = 0.5      # 0-1: how urgently to act
    background_budget: float = 0.3      # 0-1: how much to explore
    probe_interval_s: float = 90.0      # min seconds between bg probes
    max_actions_per_window: int = 4     # max actions in window_s
    window_s: float = 600.0             # rate limit window (seconds)



# ─────────────────────────────────────────────────────────────
# Axis 3: Historical priors types
# ─────────────────────────────────────────────────────────────

@dataclass
class HourlyPattern:
    """Learned pattern for one hour of the day."""
    hour: int = 0
    samples: int = 0
    avg_sinr: float = 0.0
    avg_mcs: float = 0.0
    avg_throughput_mbps: float = 0.0
    avg_rtt: float = 0.0                    # avg RTT (congestion indicator)
    ca_active_ratio: float = 0.0
    best_band_signature: str = ""       # e.g. "B1+B3+B20"
    worst_band_signature: str = ""
    # ── NR hourly fields (zero on 4G-only hardware) ──
    nr_attach_ratio: float = 0.0         # EMA of nsa_active (0.0–1.0)
    nr_sinr_avg: float = 0.0            # EMA of NR SINR when attached


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

@dataclass
class Arch4Config:
    """
    All tunable parameters in one place.
    """
    # ── Sample timing ──
    sample_interval_s: float = 2.0
    modem_reconnect_interval_s: float = 15.0
    modem_read_missed_operator_min_s: float = 20.0
    cell_structural_change_ca_min_dwell_s: float = 30.0

    # ── Decision-context capture (night anomaly review) ──
    decision_context_capture_enabled: bool = True
    decision_context_capture_start_hour: int = 0
    decision_context_capture_end_hour: int = 6
    decision_context_capture_pre_s: float = 180.0
    decision_context_capture_post_s: float = 180.0
    decision_context_capture_ring_s: float = 480.0

    # ── SINR feasibility gate (Foundation Doc: Section 3.4 / SWFS) ──
    sinr_feasibility_gate_db: float = 10.0

    # ── DEGRADED state ──
    degraded_timeout_s: float = 300.0      # 5 min max in DEGRADED
    degraded_entry_ticks: int = 10         # 20s of LQ < 0.5 before entering
    degraded_recovery_ticks: int = 15      # 30s sustained recovery before DEGRADED -> WORKING
    degraded_idle_exit_ticks: int = 15     # 30s sustained no-traffic evidence before DEGRADED -> WORKING
    degraded_reentry_guard_s: float = 30.0 # guard immediate WORKING -> DEGRADED flip after recovery

    # ── Congestion lifecycle (ARCH7) ──
    congestion_suspect_entry_ticks: int = 3   # quick suspicion entry
    congestion_suspect_clear_ticks: int = 3   # clear suspicion hysteresis
    congestion_confirm_probe_ticks: int = 4   # short confirmation probe window
    congestion_confirm_bad_ratio: float = 0.6 # bad sample fraction to confirm
    congestion_confirm_cooldown_s: float = 120.0
    congestion_confirm_clear_ticks: int = 5
    congestion_state_confirm_window_s: float = 30.0  # averaged confirmation window for state entry
    # Recurrence promotion: repeated suspect episodes imply real congestion.
    congestion_suspect_recurrence_window_s: float = 300.0  # 5 min window
    congestion_suspect_recurrence_hits: int = 3            # promote on N episodes
    # Legacy knob (compat)
    congested_entry_ticks: int = 3
    congested_check_interval_s: float = 150.0  # periodic exploration every 2.5 min
    # Congestion source split:
    # local_traffic = user/device traffic burst under otherwise healthy RF.
    # attached_cell = shared-cell pressure / external congestion.
    congestion_self_load_dl_mbps: float = 10.0
    congestion_rf_ok_sinr_db: float = 6.0
    congestion_rf_ok_rsrp_dbm: float = -90.0
    congestion_mitigation_cooldown_s: float = 1800.0  # 30 min between controlled attempts
    preemptive_switch_cooldown_s: float = 1800.0      # 30 min between preemptive switches
    # Cross-state soft congestion detector (independent of strict CONGESTED entry).
    soft_congestion_enabled: bool = True
    soft_congestion_lq_threshold: float = 0.20
    soft_congestion_rtt_min_ms: float = 120.0
    soft_congestion_entry_ticks: int = 5
    soft_congestion_clear_ticks: int = 3
    soft_congestion_min_dl_mbps: float = 0.8

    # Legacy knob (compat)
    exploring_entry_ticks: int = 3

    # ── Explicit fault states ──
    emergency_timeout_s: float = 120.0     # 2 min max in E_* fault state before timeout review
    emergency_sinr_floor: float = 3.0      # below this = radio effectively dead
    emergency_observe_s: float = 8.0       # brief transient-fault observe window before recovery ladder
    emergency_radio_down_persist_s: float = 10.0  # require sustained radio-down
    emergency_sinr_persist_s: float = 20.0        # require sustained SINR floor breach
    emergency_congestion_persist_s: float = 300.0 # sustained congested-cliff duration before E_CONGESTION_STORM
    soft_emergency_hold_s: float = 900.0          # stalled-recovery hold (15 min)
    emergency_reentry_guard_s: float = 120.0      # guard window after E_* fault -> WORKING
    emergency_recovery_stable_s: float = 20.0     # stable dwell before E_* fault -> WORKING
    bad_rf_recovery_stable_s: float = 20.0        # stable dwell before BAD_RF -> WORKING/DEGRADED
    bad_rf_action_cooldown_s: float = 120.0       # pacing between bounded BAD_RF recovery attempts
    bad_rf_systematic_probe_after_s: float = 1800.0  # escalate to systematic band probing after this many seconds in BAD_RF (30 min)
    bad_rf_capacity_min_dl_mbps: float = 20.0     # CA-capable low-SINR lane can be usable when outcome is strong
    bad_rf_capacity_max_rtt_ms: float = 120.0
    bad_rf_capacity_max_rtt_delta_ms: float = 60.0
    bad_rf_capacity_max_jitter_pct: float = 25.0
    bad_rf_capacity_max_congestion_pct: float = 50.0
    bad_rf_capacity_min_session_uptime_s: float = 30.0
    emergency_action_cooldown_s: float = 120.0    # min gap between emergency actions
    emergency_action_max_per_window: int = 3
    emergency_action_window_s: float = 900.0
    emergency_action_global_cooldown_s: float = 3600.0

    # ── MCS sampling (Foundation Doc: Section 3.4 #1) ──
    mcs_fast_ema_alpha: float = 0.30       # seconds timescale
    mcs_slow_ema_alpha: float = 0.05       # tens of seconds
    mcs_collapse_delta: float = 4.0        # fast below slow by this = collapse
    mcs_idle_floor: int = 5                # below this = idle, don't sample

    # ── RTT ──
    rtt_ema_alpha: float = 0.20
    rtt_sliding_window: int = 13           # ticks (~26s at 2s tick)
    rtt_warning_ms: float = 150.0          # EXPLORING threshold
    rtt_baseline_default: float = 55.0     # starting baseline when no history exists (operator-editable)
    rtt_cliff_ms: float = 400.0            # CONGESTED threshold
    rtt_warning_clear_ms: float = 120.0    # exit EXPLORING (hysteresis)
    rtt_cliff_clear_ms: float = 350.0      # exit CONGESTED (hysteresis)
    rtt_probe_target: str = "8.8.8.8"

    # ── V5 evidence lanes (CQ + RTT split) ──
    v5_congestion_enabled: bool = True
    # Vector A (evidence validity / CQ lane)
    v5_cq_window_samples: int = 15
    v5_cq_min_samples: int = 6
    v5_cq_hold_last_min_samples: int = 2
    v5_cq_coherence_min: float = 0.55
    v5_cq_mcs_positive_ratio_min: float = 0.50
    v5_cq_weight_cqi: float = 0.30
    v5_cq_weight_mod: float = 0.30
    v5_cq_weight_ca: float = 0.30
    v5_cq_weight_ri: float = 0.10
    v5_cq_ri_bonus: float = 0.10
    v5_cq_weight_rtt_penalty: float = 0.10
    v5_cq_rtt_ref_ms: float = 60.0
    v5_cq_rtt_span_ms: float = 120.0
    v7_qam_integration_enabled: bool = True
    v7_qam_confidence_min: float = 0.55
    v5_cq_confidence_min_for_state: float = 0.45
    v5_cq_bad_threshold: float = 0.42
    v5_cq_good_threshold: float = 0.55
    # Congestion qualifier lane (QAM-gap + RTT confirmation + persistence)
    congestion_qualifier_gap_high: float = 0.50
    congestion_qualifier_gap_mod: float = 0.25
    congestion_qualifier_rtt_high_ms: float = 95.0
    congestion_qualifier_rtt_mod_ms: float = 75.0
    congestion_qualifier_window_ticks: int = 10
    congestion_qualifier_persist_hits: int = 8
    congestion_qualifier_clear_ticks: int = 3
    congestion_qualifier_rsrq_gap_db: float = 4.0
    # Combined congestion signal normalization (dashboard/operator heuristic)
    congestion_rtt_floor_ms: float = 5.0
    congestion_rtt_ceiling_ms: float = 150.0
    congestion_rsrq_gap_threshold_db: float = DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB
    congestion_rsrq_ceiling_db: float = 6.0
    congestion_mcs_floor_ratio: float = 0.05
    congestion_rtt_weight: float = 50.0
    congestion_rsrq_weight: float = 50.0
    # RTT split by function
    v5_rtt_congestion_window: int = 20
    v5_rtt_recent_window: int = 3
    jitter_formula_pct_scale_ms: float = 5.0
    jitter_formula_visible_scale: float = 3.0
    jitter_formula_trim_frac: float = 0.10
    jitter_formula_low_frac: float = 0.20
    # SMS inbox acquisition is always independent of remote-command control.
    # Modem Live translates native SMS into the internal SmsMessage language.
    sms_inbox_poll_interval_s: float = 121.0
    sms_command_enabled: bool = False
    sms_command_approved_senders: List[str] = field(default_factory=list)
    sms_command_code_word: str = "reboot"
    sms_command_delete_processed: bool = False
    # Low-RF gate for congestion labeling
    v5_low_rf_sinr_gate_db: float = 4.0
    # Throughput hold policy
    v5_good_dl_hold_enabled: bool = True
    v5_good_dl_hold_mbps: float = 60.0

    # ── Bouncer stable-link caution gates (intent evaluator; CQ-based) ──
    bouncer_stable_cq_threshold: float = 0.65
    bouncer_stable_lq_threshold: float = 0.65
    bouncer_stable_sinr_db: float = 8.0

    # ── RSRQ trend ──
    rsrq_window_ticks: int = 30            # idle-only samples (~60-90s of idle at 2s tick)
    rsrq_degrading_slope: float = -0.05   # dB/idle-tick threshold (steeper = fewer false alarms)
    # Retired in active CGF: RSRQ baseline is owned by current qualified samples.
    # Retained only so older serialized settings can be read without becoming
    # runtime authority. SignalComputer must not consume these fields.
    rsrq_baseline_default: float = -10.0
    rsrq_ratchet_window_sec: float = 1800.0
    rsrq_ratchet_min_candidates: int = 10

    # ── Band discovery ──
    band_never_works_threshold: int = 5    # tried N times, never in CA = stop
    band_retry_interval_s: float = 3600.0  # retry "never works" band after 1h

    # ── Probe exhaustion (per-hour LTM early filter) ──
    # Prevents probing band combos that have reliably scored badly at this hour.
    # Different from learning_guard (post-hoc bad-rate 0.70): this is an early
    # candidate filter at a lower bar, with time-based retry.
    probe_exhaustion_min_attempts: float = 5.0    # need this many LTM samples
    probe_exhaustion_score_floor: float = 0.35    # below this = exhausted
    probe_exhaustion_recency_s: float = 7200.0    # retry after 2h (conditions may change)

    # ── Action cost awareness ──
    min_action_interval_s: float = 30.0    # minimum time between lever actions
    startup_grace_s: float = 1800.0        # 30 min observe-only after fresh start (fault recovery exempt)
    autonomous_connect_settle_s: float = 30.0   # mandatory post-login/attach cooldown before autonomous actions
    radio_change_settle_s: float = 30.0         # hold autonomous actions after serving band/cell changes
    band_action_fail_limit: int = 3        # consecutive unsuccessful band actions before cooldown
    band_action_cooldown_s: float = 3600.0 # suppress automatic band actions for 1h after fail limit
    objective_feedback_no_effect_block_s: float = 900.0  # suppress repeated no-effect objectives for 15 min
    restore_ca_retry_after_s: float = 1800.0  # retry failed restore_ca objective after 30 min
    restore_ca_repeat_block_s: float = 3600.0  # block repeated restore_ca probes for the same combo
    explore_repeat_block_s: float = 3600.0     # block repeated explore probes for the same combo
    ca_lost_window_s: float = 20.0         # sliding window for CA_LOST confidence
    ca_lost_on_threshold: float = 0.1      # (legacy, unused) avg below this => CA_LOST ON
    ca_lost_off_threshold: float = 0.5     # (legacy, unused) avg above this => CA_LOST OFF
    global_band_churn_window_s: float = 600.0  # rolling anti-churn budget window
    global_band_churn_max_actions: int = 6      # max automatic band actions in normal window
    global_band_churn_emergency_max_actions: int = 3  # tighter budget during fault states
    global_band_churn_cooldown_s: float = 1800.0  # cooldown after normal-state churn budget exhaustion
    global_band_churn_emergency_cooldown_s: float = 3600.0  # cooldown after fault-state churn exhaustion
    ca_lost_eval_guard_s: float = 30.0     # block CA_LOST eval after band-related actions
    ca_lost_min_samples: int = 10          # min samples before any CA_LOST transition
    ca_lost_restore_min_positive: int = 3  # min positive samples to declare CA restored
    ca_lost_operator_min_s: float = 120.0   # CA loss must persist this long before operator incident if service is otherwise usable
    hilink_priority_rotate_verify_s: float = 600.0         # verify anchor hold window (10 min)
    hilink_priority_rotate_fail_cooldown_s: float = 7200.0 # block retries after revert (2h)
    hilink_priority_rotate_success_quiet_s: float = 3600.0 # quiet window after stable hold (1h)
    # MikroTik rotate lifecycle (capacity optimization with anti-thrash bounds).
    mikrotik_priority_rotate_min_interval_s: float = 1800.0      # min gap between rotate attempts (30 min)
    mikrotik_priority_rotate_fail_cooldown_s: float = 7200.0     # block retries after revert (2h)
    mikrotik_priority_rotate_success_quiet_s: float = 7200.0     # quiet window after stable hold (2h)
    mikrotik_priority_rotate_window_s: float = 21600.0           # rolling budget window (6h)
    mikrotik_priority_rotate_max_in_window: int = 2              # max rotate attempts per window
    mikrotik_priority_rotate_sinr_gate_db: float = 14.0          # RF floor for rotate attempts
    mikrotik_priority_rotate_min_dl_mbps: float = 0.8            # traffic floor for rotate attempts
    # MikroTik explore + cell-lock retry/fail guard rails.
    mikrotik_explore_retry_after_s: float = 1800.0
    mikrotik_explore_fail_limit: int = 3
    mikrotik_explore_fail_cooldown_s: float = 3600.0
    mikrotik_cell_lock_fail_limit: int = 2
    mikrotik_cell_lock_fail_cooldown_s: float = 3600.0
    cell_lock_retry_after_s: float = 900.0               # retry failed cell-lock objective after 15 min
    restart_retry_after_s: float = 1800.0                # retry failed restart objective after 30 min
    # Escape-hatch probing (learning starvation breaker): bypass selected
    # exploration cooldown guards under strict safety/rate-limit constraints.
    escape_probe_enabled: bool = True
    escape_probe_require_idle: bool = True
    escape_probe_suppression_window_s: float = 7200.0
    escape_probe_suppression_min_count: int = 50
    escape_probe_bypassable_share_min: float = 0.60
    escape_probe_quality_confidence_max: float = 0.50
    escape_probe_posture_modifier_max: float = 0.70
    escape_probe_global_interval_s: float = 1800.0
    escape_probe_context_interval_s: float = 5400.0
    escape_probe_rf_cliff_sinr_min_db: float = 3.0
    escape_probe_rf_cliff_sinr_ticks: int = 2
    # Learning guardrails (STM/LTM) for band-action suppression.
    learning_enabled: bool = True
    learning_stm_window_s: float = 21600.0               # 6h short-term window
    learning_stm_min_attempts: int = 2
    learning_stm_bad_rate_threshold: float = 0.60
    learning_stm_hard_fail_block_count: int = 1
    learning_stm_hard_fail_block_s: float = 7200.0       # 2h after hard fail
    learning_ltm_min_attempts: int = 4
    learning_ltm_bad_rate_threshold: float = 0.70
    learning_ltm_decay: float = 0.990                    # ~23-day half-life at 3 actions/day (was 0.995 = ~46 days, too slow)
    learning_context_similarity_threshold: float = 0.92  # alias near-identical calibration contexts
    learning_context_compare_delay_s: float = 86400.0    # compare/merge provisional C* only after 24h
    learning_marker_warmup_attempts: int = 2             # after calibration marker switch, observe before advice
    # Re-open blocked combos when conditions clearly improve.
    learning_retry_min_elapsed_s: float = 1800.0         # minimum wait before re-try (30 min)
    learning_retry_sinr_improvement_db: float = 4.0      # required SINR gain vs last failed sample

    # ── Modem backend selection ──
    modem_profile_id: str = ""
    modem_backend: str = "b818"        # "mikrotik", "b818", "b535", "b530", "h112", "h122", "huawei", "openwrt", "mf296r", "mc888", "g5b", "zte_zwrt", "model"
    # Full startup acquisition wait, including mandatory physical request gaps.
    # Normal live reads use actionable_modem_read_timeout_s separately.
    modem_read_timeout_s: float = DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S
    # Consecutive stale-input ticks required before logging a warning event.
    # 0 disables the warning path entirely.
    input_freeze_warn_samples: int = 10
    # ARCH6 unified mode: Brain is the only action authority.
    arch6_unified_control: bool = False
    # ── Deployed bands (optional override) ──
    # If set, the brain uses this instead of the static DEPLOYED_BANDS.
    # B818 can auto-populate this from modem netmode if explicitly enabled.
    deployed_bands: Optional[List[int]] = None
    b818_autodetect_deployed_bands: bool = False

    # ── MikroTik connection (modem_backend="mikrotik") ──
    router_host: str = ""
    router_user: str = "admin"
    router_password: str = ""
    router_port: int = 8728                # RouterOS API port
    router_api_timeout_s: float = 30.0
    # MikroTik modem-read pacing: keep traffic reads fast, throttle modem-facing RF/AT reads.
    mikrotik_rf_monitor_interval_s: float = 3.0
    mikrotik_qcainfo_interval_s: float = 8.0
    mikrotik_qnwcfg_mcs_interval_s: float = 6.0
    mikrotik_qeng_mcs_interval_s: float = 15.0
    mikrotik_serving_cell_extra_interval_s: float = 10.0
    mikrotik_neighbor_scan_interval_s: float = 30.0
    mikrotik_wifi_status_interval_s: float = 5.0
    mikrotik_at_live_polling_enabled: bool = False
    mikrotik_runtime_capability_probe_enabled: bool = False
    mikrotik_at_read_delay_s: float = 1.0
    mikrotik_at_write_delay_s: float = 2.5
    mikrotik_at_bus_guard_s: float = 0.3
    # Safe pacing controls for ASR-based Chateau variants.
    mikrotik_action_min_session_uptime_s: float = 60.0
    mikrotik_post_write_blackout_s: float = 10.0
    mikrotik_at_latency_warn_ms: float = 1500.0
    mikrotik_adaptive_bus_guard: bool = True
    mikrotik_at_bus_guard_max_s: float = 0.8
    mikrotik_at_bus_guard_step_s: float = 0.1
    mikrotik_manual_verify_timeout_s: float = 45.0
    mikrotik_manual_verify_poll_s: float = 2.0
    mikrotik_manual_verify_min_session_s: float = 8.0
    mikrotik_radio_settle_quiet_s: float = 12.0
    post_action_throughput_observe_s: float = 6.0
    post_action_throughput_observe_poll_s: float = 2.0
    # Single in-flight mutating action lock (BAND/CELL/UE/RESTART).
    mutating_action_lock_s: float = 60.0
    # Runtime safety: suppress automatic actions briefly after transport/radio instability.
    unstable_action_lockout_s: float = 45.0
    # Planned quiet after direct BAND_SELECTION should be short and action-specific.
    band_selection_radio_quiet_s: float = 8.0

    # ── Signal normalization contract (backend-level unit mapping) ──
    # Some modem paths may expose fixed 10x encoded values (for example -120 meaning -12.0 dB).
    # Apply backend profile scales before signals enter ModemInput.
    signal_sinr_scale: float = 1.0
    signal_rsrp_scale: float = 1.0
    signal_rsrq_scale: float = 1.0
    signal_rssi_scale: float = 1.0
    signal_contract_warn: bool = True

    # ── Huawei HiLink connection ──
    # B818 remains the fallback HiLink profile for generic/legacy Huawei rows.
    # B535 HiLink profiles share this physical connection contract. Exact
    # profile settings remain scoped by profile_id in the NGUI settings store.
    b818_host: str = "192.168.8.1"         # Huawei HiLink standard default
    b818_user: str = "admin"               # HiLink web UI username
    b818_password: str = ""                # HiLink web UI password
    b818_ping_interface: str = ""           # OS NIC for RTT ping (empty=default route)
    b535_host: str = "192.168.8.1"
    b535_user: str = "admin"
    b535_password: str = ""
    b535_ping_interface: str = ""
    target_guard_model: str = ""            # optional expected model to prevent wrong-target actions
    target_guard_serial: str = ""           # optional expected serial to prevent wrong-target actions
    target_guard_firmware: str = ""         # optional fallback firmware match when serial is unavailable
    target_guard_imei: str = ""             # optional expected IMEI to prevent wrong-target actions
    network_country_suggestion: str = ""
    network_provider_suggestion: str = ""
    network_bands_suggestion: str = ""
    hilink_netmode_submit_timeout_s: float = 2.5
    hilink_netmode_continue_on_read_timeout_for_verification: bool = True
    hilink_sequential_step_wait_s: float = 20.0
    hilink_pband_wait_s: float = 5.0
    restart_expected_interruption_s: float = 75.0
    hilink_manual_verify_timeout_s: float = 12.0
    hilink_manual_verify_poll_s: float = 0.5
    hilink_manual_verify_min_session_s: float = 0.0

    # Exact TP-Link DEV2 profile; credentials use the existing settings owner.
    tplink_host: str = "192.168.1.1"
    tplink_user: str = "admin"
    tplink_password: str = ""
    tplink_ping_interface: str = ""

    # Conditional Xiaomi stock LuCI; existing settings owner retains entries.
    xiaomi_host: str = "192.168.31.1"
    xiaomi_user: str = "admin"
    xiaomi_password: str = ""
    xiaomi_ping_interface: str = ""

    # ── ZTE connection (modem_backend="mf296r" / "mc888" / "g5b" / "zte_zwrt") ──
    zte_host: str = "192.168.0.1"
    zte_user: str = "admin"
    zte_password: str = ""
    zte_ping_interface: str = ""
    zte_manual_verify_timeout_s: float = 12.0
    zte_manual_verify_poll_s: float = 0.5
    zte_manual_verify_min_session_s: float = 0.0

    # ── OpenWrt host / ModemManager connection (modem_backend="openwrt") ──
    openwrt_host: str = "192.168.8.1"
    openwrt_user: str = "root"
    openwrt_password: str = ""
    openwrt_modem_id: str = ""
    openwrt_ping_interface: str = ""

    # ── Universal modem model / emulator (modem_backend="model") ──
    # Empty name/path select the existing built-in synthetic model.
    model_profile_name: str = ""
    model_profile_path: str = ""

    # ── Probe targets ──
    rtt_probe_count: int = 3               # pings per RTT measurement
    actionable_rtt_probe_count: int = 1    # fast-tick RTT probe count
    actionable_modem_read_timeout_s: float = 2.5  # fast-tick modem read budget
    actionable_rtt_max_age_s: float = 2.5  # max age for cached RTT on fast ticks
    modem_live_register_enabled: bool = True  # 54GUC internal live-register boundary
    modem_live_register_interval_s: float = 2.0  # clocked register refresh cadence
    modem_live_register_stale_s: float = 6.0     # age before cached live register is unusable
    # Optional live access overrides. Leave None to use the modem family contract.
    # backend_io_parallel_safe: fast/slow modem reads may overlap.
    # io_serial_required: all modem reads must use one lane.
    # startup_parallel_allowed: startup identity/config probes may run together.
    # maintenance_holds_io_lock: maintenance refresh must block other modem I/O.
    modem_live_backend_io_parallel_safe: Optional[bool] = None
    modem_live_io_serial_required: Optional[bool] = None
    modem_live_startup_parallel_allowed: Optional[bool] = None
    modem_live_maintenance_holds_io_lock: Optional[bool] = None
    live_maintenance_interval_s: float = 10.0
    neighbor_scan_interval_s: float = 120.0

    # ── Axis 2: Red line detection ──

    # ── Axis 3: Historical priors ──
    hourly_pattern_min_samples: int = 10   # min samples to trust an hour's pattern
    preemptive_window_minutes: int = 20    # act this many mins before hour boundary
    # Degradation window: suppress speculative probing during hours historically
    # known to be bad on this cell. The brain holds LTM-best and waits it out.
    degradation_window_rtt_ms: float = 100.0           # avg RTT above this
    degradation_window_throughput_mbps: float = 5.0    # AND avg throughput below this
    degradation_window_severe_rtt_ms: float = 200.0    # OR RTT alone above this

    # ── Provider/modem policy: A1 B818 night training ──
    night_training_start_hour: int = 0     # local time inclusive
    night_training_end_hour: int = 6       # local time exclusive (hold until this hour)
    night_probe_dwell_s: float = 900.0     # 15 min per candidate
    night_probe_cooldown_s: float = 600.0  # min gap between band changes in night mode
    night_probe_recheck_s: float = 1800.0  # hold-mode recheck cadence
    night_probe_trigger_no_ca_s: float = 1800.0  # CA is bonus at night; no-CA alone triggers slowly
    night_probe_trigger_bad_rf_sinr_db: float = 5.0
    night_probe_trigger_bad_rsrp_dbm: float = -92.0
    # Retired A1+B818 forced midnight schedule settings.
    # Kept only for backward config compatibility; live policy no longer uses
    # the 00:00-01:00 forced sequence or hold-to-02:00 path.
    a1_forced_phase_start_hour: int = 0
    a1_forced_phase_end_hour: int = 1
    a1_forced_hold_end_hour: int = 2
    a1_forced_step_wait_s: float = 900.0
    a1_forced_post_quiet_s: float = 3600.0
    # A1+B818 last-resort fallback:
    # only after sustained B1/B3 attach failure / radio-down behavior, force
    # B20 and hold it for a while instead of treating B20 as a generally
    # preferred recovery target.
    a1_big_dip_b20_fallback_persist_s: float = 300.0
    a1_big_dip_step_wait_s: float = 300.0  # legacy compatibility only
    a1_big_dip_hold_s: float = 3600.0
    # A1 daytime CA-restore sequence during provider day window.
    a1_ca_restore_step_wait_s: float = 20.0
    a1_ca_restore_cooldown_s: float = 120.0
    a1_ca_restore_verify_s: float = 900.0
    a1_ca_restore_max_failures: int = 2
    # A1+B818 anti-thrash enforcement (active guardrails, not observe-only):
    # during restricted night window, keep only B3/B20 and limit retries.
    a1_night_restrict_start_hour: int = 1
    a1_night_restrict_end_hour: int = 6
    # Retired A1+B818 hard-freeze settings.
    # Kept only for backward config compatibility; live policy no longer uses
    # a global hard-freeze gate and keeps only restricted-night logic plus the
    # 06:01 morning sequence.
    a1_night_freeze_enabled: bool = False
    a1_night_freeze_start_hour: int = 2
    a1_night_freeze_end_hour: int = 8
    a1_night_antithrash_hold_s: float = 7200.0
    a1_night_antithrash_window_s: float = 600.0
    a1_night_antithrash_max_band_actions: int = 4
    a1_night_antithrash_strict_step_wait_s: float = 300.0
    a1_night_antithrash_strict_max_actions: int = 3
    # A1+B818 morning sequence moved under Brain in ARCH6 mode.
    a1_morning_sequence_hour: int = 6
    a1_morning_sequence_minute: int = 1
    a1_morning_step_wait_s: float = 20.0
    a1_morning_retry_delay_s: float = 1800.0
    a1_morning_bad_rf_threshold_s: float = 3600.0  # require 1 hour cumulative BAD_RF overnight to trigger morning sequence

    # ── ARCH6v1.2 policy templates ──
    policy_templates_enabled: bool = True
    policy_templates_path: str = "config/arch6_v1_2_policy_templates.json"
    policy_overlay_enabled: bool = True
    policy_overlay_path: str = "config/policy_overlay.json"

    # ── ARCH6v1.2 health-check framework ──
    health_checks_enabled: bool = True
    health_ping_rtt_max_ms: float = 800.0
    health_ping_fail_threshold: int = 3
    health_ping_recover_threshold: int = 1
    health_hold_down_s: float = 180.0
    # Multi-target WAN health checks (primary ping + async DNS + async HTTP quorum).
    health_multi_target_enabled: bool = True
    health_probe_interval_s: float = 30.0
    health_probe_timeout_s: float = 2.0
    health_multi_min_ok_targets: int = 2
    health_ping_targets: List[str] = field(
        default_factory=lambda: ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    )
    health_dns_targets: List[str] = field(default_factory=lambda: ["google.com"])
    health_http_targets: List[str] = field(
        default_factory=lambda: ["https://connectivitycheck.gstatic.com/generate_204"]
    )
    # Developer-only downstream LAN/router monitor.  This is status-only and
    # must never be used as RF, PBAND, or CA evidence.
    downstream_health_enabled: bool = False
    downstream_health_target: str = ""
    downstream_health_label: str = "Downstream router"
    downstream_health_ping_count: int = 1
    downstream_health_interval_s: float = 30.0
    downstream_health_timeout_s: float = 1.0
    downstream_health_rtt_max_ms: float = 1000.0
    downstream_health_fail_threshold: int = 3
    downstream_health_recover_threshold: int = 1
    # ── ARCH6v1.2 telemetry rollups ──
    telemetry_rollup_enabled: bool = True

    # ── ARCH6v2 calibration lane (advisory) ──
    # Compares expected (learned baseline) vs current signals and exports a
    # calibration gap score for brain/ops consumption. No direct actuation.
    calibration_enabled: bool = True
    calibration_min_samples: int = 30
    calibration_gap_threshold: float = 0.40
    calibration_gap_clear_threshold: float = 0.32
    calibration_score_window_ticks: int = 12
    calibration_set_confirm_ticks: int = 2
    calibration_clear_confirm_ticks: int = 3
    calibration_set_evidence_ratio: float = 0.55
    calibration_clear_evidence_ratio: float = 0.65
    calibration_volatility_margin_gain: float = 0.35
    calibration_volatility_margin_cap: float = 0.12
    calibration_ca_persistence_good_ratio: float = 0.60
    calibration_load_bias_rsrp_floor_dbm: float = -92.0
    calibration_load_bias_sinr_ceiling_db: float = 6.0
    calibration_load_bias_rsrq_ceiling_db: float = -11.0
    calibration_load_bias_throughput_gap_ratio: float = 0.35
    calibration_load_bias_rtt_gap_ratio: float = 0.35
    calibration_offpeak_start_hour: int = 0
    calibration_offpeak_end_hour: int = 6
    calibration_offpeak_confirm_background: bool = True
    calibration_baseline_drift_window_ticks: int = 24
    calibration_baseline_drift_sinr_gap_db: float = 2.0
    calibration_baseline_drift_rsrq_gap_db: float = 2.5
    calibration_baseline_drift_gap_score_min: float = 0.18
    calibration_baseline_drift_hold_ratio: float = 0.70
    calibration_baseline_drift_threshold_hint_gain: float = 0.5
    calibration_baseline_drift_threshold_hint_cap_db: float = 3.0
    calibration_checkpoint_interval_s: float = 300.0
    calibration_checkpoint_max_dirty_events: int = 100
    calibration_db_enabled: bool = True
    calibration_db_retention_days: int = 120


def validate_config(cfg: "Arch4Config") -> list:
    """Cross-field configuration validation.

    Call at startup to catch contradictory or dangerous config values before
    the first tick. Returns a list of error strings — empty means valid.

    Usage in engine.startup():
        from arch4.interfaces import validate_config
        for err in validate_config(self.cfg):
            log.warning("Config validation: %s", err)

    These are warnings, not hard failures — the engine continues even with
    invalid config. But the warnings are logged to the event journal so the
    operator can diagnose unexpected behaviour.
    """
    errors = []

    # RTT thresholds must be ordered: warning < cliff
    rtt_warning = float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0)
    rtt_cliff = float(getattr(cfg, "rtt_cliff_ms", 400.0) or 400.0)
    if rtt_warning >= rtt_cliff:
        errors.append(
            f"rtt_warning_ms ({rtt_warning:.0f}) must be less than "
            f"rtt_cliff_ms ({rtt_cliff:.0f})"
        )

    # SINR floor must be below a sensible ceiling
    sinr_floor_raw = getattr(cfg, "emergency_sinr_floor", None)
    sinr_floor = float(3.0 if sinr_floor_raw is None else sinr_floor_raw)
    if sinr_floor >= 15.0:
        errors.append(
            f"emergency_sinr_floor ({sinr_floor:.1f} dB) looks too high — "
            f"normal range is 0-8 dB; this would trigger emergency on good RF"
        )
    if sinr_floor < -5.0:
        errors.append(
            f"emergency_sinr_floor ({sinr_floor:.1f} dB) looks too low — "
            f"modem would stay in emergency state permanently"
        )

    # Night training window must be a valid range (start != end)
    nt_start = int(getattr(cfg, "night_training_start_hour", 0) or 0)
    nt_end = int(getattr(cfg, "night_training_end_hour", 6) or 6)
    if nt_start == nt_end:
        errors.append(
            f"night_training_start_hour ({nt_start}) and "
            f"night_training_end_hour ({nt_end}) must differ — "
            f"a zero-length window disables night training entirely"
        )
    if not (0 <= nt_start <= 23) or not (0 <= nt_end <= 23):
        errors.append(
            f"night_training hours must be 0-23 (got start={nt_start}, end={nt_end})"
        )

    # Calibration off-peak window must be a valid range
    op_start = int(getattr(cfg, "calibration_offpeak_start_hour", 0) or 0)
    op_end = int(getattr(cfg, "calibration_offpeak_end_hour", 6) or 6)
    if op_start == op_end:
        errors.append(
            f"calibration_offpeak_start_hour ({op_start}) and "
            f"calibration_offpeak_end_hour ({op_end}) must differ"
        )

    # Probe dwell must be positive and longer than cooldown makes sense
    dwell = float(getattr(cfg, "night_probe_dwell_s", 900.0) or 900.0)
    cooldown = float(getattr(cfg, "night_probe_cooldown_s", 600.0) or 600.0)
    if dwell < 60.0:
        errors.append(
            f"night_probe_dwell_s ({dwell:.0f}s) is very short — "
            f"minimum recommended is 60s to get a stable reading"
        )
    if cooldown < 30.0:
        errors.append(
            f"night_probe_cooldown_s ({cooldown:.0f}s) is very short — "
            f"minimum recommended is 30s between probes"
        )

    # Calibration min_samples must be at least 1
    min_samples = int(getattr(cfg, "calibration_min_samples", 30) or 30)
    if min_samples < 1:
        errors.append(
            f"calibration_min_samples ({min_samples}) must be at least 1"
        )

    # Modem backend must be a non-empty string
    backend = str(getattr(cfg, "modem_backend", "") or "")
    if not backend:
        errors.append(
            "modem_backend is empty — must be one of: b818, b535, b530, "
            "h112, h122, mikrotik, openwrt, mf296r, mc888, g5b, "
            "zte_zwrt, model"
        )

    return errors
