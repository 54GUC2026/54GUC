"""
arch4/modem/mikrotik.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — MikroTik Chateau LTE12 / EG120K-EA Backend

All RouterOS API calls, AT command parsing, timing quirks,
and hardware-specific workarounds live HERE and ONLY here.

Known quirks (from 14 days of debugging):
  - at-chat requires .id selector (not numeric index)
  - routeros_api lib decodes keys to str (not bytes)
  - AT command data arrives in !re sentences (not !done)
  - QCAINFO SINR is direct dB; QENG is 1/5 dB encoding
  - Read-only AT queries can use short settle delay; mutating AT writes need longer settle
  - 300ms bus guard between sequential AT commands
═══════════════════════════════════════════════════════════════
"""

from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.modem_live.startup_trace import traced_protocol
from arch4.core_contracts.traffic_counter_transition import counter_pair_increment

import re
import time
import copy
import logging
import threading
from typing import Optional, List, Dict, Any, Mapping

from arch4.interfaces import (
    ModemInput, LeverAction, LeverResult,
    CarrierInfo, NeighborCell, Arch4Config, ModemCapabilities,
    get_capabilities, normalize_band_list, parse_band_signature,
)
from arch4.config_views import (
    build_mikrotik_connection_config,
    build_mikrotik_runtime_config,
    build_signal_contract_config,
)
from arch4.modem.base import (
    AuthenticationError,
    LiveAcquisitionContract,
    ModemInterface,
    build_public_modem_status,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    matrix_cells,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.backend_acquisition_cells import (
    CONFIGURED_BAND_CELLS,
    DEVICE_RECOVERY_CELLS,
    IDENTITY_CELLS,
    SLOW_CELLS,
)
from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.operator_profiles import resolve_provider_name, resolve_provider_plmn_from_name, lookup_operator_context
from arch4.modem.signal_normalization import (
    assess_signal_contract,
    rate_limit_signal_contract_warning,
)
from arch4.modem.huawei_parsers import TRANSMODE_MAP
from arch4.lte_mcs import (
    resolve_lte_qm_with_retx_hold,
)
from arch4.modem.mikrotik_parsers import (
    _ros_bool,
    _classify_wifi_band,
    routeros_wifi,
    decode_routeros_row,
    parse_routeros_duration_seconds,
    parse_routeros_lte_bands,
    parse_routeros_lte_monitor_row,
    parse_routeros_nr_qeng,
    parse_qeng_servingcell,
    rb_to_mhz,
    routeros_antenna_status,
    routeros_monitor_ca_bands,
    routeros_monitor_active_bands,
)
from arch4.modem.lte_frequency import lte_frequency_mhz_from_dl_earfcn
from arch4.modem.lock_confirmation import parse_qnwlock_readback
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem.routeros_request_adapter import (
    RouterOsProtocolError,
    RouterOsRequestAdapter,
    RouterOsTransportError,
    is_routeros_authentication_fault,
    raise_typed_routeros_fault,
)
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CoreNetworkSettingsTarget,
    CoreNrBandTarget,
    CoreBandSetTarget,
    CoreCellLockTarget,
    CorePBandTarget,
    CoreRadioModeTarget,
    CoreRestartTarget,
    CoreUeCapabilityTarget,
    CoreWifiRadioTarget,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandCoordinationTarget,
    PhysicalCommandDispatchEvidence,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)

log = logging.getLogger("arch4.modem.mikrotik")

_MIKROTIK_MONITOR_FIELDS = frozenset(
    {
        "sinr_db",
        "rsrp_dbm",
        "rsrq_db",
        "rssi_dbm",
        "pci",
        "earfcn",
        "earfcn_dl",
        "cell_id",
        "enb_id",
        "band",
        "serving_band",
        "serving_band_signature",
        "cqi",
        "cqi0",
        "mcs_dl",
        "mcs_dl0",
        "mcs_ul",
        "qam_dl0",
        "qam_ul",
        "ri",
        "ri_valid",
        "ri_source",
        "radio_connected",
        "radio_state_text",
        "network_type",
        "session_uptime_s",
    }
)

# Additive reads on the four selected 5G profiles. Their existing LTE parser,
# capabilities, identity and physical-write trust are unchanged.
_MIKROTIK_NR_PROFILE_IDS = frozenset({
    "mikrotik_routeros_v7_rg502q", "mikrotik_routeros_v721_rg520f",
    "mikrotik_routeros_v721_rg650e", "mikrotik_routeros_r17_v7211_regression",
})
_MIKROTIK_NR_MONITOR_SCALARS = {
    "radio.nr5g_active": "nr5g_active", "radio.nr_mode": "nr_mode",
    "radio.nr_band": "nr_band", "radio.nr_pci": "nr_pci",
    "radio.nr_arfcn": "nr_arfcn", "radio.nr_rsrp_dbm": "nr_rsrp_dbm",
    "radio.nr_rsrq_db": "nr_rsrq_db", "radio.nr_sinr_db": "nr_sinr_db",
    "slow.nr_bandwidth_mhz": "nr_bandwidth_mhz", "slow.nr_cells": "nr_cells",
}

_MIKROTIK_CA_MONITOR_FIELDS = frozenset(
    {
        "carrier_details",
        "carrier_rows",
        "active_band_set",
        "active_band_set_signature",
        "band_set",
        "band_set_signature",
        "ca_active",
        "ca_state_text",
        "ca_component_band_set",
        "ca_component_band_signature",
        "ca_component_count",
        "ca_composition_signature",
        "ca_source",
    }
)

_MIKROTIK_FAST_TRAFFIC_FIELDS = frozenset(
    {
        "rx_bytes_per_sec",
        "tx_bytes_per_sec",
        "traffic_valid",
        "traffic_state_detail",
        "traffic_invalid_reason",
        "packet_session_connected",
        "connection_status_code",
        "session_uptime_s",
        "traffic_current_download_rate_bps",
        "traffic_current_upload_rate_bps",
        "traffic_rate_source",
    }
)

_MIKROTIK_TRAFFIC_COUNTER_CELLS = matrix_cells(
    "traffic",
    (
        "traffic_total_download_bytes",
        "traffic_total_upload_bytes",
        "traffic_session_download_bytes",
        "traffic_session_upload_bytes",
        "traffic_device_download_counter_bytes",
        "traffic_device_upload_counter_bytes",
        "traffic_counter_period_started_at",
        "traffic_counter_interface_started_at",
        "traffic_accounting_source",
        "traffic_accounting_status",
        "traffic_volume_poll_interval_s",
        "traffic_volume_last_sample_ts",
        "traffic_volume_next_sample_ts",
        "traffic_volume_age_s",
        "traffic_rate_source",
    ),
)

ROUTEROS_LTE_WATCHDOG_SCRIPT = "lte_watchdog"
ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT = "lte_watchdog_reset_state"
ROUTEROS_LTE_WATCHDOG_BOOT_GUARD_SCRIPT = "lte_watchdog_boot_guard"
ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS = (
    "lte_watchdog",
    "lte_watchdog_check",
    "lte_watchdog_recover",
    "lte_watchdog_reset_state",
)
ROUTEROS_LTE_WATCHDOG_KNOWN_SCRIPTS = (
    *ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS,
    ROUTEROS_LTE_WATCHDOG_BOOT_GUARD_SCRIPT,
)
ROUTEROS_LTE_WATCHDOG_ROUTINE_SCRIPTS = (
    "lte_watchdog",
    "lte_watchdog_check",
)
ROUTEROS_LTE_WATCHDOG_RECOVERY_SCRIPTS = (
    "lte_watchdog_recover",
)
ROUTEROS_LTE_WATCHDOG_ENVIRONMENT_NAMES = (
    "lteWatchdogFailCount",
    "lteWatchdogLastSuccessCount",
    "lteWatchdogLastSourceIp",
    "lteWatchdogLastTarget",
    "lteWatchdogLastStatus",
    "lteWatchdogLastError",
    "lteWatchdogLastRecoveryStatus",
    "lteWatchdogLastRecoveryError",
)
ROUTEROS_RECOVERY_STATUS_INTERVAL_S = 60.0
ROUTEROS_IDENTITY_RETRY_INTERVAL_S = 30.0
# RouterOS interface counters reset when the LTE interface is recreated.
# A cheap once-per-minute checkpoint bounds any unaccounted interval without
# adding AT traffic or changing the modem read/cooldown lane.
MIKROTIK_TRAFFIC_COUNTER_POLL_INTERVAL_S = 60.0

# Suppress noisy "Non-bytes value" warnings from routeros_api.
# These fire on every AT command because we pass str params to binary_resource.
# The lib handles it fine — it's just a cosmetic warning.
logging.getLogger("routeros_api.api_communicator.encoding_decorator").setLevel(logging.ERROR)


def _decode_row(row: dict) -> dict:
    """
    Decode a RouterOS binary_resource response row.

    get_binary_resource() returns bytes keys AND bytes values.
    get_resource() returns str keys and str values.
    This handles both cases uniformly.
    """
    return decode_routeros_row(row)




def _routeros_environment_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _routeros_script_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# ─────────────────────────────────────────────────────────────
# AT command response parsing
# ─────────────────────────────────────────────────────────────

def _parse_qcainfo(raw: str) -> List[CarrierInfo]:
    """
    Parse AT+QCAINFO response into CarrierInfo list.

    Actual EG120K-EA output (confirmed live):
      +QCAINFO: "PCC",250,50,"LTE BAND 1",1,303,-87,-11,-59,7
      +QCAINFO: "SCC",1500,100,"LTE BAND 3",1,303,-86,-12,-63,6
      +QCAINFO: "SCC",6200,50,"LTE BAND 20",1,303,-91,-20,-61,-5

    Fields: type, earfcn, bw_rb, "LTE BAND N", state(?), pci, rsrp, rsrq, rssi, sinr_db

    Note: SINR is direct dB (not 1/5 encoding like QENG).
    """
    carriers = []
    for line in raw.splitlines():
        line = line.strip()
        if "+QCAINFO:" not in line:
            continue
        # Remove prefix
        payload = line.split("+QCAINFO:")[1].strip()
        # Remove quotes
        payload = payload.replace('"', '')
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) < 8:
            continue
        try:
            is_pcc = parts[0].upper() == "PCC"
            earfcn = int(parts[1])
            bw_code = int(parts[2])

            # Band field: either bare number "3" or "LTE BAND 3"
            band_str = parts[3]
            if "BAND" in band_str.upper():
                band = int(band_str.upper().replace("LTE", "").replace("BAND", "").strip())
            else:
                band = int(band_str)

            # Remaining fields depend on format length
            # 10-field format (EG120K-EA): type,earfcn,bw,"LTE BAND N",?,pci,rsrp,rsrq,rssi,sinr
            # 8-field format (other):      type,earfcn,bw,band,rsrp,rsrq,sinr,pci[,state]
            if len(parts) >= 10:
                # EG120K-EA format
                state_or_flag = int(parts[4])
                pci = int(parts[5])
                rsrp = float(parts[6])
                _rsrq = float(parts[7])
                # parts[8] = RSSI, we skip
                sinr = float(parts[9])  # direct dB
                state = state_or_flag if not is_pcc else 0
            else:
                # Compact format
                rsrp = float(parts[4])
                _rsrq = float(parts[5])
                sinr = float(parts[6])
                pci = int(parts[7])
                state = int(parts[8]) if len(parts) > 8 else (0 if is_pcc else 1)

            bw_map = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}
            bw_mhz = bw_map.get(bw_code, bw_code / 5.0)

            carriers.append(CarrierInfo(
                band=band, earfcn=earfcn, pci=pci,
                bandwidth_mhz=bw_mhz, sinr_db=sinr,
                rsrp_dbm=rsrp, state=state if not is_pcc else 0,
                is_pcell=is_pcc,
            ))
        except (ValueError, IndexError) as e:
            log.warning("QCAINFO parse error: %s in line: %s", e, line)
    return carriers


def _earfcn_to_band(earfcn: int) -> int:
    """Map EARFCN to band number (common EU bands)."""
    if 0 <= earfcn <= 599:
        return 1
    elif 1200 <= earfcn <= 1949:
        return 3
    elif 2750 <= earfcn <= 3449:
        return 7
    elif 6150 <= earfcn <= 6449:
        return 20
    elif 1950 <= earfcn <= 2399:
        return 4
    elif 2400 <= earfcn <= 2649:
        return 5
    elif 2649 <= earfcn <= 2749:
        return 5
    elif 6450 <= earfcn <= 6599:
        return 21
    elif 9210 <= earfcn <= 9659:
        return 28
    return 0  # unknown


def _rb_to_mhz(rb: int) -> float:
    """Convert LTE resource block count to bandwidth in MHz.

    QENG servingcell reports DL/UL bandwidth as an RB index (0–5)
    that maps to a standard 3GPP channel bandwidth.  The EG120K-EA
    firmware reports the *encoded* index, not the raw RB count, so
    we accept both the index (0-5) and the actual RB count (6-100).
    """
    return rb_to_mhz(rb)


def _build_ca_signature(carriers: List[CarrierInfo]) -> str:
    """Build canonical CA signature like 'B1+B3+B20'."""
    bands = sorted(set(c.band for c in carriers if c.state >= 1 or c.is_pcell))
    return "+".join(f"B{b}" for b in bands) if bands else ""


def _parse_routeros_lte_bands(value: Any) -> List[int]:
    """Parse RouterOS LTE band text without accepting unrelated plain numbers."""
    return parse_routeros_lte_bands(value)


def _routeros_monitor_active_bands(row: Dict[str, Any], primary_band: int = 0) -> List[int]:
    """Extract active bands from RouterOS monitor fields when the OS exposes them."""
    return routeros_monitor_active_bands(row, primary_band)


def _routeros_monitor_ca_bands(row: Dict[str, Any]) -> List[int]:
    """Extract explicit CA/SCC bands from RouterOS monitor fields."""
    return routeros_monitor_ca_bands(row)


# ─────────────────────────────────────────────────────────────
# MikroTik Backend Implementation
# ─────────────────────────────────────────────────────────────

class MikroTikModem(ModemInterface):
    """
    ModemInterface implementation for MikroTik Chateau LTE12
    with Quectel EG120K-EA modem on RouterOS 7.21.
    """

    physical_request_authority_required = True

    _AT_IDENTITY_COMMANDS = {
        "identity_model": ("modem_model", "ATI"),
        "identity_imei": ("imei", "AT+CGSN"),
        "identity_firmware": ("firmware", "AT+QGMR"),
    }
    _AUX_REFRESH_ORDER = (
        "qcainfo",
        "qnwcfg",
        "serving",
        "qeng",
        "identity_model",
        "identity_imei",
        "identity_firmware",
    )
    _ROUTEROS_BATCH_ORDER = (
        "routeros_identity",
        "lte_state",
        "wifi",
    )
    _AUTH_FAILURE_RETRY_BLOCK_S = 300.0

    def __init__(self, config: Arch4Config):
        self.cfg = config
        self.conn_cfg = build_mikrotik_connection_config(config)
        self.runtime_cfg = build_mikrotik_runtime_config(config)
        self.signal_cfg = build_signal_contract_config(config)
        from arch4.modem_live.profile_catalog import modem_profile_definition
        self._definition = modem_profile_definition(
            str(config.modem_profile_id or "").strip()
            or "mikrotik_routeros_at_baseline"
        )
        if self._definition.connection['port'] is not None:
            from dataclasses import replace
            self.conn_cfg = replace(self.conn_cfg, port=self._definition.connection['port'])
        self._access_profile = modem_live_access_profile_for(
            backend="mikrotik",
            backend_family="routeros_at_like",
            profile_id=self._definition.profile_id,
        )
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(
            self._access_profile
        )
        self._pool = None
        self._routeros: Optional[RouterOsRequestAdapter] = None
        self._auth_failure_until_ts: float = 0.0
        self._auth_failure_fingerprint: tuple = ()
        self._auth_failure_reason: str = ""
        self._lte_id: Optional[str] = None  # .id of lte1 interface
        self._last_at_time: float = 0.0      # for bus guard timing
        self._at_lock = threading.RLock()
        self._lte_monitor_lock = threading.RLock()
        self._static_identity_lock = threading.RLock()
        self._last_traffic_rx: int = 0
        self._last_traffic_tx: int = 0
        self._last_traffic_time: float = 0.0
        self._traffic_volume_cache: Dict[str, Any] = {}
        self._traffic_volume_ts: float = 0.0
        self._traffic_counter_last_download_bytes: Optional[int] = None
        self._traffic_counter_accepted_at: float = 0.0
        self._traffic_counter_last_upload_bytes: Optional[int] = None
        self._traffic_session_download_bytes: int = 0
        self._traffic_session_upload_bytes: int = 0
        self._last_status: Dict[str, Any] = {}
        self._device_info_cache: Dict[str, str] = {}
        self._device_info_ts: float = 0.0
        self._routeros_identity_attempts: int = 0
        self._routeros_optional_identity_pending = {"network", "antenna"}
        self._routeros_identity_last_attempt_ts: float = 0.0
        self._at_identity_ts: Dict[str, float] = {}
        self._at_identity_attempts: Dict[str, int] = {}
        self._plmn_cache: str = ""
        self._plmn_cache_ts: float = 0.0
        self._provider_name_cache: str = ""
        self._subscription_acquired_fields: frozenset[str] = frozenset()
        self._subscription_observed_at: float = 0.0
        self._last_lte_monitor: Optional[Dict[str, Any]] = None
        self._last_lte_monitor_ts: float = 0.0
        self._last_lte_monitor_failure_ts: float = 0.0
        self._last_lte_monitor_failure_reason: str = ""
        self._last_lte_monitor_refresh_fingerprint: str = ""
        self._last_lte_monitor_refresh_seen_ts: float = 0.0
        self._last_lte_monitor_stale_warn_ts: float = 0.0
        self._last_lte_monitor_invalid_warn_ts: float = 0.0
        self._last_qm_dl0: int = 0
        self._last_qm_dl1: int = 0
        self._last_qm_ul: int = 0
        # AT-heavy reads are cached to keep per-sample latency low and keep
        # GUI throughput updates responsive during short bursts/speedtests.
        # The selected5G QCA snapshot can additionally hold one native UL
        # quantity/context under this same existing owner and timestamp.
        self._last_qcainfo: List[CarrierInfo] | Dict[str, Any] = []
        self._last_qcainfo_ts: float = 0.0
        self._last_qnwcfg_mcs: Optional[Dict[str, int]] = None
        self._last_qnwcfg_mcs_ts: float = 0.0
        self._last_qeng_mcs: Optional[Dict[str, int]] = None
        self._last_qeng_mcs_ts: float = 0.0
        self._last_lte_disabled: bool = False
        self._last_lte_disabled_ts: float = 0.0
        self._last_lte_running: Optional[bool] = None
        self._lte_disabled_refresh_s: float = 1.5
        self._last_lte_enable_invariant: Dict[str, Any] = {}
        self._last_lte_enable_invariant_fingerprint: tuple[Any, ...] = ()
        self._last_configured_bands: List[int] = []
        self._last_configured_bands_ts: float = 0.0
        self._last_configured_bands_status: str = "not_sampled"
        self._last_configured_bands_error: str = ""
        self._configured_bands_refresh_s: float = 10.0
        self._lte_monitor_interval_s: float = float(self.runtime_cfg.rf_monitor_interval_s)
        self._qcainfo_interval_s: float = float(self.runtime_cfg.qcainfo_interval_s)
        self._qeng_mcs_interval_s: float = float(self.runtime_cfg.qeng_mcs_interval_s)
        self._qnwcfg_mcs_interval_s: float = float(self.runtime_cfg.qnwcfg_mcs_interval_s)
        self._at_read_delay_s: float = float(self.runtime_cfg.at_read_delay_s)
        self._at_write_delay_s: float = float(self.runtime_cfg.at_write_delay_s)
        self._at_bus_guard_s: float = float(self.runtime_cfg.at_bus_guard_s)
        self._at_bus_guard_base_s: float = float(self._at_bus_guard_s)
        self._at_bus_guard_max_s: float = float(self.runtime_cfg.at_bus_guard_max_s)
        if self._at_bus_guard_max_s < self._at_bus_guard_base_s:
            self._at_bus_guard_max_s = self._at_bus_guard_base_s
        self._at_bus_guard_step_s: float = float(self.runtime_cfg.at_bus_guard_step_s)
        self._adaptive_bus_guard: bool = bool(self.runtime_cfg.adaptive_bus_guard)
        self._at_latency_warn_ms: float = float(self.runtime_cfg.at_latency_warn_ms)
        self._post_write_blackout_s: float = float(self.runtime_cfg.post_write_blackout_s)
        self._action_min_session_uptime_s: float = float(self.runtime_cfg.action_min_session_uptime_s)
        self._at_blackout_until: float = 0.0
        self._last_at_latency_ms: float = 0.0
        self._last_transport_failure_ts: float = 0.0
        self._last_transport_failure_reason: str = ""
        self._last_watchdog_script_status: Dict[str, Any] = {}
        self._last_watchdog_script_status_ts: float = 0.0
        self._routeros_recovery_topology_cache: Dict[str, Any] = {}
        self._routeros_recovery_topology_ts: float = 0.0
        self._routeros_recovery_topology_attempts: int = 0
        self._last_watchdog_script_run_ts: float = 0.0
        self._last_watchdog_script_error: str = ""
        self._last_device_recovery_fingerprint: Dict[str, Any] = {}
        self._last_device_recovery_diagnostic: Dict[str, Any] = {}
        self._device_recovery_diagnostic_sequence: int = 0
        self._routeros_watchdog_pause_state: Optional[Dict[str, Any]] = None
        self._last_qnwlock_guard_status: Dict[str, Any] = {}
        self._last_qnwlock_guard_audit_ts: float = 0.0
        self._last_qnwlock_readback_check_ts: float = 0.0
        self._qnwlock_renewing: bool = False
        # Serving-cell engineering data (QENG servingcell) for dashboard:
        # DL/UL bandwidth, TX power.  Refreshed on a slower cadence (~10s)
        # since these values change rarely (only on band/cell change).
        self._last_serving_cell_extra: Dict[str, Any] = {}
        self._last_serving_cell_extra_ts: float = 0.0
        self._serving_cell_extra_interval_s: float = float(self.runtime_cfg.serving_cell_extra_interval_s)
        self._last_wifi_status: tuple[Optional[bool], Optional[bool], Optional[bool]] = (None, None, None)
        self._last_wifi_status_ts: float = 0.0
        self._wifi_status_interval_s: float = float(self.runtime_cfg.wifi_status_interval_s)
        self._trans_mode_cache: str = ""
        self._trans_mode_source: str = ""
        self._runtime_capabilities: Optional[ModemCapabilities] = None
        self._runtime_capabilities_ts: float = 0.0
        self._runtime_capability_probe_interval_s: float = float(self.runtime_cfg.capability_probe_interval_s)
        self._at_live_polling_enabled: bool = bool(self.runtime_cfg.at_live_polling_enabled)
        self._runtime_capability_probe_enabled: bool = bool(
            self.runtime_cfg.runtime_capability_probe_enabled
        )
        self._signal_contract_seq: int = 0
        self._signal_contract_warning_state: Dict[tuple, Dict[str, Any]] = {}
        self._signal_scales = {
            "sinr": float(self.signal_cfg.sinr_scale),
            "rsrp": float(self.signal_cfg.rsrp_scale),
            "rsrq": float(self.signal_cfg.rsrq_scale),
            "rssi": float(self.signal_cfg.rssi_scale),
        }
        # Wi-Fi state — probed once on connect, then refreshed each sample.
        # RouterOS exposes either /interface/wifi (WiFiWave2, ROS 7.13+)
        # or /interface/wireless (legacy CAPsMAN/wireless package).
        self._wifi_api_path: Optional[str] = None   # set by _probe_wifi_api
        self._wifi_probed: bool = False
        self._aux_refresh_rr: int = 0

    def _record_signal_contract_event(self, event_type: str, assess: Dict[str, Any]) -> None:
        self._signal_contract_seq += 1
        payload = {
            "seq": int(self._signal_contract_seq),
            "event_type": str(event_type),
            "metric": str(assess.get("metric", "")),
            "raw": assess.get("raw"),
            "scale": float(assess.get("scale", 1.0) or 1.0),
            "norm": float(assess.get("norm", 0.0) or 0.0),
            "ok": bool(assess.get("ok", False)),
            "alt_scale": float(assess.get("alt_scale", 1.0) or 1.0),
            "alt_norm": float(assess.get("alt_norm", 0.0) or 0.0),
            "alt_ok": bool(assess.get("alt_ok", False)),
            "ambiguous": bool(assess.get("ambiguous", False)),
        }
        self._last_status["signal_contract_last"] = payload

    def _norm_signal(self, metric: str, raw_value: float, *, scale: float | None = None) -> float:
        if scale is None:
            scale = self._signal_scales.get(metric, 1.0)
        assess = assess_signal_contract(metric, raw_value, scale)
        norm = float(assess["norm"])
        ok = bool(assess["ok"])

        # Some RouterOS/AT paths can expose x10 encoded values (e.g. -140 => -14 dB).
        # If configured scale does not fit contract, attempt one-step x10 correction.
        if bool(assess.get("ambiguous", False)):
            self._record_signal_contract_event("ambiguous", assess)
            if bool(self.signal_cfg.contract_warn):
                decision = rate_limit_signal_contract_warning(
                    self._signal_contract_warning_state,
                    "ambiguous",
                    assess,
                )
                if bool(decision.get("emit", False)):
                    log.warning(
                        "signal contract ambiguous backend=mikrotik metric=%s raw=%s scale=%.4f norm=%.4f alt_scale=%.4f alt_norm=%.4f suppressed=%d",
                        metric,
                        raw_value,
                        float(assess["scale"]),
                        float(assess["norm"]),
                        float(assess["alt_scale"]),
                        float(assess["alt_norm"]),
                        int(decision.get("suppressed", 0) or 0),
                    )

        if not ok:
            auto_norm = float(assess.get("alt_norm", 0.0) or 0.0)
            auto_ok = bool(assess.get("alt_ok", False))
            if auto_ok:
                assess_auto = dict(assess)
                assess_auto["norm"] = auto_norm
                assess_auto["ok"] = True
                self._record_signal_contract_event("auto_corrected", assess_auto)
                log.warning(
                    "signal contract auto-correct backend=mikrotik metric=%s raw=%s scale=%.4f -> auto_scale=%.4f norm=%.4f",
                    metric,
                    raw_value,
                    float(assess["scale"]),
                    float(assess["alt_scale"]),
                    auto_norm,
                )
                return auto_norm

        if bool(self.signal_cfg.contract_warn) and not ok:
            lo, hi = float(assess["lo"]), float(assess["hi"])
            self._record_signal_contract_event("out_of_contract", assess)
            decision = rate_limit_signal_contract_warning(
                self._signal_contract_warning_state,
                "out_of_contract",
                assess,
            )
            if bool(decision.get("emit", False)):
                log.warning(
                    "signal contract warning backend=mikrotik metric=%s raw=%s scale=%.4f norm=%.4f expected=[%.1f, %.1f] suppressed=%d",
                    metric,
                    raw_value,
                    float(assess["scale"]),
                    norm,
                    lo,
                    hi,
                    int(decision.get("suppressed", 0) or 0),
                )
        return norm

    # ─── Connection lifecycle ───────────────────────────────

    def physical_request_lane_scope(self, lane: str):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self) -> Dict[str, Any]:
        return self._physical_request_authority.snapshot()

    def _install_routeros_api(self, raw_api: Any) -> RouterOsRequestAdapter:
        adapter = RouterOsRequestAdapter(
            raw_api,
            self._physical_request_authority,
            disconnect_fn=(
                getattr(self, "_pool", None).disconnect
                if getattr(self, "_pool", None) is not None
                else None
            ),
        )
        self._routeros = adapter
        return adapter

    def _run_lte1_enabled_invariant(
        self,
        routeros: RouterOsRequestAdapter,
        *,
        expected_lte_id: str,
        initial_rows: Optional[List[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Prove or repair the exact lte1 through one canonical transaction."""

        now = time.time()
        result: Dict[str, Any] = {
            "owner": "54guc.modem_live",
            "source": "routeros_api",
            "observed_at": now,
            "ok": False,
            "state": "unavailable",
            "reason": "lte1_row_unavailable",
            "disabled": None,
            "repaired": False,
            "active_jobs": [],
        }

        def exact_row(rows: Any) -> Optional[Dict[str, Any]]:
            matches = self._routeros_lte_interface_matches(rows)
            result["present_count"] = len(matches)
            if len(matches) != 1:
                result.update(
                    {
                        "state": "failed",
                        "reason": (
                            "lte1_row_unavailable"
                            if not matches
                            else "lte1_row_ambiguous"
                        ),
                    }
                )
                return None
            row = dict(matches[0])
            row_id = self._routeros_row_id(row)
            if not row_id or row_id != str(expected_lte_id or "").strip():
                result.update(
                    {
                        "state": "failed",
                        "reason": "lte1_identity_changed",
                        "row_id_present": bool(row_id),
                    }
                )
                return None
            return row

        try:
            rows = (
                list(initial_rows)
                if initial_rows is not None
                else routeros.get_resource("/interface/lte").get()
            )
        except (RouterOsTransportError, RouterOsProtocolError) as exc:
            result["read_error"] = self._routeros_read_error("lte1", exc)
            return result
        row = exact_row(rows)
        if row is None:
            return result
        disabled = self._routeros_optional_bool(row.get("disabled"))
        if disabled is None:
            result.update(
                {
                    "state": "failed",
                    "reason": "lte1_disabled_state_missing",
                }
            )
            return result
        result["disabled"] = bool(disabled)
        if not disabled:
            result.update(
                {
                    "ok": True,
                    "state": "enabled",
                    "reason": "readback_enabled",
                    "row": row,
                }
            )
            return result

        # All uncommon repair traffic has recovery priority. Re-read first:
        # RouterOS may already have repaired lte1. If it is still disabled,
        # defer only to the exact recovery/boot-guard jobs that own enabling.
        with self._physical_request_authority.lane_scope("recovery"):
            try:
                current_rows = routeros.get_resource("/interface/lte").get()
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                result.update(
                    {
                        "state": "failed",
                        "reason": "lte1_enable_prewrite_readback_unavailable",
                        "read_error": self._routeros_read_error(
                            "lte1_prewrite", exc
                        ),
                    }
                )
                return result
            current = exact_row(current_rows)
            if current is None:
                result["reason"] = "lte1_enable_prewrite_readback_unavailable"
                return result
            current_disabled = self._routeros_optional_bool(current.get("disabled"))
            if current_disabled is False:
                result.update(
                    {
                        "ok": True,
                        "state": "enabled",
                        "reason": "routeros_recovered_before_write",
                        "disabled": False,
                        "row": current,
                    }
                )
                return result
            if current_disabled is None:
                result.update(
                    {
                        "state": "failed",
                        "reason": "lte1_disabled_state_missing",
                    }
                )
                return result

            try:
                jobs = routeros.get_resource("/system/script/job").get()
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                result.update(
                    {
                        "state": "deferred",
                        "reason": "routeros_lte_enable_owner_state_unavailable",
                        "job_read_errors": [
                            self._routeros_read_error("script_jobs", exc)
                        ],
                    }
                )
                return result
            roles = self._routeros_watchdog_job_roles(jobs)
            active_local_owner = sorted(
                {*roles["recovery"], *roles["boot_guard"]}
            )
            result["active_jobs"] = active_local_owner
            if active_local_owner:
                result.update(
                    {
                        "state": "deferred",
                        "reason": "routeros_lte_enable_owner_active",
                    }
                )
                return result

            # An external RouterOS job could start after this observation, but
            # both owners would perform the same idempotent enable. A second
            # job poll cannot close that external race and only adds traffic.
            try:
                routeros.get_resource("/interface/lte").set(
                    id=str(expected_lte_id),
                    disabled="no",
                )
                readback_rows = routeros.get_resource("/interface/lte").get()
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                result.update(
                    {
                        "state": "failed",
                        "reason": "lte1_enable_write_or_readback_failed",
                        "read_error": self._routeros_read_error(
                            "lte1_enable", exc
                        ),
                    }
                )
                return result

        readback = exact_row(readback_rows)
        if readback is None:
            result["reason"] = "lte1_enable_readback_unavailable"
            return result
        readback_disabled = self._routeros_optional_bool(readback.get("disabled"))
        if readback_disabled is not False:
            result.update(
                {
                    "state": "failed",
                    "reason": "lte1_enable_not_confirmed",
                    "disabled": readback_disabled,
                }
            )
            return result
        result.update(
            {
                "ok": True,
                "state": "repaired",
                "reason": "lte1_enabled_and_verified",
                "disabled": False,
                "repaired": True,
                "row": readback,
            }
        )
        return result

    def _auth_credential_fingerprint(self) -> tuple:
        return (
            str(getattr(self.conn_cfg, "host", "") or ""),
            int(getattr(self.conn_cfg, "port", 0) or 0),
            str(getattr(self.conn_cfg, "user", "") or ""),
            str(getattr(self.conn_cfg, "password", "") or ""),
        )

    def _clear_auth_failure_latch(self) -> None:
        self._auth_failure_until_ts = 0.0
        self._auth_failure_fingerprint = ()
        self._auth_failure_reason = ""

    def _raise_if_auth_failure_latched(self, now: float) -> None:
        fingerprint = self._auth_credential_fingerprint()
        previous_fingerprint = tuple(getattr(self, "_auth_failure_fingerprint", ()) or ())
        if previous_fingerprint and previous_fingerprint != fingerprint:
            self._clear_auth_failure_latch()
            return
        until_ts = float(getattr(self, "_auth_failure_until_ts", 0.0) or 0.0)
        if until_ts <= now:
            return
        remaining_s = max(1, int(round(until_ts - now)))
        reason = str(getattr(self, "_auth_failure_reason", "") or "previous login rejection")
        raise AuthenticationError(
            f"MikroTik API authentication blocked for {remaining_s}s after {reason}; "
            "check username/password or restart after correcting credentials"
        )

    def _record_auth_failure_latch(self, reason: str, now: float) -> None:
        self._auth_failure_fingerprint = self._auth_credential_fingerprint()
        self._auth_failure_until_ts = now + self._AUTH_FAILURE_RETRY_BLOCK_S
        self._auth_failure_reason = reason

    @staticmethod
    def _is_authentication_failure(exc: Exception) -> bool:
        return is_routeros_authentication_fault(exc)

    def _discard_candidate_session(self, pool: Any, candidate: Any) -> None:
        """Best-effort cleanup only; this never classifies the failed request."""

        if pool is None:
            return
        try:
            if candidate is not None:
                candidate.disconnect()
            else:
                with self._physical_request_authority.request(
                    lane="disconnect",
                    operation="routeros:session:disconnect_candidate",
                ):
                    traced_protocol(pool.disconnect, operation="session:disconnect_candidate")
        except Exception as exc:
            log.debug("RouterOS candidate cleanup failed: %s", exc)

    def bind_startup_device_admission(self, admit) -> None:
        """Explicit RouterOS discovery adapter; Modem Live owns the decision."""
        if not callable(admit):
            raise TypeError('routeros_startup_device_admission_callable_required')
        self._startup_device_admission=admit

    def connect(self) -> bool:
        # Timeouts happen intermittently on busy routers/API service.
        # Retry with a longer socket timeout before declaring failure.
        self._raise_if_auth_failure_latched(time.time())
        timeout_s = float(self.conn_cfg.api_timeout_s)
        retries = int(self._access_profile.session_connect_attempt_limit)
        retry_sleep_s = float(
            self._access_profile.session_connect_retry_gap_s
        )
        try:
            import routeros_api
        except ImportError as exc:
            msg = (
                "MikroTik backend requires the routeros_api package, but it is "
                "missing from this runtime/build."
            )
            log.error(msg)
            raise ConnectionError(msg) from exc
        try:
            last_err = None
            self._lte_id = None
            for attempt in range(1, retries + 1):
                pool = None
                candidate = None
                try:
                    with self._physical_request_authority.request(
                        lane="session",
                        operation="routeros:session:connect",
                    ):
                        pool = routeros_api.RouterOsApiPool(
                            self.conn_cfg.host,
                            username=self.conn_cfg.user,
                            password=self.conn_cfg.password,
                            port=self.conn_cfg.port,
                            plaintext_login=True,
                        )
                        pool.set_timeout(timeout_s)
                        raw_api = traced_protocol(pool.get_api, operation="session:connect")
                    candidate = RouterOsRequestAdapter(
                        raw_api,
                        self._physical_request_authority,
                        disconnect_fn=pool.disconnect,
                    )

                    # Resolve exactly one lte1.  A first-row guess could enable
                    # or control the wrong LTE interface on a changed router.
                    with self._physical_request_authority.lane_scope("startup"):
                        lte_list = candidate.get_resource("/interface/lte").get()
                    lte_matches = self._routeros_lte_interface_matches(lte_list)
                    if len(lte_matches) != 1:
                        raise RuntimeError(
                            "routeros_exactly_one_lte1_required:"
                            f"found_{len(lte_matches)}"
                        )
                    candidate_lte_id = self._routeros_row_id(lte_matches[0])
                    if not candidate_lte_id:
                        raise RuntimeError("routeros_lte1_identity_missing")

                    # Read management identity before any LTE repair. A shared
                    # RouterOS password does not qualify the selected hardware.
                    admit=getattr(self,'_startup_device_admission',None)
                    if not callable(admit):
                        from arch4.modem.base import TargetGuardError
                        raise TargetGuardError('RouterOS startup requires device admission before LTE repair.',
                            reason='routeros_startup_device_admission_required')
                    with self._physical_request_authority.lane_scope('startup'):
                        resources=candidate.get_resource('/system/resource').get()
                        boards=candidate.get_resource('/system/routerboard').get()
                    if len(resources)!=1 or len(boards)!=1:
                        raise RuntimeError('routeros_single_native_identity_row_required')
                    resource=resources[0]; board=boards[0]
                    native={'backend':'mikrotik','vendor':'MikroTik',
                        'model':str(board.get('model') or board.get('board-name') or resource.get('board-name') or ''),
                        'serial':str(board.get('serial-number') or ''),
                        'os_name':'RouterOS','os_version':str(resource.get('version') or ''),
                        'software_version':str(resource.get('version') or ''),
                        'hardware_version':str(resource.get('architecture-name') or ''),
                    }
                    admit(native)

                    # RouterOS can remain reachable while its LTE interface is
                    # disabled. Prove and repair the candidate before making
                    # the new session visible to any other backend method.
                    invariant = self._run_lte1_enabled_invariant(
                        candidate,
                        expected_lte_id=candidate_lte_id,
                        initial_rows=lte_list,
                    )
                    if not bool(invariant.get("ok", False)):
                        raise RuntimeError(
                            str(invariant.get("reason") or "lte1_enable_invariant_failed")
                        )
                    final_lte_row = dict(invariant.get("row") or {})
                    self._routeros = candidate
                    self._pool = pool
                    self._lte_id = candidate_lte_id
                    self._device_info_cache={**native,'router_model':native['model']}
                    self._device_info_ts=time.time()
                    self._invalidate_routeros_recovery_observation(topology=True)
                    self._update_routeros_lte_state_from_row(final_lte_row)

                    log.info(
                        "Connected to %s, lte1 id=%s (attempt %d/%d, timeout %.1fs)",
                        self.conn_cfg.host,
                        self._lte_id,
                        attempt,
                        retries,
                        timeout_s,
                    )
                    self._clear_auth_failure_latch()
                    return True
                except Exception as e:
                    from arch4.modem.base import TargetGuardError
                    if isinstance(e,TargetGuardError):
                        self._routeros=None;self._lte_id=None;self._pool=None
                        self._discard_candidate_session(pool,candidate)
                        raise
                    # Detect authentication failures — no point retrying
                    if self._is_authentication_failure(e):
                        log.error("Authentication failed for MikroTik at %s: %s", self.conn_cfg.host, e)
                        self._routeros = None
                        self._lte_id = None
                        self._pool = None
                        self._discard_candidate_session(pool, candidate)
                        self._record_auth_failure_latch(str(e), time.time())
                        raise AuthenticationError(
                            f"Authentication failed for MikroTik at {self.conn_cfg.host} — check username/password"
                        ) from e
                    self._routeros = None
                    self._lte_id = None
                    self._pool = None
                    self._discard_candidate_session(pool, candidate)
                    try:
                        raise_typed_routeros_fault(
                            e,
                            operation="session:connect",
                        )
                    except RouterOsTransportError as transport_exc:
                        last_err = transport_exc
                    if attempt < retries:
                        log.warning(
                            "Connection attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt,
                            retries,
                            last_err,
                            retry_sleep_s,
                        )
                        time.sleep(retry_sleep_s)
            log.error(
                "Connection failed after %d attempt(s), timeout %.1fs: %s",
                retries,
                timeout_s,
                last_err,
            )
            raise ConnectionError(
                f"Cannot connect to MikroTik RouterOS API at {self.conn_cfg.host}:{self.conn_cfg.port} "
                f"after {retries} attempt(s): {last_err}"
            )
        except AuthenticationError:
            raise
        except ConnectionError:
            raise

    def disconnect(self) -> None:
        if self._routeros:
            try:
                self._routeros.disconnect()
            except Exception as exc:
                log.debug("disconnect: suppressed %s", exc)
        self._pool = None
        self._routeros = None
        self._lte_id = None
        self._invalidate_routeros_recovery_observation(topology=True)

    def is_connected(self) -> bool:
        if not self._routeros:
            return False
        try:
            self._routeros.get_resource(self._portable_routeros_path('/system/resource','/system/resource')).get()
            return True
        except (RouterOsTransportError, RouterOsProtocolError) as exc:
            if isinstance(exc, RouterOsTransportError):
                self._mark_transport_failure("session_probe", exc)
                return False  # A closed API transport needs a new API session.
            raise ConnectionError("routeros_session_status_unavailable") from exc

    def recovery_health_proof(self) -> Dict[str, Any]:
        """Return RouterOS proof that LTE is healthy after API reconnect.

        This intentionally uses RouterOS LTE monitor data only.  It does not
        poll AT, and it does not treat a live RouterOS API session as proof
        that lte1 is attached.
        """
        proof: Dict[str, Any] = {
            "backend_family": "routeros_at_like",
            "source": "routeros_lte_monitor",
            "healthy": False,
            "radio_connected": False,
            "reason": "",
        }
        if not self._routeros:
            proof["reason"] = "routeros_api_unavailable"
            return proof
        if not self._lte_id:
            proof["reason"] = "lte1_id_unavailable"
            return proof
        invariant = self._ensure_lte1_enabled(force=True)
        lte_disabled = invariant.get("disabled")
        if not bool(invariant.get("ok", False)):
            proof["lte_interface_disabled"] = lte_disabled
            proof["lte_enable_invariant"] = dict(invariant)
            proof["reason"] = str(
                invariant.get("reason") or "lte1_enable_invariant_failed"
            )
            return proof
        if lte_disabled is True:
            proof["lte_interface_disabled"] = True
            proof["reason"] = "lte1_disabled"
            return proof
        before_monitor_ts = float(getattr(self, "_last_lte_monitor_ts", 0.0) or 0.0)
        data = self._lte_monitor(force=True)
        after_monitor_ts = float(getattr(self, "_last_lte_monitor_ts", 0.0) or 0.0)
        if after_monitor_ts <= before_monitor_ts:
            proof["reason"] = "routeros_lte_monitor_not_fresh"
            return proof
        if not isinstance(data, dict) or not data:
            proof["reason"] = "routeros_lte_monitor_empty"
            return proof
        radio_connected = bool(data.get("radio_connected", False))
        primary_band = int(data.get("primary_band", 0) or 0)
        pci = int(data.get("pci", 0) or 0)
        session_uptime_s = float(data.get("session_uptime_s", 0.0) or 0.0)
        active_bands = normalize_band_list(data.get("active_band_set") or [])
        has_serving_evidence = bool(primary_band > 0 or pci > 0 or session_uptime_s > 0 or active_bands)
        proof.update(
            {
                "radio_connected": radio_connected,
                "primary_band": primary_band,
                "pci": pci,
                "session_uptime_s": session_uptime_s,
                "active_band_set": list(active_bands),
                "lte_interface_disabled": False,
                "healthy": bool(radio_connected and has_serving_evidence),
                "reason": "healthy" if bool(radio_connected and has_serving_evidence) else "lte_not_attached",
            }
        )
        return proof

    # ─── Observation ────────────────────────────────────────

    def _read_modem_input_impl(self, *, refresh_auxiliary: bool) -> Optional[ModemInput]:
        if not self._routeros:
            return None
        try:
            # This RouterOS-only check is deliberately before LTE monitor.
            # A disabled interface cannot produce a useful modem sample, and
            # waiting for a failed monitor would delay the known repair.
            lte_invariant = self._ensure_lte1_enabled(force=False)

            # 1. Traffic counters first (fast path) so GUI throughput reflects
            # current load without waiting for AT-command post-delays.
            rx_bps, tx_bps = self._read_traffic()
            if not self._routeros:
                return None

            # 2. RF monitor (modem-facing): throttled/cached to avoid frequent modem pokes.
            lte_data = self._lte_monitor()
            if not lte_data:
                return None
            # Identity and capabilities are boot/static facts. Read them only
            # after the first current traffic/RF truth is safely captured, then
            # retain the cache. They must never delay initial live awareness.
            if refresh_auxiliary:
                self._refresh_static_info()
            invariant_disabled = lte_invariant.get("disabled")
            lte_disabled = (
                bool(invariant_disabled)
                if invariant_disabled is not None
                else bool(self._last_lte_disabled)
            )
            if refresh_auxiliary:
                device_recovery_state = self.routeros_watchdog_script_status()
            else:
                device_recovery_state = self._recovery_status_for_publication(
                    self._last_watchdog_script_status or {}
                )
            device_recovery_diagnostic = copy.deepcopy(
                self._last_device_recovery_diagnostic or {}
            )

            # 3. Auxiliary AT lanes are optional for the fast actionable
            # snapshot. LIVE MODULE owns cadence and may refresh them on a
            # background maintenance lane instead of every authority sample.
            if refresh_auxiliary:
                self._refresh_auxiliary_cache()
            carriers = self._current_qcainfo() if self._at_live_polling_enabled else []
            qnwcfg_mcs = self._current_qnwcfg_mcs() if self._at_live_polling_enabled else None

            # If lte_monitor did not provide EARFCN, derive it from the
            # extract from QCAINFO PCell carrier
            earfcn = lte_data.get("earfcn", 0)
            if earfcn == 0 and carriers:
                pcell = next((c for c in carriers if c.is_pcell), None)
                if pcell:
                    earfcn = pcell.earfcn

            mcs_dl_raw = int(lte_data.get("mcs_dl", 0) or 0)
            mcs_ul_raw = int(lte_data.get("mcs_ul", 0) or 0)
            mcs_dl1_raw = 0
            if qnwcfg_mcs:
                mcs_dl_raw = int(qnwcfg_mcs.get("mcs_dl0", mcs_dl_raw) or 0)
                mcs_ul_raw = int(qnwcfg_mcs.get("mcs_ul", mcs_ul_raw) or 0)
                mcs_dl1_raw = int(qnwcfg_mcs.get("mcs_dl1", 0) or 0)

            # QENG is read only when monitor/QNWCFG are missing data.
            qeng = self._current_qeng_mcs() if self._at_live_polling_enabled else None
            if mcs_ul_raw == 0 or mcs_dl_raw == 0 or earfcn == 0:
                if refresh_auxiliary and self._at_live_polling_enabled and qeng is None:
                    self._refresh_auxiliary_cache(prefer_qeng=True)
                    qeng = self._current_qeng_mcs()
                if qeng:
                    if mcs_ul_raw == 0:
                        mcs_ul_raw = int(qeng.get("mcs_ul", 0) or 0)
                    if mcs_dl_raw == 0:
                        mcs_dl_raw = int(qeng.get("mcs_dl", 0) or 0)
                    if earfcn == 0:
                        earfcn = int(qeng.get("earfcn", 0) or 0)
            imported_at = self._portable_at_values()
            if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS:
                # One selected payload. A partial monitor is never completed
                # with quantities from the independently timed AT request.
                for address, key in _MIKROTIK_NR_MONITOR_SCALARS.items():
                    if address in imported_at:
                        lte_data[key] = imported_at[address]
                if imported_at.get('radio.nr5g_active') is True:
                    lte_data['network_type'] = 'NR ' + str(imported_at['radio.nr_mode']) + ' (5G)'
            if 'rf.mcs_dl0' in imported_at:mcs_dl_raw=int(imported_at['rf.mcs_dl0']or 0)
            if 'rf.mcs_dl1' in imported_at:mcs_dl1_raw=int(imported_at['rf.mcs_dl1']or 0)
            if 'rf.mcs_ul' in imported_at:mcs_ul_raw=int(imported_at['rf.mcs_ul']or 0)
            if 'rf.earfcn_dl' in imported_at:
                earfcn=int(imported_at['rf.earfcn_dl']or 0)
                for carrier in carriers:
                    if carrier.is_pcell:carrier.earfcn=earfcn
            if not self._routeros:
                return None
            if mcs_ul_raw == 0 and tx_bps > 10000:  # >10KB/s = real UL traffic
                log.debug(
                    "MCS_UL=0 despite active UL traffic (%.1f KB/s) — monitor=%s qnwcfg=%s qeng=%s",
                    tx_bps / 1000,
                    lte_data.get("mcs_ul", "absent"),
                    "hit" if (qnwcfg_mcs and qnwcfg_mcs.get("mcs_ul", 0) > 0) else "miss",
                    "hit" if (qeng and qeng.get("mcs_ul", 0) > 0) else "miss",
                )

            # Normalize primary RF signals into internal contract units.
            sinr_raw = float(lte_data.get("sinr", 0.0) or 0.0)
            rsrp_raw = float(lte_data.get("rsrp", -140.0) or -140.0)
            rsrq_raw = float(lte_data.get("rsrq", -20.0) or -20.0)
            rssi_raw = float(lte_data.get("rssi", -100.0) or -100.0)
            profile_rf = lte_data.get("_profile_rf_values")
            def primary_signal(metric, raw_value, address):
                # Internal parsed-row test doubles have no native reply. Real
                # monitor snapshots always carry their admitted field results.
                if profile_rf is None:
                    return self._norm_signal(metric, raw_value)
                value = profile_rf.get(address)
                if value is None:
                    return 0.0  # Typed sample default; excluded from fresh proof.
                configured = assess_signal_contract(metric, raw_value, self._signal_scales.get(metric, 1.0))
                original_norm = (configured["alt_norm"]
                    if not configured["ok"] and configured["alt_ok"] else configured["norm"])
                if value == original_norm:
                    return self._norm_signal(metric, raw_value)
                return self._norm_signal(metric, value, scale=1.0)
            sinr_norm = primary_signal("sinr", sinr_raw, "rf.sinr_db")
            rsrp_norm = primary_signal("rsrp", rsrp_raw, "rf.rsrp_dbm")
            rsrq_norm = primary_signal("rsrq", rsrq_raw, "rf.rsrq_db")
            rssi_norm = primary_signal("rssi", rssi_raw, "rf.rssi_dbm")

            # Apply same normalization to carrier and neighbor signal fields.
            for c in carriers:
                c.sinr_db = self._norm_signal("sinr", c.sinr_db)
                c.rsrp_dbm = self._norm_signal("rsrp", c.rsrp_dbm)

            # Build ModemInput
            active_band_signature = _build_ca_signature(carriers)
            if not active_band_signature:
                active_band_signature = str(
                    lte_data.get("active_band_set_signature", "")
                    or ""
                )
            ca_active = any(c.state >= 1 for c in carriers) or bool(lte_data.get("ca_active", False))
            if not active_band_signature and int(lte_data.get("primary_band", 0) or 0) > 0:
                active_band_signature = f"B{int(lte_data.get('primary_band', 0) or 0)}"
            # Shared LTE rule: MCS 28..31 are retransmission markers.
            # They do not define a fresh modulation class; keep last known Qm when possible.
            inferred_qam_dl0, mcs_dl_retx = resolve_lte_qm_with_retx_hold(mcs_dl_raw, self._last_qm_dl0)
            qam_dl1, mcs_dl1_retx = resolve_lte_qm_with_retx_hold(mcs_dl1_raw, self._last_qm_dl1)
            inferred_qam_ul, mcs_ul_retx = resolve_lte_qm_with_retx_hold(mcs_ul_raw, self._last_qm_ul)
            reported_qam_dl0 = int(lte_data.get("qam_dl0_reported", 0) or 0)
            reported_qam_ul = int(lte_data.get("qam_ul_reported", 0) or 0)
            qam_dl0 = reported_qam_dl0 or inferred_qam_dl0
            qam_ul_eff = reported_qam_ul or inferred_qam_ul
            if mcs_dl1_raw <= 0 and not mcs_dl1_retx:
                qam_dl1 = 0
            if qam_dl0 > 0:
                self._last_qm_dl0 = int(qam_dl0)
            if qam_dl1 > 0:
                self._last_qm_dl1 = int(qam_dl1)
            mcs_ul_supported = bool(
                mcs_ul_raw > 0
                or bool(mcs_ul_retx)
                or (qeng is not None and int(qeng.get("mcs_ul", 0) or 0) > 0)
            )
            if not mcs_ul_supported and reported_qam_ul <= 0:
                qam_ul_eff = 0
            elif qam_ul_eff > 0:
                self._last_qm_ul = int(qam_ul_eff)
            has_reported_qam = any(q > 0 for q in (reported_qam_dl0, reported_qam_ul))
            has_qam = any(q > 0 for q in (qam_dl0, qam_dl1, qam_ul_eff))
            qam_source = "reported" if has_reported_qam else ("inferred" if has_qam else "unknown")
            qam_conf = 1.0 if has_reported_qam else (0.65 if has_qam else 0.0)
            cfg_bands = self._current_configured_bands_live()
            configured_band_signature = (
                "+".join(f"B{band}" for band in cfg_bands)
                if cfg_bands
                else ""
            )

            # Wi-Fi status is cached on a short cadence; no need to query
            # RouterOS on every sample.
            wifi_on, wifi_24, wifi_5 = self._current_wifi_status()

            # 5. Dashboard extras: DL/UL bandwidth, TX power, frequencies.
            # Uses existing read_serving_cell() on a slow cadence (~10s).
            sc_extra = self._current_serving_cell_extra()
            trans_mode_value = str(sc_extra.get("trans_mode") or self._trans_mode_cache or "")
            trans_mode_source = str(sc_extra.get("trans_mode_source") or self._trans_mode_source or "")
            ri_value = int(lte_data.get("ri", 0) or 0)
            ri_published = int(ri_value if ri_value > 0 else 0)
            ri_valid = bool(ri_published > 0)
            ri_source = "routeros_lte_monitor" if ri_valid else ""
            # Derive DL/UL frequency from EARFCN (3GPP TS 36.101).
            dl_freq_mhz, ul_freq_mhz = lte_frequency_mhz_from_dl_earfcn(
                int(earfcn or 0)
            )

            # DL bandwidth: prefer monitor primary-band (@NNMhz), fall back to QENG.
            dl_bw_mhz = lte_data.get("dl_bw_mhz") or sc_extra.get("dl_bw_mhz_qeng")
            ul_bw_mhz = sc_extra.get("ul_bw_mhz")
            tac_value = int(lte_data.get("tac", 0) or sc_extra.get("tac", 0) or 0)
            if 'slow.tac' in imported_at:tac_value=int(imported_at['slow.tac']or 0)
            rrc_value = str(sc_extra.get("rrc") or "").strip()
            if not rrc_value:
                rrc_value = "Connected" if bool(lte_data.get("radio_connected", False)) else "Idle"
            provider_name = str(getattr(self, "_provider_name_cache", "") or "")
            lte_monitor_age_s = max(
                0.0,
                float(lte_data.get("_routeros_lte_monitor_age_s", time.time() - float(self._last_lte_monitor_ts or 0.0)) or 0.0),
            )
            lte_monitor_cached = bool(lte_data.get("_routeros_lte_monitor_cached", False))
            lte_monitor_fresh = bool(lte_data.get("_routeros_lte_monitor_fresh", not lte_monitor_cached))
            qnwlock_guard = dict(lte_data.get("qnwlock_guard") or {})
            ca_component_bands = normalize_band_list(lte_data.get("ca_component_band_set") or [])
            ca_component_count = int(lte_data.get("ca_component_count", 0) or 0)
            ca_component_signature = str(lte_data.get("ca_component_band_signature", "") or "")
            ca_composition_signature = str(lte_data.get("ca_composition_signature", "") or "")
            carrier_details = list(lte_data.get("carrier_details") or [])
            carrier_rows = list(lte_data.get("carrier_rows") or [])
            ca_source = str(lte_data.get("ca_source", "") or "")
            ca_observation_status = str(
                lte_data.get("_routeros_ca_observation_status", "unparsed")
                or "unparsed"
            )
            radio_connected = bool(lte_data.get("radio_connected", False))
            network_type = (str(lte_data.get("network_type") or "")
                if "radio.network_type" in (profile_rf or {})
                else "LTE (4G)" if radio_connected else "")
            traffic_volume = dict(self._traffic_volume_cache or {})
            configured_band_age_s = (
                max(0.0, time.time() - float(self._last_configured_bands_ts or 0.0))
                if self._last_configured_bands_ts > 0.0
                else None
            )

            modem = ModemInput(
                sinr_db=sinr_norm,
                rsrp_dbm=rsrp_norm,
                rsrq_db=rsrq_norm,
                rssi_dbm=rssi_norm,
                mcs_dl=mcs_dl_raw,
                mcs_ul=mcs_ul_raw,
                cqi=lte_data.get("cqi", 0),
                ri=ri_published,
                ri_valid=ri_valid,
                ri_source=ri_source,
                carriers=carriers,
                ca_active=ca_active,
                ca_band_signature=configured_band_signature,
                primary_band=lte_data.get("primary_band", 0),
                pci=lte_data.get("pci", 0),
                earfcn=earfcn,
                enb_id=lte_data.get("enb_id", 0),
                cell_id=int(lte_data.get("cell_id", 0) or 0),
                radio_connected=radio_connected,
                network_type=network_type,
                session_uptime_s=lte_data.get("session_uptime_s", 0.0),
                traffic_valid=radio_connected,
                traffic_state_detail=(
                    "routeros_lte_connected"
                    if radio_connected
                    else "routeros_lte_disconnected"
                ),
                traffic_invalid_reason=(
                    "" if radio_connected else "routeros_lte_disconnected"
                ),
                packet_session_connected=radio_connected,
                connection_status_code=(
                    "connected" if radio_connected else "disconnected"
                ),
                lte_interface_disabled=bool(lte_disabled),
                rx_bytes_per_sec=rx_bps,
                tx_bytes_per_sec=tx_bps,
                traffic_total_download_bytes=int(traffic_volume.get("traffic_total_download_bytes", 0) or 0),
                traffic_total_upload_bytes=int(traffic_volume.get("traffic_total_upload_bytes", 0) or 0),
                traffic_session_download_bytes=int(traffic_volume.get("traffic_session_download_bytes", 0) or 0),
                traffic_session_upload_bytes=int(traffic_volume.get("traffic_session_upload_bytes", 0) or 0),
                traffic_device_download_counter_bytes=traffic_volume.get("traffic_device_download_counter_bytes"),
                traffic_device_upload_counter_bytes=traffic_volume.get("traffic_device_upload_counter_bytes"),
                traffic_counter_period_started_at=traffic_volume.get("traffic_counter_period_started_at"),
                traffic_counter_interface_started_at=traffic_volume.get("traffic_counter_interface_started_at"),
                traffic_accounting_source=str(traffic_volume.get("traffic_accounting_source", "") or ""),
                traffic_accounting_status=str(traffic_volume.get("traffic_accounting_status", "") or ""),
                traffic_volume_poll_interval_s=float(traffic_volume.get("traffic_volume_poll_interval_s", 0.0) or 0.0),
                traffic_volume_last_sample_ts=float(traffic_volume.get("traffic_volume_last_sample_ts", 0.0) or 0.0),
                traffic_volume_next_sample_ts=float(traffic_volume.get("traffic_volume_next_sample_ts", 0.0) or 0.0),
                traffic_volume_age_s=traffic_volume.get("traffic_volume_age_s"),
                traffic_rate_source="routeros_monitor_traffic",
                traffic_quota_start_day=traffic_volume.get("traffic_quota_start_day"),
                traffic_quota_limit_label=str(traffic_volume.get("traffic_quota_limit_label", "") or ""),
                traffic_quota_limit_bytes=traffic_volume.get("traffic_quota_limit_bytes"),
                traffic_quota_month_threshold_pct=traffic_volume.get("traffic_quota_month_threshold_pct"),
                traffic_quota_day_threshold_pct=traffic_volume.get("traffic_quota_day_threshold_pct"),
                selected_band_set=list(cfg_bands or []),
                configured_band_pool=list(cfg_bands or []),
                configured_band_pool_signature=configured_band_signature,
                configured_band_pool_source="routeros_lte_band_mask",
                configured_band_pool_status=str(self._last_configured_bands_status or ""),
                configured_band_pool_age_s=configured_band_age_s,
                configured_band_pool_last_sample_ts=float(self._last_configured_bands_ts or 0.0),
                ca_component_band_set=list(ca_component_bands),
                ca_component_band_signature=ca_component_signature,
                ca_component_count=ca_component_count,
                ca_composition_signature=ca_composition_signature,
                ca_source=ca_source,
                mcs_dl0=mcs_dl_raw,
                qam_dl0=qam_dl0,
                mcs_dl1=mcs_dl1_raw,
                mod0=qam_dl0,
                mod1=qam_dl1,
                qam_dl_primary=qam_dl0,
                qam_dl_secondary=qam_dl1,
                qam_ul=qam_ul_eff,
                qam_source=qam_source,
                qam_confidence=qam_conf,
                mcs_ul_supported=bool(mcs_ul_supported),
                cqi0=int(lte_data.get("cqi", 0) or 0),
                cqi1=0,
                dl_bandwidth=f"{dl_bw_mhz:.0f} MHz" if dl_bw_mhz else "",
                ul_bandwidth=f"{ul_bw_mhz:.0f} MHz" if ul_bw_mhz else "",
                dl_bandwidth_mhz=float(dl_bw_mhz or 0.0),
                ul_bandwidth_mhz=float(ul_bw_mhz or 0.0),
                dl_freq_mhz=float(dl_freq_mhz or 0.0),
                ul_freq_mhz=float(ul_freq_mhz or 0.0),
                tx_power=sc_extra.get("tx_power") or "",
                tac=tac_value,
                rrc=rrc_value,
                trans_mode=trans_mode_value,
                provider_name=provider_name,
                provider_plmn=str(self._plmn_cache or ""),
                wifi_enabled=wifi_on,
                wifi_24ghz_enabled=wifi_24,
                wifi_5ghz_enabled=wifi_5,
                antenna=str(self._device_info_cache.get("antenna") or ""),
                antenna_mode=str(self._device_info_cache.get("antenna_mode") or ""),
                antenna_mode_source=str(self._device_info_cache.get("antenna_mode_source") or ""),
                antenna_switch_api_supported=False,
                # Selected 5G definitions alone can fill these current native
                # observations. A fresh LTE-only reply clears retained NR slots.
                nr_rsrp_dbm=float(lte_data.get("nr_rsrp_dbm") or 0.0),
                nr_rsrq_db=float(lte_data.get("nr_rsrq_db") or 0.0),
                nr_sinr_db=float(lte_data.get("nr_sinr_db") or 0.0),
                nr_band=int(lte_data.get("nr_band") or 0),
                nr_pci=int(lte_data.get("nr_pci") or 0),
                nr_arfcn=int(lte_data.get("nr_arfcn") or 0),
                nr_mode=str(lte_data.get("nr_mode") or ""),
                nr_cells=list(lte_data.get("nr_cells") or []),
                nr_bandwidth_mhz=float(lte_data.get("nr_bandwidth_mhz") or 0.0),
                nsa_active=lte_data.get("nr5g_active") is True and lte_data.get("nr_mode") == "NSA",
                sa_active=lte_data.get("nr5g_active") is True and lte_data.get("nr_mode") == "SA",
            )
            if lte_data.get("_profile_rf_diagnostic"):
                diagnostic = copy.deepcopy(lte_data["_profile_rf_diagnostic"])
                if device_recovery_diagnostic:
                    diagnostic["data"]["native_diagnostic"] = device_recovery_diagnostic
                device_recovery_diagnostic = diagnostic
            self._last_status = {
                "ts": time.time(),
                "provider": str(
                    provider_name
                ),
                "provider_name": str(provider_name),
                "plmn": str(self._plmn_cache or ""),
                "provider_plmn": str(self._plmn_cache or ""),
                "ca_active": bool(ca_active),
                "ca_state_text": "ACTIVE" if bool(ca_active) else "INACTIVE",
                "ca_signature": configured_band_signature,
                "ca_band_signature": configured_band_signature,
                "carrier_count": len(cfg_bands),
                "carrier_details": carrier_details,
                "carrier_rows": carrier_rows,
                "ca_component_band_set": list(ca_component_bands),
                "ca_component_band_signature": ca_component_signature,
                "ca_component_count": ca_component_count,
                "ca_composition_signature": ca_composition_signature,
                "ca_source": ca_source,
                "routeros_ca_observation_status": ca_observation_status,
                "active_band_set": (
                    parse_band_signature(active_band_signature)
                    if active_band_signature
                    else []
                ),
                "active_band_set_signature": active_band_signature,
                "band_set": (
                    parse_band_signature(active_band_signature)
                    if active_band_signature
                    else []
                ),
                "band_set_signature": active_band_signature,
                "band": int(lte_data.get("primary_band", 0) or 0),
                "serving_band": int(lte_data.get("primary_band", 0) or 0),
                "serving_band_signature": f"B{int(lte_data.get('primary_band', 0) or 0)}" if int(lte_data.get("primary_band", 0) or 0) > 0 else "",
                "pci": int(lte_data.get("pci", 0) or 0),
                "earfcn_dl": int(earfcn or 0),
                "enb_id": int(lte_data.get("enb_id", 0) or 0),
                "cell_id": int(lte_data.get("cell_id", 0) or 0),
                "tac": tac_value,
                "rrc": rrc_value,
                "trans_mode": trans_mode_value,
                "trans_mode_source": trans_mode_source,
                "ri": ri_published,
                "ri_valid": ri_valid,
                "ri_source": ri_source,
                "radio_connected": bool(getattr(modem, "radio_connected", False)),
                "radio_state_text": "Connected" if bool(getattr(modem, "radio_connected", False)) else "Disconnected",
                "network_type": network_type,
                "rx_bytes_per_sec": float(rx_bps or 0.0),
                "tx_bytes_per_sec": float(tx_bps or 0.0),
                **dict(self._traffic_volume_cache or {}),
                # Current rates belong to this fast monitor-traffic read, not
                # the independently refreshed volume-accounting cache.
                "traffic_current_download_rate_bps": float(rx_bps),
                "traffic_current_upload_rate_bps": float(tx_bps),
                "traffic_rate_source": "routeros_monitor_traffic",
                "mcs_ul": mcs_ul_raw,
                "mcs_ul_supported": bool(mcs_ul_supported),
                "mcs_ul_raw": mcs_ul_raw,
                # Retx flags mean MCS 28..31 was observed (shared LTE rule);
                # modulation uses held last valid Qm rather than a fresh class from retx marker.
                "mcs_dl_retx": bool(mcs_dl_retx),
                "mcs_dl1_retx": bool(mcs_dl1_retx),
                "mcs_ul_retx": bool(mcs_ul_retx),
                "qam_dl0": qam_dl0,
                "qam_dl1": qam_dl1,
                "qam_ul": qam_ul_eff,
                "qam_source": qam_source,
                "qam_confidence": qam_conf,
                "model": self._device_info_cache.get("model", ""),
                "router_model": self._device_info_cache.get("router_model", ""),
                "hardware_version": self._device_info_cache.get("hardware_version", ""),
                "software": self._device_info_cache.get("software_version", ""),
                "software_version": self._device_info_cache.get("software_version", ""),
                "os_name": self._device_info_cache.get("os_name", ""),
                "os_version": self._device_info_cache.get("os_version", ""),
                "webui": self._device_info_cache.get("webui", ""),
                "firmware": self._device_info_cache.get("firmware", ""),
                "serial": self._device_info_cache.get("serial", ""),
                "imei": self._device_info_cache.get("imei", ""),
                "wan_ip": self._device_info_cache.get("wan_ip", ""),
                "wan_connected": bool(self._device_info_cache.get("wan_ip")),
                "wan_iface_name": self._device_info_cache.get("wan_iface_name", ""),
                "lan_mac": self._device_info_cache.get("lan_mac", ""),
                "antenna": self._device_info_cache.get("antenna", ""),
                "antenna_mode": self._device_info_cache.get("antenna_mode", ""),
                "antenna_mode_source": self._device_info_cache.get("antenna_mode_source", ""),
                "antenna_switch_api_supported": False,
                "raw_sinr": sinr_raw,
                "raw_rsrp": rsrp_raw,
                "raw_rsrq": rsrq_raw,
                "raw_rssi": rssi_raw,
                "norm_sinr": sinr_norm,
                "norm_rsrp": rsrp_norm,
                "norm_rsrq": rsrq_norm,
                "norm_rssi": rssi_norm,
                "at_cmd_latency_ms": float(self._last_at_latency_ms),
                "at_bus_guard_s": float(self._at_bus_guard_s),
                "at_blackout_remaining_s": max(0.0, float(self._at_blackout_until - time.time())),
                "routeros_lte_monitor_age_s": lte_monitor_age_s,
                "routeros_lte_monitor_cached": lte_monitor_cached,
                "routeros_lte_monitor_fresh": lte_monitor_fresh,
                "routeros_lte_monitor_error": str(lte_data.get("_routeros_lte_monitor_error", "") or ""),
                "routeros_lte_monitor_invalid_snapshot": bool(
                    lte_data.get("_routeros_lte_monitor_invalid_snapshot", False)
                ),
                "qnwlock_guard": qnwlock_guard,
                "lte_interface_disabled": bool(lte_disabled),
                "device_recovery_state": device_recovery_state,
                "backend_diagnostic": device_recovery_diagnostic,
                "wifi_enabled": wifi_on,
                "wifi_24ghz_enabled": wifi_24,
                "wifi_5ghz_enabled": wifi_5,
                # Dashboard contract: bandwidth, frequency, TX power
                "dl_bandwidth": f"{dl_bw_mhz:.0f} MHz" if dl_bw_mhz else None,
                "ul_bandwidth": f"{ul_bw_mhz:.0f} MHz" if ul_bw_mhz else None,
                "dl_bandwidth_mhz": dl_bw_mhz,
                "ul_bandwidth_mhz": ul_bw_mhz,
                "dl_freq_mhz": dl_freq_mhz or None,
                "ul_freq_mhz": ul_freq_mhz or None,
                "tx_power": sc_extra.get("tx_power"),
                # Dashboard contract: scheduler stream fields
                "cqi0": int(lte_data.get("cqi", 0) or 0),
                "mcs_dl0": mcs_dl_raw,
                "mcs_dl1": mcs_dl1_raw,
            }
            # Publish configured LTE band mask (RouterOS /interface/lte band) so
            # runtime projections can reflect manual band changes immediately,
            # even when active CA signature has not changed yet.
            self._last_status["selected_band_set"] = cfg_bands
            self._last_status["selected_band_set_signature"] = (
                "+".join(f"B{b}" for b in cfg_bands) if cfg_bands else ""
            )
            self._last_status["configured_band_pool"] = list(cfg_bands or [])
            self._last_status["configured_band_pool_signature"] = configured_band_signature
            self._last_status["configured_band_pool_source"] = "routeros_lte_band_mask"
            self._last_status["configured_band_pool_status"] = str(self._last_configured_bands_status or "")
            self._last_status["configured_band_pool_age_s"] = configured_band_age_s
            self._last_status["configured_band_pool_last_sample_ts"] = float(
                self._last_configured_bands_ts or 0.0
            )
            if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS:
                # Public status uses established NR aliases and keeps missing
                # separate from ModemInput's historical zero defaults.
                for address, key in _MIKROTIK_NR_MONITOR_SCALARS.items():
                    public = {"nr_mode": "nr5g_mode", "nr_cells": "nr5g_cells",
                              "nr_bandwidth_mhz": "nr5g_nsa_bandwidth"}.get(
                                  key, key if key == "nr5g_active" else key.replace("nr_", "nr5g_", 1))
                    self._last_status[public] = lte_data.get(key)
                self._last_status['nr_ul_bandwidth_mhz'] = imported_at.get('slow.nr_ul_bandwidth_mhz')
            return modem
        except (RouterOsTransportError, RouterOsProtocolError) as e:
            log.error("read_modem_input failed: %s", e)
            if isinstance(e, RouterOsTransportError):
                self._mark_transport_failure("live_read", e)
            return None

    def read_modem_input(self) -> Optional[ModemInput]:
        return self._read_modem_input_impl(refresh_auxiliary=True)

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        return self._read_modem_input_impl(refresh_auxiliary=False)

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "mikrotik_startup_full_acquisition_lane_required"
            )
        before = self._mikrotik_acquisition_markers()
        # A connected receipt needs the stable device identity and live
        # radio/traffic/pool, not Wi-Fi, recovery topology or engineering AT.
        self._refresh_static_info()
        modem = self.read_actionable_modem_input()
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=(
                self._mikrotik_acquired_cells_since(
                    before,
                    include_monitor=True,
                )
                if modem is not None
                else frozenset()
            ),
            failure_reason="mikrotik_startup_acquisition_unavailable",
        )

    def _mikrotik_acquisition_markers(self) -> Dict[str, Any]:
        return {
            "identity": copy.deepcopy(self._device_info_cache),
            "subscription": float(getattr(self, "_subscription_observed_at", 0.0)),
            "monitor": float(self._last_lte_monitor_ts or 0.0),
            "configured": float(self._last_configured_bands_ts or 0.0),
            "disabled": float(self._last_lte_disabled_ts or 0.0),
            "wifi": float(self._last_wifi_status_ts or 0.0),
            "qcainfo": float(self._last_qcainfo_ts or 0.0),
            "qnwcfg": float(self._last_qnwcfg_mcs_ts or 0.0),
            "serving": float(self._last_serving_cell_extra_ts or 0.0),
            "qeng": float(self._last_qeng_mcs_ts or 0.0),
            "volume": float(self._traffic_volume_ts or 0.0),
            "device_recovery": float(self._last_watchdog_script_status_ts or 0.0),
            "device_recovery_diagnostic": int(
                self._device_recovery_diagnostic_sequence or 0
            ),
        }

    def _mikrotik_acquired_cells_since(
        self,
        before: Dict[str, Any],
        *,
        include_monitor: bool,
    ) -> frozenset[str]:
        cells: set[str] = set()
        if self._device_info_cache != before["identity"]:
            cells.update(IDENTITY_CELLS - matrix_cells_for_canonical_fields(frozenset({"imsi", "provider_plmn", "provider_name"})))
        if float(getattr(self, "_subscription_observed_at", 0.0)) > float(before["subscription"]):
            cells.update(matrix_cells_for_canonical_fields(self._subscription_acquired_fields))
        if (
            include_monitor
            and float(self._last_lte_monitor_ts or 0.0)
            > float(before["monitor"] or 0.0)
        ):
            cells.update(
                matrix_cells_for_canonical_fields(
                    _MIKROTIK_MONITOR_FIELDS
                    | _MIKROTIK_FAST_TRAFFIC_FIELDS
                )
            )
            cells.update(self._mikrotik_ca_acquired_cells())
            cells.update(self._mikrotik_nr_acquired_cells())
        if float(self._last_configured_bands_ts or 0.0) > float(
            before["configured"] or 0.0
        ):
            cells.update(CONFIGURED_BAND_CELLS)
        if float(self._last_wifi_status_ts or 0.0) > float(
            before["wifi"] or 0.0
        ):
            cells.update(
                matrix_cells_for_canonical_fields(
                    frozenset(
                        {
                            "wifi",
                            "wifi_enabled",
                            "wifi_24ghz_enabled",
                            "wifi_5ghz_enabled",
                            "wifi_6ghz_enabled",
                        }
                    )
                )
            )
        if any(
            float(after or 0.0) > float(before[name] or 0.0)
            and not (name == 'serving' and '_routeros_nr_context' in self._last_serving_cell_extra)
            for name, after in (
                ("disabled", self._last_lte_disabled_ts),
                ("qcainfo", self._last_qcainfo_ts),
                ("qnwcfg", self._last_qnwcfg_mcs_ts),
                ("serving", self._last_serving_cell_extra_ts),
                ("qeng", self._last_qeng_mcs_ts),
            )
        ):
            cells.update(SLOW_CELLS)
        if float(self._traffic_volume_ts or 0.0) > float(
            before["volume"] or 0.0
        ):
            cells.update(_MIKROTIK_TRAFFIC_COUNTER_CELLS)
        if float(self._last_watchdog_script_status_ts or 0.0) > float(
            before["device_recovery"] or 0.0
        ):
            cells.update(DEVICE_RECOVERY_CELLS)
        if int(self._device_recovery_diagnostic_sequence or 0) > int(
            before["device_recovery_diagnostic"] or 0
        ):
            cells.add("slow.backend_diagnostic")
        if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS:
            # Never let the legacy broad slow group certify a currently
            # unavailable UL value. The QCA owner below supplies exact proof.
            cells.discard('slow.nr_ul_bandwidth_mhz')
        for owner, stamp, values in self._portable_at_sources():
            cells.difference_update(self._portable_at_cells(values))
            if stamp > float(before[owner]or 0.):
                cells.update(self._portable_at_cells({key:value for key,value in values.items()if value is not None}))
        return frozenset(cells)

    def _mikrotik_ca_acquired_cells(self) -> frozenset[str]:
        status = str(
            self._last_status.get("routeros_ca_observation_status", "")
            or ""
        )
        if status not in {"active", "inactive"}:
            return frozenset()
        return matrix_cells_for_canonical_fields(
            _MIKROTIK_CA_MONITOR_FIELDS
        )

    def _mikrotik_nr_acquired_cells(self) -> frozenset[str]:
        """Only values actually decoded from this selected native response."""
        native = self._last_lte_monitor or {}
        if (self._definition.profile_id not in _MIKROTIK_NR_PROFILE_IDS
                or native.get("_routeros_nr_current_interface") is not True):
            return frozenset()
        values = native.get("_profile_rf_values", {})
        return frozenset(address for address in _MIKROTIK_NR_MONITOR_SCALARS
                         if values.get(address) is not None)

    def _capture_backend_acquisition_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
        acquired_cells: frozenset[str],
        failure_reason: str = "",
        status_override: Optional[Mapping[str, Any]] = None,
    ) -> BackendAcquisitionReceipt:
        # Public status deliberately excludes IMSI. Internal acquisition must
        # carry it only when this read actually observed it, without a new probe.
        status = self.get_last_status() if status_override is None else dict(status_override)
        if "identity.imsi" in acquired_cells:
            status["imsi"] = self._device_info_cache.get("imsi")
        # Slower AT enrichments can still supply measurements, but cannot
        # certify a missing monitor ID as belonging to its newly observed cell.
        native = self._last_lte_monitor or {}
        unavailable = getattr(self, '_profile_unavailable_native', frozenset())
        if unavailable:
            acquired_cells -= matrix_cells_for_canonical_fields(frozenset(
                address.split('.',1)[1] for address in unavailable
                if address not in _MIKROTIK_NR_MONITOR_SCALARS
                or self._routeros_nr_at_selected(native)))
        acquired_cells -= frozenset(address for address,value in
            getattr(self, '_last_traffic_profile_values', {}).items() if value is None)
        if "_profile_rf_values" in native:
            acquired_cells = acquired_cells - frozenset(
                address for address,value in native["_profile_rf_values"].items()
                if value is None and not (address in _MIKROTIK_NR_MONITOR_SCALARS
                    and self._routeros_nr_at_selected(native))
            )
            if native["_profile_rf_values"].get("rf.cqi0",0) is None:
                acquired_cells -= frozenset({"rf.cqi"})
            if native.get('_routeros_nr_no_lte'):
                acquired_cells -= frozenset({'rf.earfcn_dl', 'rf.enb_id', 'rf.cell_id',
                    'rf.pci', 'radio.serving_band', 'slow.tac'})
            if (native.get("_profile_rf_diagnostic")
                    and self._last_status.get("routeros_lte_monitor_fresh") is True):
                acquired_cells |= frozenset({"slow.backend_diagnostic"})
        if "rf.earfcn_dl" in acquired_cells and native.get("_radio_identity_audit"):
            from arch4.modem_live.radio_identity_audit import identity_diagnostic
            evidence = dict(native["_radio_identity_audit"])
            if "slow.backend_diagnostic" in acquired_cells and status.get("backend_diagnostic"):
                evidence["native_diagnostic"] = status["backend_diagnostic"]
            status["backend_diagnostic"] = identity_diagnostic(
                backend="mikrotik", observed_at=float(self._last_lte_monitor_ts),
                source="routeros_native_primary_identity", evidence=evidence)
            acquired_cells |= frozenset({"slow.backend_diagnostic"})
        for fields, key in (
            ({"serving_band"}, "primary_band"), ({"pci"}, "pci"),
            ({"cell_id"}, "cell_id"), ({"enb_id"}, "enb_id"),
            ({"earfcn", "earfcn_dl"}, "earfcn"),
        ):
            if key not in native:
                acquired_cells = acquired_cells - matrix_cells_for_canonical_fields(frozenset(fields))
        return super()._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason=failure_reason,
            status_override=status,
        )

    def _capture_mikrotik_actionable_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
        configured_before_ts: float,
    ) -> BackendAcquisitionReceipt:
        if modem is None:
            return self._capture_backend_acquisition_receipt(
                request=request,
                modem=None,
                acquired_cells=frozenset(),
                failure_reason="mikrotik_actionable_acquisition_unavailable",
            )
        # Acquisition freshness is backend-private evidence. Public status is
        # intentionally filtered and must not be used to decide what this
        # RouterOS call physically read.
        acquisition_status = (
            self._last_status
            if isinstance(self._last_status, dict)
            else {}
        )
        acquired_cells = set(
            matrix_cells_for_canonical_fields(
                _MIKROTIK_FAST_TRAFFIC_FIELDS
            )
        )
        if acquisition_status.get("routeros_lte_monitor_fresh") is True:
            acquired_cells.update(
                matrix_cells_for_canonical_fields(self._subscription_acquired_fields)
            )
            acquired_cells.update(
                matrix_cells_for_canonical_fields(
                    _MIKROTIK_MONITOR_FIELDS
                )
            )
            acquired_cells.update(self._mikrotik_ca_acquired_cells())
            acquired_cells.update(self._mikrotik_nr_acquired_cells())
        configured_after_ts = float(
            getattr(self, "_last_configured_bands_ts", 0.0) or 0.0
        )
        if configured_after_ts > configured_before_ts:
            acquired_cells.update(CONFIGURED_BAND_CELLS)
        for _, _, values in self._portable_at_sources():
            acquired_cells.difference_update(self._portable_at_cells(values))
        if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS:
            acquired_cells.discard('slow.nr_ul_bandwidth_mhz')
        acquired_cells_frozen = frozenset(acquired_cells)
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells_frozen,
            failure_reason="mikrotik_actionable_acquisition_unavailable",
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "mikrotik_fast_live_acquisition_lane_required"
            )
        configured_before_ts = float(
            getattr(self, "_last_configured_bands_ts", 0.0) or 0.0
        )
        return self._capture_mikrotik_actionable_receipt(
            request=request,
            modem=self.read_actionable_modem_input(),
            configured_before_ts=configured_before_ts,
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "post_write":
            raise ValueError(
                "mikrotik_post_write_acquisition_lane_required"
            )
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError(
                "mikrotik_post_write_stage_evidence_required"
            )
        if not stage.permits_target_verification:
            raise ValueError(
                "mikrotik_post_write_accepted_stage_required"
            )
        if isinstance(stage.target, CoreNetworkSettingsTarget):
            return self._acquire_network_settings_readback(request)
        if isinstance(stage.target, CoreRadioModeTarget):
            status = self._radio_mode_status("readback")
            self._last_status.update(status)
            return self._capture_backend_acquisition_receipt(request=request, modem=None,
                acquired_cells=frozenset({"slow.radio_mode"}), status_override=status)
        configured_before_ts = float(
            getattr(self, "_last_configured_bands_ts", 0.0) or 0.0
        )
        modem = self.read_actionable_modem_input()
        return self._capture_mikrotik_actionable_receipt(
            request=request,
            modem=modem,
            configured_before_ts=configured_before_ts,
        )

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("mikrotik_maintenance_requires_owned_steps")

    def _iter_network_settings_observation(self):
        from arch4.modem.mikrotik_network_settings import iter_network_settings
        return iter_network_settings(self)

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        raise RuntimeError("mikrotik_maintenance_requires_owned_steps")

    def iter_acquire_live_maintenance(self, request: BackendAcquisitionRequest):
        """Each yield releases ModemLive's outer I/O lock before more work."""
        if request.lane != "slow_live":
            raise ValueError(
                "mikrotik_slow_live_acquisition_lane_required"
            )
        from arch4.modem.mikrotik_radio_mode import qualified
        if qualified(self):
            from arch4.modem.mikrotik_radio_mode import observations
            mode_reads = observations(self, "slow_live")
            try:
                for status in mode_reads:
                    if status is None:
                        yield None
                    else:
                        self._last_status.update(status)
                        yield self._capture_backend_acquisition_receipt(request=request, modem=None,
                            acquired_cells=frozenset({"slow.radio_mode"}), status_override=status)
            finally:
                mode_reads.close()
        work = self._iter_live_maintenance_work()
        try:
            while True:
                # Capture evidence before releasing the lock. A live read
                # between yields must not become this maintenance receipt's
                # claimed observation.
                before = self._mikrotik_acquisition_markers()
                finished = False
                try:
                    next(work)
                except StopIteration:
                    finished = True
                acquired_cells = self._mikrotik_acquired_cells_since(
                    before, include_monitor=False,
                )
                if acquired_cells:
                    yield self._capture_backend_acquisition_receipt(
                        request=request, modem=None, acquired_cells=acquired_cells,
                        failure_reason="", status_override=self._maintenance_status(),
                    )
                elif not finished:
                    yield None
                if finished:
                    return
        finally:
            work.close()

    def _iter_live_maintenance_work(self):
        if self._routeros:
            self._refresh_static_info()
            yield None
            for name, reader in (
                ("network", self._read_routeros_network_identity),
                ("antenna", self._read_routeros_antenna_identity),
            ):
                if name not in self._routeros_optional_identity_pending:
                    continue
                with self._physical_request_authority.lane_scope("boot_static"):
                    updates = reader()
                self._device_info_cache.update({
                    key: value for key, value in updates.items()
                    if value not in (None, "")
                })
                self._device_info_ts = time.time()
                self._routeros_optional_identity_pending.discard(name)
                yield None
            self._refresh_traffic_volume_cache()
            yield None
            yield from self._iter_routeros_watchdog_status()
            yield from self._iter_refresh_auxiliary_cache()
            yield None

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=2.0,
            live_register_stale_s=6.0,
            profile_name="routeros_api_monitor",
            has_explicit_actionable_snapshot=True,
            has_explicit_maintenance_refresh=True,
            maintenance_stepwise=True,
            backend_io_parallel_safe=False,
            io_serial_required=True,
            startup_parallel_allowed=False,
            runtime_action_requires_io_lock=True,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=2.5,
            maintenance_holds_io_lock=True,
            neighbor_reads_hold_io_lock=True,
            generic_neighbor_scan_enabled=False,
            fast_read_groups=("lte_monitor", "traffic_presence", "traffic_volume", "rtt_probe"),
            slow_read_groups=(
                "routeros_lte_state",
                "wifi_status",
                "ca_info",
                "serving_cell_extra",
                "mcs",
            ),
            static_capability_groups=(
                "router_identity",
                "routerboard_identity",
                "network_identity",
                "modem_identity",
                "firmware",
                "supported_bands",
                "antenna_capability",
                "antenna_status",
                "qam_capability",
            ),
            control_write_groups=(
                "routeros_lte_band_mask",
                "cell_lock",
                "restart",
                "wifi_toggle",
                "at_chat",
            ),
            export_groups=("sidecars", "support_bundle", "history"),
            static_capability_refresh_policy="identity_or_operator_refresh_only",
        )

    def _current_qcainfo(self) -> List[CarrierInfo]:
        if isinstance(self._last_qcainfo, dict):
            return list(self._last_qcainfo.get('carriers', []))
        return list(self._last_qcainfo)

    def _current_qnwcfg_mcs(self) -> Optional[Dict[str, int]]:
        return dict(self._last_qnwcfg_mcs) if self._last_qnwcfg_mcs is not None else None

    def _current_qeng_mcs(self) -> Optional[Dict[str, int]]:
        return dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None

    def _current_serving_cell_extra(self) -> Dict[str, Any]:
        return dict(self._last_serving_cell_extra) if self._last_serving_cell_extra else {}

    def _current_wifi_status(self) -> tuple[Optional[bool], Optional[bool], Optional[bool]]:
        return tuple(self._last_wifi_status)

    def _current_configured_bands(self) -> List[int]:
        return list(getattr(self, "_last_configured_bands", []) or [])

    def _current_configured_bands_live(self) -> List[int]:
        """Return current RouterOS band mask, refreshing when its live cache is due."""
        try:
            return self._refresh_configured_band_cache(force=False)
        except Exception as exc:
            log.debug("configured band cache refresh suppressed: %s", exc)
        return self._current_configured_bands()

    def _set_configured_band_cache(self, bands: List[int], *, status: str = "fresh") -> None:
        self._last_configured_bands = sorted({int(b) for b in bands if int(b) > 0})
        self._last_configured_bands_ts = time.time()
        self._last_configured_bands_status = str(status or "fresh")
        self._last_configured_bands_error = ""

    def _mark_configured_band_cache_unavailable(self, reason: str) -> None:
        reason_text = str(reason or "unavailable").strip() or "unavailable"
        self._last_configured_bands_error = reason_text
        self._last_configured_bands_status = "stale" if self._current_configured_bands() else "unavailable"

    def _refresh_configured_band_cache(self, force: bool = False) -> List[int]:
        now = time.time()
        if (
            not force
            and float(getattr(self, "_last_configured_bands_ts", 0.0) or 0.0) > 0
            and (now - float(getattr(self, "_last_configured_bands_ts", 0.0) or 0.0)) < self._configured_bands_refresh_s
        ):
            return self._current_configured_bands()
        self._refresh_routeros_lte_state_cache(
            force=force,
            refresh_bands=True,
            refresh_disabled=False,
        )
        return self._current_configured_bands()

    def _refresh_auxiliary_cache(self, *, prefer_qeng: bool = False) -> None:
        for _ in self._iter_refresh_auxiliary_cache(prefer_qeng=prefer_qeng):
            pass

    def _iter_refresh_auxiliary_cache(self, *, prefer_qeng: bool = False):
        """Refresh RouterOS state, required serving-cell facts, and optional AT facts.

        The QENG serving-cell read is not optional enrichment on the exact
        RouterOS/Quectel lane: it is the active source for profile-supported
        TAC and UL bandwidth when RouterOS monitor omits them.  Other AT-heavy
        readers remain behind the explicit opt-in switch.
        """
        if not self._routeros:
            return
        now = time.time()

        def _due(last_ts: float, interval_s: float, *, empty: bool = False) -> bool:
            if empty:
                return True
            return (now - float(last_ts or 0.0)) >= max(0.5, float(interval_s or 0.0))

        routeros_tasks: set[str] = set()
        at_tasks: set[str] = set()
        if self._routeros_identity_due(now):
            routeros_tasks.add("routeros_identity")
        if _due(
            self._last_configured_bands_ts,
            self._configured_bands_refresh_s,
            empty=not self._last_configured_bands,
        ) or _due(self._last_lte_disabled_ts, self._lte_disabled_refresh_s):
            routeros_tasks.add("lte_state")
        if _due(self._last_wifi_status_ts, self._wifi_status_interval_s):
            routeros_tasks.add("wifi")

        for task_name in self._AT_IDENTITY_COMMANDS:
            if self._at_identity_due(task_name, now):
                at_tasks.add(task_name)
        if prefer_qeng and _due(self._last_qeng_mcs_ts, self._qeng_mcs_interval_s, empty=self._last_qeng_mcs is None):
            at_tasks.add("qeng")
        if _due(self._last_qcainfo_ts, self._qcainfo_interval_s, empty=not self._last_qcainfo):
            at_tasks.add("qcainfo")
        if _due(self._last_qnwcfg_mcs_ts, self._qnwcfg_mcs_interval_s, empty=self._last_qnwcfg_mcs is None):
            at_tasks.add("qnwcfg")
        if _due(
            self._last_serving_cell_extra_ts,
            self._serving_cell_extra_interval_s,
            empty=not self._last_serving_cell_extra,
        ):
            at_tasks.add("serving")
        if (not prefer_qeng) and _due(self._last_qeng_mcs_ts, self._qeng_mcs_interval_s, empty=self._last_qeng_mcs is None):
            at_tasks.add("qeng")

        for task_name in self._ROUTEROS_BATCH_ORDER:
            if task_name not in routeros_tasks:
                continue
            if task_name == "routeros_identity":
                self._refresh_static_info(force=False)
            elif task_name == "lte_state":
                self._refresh_routeros_lte_state_cache(force=True)
            elif task_name == "wifi":
                self._read_wifi_status()
            yield None

        if not self._at_live_polling_enabled:
            at_tasks.intersection_update({"serving"})

        if not at_tasks:
            return

        task = ""
        order = self._AUX_REFRESH_ORDER
        if prefer_qeng and "qeng" in at_tasks:
            task = "qeng"
        else:
            start = int(self._aux_refresh_rr % len(order))
            for offset in range(len(order)):
                idx = (start + offset) % len(order)
                candidate = order[idx]
                if candidate in at_tasks:
                    task = candidate
                    self._aux_refresh_rr = (idx + 1) % len(order)
                    break
        if not task:
            return

        if task == "qcainfo":
            self._read_qcainfo()
        elif task in self._AT_IDENTITY_COMMANDS:
            self._refresh_at_identity_field(task)
        elif task == "qnwcfg":
            self._read_qnwcfg_mcs()
        elif task == "serving":
            self._read_serving_cell_extra()
        elif task == "wifi":
            self._read_wifi_status()
        elif task == "qeng":
            self._read_qeng_mcs()

    def _lte_monitor(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Return one serialized RouterOS LTE monitor snapshot."""

        with self._lte_monitor_lock:
            return self._lte_monitor_serialized(force=force)

    def _lte_monitor_serialized(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Read /interface/lte/monitor for lte1 (throttled unless forced)."""
        now = time.time()
        if (
            not force
            and self._last_lte_monitor is not None
            and (now - self._last_lte_monitor_ts) < max(0.5, self._lte_monitor_interval_s)
        ):
            cached = dict(self._last_lte_monitor)
            cached["_routeros_lte_monitor_cached"] = True
            cached["_routeros_lte_monitor_fresh"] = False
            cached["_routeros_lte_monitor_age_s"] = max(0.0, now - self._last_lte_monitor_ts)
            return cached
        try:
            # RouterOS API monitor call with .id
            monitor = self._routeros.get_binary_resource(self._portable_routeros_path('/interface/lte/monitor','/interface/lte'))
            response = monitor.call("monitor", {"once": "", ".id": self._lte_id})

            if not response:
                return None

            # RouterOS binary_resource returns bytes keys AND values.
            # Decode everything to str before parsing.
            row = _decode_row(response[0])
            result = parse_routeros_lte_monitor_row(row, parse_uptime=self._parse_uptime)

            replies = {"/interface/lte/monitor": row}
            scalar_keys = {"rf.pci":"pci", "rf.cell_id":"cell_id", "rf.enb_id":"enb_id",
                           "rf.earfcn_dl":"earfcn", "slow.tac":"tac", "rf.ri":"ri",
                           "rf.cqi0":"cqi", "slow.dl_bandwidth_mhz":"dl_bw_mhz",
                           "radio.serving_band":"primary_band",
                           "identity.imei":"imei", "identity.imsi":"imsi",
                           "identity.firmware":"firmware",
                           "health.radio_connected":"radio_connected",
                           "radio.network_type":"network_type",
                           "traffic.session_uptime_s":"session_uptime_s",
                           "identity.provider_name":"operator",
                           "identity.provider_plmn":"provider_plmn"}
            addresses = {"rf.sinr_db", "rf.rsrp_dbm", "rf.rsrq_db", "rf.rssi_dbm"} | set(scalar_keys)
            nr_profile = self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS
            nr_current = (nr_profile and len(response) == 1
                          and (not row.get(".id") or row[".id"] == self._lte_id))
            if nr_profile:
                scalar_keys.update(_MIKROTIK_NR_MONITOR_SCALARS)
            if nr_current:
                addresses.update(_MIKROTIK_NR_MONITOR_SCALARS)
            if nr_profile:
                addresses.difference_update(set(_MIKROTIK_NR_MONITOR_SCALARS) | {'radio.network_type'})
            issues = []
            rf = decode_native_profile(self._definition,
                replies, {"signal_scales": self._signal_scales,
                    "routeros_nr_current_interface": bool(nr_current),
                    "provider_names": dict(lookup_operator_context().provider_plmn_map),
                    "provider_plmn_by_name": {str(value).lower(): plmn
                        for value in row.values()
                        if (plmn := resolve_provider_plmn_from_name(str(value or '')))}},
                addresses=addresses & self._definition.available_addresses(replies), issues=issues,
            )
            if nr_current:
                # Preserve raw type for this NR-only pass. The established LTE
                # pass still receives the original string-decoded native row.
                nr_row = dict(row)
                for key, value in response[0].items():
                    if (key.decode() if isinstance(key, bytes) else str(key)) == 'ca-band':
                        if isinstance(value, (list, dict)):
                            nr_row['ca-band'] = value
                nr_replies = {'/interface/lte/monitor': nr_row}
                nr_context = {'routeros_nr_current_interface': True,
                              'routeros_nr_read_source': 'monitor'}
                nr_addresses = set(_MIKROTIK_NR_MONITOR_SCALARS) | {'radio.network_type'}
                rf.update(decode_native_profile(self._definition, nr_replies, nr_context,
                    addresses=self._definition.available_addresses(nr_replies,
                        context=nr_context, addresses=nr_addresses), issues=issues))
            result["_profile_rf_values"] = rf
            if nr_profile:
                result["_routeros_nr_current_interface"] = bool(nr_current)
                result['_routeros_nr_declared_rat'] = str(row.get('data-class') or '').strip().upper()
                # None is current missing, never an invitation to reuse a
                # previous carrier or classify a label-only NR failure.
                result.update({key: None for key in _MIKROTIK_NR_MONITOR_SCALARS.values()})
            # Feed imported native scalars into the same serialized monitor
            # snapshot before identity merge, stale guard and publication.
            # No rule for this read leaves the existing native branch intact.
            for address,key in scalar_keys.items():
                if address not in rf:
                    continue
                if rf[address] is None:
                    result.pop(key,None)
                else:
                    result[key]=rf[address]
            if nr_current and result.get('_routeros_nr_declared_rat') in {'5G NSA', '5G SA'}:
                # Provenance of this existing native request, distinct from
                # whether its LTE RF snapshot can be admitted. No new clock
                # owner: observed_at is this monitor call's existing timestamp.
                result['_routeros_nr_native_context'] = {
                    'identity': self._routeros_nr_context(result),
                    'rat': result['_routeros_nr_declared_rat'], 'observed_at': now}
            from arch4.modem_live.profile_diagnostics import field_diagnostic
            diagnostic = field_diagnostic(self._definition,
                {issue.address: issue for issue in issues},
                previous=(self._last_lte_monitor or {}).get("_profile_rf_diagnostic"))
            if diagnostic is not None:
                result["_profile_rf_diagnostic"] = diagnostic

            result["_routeros_lte_monitor_cached"] = False
            result["_routeros_lte_monitor_fresh"] = True
            result["_routeros_lte_monitor_age_s"] = 0.0
            audit_result = dict(result)
            for metric, address in (
                ("sinr", "rf.sinr_db"), ("rsrp", "rf.rsrp_dbm"),
                ("rsrq", "rf.rsrq_db"), ("rssi", "rf.rssi_dbm"),
            ):
                audit_result.pop(metric, None)
                if rf.get(address) is not None:
                    audit_result[metric] = rf[address]
            invalid_reason = self._routeros_lte_monitor_invalid_snapshot(audit_result, row, now=now)
            if audit_result.get('_routeros_nr_no_lte'):
                result['_routeros_nr_no_lte'] = True
            if invalid_reason:
                if nr_profile and self._last_lte_monitor is not None:
                    context = result.get('_routeros_nr_native_context')
                    if (context and context['rat'] == '5G SA'
                            and context['identity'][2] and context['identity'][3]
                            and context['identity'] == self._routeros_nr_context(self._last_lte_monitor)):
                        # A same-device/SIM SA row can provide fresh selected
                        # context while its absent LTE RF stays rejected. This
                        # does not admit any default RF or label-only NR fact.
                        context['partial_sa'] = True
                        context['has_nr_values'] = any(rf.get(key) is not None
                            for key in _MIKROTIK_NR_MONITOR_SCALARS
                            if key not in {'radio.nr_mode', 'radio.nr5g_active'})
                        self._last_lte_monitor['_routeros_nr_native_context'] = context
                    else:
                        self._last_lte_monitor.pop('_routeros_nr_native_context', None)
                return self._routeros_lte_monitor_cached_after_invalid_snapshot(invalid_reason, row, now=now)
            if self._routeros_lte_monitor_stale(audit_result, now=now):
                if nr_profile and self._last_lte_monitor is not None:
                    self._last_lte_monitor.pop('_routeros_nr_native_context', None)
                cached = dict(self._last_lte_monitor or result)
                cached["_routeros_lte_monitor_cached"] = True
                cached["_routeros_lte_monitor_fresh"] = False
                cached["_routeros_lte_monitor_age_s"] = max(
                    0.0,
                    now - float(self._last_lte_monitor_ts or 0.0),
                )
                cached["_routeros_lte_monitor_error"] = (
                    "routeros_lte_monitor_stale"
                )
                return cached
            self._merge_lte_monitor_identity(result, observed_at=now)
            try:
                guard_status = self._audit_routeros_qnwlock_guard(result)
                if guard_status:
                    result["qnwlock_guard"] = dict(guard_status)
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                log.debug("RouterOS QNWLOCK guard audit suppressed: %s", exc)
            self._last_lte_monitor = dict(result)
            self._last_lte_monitor_ts = now
            return result

        except (RouterOsTransportError, RouterOsProtocolError) as e:
            log.error("lte_monitor failed: %s", e)
            if self._last_lte_monitor is not None:
                self._last_lte_monitor.pop('_routeros_nr_native_context', None)
            if isinstance(e, RouterOsTransportError):
                self._mark_transport_failure("lte_monitor", e)
                return None
            if self._last_lte_monitor is not None:
                cache_age = max(0.0, now - float(self._last_lte_monitor_ts or 0.0))
                max_cache_age = max(2.0, float(self._lte_monitor_interval_s or 0.0) * 2.0)
                if cache_age <= max_cache_age:
                    cached = dict(self._last_lte_monitor)
                    cached["_routeros_lte_monitor_cached"] = True
                    cached["_routeros_lte_monitor_fresh"] = False
                    cached["_routeros_lte_monitor_age_s"] = cache_age
                    cached["_routeros_lte_monitor_error"] = str(e)
                    return cached
                reason = f"{type(e).__name__}: {e}"
                if (
                    (now - float(self._last_lte_monitor_failure_ts or 0.0)) > 5.0
                    or reason != self._last_lte_monitor_failure_reason
                ):
                    log.warning(
                        "lte_monitor cache stale after failed refresh; rejecting cached RouterOS RF "
                        "sample age=%.1fs max=%.1fs error=%s",
                        cache_age,
                        max_cache_age,
                        reason,
                    )
                self._last_lte_monitor_failure_ts = now
                self._last_lte_monitor_failure_reason = reason
            return None

    def _routeros_lte_monitor_invalid_snapshot(
        self,
        result: Dict[str, Any],
        raw_row: Dict[str, Any],
        *,
        now: float,
    ) -> str:
        """Detect connected RouterOS monitor rows missing serving-cell truth.

        RouterOS can still answer ``monitor once`` while the LTE monitor payload
        temporarily omits serving-cell identity and may expose only exact floor
        RF values. Treating that row as live creates fake RF cliffs:
        SINR=0, RSRP=-140, band/PCI/EARFCN=0.
        """
        del now
        if not bool(result.get("radio_connected", False)):
            return ""
        if bool(result.get("_routeros_invalid_earfcn", False)):
            return "connected_monitor_invalid_earfcn"
        identity_values = (
            result.get("primary_band", 0),
            result.get("pci", 0),
            result.get("earfcn", 0),
            result.get("cell_id", 0),
            result.get("enb_id", 0),
            result.get("active_band_set") or [],
        )
        for value in identity_values:
            try:
                if isinstance(value, (list, tuple, set)):
                    if any(int(v or 0) > 0 for v in value):
                        return ""
                elif int(value or 0) > 0:
                    return ""
            except Exception:
                if value:
                    return ""
        if (self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS
                and result.get('_routeros_nr_current_interface') is True
                and result.get('_routeros_nr_declared_rat') == '5G SA'
                and (all(result.get(key) is not None for key in
                         ('nr_rsrp_dbm', 'nr_rsrq_db', 'nr_sinr_db'))
                     or (self._routeros_nr_at_selected(result)
                         and self._last_serving_cell_extra.get('_profile_values', {}).get('radio.nr5g_active') is True
                         and self._last_serving_cell_extra.get('_profile_values', {}).get('radio.nr_mode') == 'SA'))):
            # Only complete fresh same-response NR permits missing LTE. No
            # label-only/empty monitor exception, and no invented LTE identity.
            result['_routeros_nr_no_lte'] = True
            return ""
        has_rf = any(key in result for key in ("sinr", "rsrp", "rsrq", "rssi"))
        if has_rf:
            try:
                sinr = float(result.get("sinr", 0.0) or 0.0)
                rsrp = float(result.get("rsrp", 0.0) or 0.0)
                rsrq = float(result.get("rsrq", 0.0) or 0.0)
                rssi = float(result.get("rssi", 0.0) or 0.0)
            except Exception:
                return ""
            floor_rf = sinr <= 0.0 and rsrp <= -139.0 and rsrq <= -19.0 and rssi <= -99.0
            if not floor_rf:
                return ""
        raw_keys = {str(key or "").strip().lower() for key in raw_row.keys()}
        if raw_keys & {"status", "session-uptime", "registration-status"}:
            return "connected_monitor_missing_rf_identity"
        return ""

    def _routeros_lte_monitor_cached_after_invalid_snapshot(
        self,
        reason: str,
        raw_row: Dict[str, Any],
        *,
        now: float,
    ) -> Optional[Dict[str, Any]]:
        cache_age = max(0.0, now - float(self._last_lte_monitor_ts or 0.0))
        max_cache_age = max(30.0, float(self._lte_monitor_interval_s or 0.0) * 10.0)
        key_list = ",".join(sorted(str(key or "") for key in raw_row.keys()))
        if self._last_lte_monitor is not None and cache_age <= max_cache_age:
            cached = dict(self._last_lte_monitor)
            cached["_routeros_lte_monitor_cached"] = True
            cached["_routeros_lte_monitor_fresh"] = False
            cached["_routeros_lte_monitor_age_s"] = cache_age
            cached["_routeros_lte_monitor_invalid_snapshot"] = True
            cached["_routeros_lte_monitor_error"] = reason
            if (now - float(getattr(self, "_last_lte_monitor_invalid_warn_ts", 0.0) or 0.0)) > 10.0:
                log.warning(
                    "RouterOS LTE monitor invalid partial snapshot; holding last good RF "
                    "age=%.1fs max=%.1fs reason=%s raw_keys=%s",
                    cache_age,
                    max_cache_age,
                    reason,
                    key_list,
                )
                self._last_lte_monitor_invalid_warn_ts = now
            self._last_lte_monitor_failure_ts = now
            self._last_lte_monitor_failure_reason = reason
            return cached

        if (now - float(getattr(self, "_last_lte_monitor_invalid_warn_ts", 0.0) or 0.0)) > 10.0:
            log.warning(
                "RouterOS LTE monitor invalid partial snapshot; rejecting RF sample "
                "cache_age=%.1fs max=%.1fs reason=%s raw_keys=%s",
                cache_age,
                max_cache_age,
                reason,
                key_list,
            )
            self._last_lte_monitor_invalid_warn_ts = now
        self._last_lte_monitor_failure_ts = now
        self._last_lte_monitor_failure_reason = reason
        return None

    def _routeros_lte_monitor_stale(self, result: Dict[str, Any], *, now: float) -> bool:
        """Mark an unchanged RouterOS RF lane stale without dropping transport."""
        try:
            session_uptime_s = float(result.get("session_uptime_s", 0.0) or 0.0)
        except Exception:
            session_uptime_s = 0.0
        if session_uptime_s <= 0.0 or not bool(result.get("radio_connected", False)):
            self._last_lte_monitor_refresh_fingerprint = ""
            self._last_lte_monitor_refresh_seen_ts = 0.0
            return False

        bands = normalize_band_list(result.get("active_band_set") or [])
        fingerprint = "|".join(
            [
                str(int(result.get("primary_band", 0) or 0)),
                str(int(result.get("pci", 0) or 0)),
                str(int(result.get("earfcn", 0) or 0)),
                str(int(result.get("cell_id", 0) or 0)),
                "+".join(str(int(b)) for b in bands),
                f"{float(result.get('sinr', 0.0) or 0.0):.1f}",
                f"{float(result.get('rsrp', 0.0) or 0.0):.1f}",
                f"{float(result.get('rsrq', 0.0) or 0.0):.1f}",
                f"{float(result.get('rssi', 0.0) or 0.0):.1f}",
                f"{session_uptime_s:.1f}",
            ]
        )
        last_fingerprint = str(getattr(self, "_last_lte_monitor_refresh_fingerprint", "") or "")
        if fingerprint != last_fingerprint:
            self._last_lte_monitor_refresh_fingerprint = fingerprint
            self._last_lte_monitor_refresh_seen_ts = now
            return False

        stale_age_s = max(0.0, now - float(getattr(self, "_last_lte_monitor_refresh_seen_ts", 0.0) or now))
        stale_threshold_s = max(8.0, float(self._lte_monitor_interval_s or 0.0) * 3.0)
        if stale_age_s < stale_threshold_s:
            return False

        if (now - float(getattr(self, "_last_lte_monitor_stale_warn_ts", 0.0) or 0.0)) > 10.0:
            log.warning(
                "RouterOS LTE monitor stale: rejecting unchanged monitor row "
                "stale_age=%.1fs threshold=%.1fs session_uptime=%.1fs band=%s pci=%s",
                stale_age_s,
                stale_threshold_s,
                session_uptime_s,
                result.get("primary_band"),
                result.get("pci"),
            )
        self._last_lte_monitor_stale_warn_ts = now
        self._last_lte_monitor_failure_ts = now
        self._last_lte_monitor_failure_reason = "routeros_lte_monitor_stale"
        return True

    def _read_qcainfo(self) -> List[CarrierInfo]:
        """Read CA info via AT+QCAINFO with proper timing."""
        now = time.time()
        if self._last_qcainfo and (now - self._last_qcainfo_ts) < max(1.0, self._qcainfo_interval_s):
            return self._current_qcainfo()
        raw = self._at_command("AT+QCAINFO")
        if not raw or "ERROR" in raw:
            return self._current_qcainfo()
        lte_raw = raw
        if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS:
            lte_rows = []
            for line in raw.splitlines():
                parts = line.split(':', 1)[-1].split(',')
                if (line.strip().startswith('+QCAINFO:') and len(parts) > 3
                        and re.fullmatch(r'\s*"NR5G BAND [0-9]{1,3}"\s*', parts[3])):
                    continue
                lte_rows.append(line)
            lte_raw = '\n'.join(lte_rows)
        carriers = _parse_qcainfo(lte_raw)
        monitor = self._last_lte_monitor or {}
        if self._routeros_nr_monitor_context_current(monitor):
            values = self._profile_native_values({'AT+QCAINFO': raw},
                {'slow.nr_ul_bandwidth_mhz'}, context={'routeros_nr_read_source': 'qcainfo'})
            if carriers or values.get('slow.nr_ul_bandwidth_mhz') is not None:
                self._last_qcainfo = {'carriers': list(carriers), '_profile_values': values,
                    '_routeros_nr_context': self._routeros_nr_context(monitor)}
                self._last_qcainfo_ts = now
                return list(carriers)
            # Empty/unknown replies are not a successful new snapshot. Keep
            # the legacy carrier getter/clock and withhold the missing UL field.
            if isinstance(self._last_qcainfo, dict):
                self._last_qcainfo['_profile_values'] = values
            return self._current_qcainfo()
        if carriers:
            self._last_qcainfo = list(carriers)
            self._last_qcainfo_ts = now
            return carriers
        return self._current_qcainfo()

    @staticmethod
    def _parse_routeros_counter_bytes(value: Any) -> Optional[int]:
        if value is None:
            return None
        text = str(value).strip().replace(" ", "")
        if not text:
            return None
        match = re.match(r"^(\d+)", text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_trans_mode(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"(?:TM\s*)?\[?\s*([1-9])\s*\]?", text, flags=re.IGNORECASE)
        if not match:
            return text
        num = match.group(1)
        base = f"TM[{num}]"
        label = TRANSMODE_MAP.get(num, "")
        return f"{base} ({label})" if label else base

    def _portable_routeros_path(self, alias, native):
        definition=getattr(self,'_definition',None)
        if definition is None or not definition.has_request(alias):return native
        return definition.request(alias,protocol='routeros_api')['path']

    def _portable_at_sources(self):
        """Imported alternate inputs stay with their existing cache/clock."""
        for name,cache,stamp in (
                ('qcainfo', self._last_qcainfo if isinstance(self._last_qcainfo, dict) else {}, self._last_qcainfo_ts),
                ('qnwcfg',self._last_qnwcfg_mcs,self._last_qnwcfg_mcs_ts),
                ('qeng',self._last_qeng_mcs,self._last_qeng_mcs_ts),
                ('serving',self._last_serving_cell_extra,self._last_serving_cell_extra_ts)):
            values=(cache or {}).get('_profile_values',{})
            if name == 'qcainfo' and not self._routeros_nr_qcainfo_selected():
                values = {}
            if any(key in _MIKROTIK_NR_MONITOR_SCALARS for key in values):
                if not self._routeros_nr_at_selected(self._last_lte_monitor or {}):
                    values = {key: value for key, value in values.items()
                              if key not in set(_MIKROTIK_NR_MONITOR_SCALARS) | {'radio.network_type'}}
            if values:yield name,float(stamp or 0.),values

    def _routeros_nr_context(self, monitor):
        return (self._definition.profile_id, self._lte_id, str(monitor.get('imei') or ''),
                str(monitor.get('imsi') or ''))

    def _routeros_nr_monitor_context_current(self, monitor):
        context = monitor.get('_routeros_nr_native_context') or {}
        stamp = float(context.get('observed_at') or 0.)
        return bool(self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS
            and monitor.get('_routeros_nr_current_interface') is True
            and context.get('identity') == self._routeros_nr_context(monitor)
            and context.get('rat') in {'5G NSA', '5G SA'}
            and context['identity'][2] and context['identity'][3]
            and stamp > 0. and 0. <= time.time() - stamp <= max(2., self._lte_monitor_interval_s * 2.)
            and stamp >= float(self._last_lte_monitor_failure_ts or 0.))

    def _routeros_nr_qcainfo_selected(self):
        """One QCA snapshot's UL field cannot cross its native context."""
        monitor = self._last_lte_monitor or {}
        cache = self._last_qcainfo
        stamp = float(self._last_qcainfo_ts or 0.)
        return bool(self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS
            and isinstance(cache, dict) and stamp > 0.
            and 0. <= time.time() - stamp <= max(1., self._qcainfo_interval_s)
            and self._routeros_nr_monitor_context_current(monitor)
            and self._routeros_nr_context(monitor)[3]
            and cache.get('_routeros_nr_context') == self._routeros_nr_context(monitor))

    def _routeros_nr_at_selected(self, monitor):
        """The existing serving cache may supply one whole alternate payload."""
        if not self._routeros_nr_monitor_context_current(monitor):
            return False
        context = monitor['_routeros_nr_native_context']
        values = monitor.get('_profile_rf_values', {})
        if (context.get('has_nr_values') if context.get('partial_sa') else
                any(values.get(key) is not None for key in _MIKROTIK_NR_MONITOR_SCALARS
                    if key not in {'radio.nr_mode', 'radio.nr5g_active'})):
            return False
        cache = self._last_serving_cell_extra
        stamp = float(self._last_serving_cell_extra_ts or 0.)
        return bool(stamp > 0. and 0. <= time.time() - stamp <= self._serving_cell_extra_interval_s
            and cache.get('_routeros_nr_context') == self._routeros_nr_context(monitor)
            and self._routeros_nr_context(monitor)[3]
            and cache.get('_profile_values', {}).get('radio.nr_mode') == context['rat'][3:]
            and any(value is not None for key, value in cache.get('_profile_values', {}).items()
                    if key in _MIKROTIK_NR_MONITOR_SCALARS and key != 'radio.nr_mode'))

    def _portable_at_values(self):
        result={}
        for _,_,values in sorted(self._portable_at_sources(),key=lambda row:row[1]):
            result.update(values)
        return result

    @staticmethod
    def _portable_at_cells(values):
        names={address.split('.',1)[1]for address in values}
        if 'rf.mcs_dl0'in values:names.add('mcs_dl')
        return frozenset(values)|matrix_cells_for_canonical_fields(frozenset(names))

    def _profile_native_values(self, replies, addresses, context=None):
        """Convert only replies already read by the current native owner."""
        values = decode_native_profile(self._definition, replies, context,
            addresses=self._definition.available_addresses(replies,context=context,addresses=addresses),
            issues=[])
        # Retained identity remains available for display; an unrelated static
        # update must not certify a missing corrected field as freshly read.
        self._profile_unavailable_native = (
            getattr(self, '_profile_unavailable_native', frozenset()).difference(values)
            | frozenset(address for address,value in values.items()if value is None))
        return values

    def _read_counter_period_started_at(self) -> Optional[float]:
        """Read boot evidence before counters, never infer it from LTE uptime.

        One read on the existing minute counter lane. The conservative lower
        bound accounts for whole-second uptime resolution and request latency.
        A reboot between this read and stats cannot falsely qualify a reset.
        """
        started = time.time()
        try:
            rows = self._routeros.get_resource(self._portable_routeros_path('/system/resource','/system/resource')).get() or []
        except (RouterOsTransportError, RouterOsProtocolError) as exc:
            log.debug("counter period evidence unavailable: %s", exc)
            return None
        if not rows:
            return None
        raw = str(_decode_row(rows[0]).get("uptime") or "").strip()
        uptime = parse_routeros_duration_seconds(raw)
        if uptime is None:
            return None
        return max(0.0, started - uptime - 1.0) or None

    def _traffic_interface_started_at(self, *, sampled_at: float) -> Optional[float]:
        """Bind fresh native session uptime to the existing minute counter read."""
        monitor = dict(getattr(self, "_last_lte_monitor", None) or {})
        observed_at = float(getattr(self, "_last_lte_monitor_ts", 0.0))
        max_age = max(2.0, float(getattr(self, "_lte_monitor_interval_s", 0.0)) * 2.0)
        if (not monitor.get("radio_connected") or observed_at <= 0.0
                or not 0.0 <= sampled_at - observed_at <= max_age
                or float(getattr(self, "_last_lte_monitor_failure_ts", 0.0)) > observed_at):
            return None
        profile_values = monitor.get('_profile_rf_values', {})
        uptime = (profile_values['traffic.session_uptime_s']
            if 'traffic.session_uptime_s' in profile_values else
            parse_routeros_duration_seconds(monitor.get("session_uptime_raw", "")))
        if uptime is None:
            return None
        return max(0.0, observed_at - uptime - 1.0) or None

    def _publish_traffic_counter_cache(
        self,
        *,
        routeros_dl: Optional[int],
        routeros_ul: Optional[int],
        sampled_at: float,
        status: str,
        period_started_at: Optional[float] = None,
        interface_started_at: Optional[float] = None,
    ) -> None:
        next_sample = sampled_at + MIKROTIK_TRAFFIC_COUNTER_POLL_INTERVAL_S
        self._traffic_volume_cache = {
            "traffic_total_download_bytes": int(routeros_dl or 0),
            "traffic_total_upload_bytes": int(routeros_ul or 0),
            "traffic_session_download_bytes": int(self._traffic_session_download_bytes),
            "traffic_session_upload_bytes": int(self._traffic_session_upload_bytes),
            "traffic_device_download_counter_bytes": routeros_dl,
            "traffic_device_upload_counter_bytes": routeros_ul,
            "traffic_counter_period_started_at": period_started_at,
            "traffic_counter_interface_started_at": interface_started_at,
            "traffic_accounting_source": "routeros_interface_stats",
            "traffic_accounting_status": str(status or "not_sampled"),
            "traffic_volume_poll_interval_s": MIKROTIK_TRAFFIC_COUNTER_POLL_INTERVAL_S,
            "traffic_volume_last_sample_ts": sampled_at,
            "traffic_volume_next_sample_ts": next_sample,
            "traffic_volume_age_s": 0.0,
            "traffic_rate_source": "routeros_monitor_traffic",
            "traffic_quota_start_day": None,
            "traffic_quota_limit_label": "",
            "traffic_quota_left_bytes": None,
            "traffic_quota_limit_bytes": None,
        }
        self._last_status.update(self._traffic_volume_cache)
        self._traffic_volume_ts = sampled_at

    def _read_interface_byte_counters(self) -> tuple[Optional[int], Optional[int]]:
        """Read RouterOS-owned cumulative interface byte counters for lte1."""
        if not self._routeros:
            return None, None
        try:
            binary = self._routeros.get_binary_resource(self._portable_routeros_path('/interface/print_stats','/interface'))
            rows = binary.call("print", {"stats": ""}, {"name": "lte1"}) or []
            for raw_row in rows or []:
                row = _decode_row(raw_row)
                name = str(row.get("name") or "").strip().lower()
                row_id = str(row.get(".id") or row.get("id") or "").strip()
                if name and name != "lte1":
                    continue
                if not name and self._lte_id and row_id and row_id != str(self._lte_id):
                    continue
                rx_total = None
                tx_total = None
                for key in ("rx-byte", "rx-bytes", "rx-byte-total", "rx-total-byte"):
                    rx_total = self._parse_routeros_counter_bytes(row.get(key))
                    if rx_total is not None:
                        break
                for key in ("tx-byte", "tx-bytes", "tx-byte-total", "tx-total-byte"):
                    tx_total = self._parse_routeros_counter_bytes(row.get(key))
                    if tx_total is not None:
                        break
                values = self._profile_native_values({'/interface/print_stats': row}, {
                    'traffic.traffic_device_download_counter_bytes',
                    'traffic.traffic_device_upload_counter_bytes'})
                from arch4.modem_live.profile_definition.results import admit
                def counter(address, native):
                    if address not in values:return native
                    try:value=admit(values[address], 'integer')
                    except ValueError:return None
                    return value if value is not None and value>=0 else None
                rx_total = counter('traffic.traffic_device_download_counter_bytes', rx_total)
                tx_total = counter('traffic.traffic_device_upload_counter_bytes', tx_total)
                if rx_total is not None and tx_total is not None:
                    return rx_total, tx_total
        except (RouterOsTransportError, RouterOsProtocolError) as e:
            log.debug("interface byte counter read unavailable: %s", e)
            if isinstance(e, RouterOsTransportError):
                self._mark_transport_failure("interface_byte_counters", e)
        return None, None

    def _refresh_traffic_volume_cache(self, *, force: bool = False) -> None:
        """Publish current RouterOS counters; Foundation owns durable accounting."""
        now = time.time()
        if (
            not force
            and self._traffic_volume_ts > 0.0
            and (now - self._traffic_volume_ts)
            < MIKROTIK_TRAFFIC_COUNTER_POLL_INTERVAL_S
        ):
            return
        period_started_at = self._read_counter_period_started_at()
        rx_total, tx_total = self._read_interface_byte_counters()
        if rx_total is None or tx_total is None:
            return
        # Judge cached monitor freshness at counter-read start, so latency of
        # this existing minute read cannot expire otherwise current evidence.
        interface_started_at = self._traffic_interface_started_at(sampled_at=now)

        had_baseline = bool(
            self._traffic_counter_last_download_bytes is not None
            or self._traffic_counter_last_upload_bytes is not None
        )
        previous_at = self._traffic_counter_accepted_at
        increment = counter_pair_increment(
            (self._traffic_counter_last_download_bytes, self._traffic_counter_last_upload_bytes)
            if had_baseline else None,
            (rx_total, tx_total),
            previous_at=previous_at,
            observed_at=time.time(),
            period_started_at=period_started_at,
            interface_started_at=interface_started_at,
        )
        counter_restarted = bool(had_baseline and increment is not None and (
            (period_started_at is not None and previous_at < period_started_at)
            or (interface_started_at is not None and previous_at < interface_started_at
                and rx_total < self._traffic_counter_last_download_bytes)))
        if increment is not None:
            self._traffic_counter_last_download_bytes = rx_total
            self._traffic_counter_last_upload_bytes = tx_total
            self._traffic_counter_accepted_at = now
            self._traffic_session_download_bytes += increment[0]
            self._traffic_session_upload_bytes += increment[1]
        status = (
            "baseline"
            if not had_baseline
            else "counter_discontinuity"
            if increment is None
            else "counter_reset"
            if counter_restarted
            else "updated"
        )
        self._publish_traffic_counter_cache(
            routeros_dl=rx_total,
            routeros_ul=tx_total,
            sampled_at=now,
            status=status,
            period_started_at=period_started_at,
            interface_started_at=interface_started_at,
        )

    def _read_traffic(self) -> tuple:
        """Read interface traffic counters and compute bytes/sec."""
        self._last_traffic_profile_values = {
            'traffic.rx_bytes_per_sec': None, 'traffic.tx_bytes_per_sec': None}
        try:
            res = self._routeros.get_binary_resource(self._portable_routeros_path('/interface/monitor-traffic','/interface'))
            traffic = res.call("monitor-traffic", {"interface": "lte1", "once": ""})
            if not traffic:
                return 0.0, 0.0

            row = _decode_row(traffic[0])
            values = self._profile_native_values({'/interface/monitor-traffic': row},
                {'traffic.rx_bytes_per_sec', 'traffic.tx_bytes_per_sec'})
            rx = (values['traffic.rx_bytes_per_sec'] if 'traffic.rx_bytes_per_sec' in values
                else int(row.get("rx-bits-per-second", "0")) / 8)
            tx = (values['traffic.tx_bytes_per_sec'] if 'traffic.tx_bytes_per_sec' in values
                else int(row.get("tx-bits-per-second", "0")) / 8)
            self._last_traffic_profile_values = values
            rx,tx = float(rx or 0.0),float(tx or 0.0)
            return rx, tx

        except (RouterOsTransportError, RouterOsProtocolError) as e:
            log.warning("traffic read failed: %s", e)
            if isinstance(e, RouterOsTransportError):
                self._mark_transport_failure("traffic", e)
            return 0.0, 0.0

    def _read_qnwcfg_mcs(self) -> Optional[Dict[str, int]]:
        """Read MCS snapshot via AT+QNWCFG="mcs" (EG120K preferred path)."""
        now = time.time()
        if (
            self._last_qnwcfg_mcs is not None
            and (now - self._last_qnwcfg_mcs_ts) < max(2.0, self._qnwcfg_mcs_interval_s)
        ):
            return dict(self._last_qnwcfg_mcs)

        raw = self._at_command('AT+QNWCFG="mcs"')
        previous = dict(self._last_qnwcfg_mcs) if self._last_qnwcfg_mcs is not None else None
        result = self._parse_qnwcfg_mcs(raw, now)
        if raw and "ERROR" not in raw and getattr(self, '_definition', None) is not None:
            values = self._profile_native_values({'AT+QNWCFG="mcs"': raw}, set({'rf.mcs_dl0': 'mcs_dl0', 'rf.mcs_dl1': 'mcs_dl1', 'rf.mcs_ul': 'mcs_ul'}),
                context={'previous_qnwcfg': previous})
            if values:
                result = dict(result or {})
                for address, key in {'rf.mcs_dl0': 'mcs_dl0', 'rf.mcs_dl1': 'mcs_dl1', 'rf.mcs_ul': 'mcs_ul'}.items():
                    if address in values:
                        if values[address] is None: result.pop(key, None)
                        else: result[key] = values[address]
                result['_profile_values'] = dict(values)
                self._last_qnwcfg_mcs = dict(result)
                if any(value is not None for value in values.values()): self._last_qnwcfg_mcs_ts = now
        return result

    def _parse_qnwcfg_mcs(self, raw, now):
        if not raw or "ERROR" in raw:
            return dict(self._last_qnwcfg_mcs) if self._last_qnwcfg_mcs is not None else None
        try:
            for line in raw.splitlines():
                line = line.strip()
                if "+QNWCFG:" not in line or '"mcs"' not in line:
                    continue
                m = re.search(r'\+QNWCFG:\s*"mcs"\s*,\s*(.*)$', line)
                if not m:
                    continue
                payload = m.group(1).split("\r", 1)[0].strip()
                nums: List[int] = []
                for token in payload.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    if not re.fullmatch(r"-?\d+", token):
                        break
                    nums.append(int(token))
                if not nums:
                    continue
                result = {
                    "mcs_dl0": int(nums[0]) if len(nums) > 0 else 0,
                    "mcs_ul": int(nums[1]) if len(nums) > 1 else 0,
                    "mcs_dl1": int(nums[2]) if len(nums) > 2 else 0,
                }
                self._last_qnwcfg_mcs = dict(result)
                self._last_qnwcfg_mcs_ts = now
                return dict(result)
            return dict(self._last_qnwcfg_mcs) if self._last_qnwcfg_mcs is not None else None
        except Exception:
            return dict(self._last_qnwcfg_mcs) if self._last_qnwcfg_mcs is not None else None

    def _read_qeng_mcs(self) -> Optional[Dict[str, int]]:
        """Read MCS_UL and EARFCN from the secondary AT+QENG surface.

        QENG format (EG120K-EA):
        NOCONN (19 fields):
        +QENG: "servingcell","NOCONN","LTE","FDD",mcc,mnc,cellid,pci,earfcn,band,
                dl_bw,ul_bw,tac,rsrp,rsrq,rssi,sinr,cqi,tx_power

        CONNECT (20+ fields — extended with srxlev, and sometimes UL/DL MCS):
        +QENG: "servingcell","CONNECT","LTE","FDD",mcc,mnc,cellid,pci,earfcn,band,
                dl_bw,ul_bw,tac,rsrp,rsrq,rssi,sinr,cqi,tx_power,srxlev

        Some EG12x firmware versions add DL_MCS and UL_MCS at indices 20 and 21
        when in CONNECT state. We try to extract them if present.
        """
        now = time.time()
        if self._last_qeng_mcs is not None and (now - self._last_qeng_mcs_ts) < max(1.0, self._qeng_mcs_interval_s):
            return dict(self._last_qeng_mcs)
        raw = self._at_command('AT+QENG="servingcell"')
        if (self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS
                and parse_routeros_nr_qeng(raw).get('nr_mode')):
            # The optional LTE MCS reader must not interpret SA TAC as LTE
            # EARFCN or carry its previous LTE values into an NSA response.
            self._last_qeng_mcs = {}
            self._last_qeng_mcs_ts = now
            return {}
        previous = dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None
        result = self._parse_qeng_mcs(raw, now)
        if raw and "ERROR" not in raw and getattr(self, '_definition', None) is not None:
            values = self._profile_native_values({'AT+QENG="servingcell"': raw}, set({'rf.mcs_dl0': 'mcs_dl', 'rf.mcs_ul': 'mcs_ul', 'rf.earfcn_dl': 'earfcn'}),
                context={'previous_qeng_mcs': previous})
            if values:
                result = dict(result or {})
                for address, key in {'rf.mcs_dl0': 'mcs_dl', 'rf.mcs_ul': 'mcs_ul', 'rf.earfcn_dl': 'earfcn'}.items():
                    if address in values:
                        if values[address] is None: result.pop(key, None)
                        else: result[key] = values[address]
                result['_profile_values'] = dict(values)
                self._last_qeng_mcs = dict(result)
                if any(value is not None for value in values.values()): self._last_qeng_mcs_ts = now
        return result

    def _parse_qeng_mcs(self, raw, now):
        if not raw or "ERROR" in raw:
            return dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None
        try:
            line = [row for row in raw.strip().split("\n") if "servingcell" in row]
            if not line:
                return dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None
            parts = line[0].split(",")
            result = {}

            # EARFCN is at index 8
            if len(parts) > 8 and parts[8] != "-":
                result["earfcn"] = int(parts[8])

            # F1.7: Check state — MCS fields only possible in CONNECT mode
            state = parts[1].strip('"').upper() if len(parts) > 1 else ""
            if state in ("CONNECT", "CONNECTED"):
                # Some firmware puts DL_MCS at index 20, UL_MCS at index 21
                if len(parts) > 21 and parts[21] != "-":
                    try:
                        val = int(parts[21])
                        if 0 < val <= 28:  # valid MCS range
                            result["mcs_ul"] = val
                    except ValueError:
                        pass
                if len(parts) > 20 and parts[20] != "-":
                    try:
                        val = int(parts[20])
                        if 0 < val <= 28:
                            result["mcs_dl"] = val
                    except ValueError:
                        pass

            if result:
                self._last_qeng_mcs = dict(result)
                self._last_qeng_mcs_ts = now
                return result
            return dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None
        except (IndexError, ValueError):
            return dict(self._last_qeng_mcs) if self._last_qeng_mcs is not None else None

    def read_neighbors(self) -> List[NeighborCell]:
        """MikroTik neighbor scan is intentionally disabled.

        RouterOS cache gives us the useful live facts without an extra AT
        modem poke. The profile marks neighbors unsupported so scheduled
        runtime paths must not call this method.
        """
        return []

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        """Measure user-path RTT without entering the RouterOS API/AT lane."""
        return measure_host_ping_rtt(
            target=str(target or "8.8.8.8"), count=max(1, int(count or 1)),
            timeout_s=timeout_s,
        )

    # ─── Lever execution ────────────────────────────────────

    def _execute_typed_cell_lock_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
        target: CoreCellLockTarget,
    ) -> ModemLivePhysicalStageResult:
        """Execute the proven QNWLOCK write/readback/guard sequence.

        The protocol remains backend-private.  The only caller is the typed
        physical-stage boundary, so the tuned RouterOS/AT sequence cannot be
        invoked as a second command route.
        """

        command = (
            'AT+QNWLOCK="common/4g",0'
            if target.unlock
            else (
                f'AT+QNWLOCK="common/4g",1,'
                f"{target.earfcn},{target.pci}"
            )
        )
        started = time.time()
        raw = self._at_command(
            command,
            mutating=True,
            post_delay_s=1.0,
        )
        accepted = bool(
            raw is not None and "ERROR" not in str(raw).upper()
        )
        readback_command = 'AT+QNWLOCK="common/4g"'
        readback_raw = (
            self._at_command(
                readback_command,
                wait_all_output=True,
                post_delay_s=0.2,
            )
            if accepted
            else None
        )
        readback = parse_qnwlock_readback(readback_raw)
        guard: Dict[str, Any] = {}
        if accepted:
            if target.unlock:
                guard = self._set_routeros_qnwlock_guard(
                    False,
                    reset_fail_count=True,
                )
            else:
                guard = self._set_routeros_qnwlock_guard(
                    True,
                    pci=target.pci,
                    earfcn=target.earfcn,
                    band=int(target.band or 0),
                )
            self._begin_post_write_blackout(reason=plan.stage_kind)
        returned = time.time()
        return ModemLivePhysicalStageResult(
            plan=plan,
            request_dispatched_at=started,
            transport_returned_at=returned,
            outcome="accepted" if accepted else "rejected",
            reason=(
                "accepted"
                if accepted
                else "routeros_cell_lock_rejected"
            ),
            radio_drop_detected=False,
            detail={
                "transport": "routeros_at_chat_qnwlock",
                "command": command,
                "response": str(raw or ""),
                "readback_command": readback_command,
                "readback": dict(readback or {}),
                "guard": dict(guard or {}),
            },
        )

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("mikrotik_physical_stage_plan_required")
        if isinstance(plan.target, CoreNetworkSettingsTarget):
            raise RuntimeError("network_settings_read_only")
        if not self._routeros or not self._lte_id:
            raise RuntimeError("routeros_lte_transport_unavailable")
        target = plan.target
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem.mikrotik_radio_mode import execute
            return execute(self, plan)
        if isinstance(target, CoreNrBandTarget) and plan.preceding_mode is not None:
            observed = self._radio_mode_status("event_write", required=True)["radio_mode"]
            if observed != plan.preceding_mode.target.configuration():
                raise RuntimeError("mikrotik_nr_enabled_mode_configuration_changed")
        if isinstance(target, PhysicalCommandCoordinationTarget):
            started = time.time()
            if target.operation == "device_local_recovery_pause":
                status = self._pause_routeros_lte_watchdog(
                    "typed_physical_command"
                )
            elif target.operation == "device_local_recovery_resume":
                status = self._resume_routeros_lte_watchdog(
                    "typed_physical_command"
                )
            else:
                raise RuntimeError(
                    "routeros_coordination_operation_unsupported"
                )
            returned = time.time()
            accepted = bool(status.get("ok", False))
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=started,
                transport_returned_at=returned,
                outcome="accepted" if accepted else "rejected",
                reason=(
                    "accepted"
                    if accepted
                    else f"{target.operation}_failed"
                ),
                radio_drop_detected=False,
                detail={"coordination": dict(status)},
            )
        if isinstance(target, (CoreBandSetTarget, CorePBandTarget, CoreBandAutoTarget)):
            if isinstance(target, CoreBandSetTarget):
                band_value = ",".join(str(band) for band in target.bands)
            elif isinstance(target, CorePBandTarget):
                band_value = "" if target.pband == 0 else str(target.pband)
            else:
                if target.domain != "lte_allocated_auto":
                    raise RuntimeError("routeros_nr_auto_not_implemented")
                band_value = ""
            resource = self._routeros.get_resource("/interface/lte")
            definition=getattr(self,'_definition',None)
            parameters=(definition.prepare_native_write_fields('routeros:lte.band',{'band':band_value})
                        if definition is not None else {'band':band_value})
            started = time.time()
            accepted = False
            reason = "routeros_band_write_rejected"
            try:
                resource.set(id=self._lte_id, **parameters)
                returned = time.time()
                accepted = True
                reason = "accepted"
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                returned = time.time()
                reason = f"routeros_band_transport_error:{type(exc).__name__}"
                if isinstance(exc, RouterOsTransportError):
                    self._mark_transport_failure("band_write", exc)
            if accepted:
                self._invalidate_radio_caches()
                self._begin_post_write_blackout(reason=plan.stage_kind)
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=started,
                transport_returned_at=returned,
                outcome="accepted" if accepted else "rejected",
                reason=reason,
                radio_drop_detected=False,
                detail={"transport": "routeros_lte_band_property"},
            )
        if isinstance(target, CoreCellLockTarget):
            return self._execute_typed_cell_lock_stage(plan, target)
        if isinstance(target, CoreWifiRadioTarget):
            radio = {
                "2.4ghz": "2.4",
                "5ghz": "5",
            }.get(target.band)
            if radio is None:
                raise RuntimeError(
                    f"routeros_wifi_radio_unsupported:{target.band}"
                )
            started = time.time()
            accepted = self._set_wifi_radio_from_typed_stage(
                radio=radio,
                enabled=target.enabled,
            )
            returned = time.time()
            if accepted:
                self._last_wifi_status = (None, None, None)
                self._last_wifi_status_ts = 0.0
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=started,
                transport_returned_at=returned,
                outcome="accepted" if accepted else "rejected",
                reason=(
                    "accepted"
                    if accepted
                    else "routeros_wifi_radio_rejected"
                ),
                radio_drop_detected=False,
                detail={"transport": "routeros_wifi_interface_property"},
            )
        if isinstance(target, CoreRestartTarget):
            started = time.time()
            raw = self._at_command("AT+CFUN=1,1", mutating=True)
            returned = time.time()
            accepted = bool(
                raw is not None and "ERROR" not in str(raw).upper()
            )
            if accepted:
                self._begin_post_write_blackout(reason=plan.stage_kind)
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=started,
                transport_returned_at=returned,
                outcome="accepted" if accepted else "rejected",
                reason="accepted" if accepted else "routeros_restart_rejected",
                radio_drop_detected=False,
                detail={"transport": "routeros_at_chat_cfun"},
            )
        if isinstance(target, CoreUeCapabilityTarget):
            if plan.stage_kind == "ue_dl_256qam":
                command = (
                    f'AT+QNWPREFCFG="dl_256qam",'
                    f'{"1" if target.dl_256qam else "0"}'
                )
            elif plan.stage_kind == "ue_ul_64qam":
                command = (
                    f'AT+QNWPREFCFG="ul_64qam",'
                    f'{"1" if target.ul_64qam else "0"}'
                )
            elif plan.stage_kind == "ue_restart":
                if target.restart is not True:
                    raise RuntimeError("routeros_ue_restart_false_unsupported")
                command = "AT+CFUN=1,1"
            else:
                raise RuntimeError("routeros_ue_stage_unsupported")
            started = time.time()
            raw = self._at_command(command, mutating=True)
            returned = time.time()
            accepted = bool(
                raw is not None and "ERROR" not in str(raw).upper()
            )
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=started,
                transport_returned_at=returned,
                outcome="accepted" if accepted else "rejected",
                reason="accepted" if accepted else "routeros_ue_write_rejected",
                radio_drop_detected=False,
                detail={"transport": "routeros_at_chat_ue"},
            )
        raise RuntimeError("routeros_typed_target_unsupported")

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("mikrotik_generic_execute_lever_forbidden")

    def _exec_band_selection(self, action: LeverAction) -> LeverResult:
        """Lever 1: Set allowed bands via RouterOS API.

        Supports two modes:
          - sequential=True: stack bands one at a time (B3 anchor first)
          - sequential=False (default): atomic set of all bands at once

        Sequential stacking ensures B3 becomes anchor (20 MHz primary),
        then adds B1 and B20/B28 as secondary carriers.
        """
        del action
        raise RuntimeError(
            "mikrotik_mapping_band_selection_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_pband_anchor(self, action: LeverAction) -> LeverResult:
        """PBAND change via RouterOS band-mask stack.

        MikroTik does not expose a Huawei-style PBAND API. RouterOS band
        selection is a configured allow-list; the network still chooses the
        active PCC. Apply the requested allow-list once, then verify configured
        pool and active PCC/SCC separately.
        """
        del action
        raise RuntimeError(
            "mikrotik_mapping_pband_anchor_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_cell_lock(self, action: LeverAction) -> LeverResult:
        """Apply or clear a generic CELL_LOCK through RouterOS LTE at-chat."""
        del action
        raise RuntimeError(
            "mikrotik_mapping_cell_lock_forbidden:"
            "use_execute_physical_stage"
        )

    def _set_routeros_qnwlock_guard(
        self,
        active: bool,
        *,
        reset_fail_count: bool = False,
        pci: int | None = None,
        earfcn: int = 0,
        band: int = 0,
    ) -> Dict[str, Any]:
        del reset_fail_count
        active_bool = bool(active)
        from arch4.core_contracts.command_targets import core_lte_pci
        target_pci = core_lte_pci(pci) if active_bool else 0
        target_earfcn = int(earfcn or 0) if active_bool else 0
        if active_bool and target_earfcn <= 0:
            raise ValueError("qnwlock_guard_earfcn_required")
        target_band = int(band or 0) if active_bool else 0
        command = (
            f'AT+QNWLOCK="common/4g",1,{target_earfcn},{target_pci}'
            if active_bool
            else ""
        )
        values: Dict[str, Any] = {
            "qnwlockGuardActive": active_bool,
            "qnwlockGuardPci": target_pci,
            "qnwlockGuardEarfcn": target_earfcn,
            "qnwlockGuardBand": target_band,
            "qnwlockGuardCommand": command,
            "qnwlockGuardRenewAfterReset": active_bool,
            "qnwlockGuardUpdatedTs": int(time.time()) if active_bool else 0,
        }
        result = self._write_routeros_global_state(values)
        if bool(result.get("ok", False)):
            self._last_qnwlock_guard_status = {
                "active": active_bool,
                "pci": target_pci,
                "earfcn": target_earfcn,
                "band": target_band,
                "command": command,
                "renew_after_reset": active_bool,
                "source": "routeros_environment",
                "updated_ts": time.time(),
            }
            return result
        result["non_blocking"] = True
        result["reason"] = str(result.get("reason") or "routeros_environment_unavailable")
        return result

    @staticmethod
    def _routeros_env_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {"true", "yes", "on", "1"}

    @staticmethod
    def _routeros_env_int(value: Any) -> int:
        try:
            return int(str(value or "0").strip() or "0")
        except Exception:
            return 0

    def _read_routeros_environment_values(self, names: List[str]) -> Dict[str, str]:
        wanted = {str(name or "").strip() for name in names if str(name or "").strip()}
        if not wanted:
            return {}
        try:
            resource = self._routeros.get_resource(
                "/system/script/environment"
            )
            rows = resource.get()
        except Exception as exc:
            log.debug("RouterOS environment read unavailable: %s", exc)
            return {}
        return {
            name: str(row.get("value") if row.get("value") is not None else "")
            for row in rows or []
            if (name := str(row.get("name") or "").strip()) in wanted
        }

    @staticmethod
    def _routeros_guard_pci(value: Any) -> int | None:
        from arch4.core_contracts.command_targets import core_lte_pci
        if value is None or isinstance(value, bool):
            return None
        try:
            return core_lte_pci(int(str(value).strip()))
        except (TypeError, ValueError):
            return None

    def _read_routeros_qnwlock_guard(self) -> Dict[str, Any]:
        raw = self._read_routeros_environment_values(
            [
                "qnwlockGuardActive",
                "qnwlockGuardPci",
                "qnwlockGuardEarfcn",
                "qnwlockGuardBand",
                "qnwlockGuardCommand",
                "qnwlockGuardRenewAfterReset",
                "qnwlockGuardUpdatedTs",
            ]
        )
        active = self._routeros_env_bool(raw.get("qnwlockGuardActive"))
        return {
            "active": active,
            "pci": self._routeros_guard_pci(raw.get("qnwlockGuardPci")) if active else 0,
            "earfcn": self._routeros_env_int(raw.get("qnwlockGuardEarfcn")) if active else 0,
            "band": self._routeros_env_int(raw.get("qnwlockGuardBand")) if active else 0,
            "command": str(raw.get("qnwlockGuardCommand") or ""),
            "renew_after_reset": self._routeros_env_bool(raw.get("qnwlockGuardRenewAfterReset")),
            "updated_ts": self._routeros_env_int(raw.get("qnwlockGuardUpdatedTs")),
            "source": "routeros_environment" if raw else "routeros_environment_missing",
        }

    def _cached_active_qnwlock_guard(self) -> Dict[str, Any]:
        status = dict(getattr(self, "_last_qnwlock_guard_status", {}) or {})
        if not bool(status.get("active", False)):
            return {}
        target_pci = self._routeros_guard_pci(status.get("pci"))
        target_earfcn = int(status.get("earfcn", 0) or 0)
        if target_pci is None or target_earfcn <= 0:
            return {}
        return {
            "active": True,
            "pci": target_pci,
            "earfcn": target_earfcn,
            "band": int(status.get("band", 0) or 0),
            "command": str(status.get("command") or f'AT+QNWLOCK="common/4g",1,{target_earfcn},{target_pci}'),
            "renew_after_reset": bool(status.get("renew_after_reset", True)),
            "updated_ts": int(status.get("updated_ts", 0) or 0),
            "source": "runtime_cached_qnwlock_guard",
        }

    def _qnwlock_guard_match_state(self, monitor: Mapping[str, Any], guard: Mapping[str, Any]) -> Dict[str, Any]:
        target_pci = self._routeros_guard_pci(guard.get("pci"))
        target_earfcn = int(guard.get("earfcn", 0) or 0)
        target_band = int(guard.get("band", 0) or 0)
        current_pci = self._routeros_guard_pci(monitor.get("pci"))
        current_earfcn = int(monitor.get("earfcn", 0) or 0)
        current_band = int(
            monitor.get("primary_band", 0)
            or monitor.get("serving_band", 0)
            or monitor.get("band", 0)
            or 0
        )
        radio_connected = bool(monitor.get("radio_connected", False))
        cell_matches = bool(current_pci is not None and target_pci is not None
                            and current_pci == target_pci and current_earfcn == target_earfcn)
        band_matches = bool(target_band <= 0 or current_band <= 0 or current_band == target_band)
        serving_matches = bool(radio_connected and cell_matches and band_matches)
        return {
            "radio_connected": radio_connected,
            "target_pci": target_pci,
            "target_earfcn": target_earfcn,
            "target_band": target_band,
            "current_pci": current_pci,
            "current_earfcn": current_earfcn,
            "current_band": current_band,
            "cell_matches": cell_matches,
            "band_matches": band_matches,
            "serving_matches": serving_matches,
        }

    def _cached_qnwlock_guard_requires_audit(self, monitor: Mapping[str, Any]) -> bool:
        guard = self._cached_active_qnwlock_guard()
        if not guard:
            return False
        match = self._qnwlock_guard_match_state(monitor, guard)
        return bool(match.get("radio_connected", False) and not match.get("serving_matches", False))

    def _audit_routeros_qnwlock_guard(
        self,
        monitor: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        if bool(getattr(self, "_qnwlock_renewing", False)):
            return dict(getattr(self, "_last_qnwlock_guard_status", {}) or {})
        now = time.time()
        last_audit = float(getattr(self, "_last_qnwlock_guard_audit_ts", 0.0) or 0.0)
        if not force and (now - last_audit) < 30.0 and not self._cached_qnwlock_guard_requires_audit(monitor):
            return dict(getattr(self, "_last_qnwlock_guard_status", {}) or {})
        self._last_qnwlock_guard_audit_ts = now
        guard = self._read_routeros_qnwlock_guard()
        if not bool(guard.get("active", False)) and guard.get("source") == "routeros_environment_missing":
            cached_guard = self._cached_active_qnwlock_guard()
            if cached_guard:
                guard = {**cached_guard, "environment_source": "routeros_environment_missing"}
        if not bool(guard.get("active", False)):
            status = {**guard, **self._qnwlock_guard_match_state(monitor, guard), "status": "inactive"}
            self._last_qnwlock_guard_status = status
            return dict(status)

        target_pci = self._routeros_guard_pci(guard.get("pci"))
        target_earfcn = int(guard.get("earfcn", 0) or 0)
        if target_pci is None or target_earfcn <= 0:
            status = {**guard, **self._qnwlock_guard_match_state(monitor, guard), "status": "invalid_target", "renewed": False}
            self._last_qnwlock_guard_status = status
            return dict(status)

        match_state = self._qnwlock_guard_match_state(monitor, guard)
        radio_connected = bool(match_state.get("radio_connected", False))
        serving_matches = bool(match_state.get("serving_matches", False))
        readback: Dict[str, Any] = {}
        last_readback = float(getattr(self, "_last_qnwlock_readback_check_ts", 0.0) or 0.0)
        readback_due = force or (now - last_readback) >= 120.0
        if readback_due or not serving_matches:
            self._last_qnwlock_readback_check_ts = now
            raw = self._at_command('AT+QNWLOCK="common/4g"', wait_all_output=True, post_delay_s=0.2)
            readback = parse_qnwlock_readback(raw)

        readback_matches = None
        if readback:
            readback_matches = bool(
                readback.get("enabled", False)
                and int(readback.get("pci", 0) or 0) == target_pci
                and int(readback.get("earfcn", 0) or 0) == target_earfcn
            )
        needs_renew = (not serving_matches) or readback_matches is False
        if not radio_connected:
            status = {
                **guard,
                **match_state,
                "status": "waiting_for_radio",
                "renewed": False,
                "readback": readback,
            }
            self._last_qnwlock_guard_status = status
            return dict(status)
        if not needs_renew:
            status = {
                **guard,
                **match_state,
                "status": "verified",
                "renewed": False,
                "readback": readback,
            }
            self._last_qnwlock_guard_status = status
            return dict(status)

        return_status = {
            **guard,
            **match_state,
            "status": "renewal_requires_command_authority",
            "renewed": False,
            "readback_before": readback,
            "readback": readback,
            "mutation_performed": False,
        }
        self._last_qnwlock_guard_status = return_status
        return dict(return_status)

    def _annotate_qnwlock_renewal_after_radio_event(
        self,
        result: LeverResult,
        reason: str,
    ) -> LeverResult:
        """Renew active RouterOS QNWLOCK intent after a radio-disruptive event."""
        details = dict(result.details or {})
        try:
            monitor = self._lte_monitor(force=True) or {}
            status = self._audit_routeros_qnwlock_guard(monitor, force=True)
            details["routeros_qnwlock_renewal"] = {
                "mode": "post_radio_event",
                "reason": str(reason or ""),
                "monitor": {
                    "radio_connected": bool(monitor.get("radio_connected", False)),
                    "primary_band": int(monitor.get("primary_band", 0) or 0),
                    "pci": int(monitor.get("pci", 0) or 0),
                    "earfcn": int(monitor.get("earfcn", 0) or 0),
                },
                "status": dict(status or {}),
                "non_blocking": True,
                "system_watchdog_allowed": True,
            }
        except Exception as exc:
            details["routeros_qnwlock_renewal"] = {
                "mode": "post_radio_event",
                "reason": str(reason or ""),
                "status": "suppressed",
                "error": str(exc),
                "non_blocking": True,
                "system_watchdog_allowed": True,
            }
        result.details = details
        return result

    @staticmethod
    def _routeros_row_id(row: Mapping[str, Any]) -> str:
        return str(row.get(".id") or row.get("id") or "").strip()

    @staticmethod
    def _routeros_named_row(rows: Any, name: str) -> Optional[Dict[str, Any]]:
        target = str(name or "").strip()
        if not target:
            return None
        target_fold = target.casefold()
        name_match: Optional[Dict[str, Any]] = None
        for row in rows or []:
            row_name = str(row.get("name") or "").strip()
            if row_name == target:
                return row
            if name_match is None and row_name.casefold() == target_fold:
                name_match = row
        return name_match

    @staticmethod
    def _routeros_scheduler_script_target(row: Mapping[str, Any]) -> str:
        """Return the exact script invoked by a dedicated scheduler row."""

        on_event = str(
            row.get("on-event")
            or row.get("on_event")
            or row.get("event")
            or ""
        ).strip()
        if not on_event:
            return ""
        normalized = " ".join(on_event.split())
        match = re.fullmatch(
            r"/?system(?:/|\s+)script(?:/|\s+)run\s+"
            r"(?:number\s*=\s*)?"
            r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_.-]+))\s*;?",
            normalized,
            flags=re.IGNORECASE,
        )
        if match is None:
            return ""
        return str(next(group for group in match.groups() if group is not None)).casefold()

    @staticmethod
    def _routeros_watchdog_job_roles(rows: Any) -> Dict[str, List[str]]:
        """Classify exact active RouterOS script jobs once for all callers."""

        names = sorted(
            {
                str(row.get("script") or row.get("name") or "")
                .strip()
                .casefold()
                for row in rows or []
                if str(row.get("script") or row.get("name") or "").strip()
            }
        )
        return {
            "routine": [
                name for name in names
                if name in ROUTEROS_LTE_WATCHDOG_ROUTINE_SCRIPTS
            ],
            "recovery": [
                name for name in names
                if name in ROUTEROS_LTE_WATCHDOG_RECOVERY_SCRIPTS
            ],
            "boot_guard": [
                name for name in names
                if name == ROUTEROS_LTE_WATCHDOG_BOOT_GUARD_SCRIPT
            ],
            "reset": [
                name for name in names
                if name == ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT
            ],
            "unknown": [
                name for name in names
                if "lte_watchdog" in name
                and name not in ROUTEROS_LTE_WATCHDOG_KNOWN_SCRIPTS
            ],
        }

    @classmethod
    def _routeros_watchdog_scheduler_roles(
        cls,
        rows: Any,
    ) -> Dict[str, Any]:
        """Classify watchdog schedulers once, by exact invoked script."""

        by_script: Dict[str, List[Dict[str, Any]]] = {
            name: [] for name in ROUTEROS_LTE_WATCHDOG_KNOWN_SCRIPTS
        }
        unclassified: List[Dict[str, Any]] = []
        for raw_row in rows or []:
            row = dict(raw_row)
            target = cls._routeros_scheduler_script_target(row)
            if target in by_script:
                by_script[target].append(row)
                continue
            name = str(row.get("name") or "").strip().casefold()
            on_event = str(
                row.get("on-event")
                or row.get("on_event")
                or row.get("event")
                or ""
            ).strip().casefold()
            if "lte_watchdog" in name or "lte_watchdog" in on_event:
                unclassified.append(row)
        return {
            "by_script": by_script,
            "unclassified": unclassified,
        }

    def _set_routeros_lte_watchdog_scheduler_disabled(
        self,
        disabled: bool,
        *,
        expected_row_id: str = "",
    ) -> Dict[str, Any]:
        """Enable/disable the RouterOS scheduler that runs the LTE watchdog script."""
        try:
            sched = self._routeros.get_resource("/system/scheduler")
            roles = self._routeros_watchdog_scheduler_roles(sched.get())
            by_script = dict(roles.get("by_script") or {})
            matches = list(by_script.get(ROUTEROS_LTE_WATCHDOG_SCRIPT) or [])
            illegal_helpers = {
                name: len(list(by_script.get(name) or []))
                for name in ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS[1:]
                if by_script.get(name)
            }
            unclassified_count = len(list(roles.get("unclassified") or []))
            if len(matches) > 1:
                return {
                    "ok": False,
                    "scheduler_name": "",
                    "scheduler_present": True,
                    "disabled": bool(disabled),
                    "reason": "watchdog_periodic_scheduler_ambiguous",
                    "match_count": len(matches),
                }
            if illegal_helpers or unclassified_count:
                return {
                    "ok": False,
                    "scheduler_name": "",
                    "scheduler_present": bool(matches),
                    "disabled": bool(disabled),
                    "reason": "watchdog_scheduler_topology_invalid",
                    "scheduled_helpers": illegal_helpers,
                    "unclassified_count": unclassified_count,
                }
            if not matches:
                retained_id = str(expected_row_id or "").strip()
                if retained_id:
                    return {
                        "ok": False,
                        "scheduler_name": "",
                        "scheduler_present": False,
                        "disabled": bool(disabled),
                        "reason": "watchdog_scheduler_identity_missing",
                        "expected_row_id": retained_id,
                    }
                return {
                    "ok": True,
                    "scheduler_name": "",
                    "scheduler_present": False,
                    "disabled": bool(disabled),
                    "reason": "scheduler_not_configured",
                    "match": "not_found",
                }
            row = matches[0]
            name = str(row.get("name") or "").strip()
            row_id = self._routeros_row_id(row)
            previously_disabled = _ros_bool(row.get("disabled"))
            if not row_id:
                return {
                    "ok": False,
                    "scheduler_name": name,
                    "scheduler_present": True,
                    "match": "on-event:lte_watchdog",
                    "reason": "scheduler_id_missing",
                }
            retained_id = str(expected_row_id or "").strip()
            if retained_id and retained_id != row_id:
                return {
                    "ok": False,
                    "scheduler_name": name,
                    "scheduler_present": True,
                    "match": "on-event:lte_watchdog",
                    "reason": "watchdog_scheduler_identity_changed",
                    "row_id": row_id,
                    "expected_row_id": retained_id,
                }
            sched.set(id=row_id, disabled="yes" if disabled else "no")
            self._invalidate_routeros_recovery_observation(topology=True)
            return {
                "ok": True,
                "scheduler_name": name,
                "scheduler_present": True,
                "match": "on-event:lte_watchdog",
                "row_id": row_id,
                "previously_disabled": previously_disabled,
                "disabled": bool(disabled),
            }
        except Exception as exc:
            return {"ok": False, "scheduler_name": "", "reason": str(exc)}

    def _invalidate_routeros_recovery_observation(
        self,
        *,
        topology: bool,
    ) -> None:
        """Invalidate the one RouterOS recovery observation cache."""

        self._last_watchdog_script_status = {}
        self._last_watchdog_script_status_ts = 0.0
        if topology:
            self._routeros_recovery_topology_cache = {}
            self._routeros_recovery_topology_ts = 0.0
            self._routeros_recovery_topology_attempts = 0

    def clear_routeros_watchdog_runtime_cache(self) -> None:
        """Clear RouterOS recovery evidence through its backend owner."""

        self._invalidate_routeros_recovery_observation(topology=True)
        self._last_watchdog_script_run_ts = 0.0
        self._last_watchdog_script_error = ""
        self._last_device_recovery_fingerprint = {}
        self._last_device_recovery_diagnostic = {}

    def _run_routeros_named_script(self, script_name: str) -> Dict[str, Any]:
        """Run a specific RouterOS helper script by name."""
        name = str(script_name or "").strip()
        if not name:
            return {"ok": False, "reason": "script_name_missing"}
        try:
            scripts = self._routeros.get_resource("/system/script")
            row = self._routeros_named_row(scripts.get(), name)
            if row is None:
                return {"ok": False, "script_name": name, "reason": "script_not_found"}
            number = self._routeros_row_id(row) or name
            call_fn = getattr(scripts, "call", None)
            if callable(call_fn):
                call_fn("run", {"number": number})
            else:
                binary = self._routeros.get_binary_resource("/system/script")
                binary.call("run", {"number": number})
            self._invalidate_routeros_recovery_observation(topology=False)
            return {"ok": True, "script_name": name, "number": number}
        except Exception as exc:
            return {"ok": False, "script_name": name, "reason": str(exc)}

    def _pause_routeros_lte_watchdog(self, reason: str) -> Dict[str, Any]:
        """Pause RouterOS-owned LTE recovery while 54GUC intentionally disturbs LTE."""
        if getattr(self, "_routeros_watchdog_pause_state", None) is not None:
            return {
                "ok": False,
                "reason": "routeros_watchdog_pause_already_active",
            }
        disable = self._set_routeros_lte_watchdog_scheduler_disabled(True)
        reset_required = bool(disable.get("scheduler_present", True))
        if reset_required:
            reset = self._run_routeros_named_script(ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT)
        else:
            reset = {
                "ok": True,
                "script_name": ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT,
                "skipped": True,
                "reason": "scheduler_not_configured",
            }
        ok = bool(disable.get("ok", False)) and (
            bool(reset.get("ok", False)) or not reset_required
        )
        rollback: Dict[str, Any] = {}
        if bool(disable.get("ok", False)):
            self._routeros_watchdog_pause_state = {
                "scheduler_present": bool(
                    disable.get("scheduler_present", False)
                ),
                "previously_disabled": bool(
                    disable.get("previously_disabled", False)
                ),
                "scheduler_name": str(
                    disable.get("scheduler_name") or ""
                ),
                "scheduler_row_id": str(disable.get("row_id") or ""),
            }
        if (
            not ok
            and getattr(self, "_routeros_watchdog_pause_state", None)
            is not None
        ):
            prior_disabled = bool(
                self._routeros_watchdog_pause_state.get(
                    "previously_disabled", False
                )
            )
            rollback = self._set_routeros_lte_watchdog_scheduler_disabled(
                prior_disabled,
                expected_row_id=str(
                    self._routeros_watchdog_pause_state.get("scheduler_row_id") or ""
                ),
            )
            if bool(rollback.get("ok", False)):
                self._routeros_watchdog_pause_state = None
        result = {
            "ok": ok,
            "reason": str(reason or ""),
            "scheduler": disable,
            "reset_state": reset,
            "reset_required": reset_required,
            "rollback": rollback,
        }
        if not ok:
            log.warning("RouterOS LTE watchdog pause incomplete: %s", result)
        return result

    def _resume_routeros_lte_watchdog(self, reason: str) -> Dict[str, Any]:
        """Reset state and restore the exact pre-action scheduler state."""
        pause_state = getattr(self, "_routeros_watchdog_pause_state", None)
        if not isinstance(pause_state, Mapping):
            return {
                "ok": False,
                "reason": "routeros_watchdog_pause_state_missing",
            }
        restore_disabled = bool(
            pause_state.get("previously_disabled", False)
        )
        restore = self._set_routeros_lte_watchdog_scheduler_disabled(
            restore_disabled,
            expected_row_id=str(pause_state.get("scheduler_row_id") or ""),
        )
        reset_required = bool(restore.get("scheduler_present", True))
        if reset_required:
            reset = self._run_routeros_named_script(ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT)
        else:
            reset = {
                "ok": True,
                "script_name": ROUTEROS_LTE_WATCHDOG_RESET_SCRIPT,
                "skipped": True,
                "reason": "scheduler_not_configured",
            }
        ok = bool(restore.get("ok", False)) and (
            bool(reset.get("ok", False)) or not reset_required
        )
        if ok:
            self._routeros_watchdog_pause_state = None
        result = {
            "ok": ok,
            "reason": str(reason or ""),
            "reset_state": reset,
            "scheduler": restore,
            "reset_required": reset_required,
            "restored_disabled": restore_disabled,
        }
        if not ok:
            log.warning("RouterOS LTE watchdog resume incomplete: %s", result)
        return result

    def _routeros_watchdog_pause_failed_result(
        self,
        action: LeverAction,
        pause: Mapping[str, Any],
        reason: str,
    ) -> Optional[LeverResult]:
        """Fail closed before a RouterOS LTE mutation if watchdog pause is not proven."""
        if bool((pause or {}).get("ok", False)):
            return None
        resume = self._resume_routeros_lte_watchdog(f"{reason}_abort")
        result = LeverResult(
            success=False,
            lever=action.lever,
            error="routeros_watchdog_pause_failed",
            radio_drop_detected=False,
            details={
                "routeros_mutation_blocked": True,
                "routeros_mutation_reason": str(reason or ""),
            },
        )
        self._annotate_routeros_watchdog_coordination(result, pause, resume)
        return result

    @staticmethod
    def _annotate_routeros_watchdog_coordination(
        result: LeverResult,
        pause: Mapping[str, Any],
        resume: Mapping[str, Any],
    ) -> LeverResult:
        details = dict(result.details or {})
        details["routeros_lte_watchdog"] = {
            "mode": "scheduler_paused_around_disruptive_action",
            "pause": dict(pause or {}),
            "resume": dict(resume or {}),
        }
        result.details = details
        return result

    def _exec_traffic_presence(self, action: LeverAction) -> LeverResult:
        """Lever 3: Adjust keepalive interval."""
        del action
        raise RuntimeError(
            "mikrotik_mapping_traffic_presence_forbidden:"
            "no_typed_cgf_command_contract"
        )

    def _exec_traffic_volume(self, action: LeverAction) -> LeverResult:
        """Lever 4: SQM / queue management."""
        del action
        raise RuntimeError(
            "mikrotik_mapping_traffic_volume_forbidden:"
            "no_typed_cgf_command_contract"
        )

    def _exec_ue_capability(self, action: LeverAction) -> LeverResult:
        """Lever 5: UE capability flags via AT commands."""
        del action
        raise RuntimeError(
            "mikrotik_mapping_ue_capability_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_restart(self, action: LeverAction) -> LeverResult:
        """Explicit restart lever path for emergency recovery."""
        del action
        raise RuntimeError(
            "mikrotik_mapping_restart_forbidden:"
            "use_execute_physical_stage"
        )

    # ─── Startup verification ───────────────────────────────

    def verify_static_config(self) -> Dict[str, bool]:
        result = {}
        try:
            # TTL IPv4
            mangle4 = self._routeros.get_resource("/ip/firewall/mangle").get()
            result["ttl_ipv4"] = any(
                r.get("action") == "change-ttl" and "65" in str(r.get("new-ttl", ""))
                for r in mangle4
            )
            # MSS IPv4
            result["mss_ipv4"] = any(
                r.get("action") == "change-mss" and "clamp" in str(r.get("new-mss", ""))
                for r in mangle4
            )

            # TTL IPv6
            mangle6 = self._routeros.get_resource("/ipv6/firewall/mangle").get()
            result["ttl_ipv6"] = any(
                r.get("action") == "change-hop-limit" and "65" in str(r.get("new-hop-limit", ""))
                for r in mangle6
            )
            # MSS IPv6
            result["mss_ipv6"] = any(
                r.get("action") == "change-mss" and "clamp" in str(r.get("new-mss", ""))
                for r in mangle6
            )

            # Keepalive
            sched = self._routeros.get_resource("/system/scheduler").get()
            result["keepalive"] = any(
                "keepalive" in r.get("name", "").lower()
                and not _ros_bool(r.get("disabled", False))
                for r in sched
            )


        except Exception as e:
            log.error("verify_static_config failed: %s", e)
        return result

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError(
            "mikrotik_direct_static_config_write_forbidden:"
            "startup_is_verify_only"
        )

    def verify_ue_capability(self) -> Dict[str, Any]:
        result = {}
        if not self._at_live_polling_enabled:
            return {}

        # 256QAM DL — EG120K-EA uses QCFG, not QNWPREFCFG
        raw = self._at_command('AT+QCFG="dl_256qam"')
        if raw and "ERROR" not in raw:
            # Response: +QCFG: "dl_256qam",1
            result["dl_256qam"] = "1" if ",1" in raw else "0"
        else:
            result["dl_256qam"] = "not_supported"

        # 64QAM UL — no direct query on EG120K-EA; Cat 12 implies 64QAM UL
        result["ul_64qam"] = "cat12_implied"

        # UE category — EG120K-EA is Cat 12 (600/150 Mbps) per datasheet
        result["ue_category"] = "12"

        # Do not probe QNWLOCK here. On RouterOS/MBIM it can disturb the LTE
        # control plane, so capability truth stays catalog-owned and unsupported.
        result["pci_lock_available"] = False

        # Band priority
        raw = self._at_command('AT+QCFG="lte/bandprior"')
        if raw and "ERROR" not in raw:
            result["band_priority"] = raw.strip()
        else:
            result["band_priority"] = "not_supported"

        return result

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError(
            "mikrotik_direct_ue_capability_write_forbidden:"
            "use_execute_physical_stage"
        )

    def restart_modem(self) -> bool:
        raise RuntimeError(
            "mikrotik_direct_restart_write_forbidden:"
            "use_execute_physical_stage"
        )

    def _recover_lte1_then_restart_if_needed(self) -> bool:
        raise RuntimeError(
            "mikrotik_direct_recovery_mutation_forbidden:"
            "submit_a_typed_core_command"
        )

    def read_fresh_device_identity(self) -> Dict[str, str]:
        if self._routeros is None:
            return {}
        return self._refresh_routeros_identity_cache(force=True)

    def get_modem_info(self) -> Dict[str, str]:
        if self._routeros is not None and not self._device_info_cache:
            self._refresh_routeros_identity_cache(force=True)
        return dict(self._device_info_cache)

    def get_report_info(self) -> Dict[str, str]:
        """Return cached identity for reporting without probing RouterOS/AT."""
        info = dict(self._device_info_cache or {})
        status = self._last_status if isinstance(self._last_status, dict) else {}
        for key in (
            "model",
            "router_model",
            "modem_model",
            "firmware",
            "software_version",
            "serial",
            "imei",
            "imsi",
            "provider",
            "provider_name",
            "provider_plmn",
            "plmn",
            "os_name",
            "os_version",
            "webui",
            "wan_ip",
            "wan_iface_name",
            "lan_mac",
        ):
            value = status.get(key)
            if value not in (None, "", [], {}):
                info.setdefault(key, str(value))
        return info

    @staticmethod
    def _clean_identity_value(raw: Any, cache_key: str = "") -> str:
        lines = [
            line.strip()
            for line in str(raw or "").replace("\r", "\n").split("\n")
            if line.strip() and line.strip().upper() != "OK"
        ]
        text = " ".join(lines)
        if cache_key == "imei":
            match = re.search(r"\b(\d{14,17})\b", text)
            return match.group(1) if match else text
        return text

    def _compose_identity_model(self) -> None:
        router_model = str(self._device_info_cache.get("router_model") or "").strip()
        modem_model = str(self._device_info_cache.get("modem_model") or "").strip()
        if router_model and modem_model:
            self._device_info_cache["model"] = f"{router_model} {modem_model}"
        elif router_model:
            self._device_info_cache["model"] = router_model
        elif modem_model:
            self._device_info_cache["model"] = modem_model

    def _merge_lte_monitor_identity(
        self,
        monitor: Dict[str, Any],
        *,
        observed_at: float,
    ) -> None:
        """Merge facts already carried by one accepted RouterOS monitor row."""

        acquired = set()
        # Parser's operator comes exclusively from monitor current-operator,
        # never interface configuration operator. This adds no physical read.
        name = str(monitor.get("operator") or "").strip()
        if name.lower() not in {"", "true", "false", "unknown", "none", "null"}:
            match = re.search(r"\b(\d{5,6})\b", name)
            selected = monitor.get('_profile_rf_values', {})
            self._plmn_cache = (str(selected['identity.provider_plmn'] or '')
                if 'identity.provider_plmn' in selected else
                match.group(1) if match else (resolve_provider_plmn_from_name(name) or ""))
            self._provider_name_cache = (
                name if 'identity.provider_name' in selected else
                (resolve_provider_name(self._plmn_cache) or name)
                if self._plmn_cache else name
            )
            self._plmn_cache_ts = float(observed_at)
            acquired.update({"provider_name", "provider_plmn"})
        if str(monitor.get("imsi") or "").strip():
            acquired.add("imsi")
        self._subscription_acquired_fields = frozenset(acquired)
        self._subscription_observed_at = float(observed_at)

        updates = {
            key: str(monitor.get(key) or "").strip()
            for key in ("imei", "imsi", "vendor", "modem_model", "firmware")
        }
        updates = {key: value for key, value in updates.items() if value}
        if not updates:
            return
        with self._static_identity_lock:
            self._device_info_cache.update(updates)
            self._compose_identity_model()
            self._device_info_ts = float(observed_at)

    @staticmethod
    def _clean_ip_address(value: Any) -> str:
        text = str(value or "").strip().split("/", 1)[0].strip()
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
            return ""
        parts = []
        for part in text.split("."):
            try:
                num = int(part)
            except Exception:
                return ""
            if num < 0 or num > 255:
                return ""
            parts.append(str(num))
        if parts[0] in {"0", "127"} or text.startswith("169.254."):
            return ""
        return ".".join(parts)

    @staticmethod
    def _clean_mac_address(value: Any) -> str:
        text = str(value or "").strip().upper().replace("-", ":")
        if not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", text):
            return ""
        return text

    def _read_routeros_network_identity(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self._routeros is None:
            return out
        try:
            rows = self._routeros.get_resource(self._portable_routeros_path('/ip/address','/ip/address')).get()
            preferred = None
            for row in rows or []:
                iface = str(row.get("interface") or row.get("actual-interface") or "").strip()
                address = self._clean_ip_address(row.get("address"))
                if not address:
                    continue
                if iface.lower().startswith(("lte", "wwan", "cellular")):
                    preferred = (address, iface)
                    break
            if preferred is not None:
                out["wan_ip"], out["wan_iface_name"] = preferred
                values = self._profile_native_values({'/ip/address': row}, {'slow.wan_ip'})
                if 'slow.wan_ip' in values:
                    out['wan_ip'] = values['slow.wan_ip'] or ''
        except Exception as exc:
            log.debug("_read_routeros_network_identity: wan_ip suppressed %s", exc)
        try:
            rows = self._routeros.get_resource("/interface/bridge").get()
            for row in rows or []:
                mac = self._clean_mac_address(
                    row.get("mac-address")
                    or row.get("admin-mac")
                    or row.get("auto-mac")
                )
                if mac:
                    out["lan_mac"] = mac
                    break
        except Exception as exc:
            log.debug("_read_routeros_network_identity: bridge mac suppressed %s", exc)
        if not out.get("lan_mac"):
            try:
                rows = self._routeros.get_resource("/interface/ethernet").get()
                for row in rows or []:
                    name = str(row.get("name") or "").strip().lower()
                    if name in {"ether1", "wan"}:
                        continue
                    mac = self._clean_mac_address(row.get("mac-address"))
                    if mac:
                        out["lan_mac"] = mac
                        break
                if not out.get("lan_mac"):
                    for row in rows or []:
                        mac = self._clean_mac_address(row.get("mac-address"))
                        if mac:
                            out["lan_mac"] = mac
                            break
            except Exception as exc:
                log.debug("_read_routeros_network_identity: ethernet mac suppressed %s", exc)
        return out

    def _read_routeros_antenna_identity(self) -> Dict[str, str]:
        """Read RouterOS-exposed antenna status from startup/static rows only."""
        if self._routeros is None:
            return {}
        for reader in (self._read_routeros_lte_settings_row, self._read_lte_interface_row_from_routeros):
            try:
                row = reader()
            except Exception as exc:
                log.debug("_read_routeros_antenna_identity: suppressed %s", exc)
                continue
            info = routeros_antenna_status(row or {})
            if info:
                return {str(k): str(v) for k, v in info.items()}
        return {}

    def _refresh_routeros_identity_cache(self, force: bool = False) -> Dict[str, str]:
        if self._routeros is None:
            return {}
        if not hasattr(self, "_static_identity_lock"):
            self._static_identity_lock = threading.RLock()
        with self._static_identity_lock:
            now = time.time()
            if force:
                self._routeros_optional_identity_pending = {"network", "antenna"}
            if not force and not self._routeros_identity_due(now):
                return {}
            self._routeros_identity_attempts = int(
                getattr(self, "_routeros_identity_attempts", 0) or 0
            ) + 1
            self._routeros_identity_last_attempt_ts = now
            info = dict(self._device_info_cache)
            fresh: Dict[str, str] = {}

            def commit(updates: Dict[str, Any]) -> None:
                for key, value in updates.items():
                    if value not in (None, ""):
                        info[key] = value
                        fresh[key] = value
                self._device_info_cache = dict(info)
                self._compose_identity_model()
                self._device_info_ts = now

            try:
                resource = self._routeros.get_resource(self._portable_routeros_path('/system/resource','/system/resource'))
                resource_rows = resource.get()
                updates: Dict[str, Any] = {}
                if resource_rows:
                    row = resource_rows[0]
                    routeros_version = str(row.get("version") or "").strip()
                    router_shell = str(
                        row.get("board-name")
                        or row.get("platform")
                        or row.get("board")
                        or ""
                    ).strip()
                    architecture = str(row.get("architecture-name") or "").strip()
                    if routeros_version:
                        updates["software_version"] = routeros_version
                        updates["software"] = routeros_version
                        updates["os_name"] = "RouterOS"
                        updates["os_version"] = routeros_version
                        updates["webui"] = f"WebFig / RouterOS {routeros_version}"
                    if router_shell:
                        updates["router_model"] = router_shell
                    if architecture:
                        updates["hardware_version"] = architecture
                    values = self._profile_native_values({'/system/resource': row},
                        {'identity.hardware_version', 'identity.software_version', 'identity.software'})
                    updates.update({address.split('.',1)[1]:value for address,value in values.items()})
                commit(updates)
            except Exception as exc:
                log.warning("RouterOS identity source unavailable: system/resource: %s", exc)

            try:
                rb = self._routeros.get_resource(self._portable_routeros_path('/system/routerboard','/system/routerboard'))
                rows = rb.get()
                updates = {}
                if rows:
                    rb_row = rows[0]
                    serial = str(rb_row.get("serial-number") or "").strip()
                    routerboard_model = str(
                        rb_row.get("model")
                        or rb_row.get("board-name")
                        or rb_row.get("routerboard")
                        or ""
                    ).strip()
                    if serial:
                        updates["serial"] = serial
                    if routerboard_model:
                        updates["router_model"] = routerboard_model
                        if not info.get("hardware_version"):
                            updates["hardware_version"] = routerboard_model
                    values = self._profile_native_values({'/system/routerboard': rb_row},
                        {'identity.serial', 'identity.router_model', 'identity.hardware_version'})
                    updates.update({address.split('.',1)[1]:value for address,value in values.items()})
                commit(updates)
            except Exception as exc:
                log.warning("RouterOS identity source unavailable: system/routerboard: %s", exc)
            return fresh

    def _routeros_identity_complete(self) -> bool:
        info = dict(getattr(self, "_device_info_cache", {}) or {})
        return all(
            bool(str(info.get(field) or "").strip())
            for field in ("router_model", "software_version", "serial")
        )

    def _routeros_identity_due(self, now: Optional[float] = None) -> bool:
        if self._routeros_identity_complete():
            return False
        attempts = int(getattr(self, "_routeros_identity_attempts", 0) or 0)
        if attempts >= 2:
            return False
        if attempts <= 0:
            return True
        observed_now = float(time.time() if now is None else now)
        last_attempt = float(
            getattr(self, "_routeros_identity_last_attempt_ts", 0.0) or 0.0
        )
        return (
            observed_now - last_attempt
        ) >= ROUTEROS_IDENTITY_RETRY_INTERVAL_S

    def _at_identity_complete(self, task_name: str) -> bool:
        key_cmd = self._AT_IDENTITY_COMMANDS.get(task_name)
        return bool(key_cmd and self._device_info_cache.get(key_cmd[0]))

    def _at_identity_due(self, task_name: str, now: float) -> bool:
        last_ts = float(self._at_identity_ts.get(task_name, 0.0) or 0.0)
        if self._at_identity_complete(task_name):
            return False
        attempts = int(
            dict(getattr(self, "_at_identity_attempts", {}) or {}).get(
                task_name, 0
            )
            or 0
        )
        if attempts >= 2:
            return False
        return (now - last_ts) >= max(
            ROUTEROS_IDENTITY_RETRY_INTERVAL_S,
            float(self._at_read_delay_s or 0.0),
        )

    def _refresh_at_identity_field(self, task_name: str) -> None:
        key_cmd = self._AT_IDENTITY_COMMANDS.get(task_name)
        if not key_cmd:
            return
        cache_key, cmd = key_cmd
        attempts = dict(getattr(self, "_at_identity_attempts", {}) or {})
        attempts[task_name] = int(
            attempts.get(task_name, 0) or 0
        ) + 1
        self._at_identity_attempts = attempts
        try:
            with self._physical_request_authority.lane_scope("boot_static"):
                raw = self._at_command(cmd)
            if raw:
                self._device_info_cache[cache_key] = self._clean_identity_value(raw, cache_key)
                self._compose_identity_model()
        except Exception as exc:
            log.debug("_refresh_at_identity_field(%s): suppressed %s", task_name, exc)
        finally:
            self._at_identity_ts[task_name] = time.time()




    def read_plmn(self) -> Optional[str]:
        """Return last acquired serving PLMN without causing physical I/O."""
        return self._plmn_cache or None


    def _read_serving_cell_extra(self) -> Dict[str, Any]:
        """Throttled read of QENG servingcell for TAC and dashboard facts.

        DL bandwidth comes from the monitor primary-band string (free),
        but TAC, UL bandwidth and TX power may only be in QENG servingcell.
        Refreshed on a slow cadence (~10s) since these values change
        only on band/cell changes.
        """
        now = time.time()
        if (
            self._last_serving_cell_extra
            and (now - self._last_serving_cell_extra_ts) < self._serving_cell_extra_interval_s
        ):
            return dict(self._last_serving_cell_extra)
        sc = self.read_serving_cell()
        if not sc:
            return dict(self._last_serving_cell_extra) if self._last_serving_cell_extra else {}
        result: Dict[str, Any] = {}
        # UL bandwidth (RB index → MHz)
        ul_rb = sc.get("ul_bw_rb", 0)
        if ul_rb is not None:
            ul_mhz = _rb_to_mhz(int(ul_rb))
            if ul_mhz > 0:
                result["ul_bw_mhz"] = ul_mhz
        # DL bandwidth from QENG when monitor did not provide it.
        dl_rb = sc.get("dl_bw_rb", 0)
        if dl_rb is not None:
            dl_mhz = _rb_to_mhz(int(dl_rb))
            if dl_mhz > 0:
                result["dl_bw_mhz_qeng"] = dl_mhz
        # TX power
        tx = sc.get("tx_power")
        if tx is not None and str(tx).strip() not in ("-", ""):
            tx_str = str(tx).strip()
            try:
                result["tx_power"] = f"{int(tx_str)} dBm"
            except (ValueError, TypeError):
                result["tx_power"] = tx_str
        tac = sc.get("tac")
        if tac is not None and str(tac).strip() not in ("", "-"):
            try:
                tac_text = str(tac).strip()
                result["tac"] = int(tac_text, 16) if re.search(r"[a-fA-F]", tac_text) else int(tac_text)
            except (ValueError, TypeError):
                pass
        state = str(sc.get("state") or "").strip().upper()
        if state in {"CONNECT", "CONNECTED"}:
            result["rrc"] = "Connected"
        elif state in {"NOCONN", "IDLE"}:
            result["rrc"] = "Idle"
        elif state:
            result["rrc"] = state
        if not self._trans_mode_cache:
            trans_mode = self._format_trans_mode(sc.get("trans_mode"))
            if trans_mode:
                self._trans_mode_cache = trans_mode
                self._trans_mode_source = "qeng_servingcell_once"
                result["trans_mode"] = trans_mode
                result["trans_mode_source"] = self._trans_mode_source
        for address, value in sc.get('_profile_extra', {}).items():
            key = {'slow.dl_bandwidth_mhz': 'dl_bw_mhz_qeng', 'slow.ul_bandwidth_mhz': 'ul_bw_mhz',
                'slow.tac': 'tac', 'radio.tx_power': 'tx_power', 'slow.rrc': 'rrc',
                'slow.trans_mode': 'trans_mode'}[address]
            if value is None: result.pop(key, None)
            else: result[key] = value
        projected = {key:value for key,value in sc.get('_profile_extra', {}).items()
            if key == 'slow.tac'}
        if '_profile_nr' in sc:
            projected.update(sc['_profile_nr'])
            result['_routeros_nr_context'] = self._routeros_nr_context(self._last_lte_monitor or {})
        if projected: result['_profile_values'] = projected
        self._last_serving_cell_extra = result
        self._last_serving_cell_extra_ts = now
        return dict(result)

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        """MikroTik neighbor scan is disabled by the official RouterOS profile."""
        return []

    def read_serving_cell(self) -> Dict[str, Any]:
        """Read serving cell engineering data via AT+QENG="servingcell".

        Returns dict with: state, mcc, mnc, cellid, pci, earfcn, band,
        dl_bw_rb, ul_bw_rb, tac, rsrp, rsrq, rssi, sinr, cqi, tx_power.
        """
        raw = self._at_command('AT+QENG="servingcell"')
        parsed = parse_qeng_servingcell(raw)
        nr = (parse_routeros_nr_qeng(raw) if self._definition.profile_id in _MIKROTIK_NR_PROFILE_IDS else {})
        if nr.get('nr_mode'):
            # NSA's separate LTE anchor and SA's NCI/TAC are not legacy LTE
            # columns. Only the NR fixedcodec owns this alternate payload.
            parsed = {'state': 'CONNECT' if nr.get('nr5g_active') is True else 'NOCONN'}
            monitor = self._last_lte_monitor or {}
            context = {'routeros_nr_read_source': 'qeng', 'routeros_nr_current_interface': True}
            # A valid monitor (even partial) stays primary. Decoding the unused
            # alternative cannot mark its missing fields unavailable globally.
            substantive = any(monitor.get('_profile_rf_values', {}).get(key) is not None
                for key in _MIKROTIK_NR_MONITOR_SCALARS
                if key not in {'radio.nr_mode', 'radio.nr5g_active'})
            if monitor.get('_routeros_nr_native_context', {}).get('partial_sa'):
                substantive = monitor['_routeros_nr_native_context']['has_nr_values']
            if (self._routeros_nr_monitor_context_current(monitor)
                    and not substantive):
                parsed['_profile_nr'] = self._profile_native_values(
                    {'AT+QENG="servingcell"': raw}, set(_MIKROTIK_NR_MONITOR_SCALARS), context=context)
                if parsed['_profile_nr'].get('radio.nr5g_active') is True:
                    # Network label is derived from the same qualified NR
                    # response; it gets this request's proof, not LTE freshness.
                    parsed['_profile_nr']['radio.network_type'] = 'NR ' + str(nr['nr_mode']) + ' (5G)'
            return parsed
        if parsed and getattr(self, '_definition', None) is not None:
            parsed['_profile_extra'] = self._profile_native_values(
                {'AT+QENG="servingcell"': raw}, {'slow.ul_bandwidth_mhz', 'slow.dl_bandwidth_mhz',
                    'slow.tac', 'radio.tx_power', 'slow.rrc', 'slow.trans_mode'},
                context={'previous_serving_extra': self._last_serving_cell_extra})
        return parsed

    def read_band_priority(self) -> List[int]:
        """Read modem band priority via AT+QCFG="lte/bandprior".

        Returns list of band numbers in priority order, e.g. [3, 1, 20].
        """
        if not self._at_live_polling_enabled:
            return []
        raw = self._at_command('AT+QCFG="lte/bandprior"')
        if not raw or "ERROR" in raw:
            return []
        try:
            # +QCFG: "lte/bandprior",03,07,01
            match = re.search(r'"lte/bandprior",([\d,]+)', raw)
            if match:
                return [int(b) for b in match.group(1).split(",")]
        except (ValueError, AttributeError):
            pass
        return []

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError(
            "mikrotik_direct_band_priority_write_forbidden:"
            "use_execute_physical_stage"
        )

    def _routeros_lte_interface_matches(self, rows: Any) -> List[Dict[str, Any]]:
        retained_id = str(getattr(self, "_lte_id", "") or "").strip()
        matches: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_row in rows or []:
            row = dict(raw_row)
            row_id = self._routeros_row_id(row)
            name = str(row.get("name") or "").strip().casefold()
            if name != "lte1" and not (retained_id and row_id == retained_id):
                continue
            identity = row_id or f"name:{name}:{len(matches)}"
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            matches.append(row)
        return matches

    def _read_lte_interface_rows_from_routeros(self) -> List[Dict[str, Any]]:
        if not self._routeros:
            return []
        try:
            lte = self._routeros.get_resource(self._portable_routeros_path('/interface/lte/print','/interface/lte'))
            return self._routeros_lte_interface_matches(lte.get())
        except Exception as exc:
            log.debug("_read_lte_interface_rows_from_routeros: suppressed %s", exc)
        return []

    def _read_lte_interface_row_from_routeros(self) -> Dict[str, Any]:
        matches = self._read_lte_interface_rows_from_routeros()
        if len(matches) == 1:
            return dict(matches[0])
        if matches:
            log.warning("RouterOS LTE interface identity is ambiguous: %d matches", len(matches))
        return {}

    def _read_routeros_lte_settings_row(self) -> Dict[str, Any]:
        if not self._routeros:
            return {}
        try:
            rows = self._routeros.get_resource("/interface/lte/settings").get()
        except Exception as exc:
            log.debug("_read_routeros_lte_settings_row: suppressed %s", exc)
            return {}
        return dict(rows[0]) if rows else {}

    def _refresh_routeros_lte_state_cache(
        self,
        force: bool = False,
        *,
        refresh_bands: bool = True,
        refresh_disabled: bool = True,
    ) -> None:
        """Compact RouterOS-only LTE state refresh.

        One `/interface/lte` fetch updates multiple cached facts. This is safe
        for the RouterOS lane and avoids separate reads for band mask and
        disabled state. It never touches LTE `at-chat`.
        """
        now = time.time()
        last_configured_ts = float(getattr(self, "_last_configured_bands_ts", 0.0) or 0.0)
        configured_refresh_s = float(getattr(self, "_configured_bands_refresh_s", 3600.0) or 3600.0)
        last_disabled_ts = float(getattr(self, "_last_lte_disabled_ts", 0.0) or 0.0)
        disabled_refresh_s = float(getattr(self, "_lte_disabled_refresh_s", 60.0) or 60.0)
        need_bands = bool(
            refresh_bands
            and (
                force
                or last_configured_ts <= 0
                or (now - last_configured_ts) >= configured_refresh_s
            )
        )
        need_disabled = bool(
            refresh_disabled
            and (
                force
                or last_disabled_ts <= 0
                or (now - last_disabled_ts) >= disabled_refresh_s
            )
        )
        if not need_bands and not need_disabled:
            return
        row = self._read_lte_interface_row_from_routeros()
        if not row:
            if need_bands:
                self._mark_configured_band_cache_unavailable("lte1_row_unavailable")
            return
        self._update_routeros_lte_state_from_row(
            row,
            observed_at=now,
            update_bands=need_bands,
            update_disabled=need_disabled,
        )

    def _update_routeros_lte_state_from_row(
        self,
        row: Mapping[str, Any],
        *,
        observed_at: Optional[float] = None,
        update_bands: bool = True,
        update_disabled: bool = True,
    ) -> None:
        """Update every cheap LTE-interface fact from one RouterOS row."""

        now = float(observed_at if observed_at is not None else time.time())
        self._last_lte_observed_id = self._routeros_row_id(row)
        if update_bands:
            try:
                bands = self._configured_bands_from_lte_interface_row(row)
            except ValueError as exc:
                self._mark_configured_band_cache_unavailable(str(exc))
            else:
                self._last_configured_bands = sorted(
                    {int(band) for band in bands if int(band) > 0}
                )
                self._last_configured_bands_ts = now
                self._last_configured_bands_status = "fresh"
                self._last_configured_bands_error = ""
        if update_disabled:
            disabled = self._routeros_optional_bool(row.get("disabled"))
            if disabled is not None:
                self._last_lte_disabled = bool(disabled)
                self._last_lte_disabled_ts = now
        running = self._routeros_optional_bool(row.get("running"))
        if running is not None:
            self._last_lte_running = bool(running)

    @staticmethod
    def _configured_bands_from_lte_interface_row(row: Mapping[str, Any]) -> List[int]:
        if "band" not in row:
            raise ValueError("band_field_missing")
        band_str = str(row.get("band") or "").strip()
        if not band_str:
            return []
        bands = [int(b) for b in re.findall(r"\d+", band_str)]
        if bands:
            return bands
        if band_str.lower() in {"auto", "all", "none", "default"}:
            return []
        raise ValueError(f"band_field_unparseable:{band_str[:60]}")

    def _read_configured_bands_from_routeros(self) -> List[int]:
        row = self._read_lte_interface_row_from_routeros()
        if not row:
            raise RuntimeError("lte1_row_unavailable")
        return self._configured_bands_from_lte_interface_row(row)

    @staticmethod
    def _band_pool_equal(observed: List[int], requested: List[int]) -> Optional[bool]:
        if not observed or not requested:
            return None
        return sorted({int(b) for b in observed if int(b) > 0}) == sorted(
            {int(b) for b in requested if int(b) > 0}
        )

    def get_current_bands(self) -> List[int]:
        """Return cached configured LTE band mask.

        Public consumers use cached Modem Live Module truth. The private
        RouterOS read is only used by connect/action/maintenance refresh.
        """
        return self._current_configured_bands()

    def _record_lte1_enabled_invariant(self, result: Mapping[str, Any]) -> None:
        current = dict(result or {})
        state = str(current.get("state") or "unknown")
        # Cache and fresh readback are two observation paths for the same
        # healthy fact.  Keep the reason only for states where it explains an
        # anomaly, repair or ownership deferral.
        semantic_reason = (
            "" if state == "enabled" else str(current.get("reason") or "")
        )
        fingerprint = (
            state,
            semantic_reason,
            current.get("disabled"),
            tuple(current.get("active_jobs") or ()),
        )
        previous = tuple(
            getattr(self, "_last_lte_enable_invariant_fingerprint", ()) or ()
        )
        self._last_lte_enable_invariant = copy.deepcopy(current)
        if fingerprint == previous:
            return
        self._last_lte_enable_invariant_fingerprint = fingerprint
        # The first normal enabled observation is routine.  Repairs, failures,
        # deferrals and recovery back to enabled are useful transition proof.
        if fingerprint[0] == "enabled" and not previous:
            return
        self._device_recovery_diagnostic_sequence = int(
            getattr(self, "_device_recovery_diagnostic_sequence", 0) or 0
        ) + 1
        severity = (
            "INFO"
            if fingerprint[0] in {"enabled", "repaired", "deferred"}
            else "WARNING"
        )
        diagnostic = ModemBackendDiagnostic(
            owner="54guc.modem.mikrotik",
            sequence=int(self._device_recovery_diagnostic_sequence),
            observed_at=float(current.get("observed_at", 0.0) or time.time()),
            kind="mikrotik_lte1_enabled_invariant",
            backend="mikrotik",
            severity=severity,
            data={"lte1_enabled_invariant": copy.deepcopy(current)},
        )
        self._last_device_recovery_diagnostic = diagnostic.to_payload()

    def _ensure_lte1_enabled(self, *, force: bool = False) -> Dict[str, Any]:
        """Keep the exact RouterOS lte1 interface enabled in Live operation.

        This uses only `/interface/lte` and `/system/script/job`.  It does not
        enter the modem AT lane.  A RouterOS recovery/boot-guard job may finish
        its own bounded enable transaction; every other disabled observation
        is repaired here and verified by a fresh RouterOS readback.
        """

        now = time.time()
        result: Dict[str, Any] = {
            "owner": "54guc.modem_live",
            "source": "routeros_api",
            "observed_at": now,
            "ok": False,
            "state": "unavailable",
            "reason": "routeros_api_unavailable",
            "disabled": None,
            "repaired": False,
            "active_jobs": [],
        }
        if not self._routeros:
            self._record_lte1_enabled_invariant(result)
            return result

        cache_age = now - float(getattr(self, "_last_lte_disabled_ts", 0.0) or 0.0)
        if (
            not force
            and getattr(self, "_last_lte_disabled_ts", 0.0)
            and cache_age < max(0.5, float(getattr(self, "_lte_disabled_refresh_s", 1.5) or 1.5))
            and not bool(getattr(self, "_last_lte_disabled", False))
        ):
            result.update(
                {
                    "ok": True,
                    "state": "enabled",
                    "reason": "cached_enabled",
                    "disabled": False,
                    "cache_age_s": max(0.0, cache_age),
                }
            )
            self._record_lte1_enabled_invariant(result)
            return result

        result = self._run_lte1_enabled_invariant(
            self._routeros,
            expected_lte_id=str(self._lte_id or ""),
        )
        row = dict(result.get("row") or {})
        if row:
            self._update_routeros_lte_state_from_row(row)
        if result.get("disabled") is not None:
            self._last_lte_disabled = bool(result.get("disabled"))
        if bool(result.get("repaired", False)):
            self._last_lte_monitor_ts = 0.0
            self._invalidate_routeros_recovery_observation(topology=False)
            log.warning(
                "RouterOS lte1 was disabled; Modem Live enabled and verified it"
            )
        self._record_lte1_enabled_invariant(result)
        return result

    def _read_lte_interface_disabled(self, force: bool = False) -> bool:
        now = time.time()
        if (
            not force
            and (now - self._last_lte_disabled_ts) < max(0.5, self._lte_disabled_refresh_s)
        ):
            return bool(self._last_lte_disabled)
        self._refresh_routeros_lte_state_cache(
            force=force,
            refresh_bands=False,
            refresh_disabled=True,
        )
        return bool(self._last_lte_disabled)

    @staticmethod
    def _routeros_optional_bool(value: Any) -> Optional[bool]:
        if value is None or str(value).strip() == "":
            return None
        return _ros_bool(value)

    @staticmethod
    def _routeros_optional_int(value: Any) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _routeros_read_error(label: str, exc: Exception) -> str:
        detail = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
        if len(detail) > 120:
            detail = detail[:117] + "..."
        return f"{label}:{type(exc).__name__}:{detail}" if detail else f"{label}:{type(exc).__name__}"

    def _routeros_recovery_rows(
        self,
        path: str,
        *,
        label: str,
        errors: List[str],
        section_reads: Dict[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            resource = self._routeros.get_resource(path)
            rows = [_decode_row(dict(row)) for row in (resource.get() or [])]
        except Exception as exc:
            errors.append(self._routeros_read_error(label, exc))
            section_reads[label] = {
                "source": path, "observed_at": None,
                "attempted_at": time.time(), "read_status": "read_failed",
            }
            return None
        completed_at = time.time()
        section_reads[label] = {
            "source": path, "observed_at": completed_at,
            "attempted_at": completed_at, "read_status": "ok",
        }
        return rows

    @staticmethod
    def _recovery_status_for_publication(status: Mapping[str, Any]) -> Dict[str, Any]:
        """Age the retained reads, never the contents. This performs no modem I/O."""

        if not status:
            return {}
        public = copy.deepcopy(dict(status))
        now = time.time()
        cycle_at = float(public.get("observed_at", 0.0) or 0.0)
        public["captured_at"] = now
        public["age_s"] = now - cycle_at if 0.0 < cycle_at <= now else None
        stale_sections = []
        for name, reading in public["section_reads"].items():
            source_at = reading["observed_at"]
            age = now - source_at if source_at is not None else None
            reading["age_s"] = age if age is not None and age >= 0.0 else None
            if reading["read_status"] != "ok":
                freshness = reading["read_status"]
            elif age is None or age < 0.0:
                freshness = "clock_uncertain"
            elif age >= ROUTEROS_RECOVERY_STATUS_INTERVAL_S:
                freshness = "stale"
            elif source_at < cycle_at:
                freshness = "cached"
            else:
                freshness = "fresh"
            reading["freshness"] = freshness
            if freshness in {"stale", "clock_uncertain"}:
                stale_sections.append(name)
        public["stale_sections"] = sorted(stale_sections)
        public["stale"] = bool(stale_sections)
        return public

    def _device_recovery_transition_fingerprint(
        self,
        status: Mapping[str, Any],
    ) -> Dict[str, Any]:
        scheduler = dict(status.get("scheduler") or {})
        lte = dict(status.get("lte_interface") or {})
        scripts = dict(status.get("scripts") or {})
        environment = dict(status.get("environment") or {})
        system_watchdog = dict(status.get("system_watchdog") or {})
        raw_state = str(status.get("state") or "unknown")
        fingerprint_state = (
            "healthy"
            if raw_state == "checking"
            and str(status.get("package_state") or "") == "complete"
            and lte.get("running") is True
            else raw_state
        )
        return {
            "state": fingerprint_state,
            "lte_interface": {
                "present_count": lte.get("present_count"),
                "disabled": lte.get("disabled"),
                "running": lte.get("running"),
            },
            "scheduler": {
                "match_count": scheduler.get("match_count"),
                "disabled": scheduler.get("disabled"),
            },
            "boot_guard_scheduler": {
                "match_count": dict(status.get("boot_guard_scheduler") or {}).get(
                    "match_count"
                ),
                "disabled": dict(status.get("boot_guard_scheduler") or {}).get(
                    "disabled"
                ),
            },
            "package_state": str(status.get("package_state") or "unknown"),
            "script_counts": {
                name: dict(scripts.get(name) or {}).get("count")
                for name in ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS
            },
            # The normal watchdog and check scripts run repeatedly. Recording
            # those counters would create noise. Recover/reset counters are
            # different: a change proves that RouterOS attempted recovery even
            # when the job finished before this observation.
            "recovery_runs": {
                name: {
                    "run_count": dict(scripts.get(name) or {}).get("run_count"),
                    "last_started": dict(scripts.get(name) or {}).get(
                        "last_started"
                    ),
                }
                for name in (
                    "lte_watchdog_recover",
                    "lte_watchdog_reset_state",
                )
            },
            # Routine watchdog/check jobs are intentionally absent here. They
            # happen every minute and are not recovery transitions.
            "active_jobs": list(status.get("active_jobs") or ()),
            "environment": {
                name: environment.get(name)
                for name in (
                    "lteWatchdogFailCount",
                    "lteWatchdogLastError",
                    "lteWatchdogLastRecoveryStatus",
                    "lteWatchdogLastRecoveryError",
                )
            },
            "system_watchdog": {
                "enabled": system_watchdog.get("enabled"),
                "watch_address": system_watchdog.get("watch_address"),
            },
            "stale_sections": list(status.get("stale_sections") or ()),
        }

    def _publish_device_recovery_transition(
        self,
        status: Mapping[str, Any],
    ) -> None:
        fingerprint = self._device_recovery_transition_fingerprint(status)
        # Read ages belong in evidence, but age alone is not a new recovery.
        comparison = {k: v for k, v in fingerprint.items() if k != "stale_sections"}
        if comparison == dict(self._last_device_recovery_fingerprint or {}):
            return
        self._last_device_recovery_fingerprint = copy.deepcopy(comparison)
        self._device_recovery_diagnostic_sequence += 1
        state = str(status.get("state") or "unknown")
        severity = (
            "INFO"
            if state in {"healthy", "observed", "checking", "not_installed", "resetting"}
            else "WARNING"
        )
        diagnostic = ModemBackendDiagnostic(
            owner="54guc.modem.mikrotik",
            sequence=int(self._device_recovery_diagnostic_sequence),
            observed_at=float(status.get("observed_at", 0.0) or time.time()),
            kind="mikrotik_device_recovery_state_changed",
            backend="mikrotik",
            severity=severity,
            data={
                "device_recovery_state": fingerprint,
                "complete": bool(status.get("complete", False)),
                "errors": list(status.get("errors") or ()),
                "captured_at": status.get("captured_at"),
                "section_reads": copy.deepcopy(status.get("section_reads") or {}),
            },
        )
        self._last_device_recovery_diagnostic = diagnostic.to_payload()

    @staticmethod
    def _routeros_status_row_summary(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "name": str(row.get("name") or "").strip(),
            "disabled": MikroTikModem._routeros_optional_bool(row.get("disabled")),
            "run_count": MikroTikModem._routeros_optional_int(
                row.get("run-count") or row.get("run_count")
            ),
            "last_started": str(
                row.get("last-started") or row.get("last_started") or ""
            ).strip(),
            "next_run": str(
                row.get("next-run") or row.get("next_run") or ""
            ).strip(),
        }

    def routeros_watchdog_script_status(
        self,
        script_name: str = "lte_watchdog",
        *,
        force_topology: bool = False,
    ) -> Dict[str, Any]:
        steps = self._iter_routeros_watchdog_status(script_name, force_topology=force_topology)
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return done.value

    def _iter_routeros_watchdog_status(
        self, script_name: str = "lte_watchdog", *, force_topology: bool = False,
    ):
        """Read RouterOS-owned recovery state without changing it."""

        name = str(script_name or ROUTEROS_LTE_WATCHDOG_SCRIPT).strip()
        if name != ROUTEROS_LTE_WATCHDOG_SCRIPT:
            raise ValueError("routeros_watchdog_status_canonical_script_required")
        now = time.time()
        cached = dict(self._last_watchdog_script_status or {})
        cache_ts = float(self._last_watchdog_script_status_ts or 0.0)
        if cached and cache_ts > 0.0 and (now - cache_ts) < ROUTEROS_RECOVERY_STATUS_INTERVAL_S:
            return self._recovery_status_for_publication(cached)
        if self._routeros is None:
            return {
                "schema": "DeviceRecoveryState.v1",
                "owner": "54guc.modem_live",
                "source": "routeros_api",
                "observed_at": now,
                "available": False,
                "complete": False,
                "stale": False,
                "state": "unavailable",
                "errors": ["routeros_api:unavailable"],
                "stale_sections": [],
                "captured_at": now,
                "section_reads": {},
            }

        errors: List[str] = []
        if float(getattr(self, "_last_lte_disabled_ts", 0.0) or 0.0) <= 0.0:
            self._refresh_routeros_lte_state_cache(
                force=True,
                refresh_bands=True,
                refresh_disabled=True,
            )
            yield None
        lte_row: Dict[str, Any] = {}
        if str(getattr(self, "_lte_id", "") or "").strip():
            lte_row = {
                ".id": str(self._lte_id),
                "name": "lte1",
                "disabled": (
                    "yes" if bool(getattr(self, "_last_lte_disabled", False)) else "no"
                ),
            }
            running = getattr(self, "_last_lte_running", None)
            if running is not None:
                lte_row["running"] = "yes" if bool(running) else "no"
        lte_rows = [lte_row] if lte_row else []
        lte_read_at = float(getattr(self, "_last_lte_disabled_ts", 0.0) or 0.0)
        section_reads: Dict[str, Any] = {
            "lte_interface": {
                "source": "/interface/lte", "observed_at": lte_read_at or None,
                "attempted_at": lte_read_at or None,
                "read_status": "ok" if lte_read_at > 0.0 else "unavailable",
            },
            "script_jobs": {
                "source": "/system/script/job", "observed_at": None,
                "attempted_at": None, "read_status": "not_read",
            },
        }

        topology_cache = dict(
            getattr(self, "_routeros_recovery_topology_cache", {}) or {}
        )
        topology_attempts = int(
            getattr(self, "_routeros_recovery_topology_attempts", 0) or 0
        )
        topology_needed = bool(
            force_topology
            or not topology_cache
            or (
                bool(topology_cache.get("errors"))
                and topology_attempts < 2
            )
        )
        if topology_needed:
            topology_errors: List[str] = []
            topology_reads: Dict[str, Any] = {}
            scheduler_rows = self._routeros_recovery_rows(
                "/system/scheduler", label="scheduler", errors=topology_errors,
                section_reads=topology_reads,
            ) or []
            yield None
            resource_rows = self._routeros_recovery_rows(
                "/system/resource", label="system_resource", errors=topology_errors,
                section_reads=topology_reads,
            ) or []
            yield None
            watchdog_rows = self._routeros_recovery_rows(
                "/system/watchdog", label="system_watchdog", errors=topology_errors,
                section_reads=topology_reads,
            ) or []
            yield None
            topology_cache = {
                "scheduler_rows": scheduler_rows,
                "resource_rows": resource_rows,
                "watchdog_rows": watchdog_rows,
                "errors": list(topology_errors),
                "observed_at": now,
                "section_reads": topology_reads,
            }
            self._routeros_recovery_topology_cache = copy.deepcopy(topology_cache)
            self._routeros_recovery_topology_ts = now
            self._routeros_recovery_topology_attempts = topology_attempts + 1
        else:
            scheduler_rows = list(topology_cache.get("scheduler_rows") or [])
            resource_rows = list(topology_cache.get("resource_rows") or [])
            watchdog_rows = list(topology_cache.get("watchdog_rows") or [])
        errors.extend(list(topology_cache.get("errors") or []))
        section_reads.update(copy.deepcopy(topology_cache["section_reads"]))

        # Script run counters are changing recovery evidence. They stay on the
        # minute lane; static scheduler/router/watchdog topology does not.
        script_rows = self._routeros_recovery_rows(
            "/system/script", label="scripts", errors=errors, section_reads=section_reads,
        ) or []
        yield None
        environment_errors: List[str] = []
        environment_rows = self._routeros_recovery_rows(
            "/system/script/environment",
            label="script_environment",
            errors=environment_errors,
            section_reads=section_reads,
        )
        yield None
        if environment_rows is None:
            errors.extend(environment_errors)
            environment_rows = []

        environment_probe = {
            str(row.get("name") or "").strip(): row.get("value")
            for row in environment_rows
        }
        fail_count_probe = self._routeros_optional_int(
            environment_probe.get("lteWatchdogFailCount")
        )
        recovery_status_probe = str(
            environment_probe.get("lteWatchdogLastRecoveryStatus") or ""
        ).strip().lower()
        lte_risk = bool(
            getattr(self, "_last_lte_disabled", False)
            or getattr(self, "_last_lte_running", None) is False
        )
        recovery_risk = bool(
            lte_risk
            or (fail_count_probe is not None and fail_count_probe > 0)
            or recovery_status_probe in {"running", "starting", "recovering"}
        )
        job_rows: List[Dict[str, Any]] = []
        if topology_needed or recovery_risk:
            job_rows = self._routeros_recovery_rows(
                "/system/script/job", label="script_jobs", errors=errors,
                section_reads=section_reads,
            ) or []
            yield None

        lte_matches = [
            row
            for row in lte_rows
            if str(row.get("name") or "").strip().casefold() == "lte1"
            or (
                str(getattr(self, "_lte_id", "") or "").strip()
                and str(row.get(".id") or row.get("id") or "").strip()
                == str(getattr(self, "_lte_id", "") or "").strip()
            )
        ]
        lte_row = lte_matches[0] if lte_matches else {}
        lte_interface = {
            "present_count": len(lte_matches),
            "name": str(lte_row.get("name") or "lte1" if lte_matches else "").strip(),
            "disabled": self._routeros_optional_bool(lte_row.get("disabled")),
            "running": self._routeros_optional_bool(lte_row.get("running")),
        }

        scheduler_roles = self._routeros_watchdog_scheduler_roles(scheduler_rows)
        schedulers_by_script = dict(scheduler_roles.get("by_script") or {})
        scheduler_matches = list(
            schedulers_by_script.get(ROUTEROS_LTE_WATCHDOG_SCRIPT) or []
        )
        scheduler_row = scheduler_matches[0] if len(scheduler_matches) == 1 else {}
        scheduler = self._routeros_status_row_summary(scheduler_row)
        scheduler["match_count"] = len(scheduler_matches)
        scheduler["present"] = bool(scheduler_matches)
        scheduler["enabled"] = (
            not bool(scheduler["disabled"])
            if scheduler["disabled"] is not None
            else None
        )
        boot_guard_matches = list(
            schedulers_by_script.get(ROUTEROS_LTE_WATCHDOG_BOOT_GUARD_SCRIPT)
            or []
        )
        boot_guard_row = (
            boot_guard_matches[0] if len(boot_guard_matches) == 1 else {}
        )
        boot_guard_scheduler = self._routeros_status_row_summary(boot_guard_row)
        boot_guard_scheduler["match_count"] = len(boot_guard_matches)
        boot_guard_scheduler["present"] = bool(boot_guard_matches)
        boot_guard_scheduler["enabled"] = (
            not bool(boot_guard_scheduler["disabled"])
            if boot_guard_scheduler["disabled"] is not None
            else None
        )
        scheduled_helpers = {
            helper: len(list(schedulers_by_script.get(helper) or []))
            for helper in ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS[1:]
            if schedulers_by_script.get(helper)
        }
        unclassified_schedulers = [
            self._routeros_status_row_summary(row)
            for row in list(scheduler_roles.get("unclassified") or [])
        ]

        scripts: Dict[str, Dict[str, Any]] = {}
        for required_name in ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS:
            matches = [
                row
                for row in script_rows
                if str(row.get("name") or "").strip().casefold()
                == required_name.casefold()
            ]
            summary = self._routeros_status_row_summary(matches[0] if matches else {})
            summary["count"] = len(matches)
            summary["present"] = bool(matches)
            scripts[required_name] = summary

        job_roles = self._routeros_watchdog_job_roles(job_rows)
        routine_jobs = list(job_roles["routine"])
        recovery_jobs = list(job_roles["recovery"])
        boot_guard_jobs = list(job_roles["boot_guard"])
        reset_jobs = list(job_roles["reset"])
        unknown_jobs = list(job_roles["unknown"])
        # Downstream treats active_jobs as a reason to defer CGF recovery. Only
        # jobs that can alter recovery state belong in that contract.
        active_jobs = sorted({*recovery_jobs, *boot_guard_jobs, *reset_jobs})
        environment_by_name = {
            str(row.get("name") or "").strip(): row.get("value")
            for row in environment_rows
        }
        environment = {
            env_name: environment_by_name.get(env_name)
            for env_name in ROUTEROS_LTE_WATCHDOG_ENVIRONMENT_NAMES
        }
        environment["lteWatchdogFailCount"] = self._routeros_optional_int(
            environment.get("lteWatchdogFailCount")
        )

        resource_row = resource_rows[0] if resource_rows else {}
        router = {
            "uptime": str(resource_row.get("uptime") or "").strip(),
            "version": str(resource_row.get("version") or "").strip(),
        }
        watchdog_row = watchdog_rows[0] if watchdog_rows else {}
        watchdog_timer = self._routeros_optional_bool(
            watchdog_row.get("watchdog-timer") or watchdog_row.get("watchdog_timer")
        )
        watch_address = str(
            watchdog_row.get("watch-address") or watchdog_row.get("watch_address") or ""
        ).strip()
        system_watchdog = {
            "available": bool(watchdog_rows),
            "enabled": bool(watchdog_timer or watch_address) if watchdog_timer is not None else bool(watch_address),
            "watchdog_timer": watchdog_timer,
            "watch_address": watch_address,
            "ping_timeout": str(
                watchdog_row.get("ping-timeout") or watchdog_row.get("ping_timeout") or ""
            ).strip(),
            "ping_start_after_boot": str(
                watchdog_row.get("ping-start-after-boot")
                or watchdog_row.get("ping_start_after_boot")
                or ""
            ).strip(),
        }

        sections: Dict[str, Any] = {
            "lte_interface": lte_interface,
            "scheduler": scheduler,
            "boot_guard_scheduler": boot_guard_scheduler,
            "scheduled_helpers": scheduled_helpers,
            "unclassified_schedulers": unclassified_schedulers,
            "scripts": scripts,
            "active_jobs": active_jobs,
            "routine_jobs": routine_jobs,
            "unknown_jobs": unknown_jobs,
            "environment": environment,
            "router": router,
            "system_watchdog": system_watchdog,
        }
        required_counts = [
            int(dict(scripts.get(required_name) or {}).get("count", 0) or 0)
            for required_name in ROUTEROS_LTE_WATCHDOG_REQUIRED_SCRIPTS
        ]
        fail_count = self._routeros_optional_int(
            environment.get("lteWatchdogFailCount")
        )
        last_status = str(environment.get("lteWatchdogLastStatus") or "").strip().lower()
        last_error = str(environment.get("lteWatchdogLastError") or "").strip()
        recovery_status = str(
            environment.get("lteWatchdogLastRecoveryStatus") or ""
        ).strip().lower()
        recovery_error = str(
            environment.get("lteWatchdogLastRecoveryError") or ""
        ).strip()
        any_required_script = any(count > 0 for count in required_counts)
        topology_absent = bool(
            not scheduler_matches
            and not boot_guard_matches
            and not any_required_script
            and not scheduled_helpers
            and not unclassified_schedulers
        )
        topology_valid = bool(
            len(scheduler_matches) == 1
            and all(count == 1 for count in required_counts)
            and len(boot_guard_matches) <= 1
            and not scheduled_helpers
            and not unclassified_schedulers
        )
        package_state = (
            "not_installed"
            if topology_absent
            else "complete"
            if topology_valid
            else "misconfigured"
        )
        recovery_started_without_job = bool(
            recovery_status == "running" or recovery_status.startswith("starting")
        ) and not bool(recovery_jobs)
        recovery_active = bool(recovery_jobs)
        boot_guard_active = bool(boot_guard_jobs)
        reset_active = bool(reset_jobs)
        device_recovery_active = bool(
            recovery_active
            or boot_guard_active
            or reset_active
        )
        if errors:
            state = "read_incomplete"
        elif lte_interface.get("present_count") != 1:
            state = "misconfigured"
        elif recovery_active or boot_guard_active:
            state = "recovering"
        elif reset_active:
            state = "resetting"
        elif topology_absent:
            state = "not_installed"
        elif not topology_valid:
            state = "misconfigured"
        elif lte_interface.get("disabled") is True:
            state = "lte_disabled"
        elif (
            bool(last_error)
            or bool(recovery_error)
            or recovery_started_without_job
            or (fail_count is not None and fail_count > 0)
        ):
            state = "degraded"
        elif scheduler.get("disabled") is True:
            state = "watchdog_disabled"
        elif lte_interface.get("running") is False:
            state = "lte_not_running"
        elif routine_jobs or (
            last_status == "failed"
            and fail_count == 0
            and not last_error
            and not recovery_error
        ):
            state = "checking"
        elif last_status == "failed":
            state = "degraded"
        elif lte_interface.get("running") is True and last_status == "stable":
            state = "healthy"
        else:
            state = "observed"

        status = {
            "schema": "DeviceRecoveryState.v1",
            "owner": "54guc.modem_live",
            "source": "routeros_api",
            "observed_at": now,
            "available": bool(
                lte_rows
                or scheduler_rows
                or script_rows
                or resource_rows
                or watchdog_rows
                or environment_rows
            ),
            "complete": not bool(errors),
            "section_reads": section_reads,
            "errors": list(errors),
            "state": state,
            "active": device_recovery_active,
            "package_state": package_state,
            **sections,
        }
        self._last_watchdog_script_status = copy.deepcopy(status)
        self._last_watchdog_script_status_ts = now
        self._last_watchdog_script_error = ";".join(errors)
        status = self._recovery_status_for_publication(status)
        self._publish_device_recovery_transition(status)
        return status

    def configure_routeros_watchdog_globals(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        del values
        raise RuntimeError(
            "mikrotik_direct_watchdog_configuration_forbidden:"
            "routeros_external_owner"
        )

    def _write_routeros_global_state(
        self,
        values: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Write RouterOS v7 globals through one explicit boundary adapter.

        Existing globals are updated through their exact environment rows. If
        any named global does not exist yet, RouterOS must create it by running
        one bounded temporary script. A transport or update failure never
        switches paths, and temporary-script cleanup is mandatory.
        """

        if not values:
            return {"ok": True, "mode": "no_change", "applied": {}}
        try:
            environment = self._routeros.get_resource(
                "/system/script/environment"
            )
            rows = environment.get()
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"routeros_global_environment_read_failed:{exc}",
                "applied": {},
            }

        by_name = {
            str(row.get("name") or "").strip(): row
            for row in rows or []
            if str(row.get("name") or "").strip()
        }
        if all(name in by_name for name in values):
            set_fn = getattr(environment, "set", None)
            if not callable(set_fn):
                raise RuntimeError("routeros_global_environment_set_unavailable")
            for name, value in values.items():
                row = by_name[name]
                row_id = str(row.get(".id") or row.get("id") or "").strip()
                if not row_id:
                    raise RuntimeError(
                        f"routeros_global_environment_id_missing:{name}"
                    )
                set_fn(
                    id=row_id,
                    value=_routeros_environment_value(value),
                )
            return {
                "ok": True,
                "mode": "existing_environment",
                "path": "/system/script/environment",
                "applied": dict(values),
            }

        return self._create_routeros_globals_with_temporary_script(values)

    def _create_routeros_globals_with_temporary_script(
        self,
        values: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Create missing RouterOS globals and prove temporary cleanup."""

        try:
            resource = self._routeros.get_resource("/system/script")
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"routeros_global_script_resource_failed:{exc}",
                "applied": {},
            }

        add_fn = getattr(resource, "add", None)
        call_fn = getattr(resource, "call", None)
        remove_fn = getattr(resource, "remove", None)
        get_fn = getattr(resource, "get", None)
        if not all(callable(fn) for fn in (add_fn, call_fn, remove_fn, get_fn)):
            raise RuntimeError("routeros_temporary_global_writer_incomplete")

        script_name = f"54guc_qnwlock_guard_set_{int(time.time() * 1000)}"
        source = "\n".join(
            f":global {str(name or '').strip()} {_routeros_script_literal(value)}"
            for name, value in values.items()
            if str(name or "").strip()
        )
        if not source:
            raise RuntimeError("routeros_temporary_global_writer_empty_source")

        script_id = ""
        created = False
        operation_error: Exception | None = None
        try:
            add_fn(name=script_name, source=source, policy="read,write,test")
            created = True
            matches = [
                row
                for row in get_fn() or []
                if str(row.get("name") or "").strip() == script_name
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "routeros_temporary_global_writer_identity_invalid:"
                    f"found_{len(matches)}"
                )
            script_id = str(
                matches[0].get(".id") or matches[0].get("id") or ""
            ).strip()
            if not script_id:
                raise RuntimeError(
                    "routeros_temporary_global_writer_identity_missing"
                )
            call_fn("run", {"number": script_id})
        except Exception as exc:
            operation_error = exc
        finally:
            if created:
                try:
                    if script_id:
                        remove_fn(id=script_id)
                    else:
                        remove_fn(numbers=script_name)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "routeros_temporary_global_writer_cleanup_failed"
                    ) from cleanup_error
        if operation_error is not None:
            return {
                "ok": False,
                "reason": (
                    "routeros_temporary_global_writer_failed:"
                    f"{operation_error}"
                ),
                "applied": {},
            }
        return {
            "ok": True,
            "mode": "temporary_script",
            "script_name": script_name,
            "script_id": script_id,
            "applied": dict(values),
        }

    def read_sms_messages(self, *, limit: int = 20, unread_only: bool = False) -> List[Dict[str, Any]]:
        """Reject the retired inbox path without accessing RouterOS."""
        del limit, unread_only
        raise RuntimeError("mikrotik_sms_unavailable_full_storage_recovery_unverified")

    def send_sms_message(self, number: str, text: str) -> bool:
        """Outbound RouterOS SMS is outside CGF's read-only contract."""
        del number, text
        return False

    def delete_sms_message(self, message_id: str) -> bool:
        """RouterOS inbox mutation is outside CGF's read-only contract."""
        del message_id
        return False

    def run_routeros_watchdog_script(self, script_name: str = "lte_watchdog") -> Dict[str, Any]:
        del script_name
        raise RuntimeError(
            "mikrotik_direct_watchdog_run_forbidden:"
            "routeros_external_owner"
        )

    def get_capabilities(self):
        base = get_capabilities("mikrotik")
        if not bool(getattr(self, "_runtime_capability_probe_enabled", True)):
            return copy.deepcopy(base)
        runtime_capabilities = getattr(self, "_runtime_capabilities", None)
        if getattr(self, "_last_lte_monitor", None) is None and runtime_capabilities is None:
            return copy.deepcopy(base)
        now = time.time()
        probe_interval_s = max(0.0, float(getattr(self, "_runtime_capability_probe_interval_s", 300.0)))
        if (
            runtime_capabilities is not None
            and (now - float(getattr(self, "_runtime_capabilities_ts", 0.0) or 0.0))
            < probe_interval_s
        ):
            return copy.deepcopy(runtime_capabilities)
        caps = self._probe_runtime_capabilities(force=False)
        self._runtime_capabilities = caps
        self._runtime_capabilities_ts = now
        return copy.deepcopy(caps)

    def _radio_mode_status(self, lane, *, required=False):
        from arch4.modem.mikrotik_radio_mode import status
        return status(self, lane, required=required)

    def _probe_runtime_capabilities(self, force: bool = False) -> ModemCapabilities:
        """
        Probe mutable capabilities on the live modem session.
        Probes are non-mutating and safe:
          - Restart command support: AT+CFUN?
        """
        base = get_capabilities("mikrotik")
        if (not self._routeros or not self._lte_id) and not force:
            return base
        caps = copy.deepcopy(base)
        caps.supports_cell_lock = bool(self._routeros and self._lte_id)
        try:
            cfun_raw = self._at_command("AT+CFUN?", post_delay_s=0.4)
            cfun_text = str(cfun_raw or "")
            cfun_ok = bool(cfun_text) and ("ERROR" not in cfun_text.upper())
            caps.supports_restart = bool(cfun_ok and "+CFUN:" in cfun_text)
        except Exception:
            caps.supports_restart = False
        try:
            if isinstance(self._last_status, dict):
                self._last_status["cap_probe_ts"] = time.time()
                self._last_status["cap_probe_cell_lock"] = bool(caps.supports_cell_lock)
                self._last_status["cap_probe_restart"] = bool(caps.supports_restart)
        except Exception as exc:
            log.debug("unknown: suppressed %s", exc)
            pass
        return caps

    def get_last_status(self) -> Dict[str, Any]:
        status = dict(self._last_status)
        if status.get("device_recovery_state"):
            status["device_recovery_state"] = self._recovery_status_for_publication(
                status["device_recovery_state"]
            )
        return build_public_modem_status(status)

    def _maintenance_status(self) -> Dict[str, Any]:
        # Maintenance receipts must contain the caches they just refreshed,
        # without causing another live read merely to format their values.
        status = dict(self._last_status)
        status.update(self._device_info_cache)
        status["provider_plmn"] = self._plmn_cache
        status["provider_name"] = getattr(self, "_provider_name_cache", "")
        status.update(self._traffic_volume_cache)
        status.update({address.split('.',1)[1]:value for address,value in self._portable_at_values().items()})
        for address, value in self._portable_at_values().items():
            if address in _MIKROTIK_NR_MONITOR_SCALARS:
                key = _MIKROTIK_NR_MONITOR_SCALARS[address]
                public = {'nr_mode': 'nr5g_mode', 'nr_cells': 'nr5g_cells',
                          'nr_bandwidth_mhz': 'nr5g_nsa_bandwidth'}.get(
                    key, key if key == 'nr5g_active' else key.replace('nr_', 'nr5g_', 1))
                status[public] = value
        extra = self._current_serving_cell_extra()
        status.update({key: value for key, value in extra.items()
                       if key in {"tac", "rrc", "tx_power", "trans_mode", "trans_mode_source"}})
        if "ul_bw_mhz" in extra:
            status["ul_bandwidth_mhz"] = extra["ul_bw_mhz"]
            status["ul_bandwidth"] = f"{extra['ul_bw_mhz']:.0f} MHz"
        status["lte_interface_disabled"] = self._last_lte_disabled
        status["configured_band_pool"] = self._current_configured_bands()
        status["configured_band_pool_status"] = self._last_configured_bands_status
        status["configured_band_pool_error"] = self._last_configured_bands_error
        status["configured_band_pool_last_sample_ts"] = self._last_configured_bands_ts
        status["configured_band_pool_age_s"] = (
            max(0.0, time.time() - self._last_configured_bands_ts)
            if self._last_configured_bands_ts > 0 else None
        )
        wifi, wifi24, wifi5 = self._current_wifi_status()
        status.update(wifi_enabled=wifi, wifi_24ghz_enabled=wifi24, wifi_5ghz_enabled=wifi5)
        status["device_recovery_state"] = self._recovery_status_for_publication(
            self._last_watchdog_script_status
        )
        status["backend_diagnostic"] = copy.deepcopy(self._last_device_recovery_diagnostic)
        return build_public_modem_status(status)

    def get_report_status(self) -> Dict[str, Any]:
        """Return cached public status for reporting without live modem I/O."""
        return self.get_last_status()

    def _refresh_static_info(self, force: bool = False) -> None:
        with self._physical_request_authority.lane_scope("boot_static"):
            self._refresh_routeros_identity_cache(force=force)

    # ─── Internal helpers ───────────────────────────────────

    def _at_command(
        self,
        cmd: str,
        *,
        mutating: bool = False,
        post_delay_s: Optional[float] = None,
        wait_all_output: bool = False,
    ) -> Optional[str]:
        """
        Send AT command via RouterOS at-chat with proper timing.

        Handles:
          - .id selector (not numeric index)
          - Bus guard (300ms between commands)
          - Post-command delay (short for reads, longer for mutating writes)
          - Optional wait=yes only for rare multiline diagnostics
          - String key decoding (routeros_api quirk)
        """
        with self._at_lock:
            if not self._routeros or not self._lte_id:
                return None

            definition=getattr(self,'_definition',None)
            if not mutating and definition is not None and definition.has_request(cmd):
                cmd=definition.request(cmd,protocol='routeros_at')['input']

            # After mutating actions (band/cell/capability), temporarily pause AT reads
            # so RRC re-establishment can complete without management-pressure collisions.
            if (not mutating) and (time.time() < self._at_blackout_until):
                return None

            # Bus guard. RouterOS API supports tagged commands, but LTE at-chat
            # is a modem serial lane behind RouterOS and must stay one-at-a-time.
            elapsed = time.time() - self._last_at_time
            min_gap = max(0.0, float(self._at_bus_guard_s))
            if elapsed < min_gap:
                time.sleep(min_gap - elapsed)

            try:
                started = time.time()
                res = self._routeros.get_binary_resource("/interface/lte")
                params = {
                    ".id": self._lte_id,
                    "input": cmd,
                }
                # RouterOS wait=yes waits up to 5s for complete multiline
                # output. Keep it opt-in: normal polling must stay short.
                if bool(wait_all_output):
                    params["wait"] = "yes"
                response = res.call("at-chat", params)
                self._last_at_time = time.time()

                # Extract output from response
                output = ""
                for sentence in response:
                    row = _decode_row(sentence)
                    val = row.get("output", "")
                    output += val
                call_completed = time.time()

                # Post-command delay for modem state refresh.
                delay = (
                    self._at_write_delay_s if mutating else self._at_read_delay_s
                ) if post_delay_s is None else float(post_delay_s)
                if delay > 0:
                    time.sleep(delay)

                # Track AT transport latency only (exclude intentional post-delay sleep).
                latency_ms = (call_completed - started) * 1000.0
                self._last_at_latency_ms = float(latency_ms)
                if latency_ms >= self._at_latency_warn_ms:
                    log.warning(
                        "AT latency high: %.0fms cmd=%s guard=%.2fs",
                        latency_ms,
                        cmd,
                        self._at_bus_guard_s,
                    )
                    if self._adaptive_bus_guard and self._at_bus_guard_s < self._at_bus_guard_max_s:
                        old_guard = self._at_bus_guard_s
                        self._at_bus_guard_s = min(
                            self._at_bus_guard_max_s,
                            self._at_bus_guard_s + max(0.01, self._at_bus_guard_step_s),
                        )
                        if self._at_bus_guard_s > old_guard:
                            log.warning(
                                "Adaptive AT guard increased: %.2fs -> %.2fs (latency %.0fms)",
                                old_guard,
                                self._at_bus_guard_s,
                                latency_ms,
                            )
                return output

            except (RouterOsTransportError, RouterOsProtocolError) as e:
                log.error("AT command '%s' failed: %s", cmd, e)
                if isinstance(e, RouterOsTransportError):
                    self._mark_transport_failure(f"at:{cmd}", e)
                self._last_at_time = time.time()
                return None

    def _begin_post_write_blackout(self, *, reason: str) -> None:
        if self._post_write_blackout_s <= 0:
            return
        now = time.time()
        new_until = now + self._post_write_blackout_s
        if new_until > self._at_blackout_until:
            self._at_blackout_until = new_until
            log.info(
                "AT read blackout enabled for %.1fs after %s",
                self._post_write_blackout_s,
                reason,
            )

    def _mark_transport_failure(self, source: str, exc: Exception) -> None:
        now = time.time()
        reason = f"{source}: {exc}"
        if (now - self._last_transport_failure_ts) > 3.0 or reason != self._last_transport_failure_reason:
            log.error("RouterOS transport failure (%s) — forcing reconnect path", reason)
        self._last_transport_failure_ts = now
        self._last_transport_failure_reason = reason
        try:
            self.disconnect()
        except Exception as exc:
            log.debug("_mark_transport_failure: suppressed %s", exc)
            pass
        self._last_lte_monitor = None
        self._last_lte_monitor_ts = 0.0
        self._last_qcainfo = []
        self._last_qcainfo_ts = 0.0
        self._last_qnwcfg_mcs = None
        self._last_qnwcfg_mcs_ts = 0.0
        self._last_qeng_mcs = None
        self._last_qeng_mcs_ts = 0.0
        self._last_serving_cell_extra = {}
        self._last_serving_cell_extra_ts = 0.0
        self._last_wifi_status = (None, None, None)
        self._last_wifi_status_ts = 0.0
        self._wifi_probed = False
        self._wifi_api_path = None
        self._at_blackout_until = 0.0

    def _wait_for_radio(self, timeout: float = 30, *, initial_quiet_s: float = 0.0) -> bool:
        """Wait for radio to come back after disruption."""
        deadline = time.time() + timeout
        quiet_s = max(0.0, min(float(initial_quiet_s or 0.0), float(timeout or 0.0)))
        if quiet_s > 0.0:
            time.sleep(quiet_s)
        while True:
            try:
                data = self._lte_monitor(force=True)
                if data and data.get("radio_connected"):
                    return True
            except Exception as exc:
                log.debug("_wait_for_radio: suppressed %s", exc)
                pass
            remaining = deadline - time.time()
            if remaining <= 0.0:
                return False
            time.sleep(min(2.0, remaining))
        return False

    def _wait_for_primary_band(self, band: int, *, timeout: float, initial_quiet_s: float = 0.0) -> bool:
        """Wait until RouterOS monitor reports the requested primary band."""
        band = int(band or 0)
        if band <= 0:
            return True
        deadline = time.time() + max(0.0, float(timeout or 0.0))
        quiet_s = max(0.0, min(float(initial_quiet_s or 0.0), float(timeout or 0.0)))
        if quiet_s > 0.0:
            time.sleep(quiet_s)
        while True:
            try:
                data = self._lte_monitor(force=True) or {}
                if bool(data.get("radio_connected")) and int(data.get("primary_band", 0) or 0) == band:
                    return True
            except Exception as exc:
                log.debug("_wait_for_primary_band: suppressed %s", exc)
            remaining = deadline - time.time()
            if remaining <= 0.0:
                return False
            time.sleep(min(2.0, remaining))

    def _invalidate_radio_caches(self) -> None:
        self._last_lte_monitor_ts = 0.0
        self._last_qcainfo_ts = 0.0
        self._last_qnwcfg_mcs_ts = 0.0
        self._last_qeng_mcs_ts = 0.0
        self._last_serving_cell_extra_ts = 0.0

    # ── Wi-Fi status & control ──────────────────────────────────────────

    def _probe_wifi_api(self) -> Optional[str]:
        """Detect which RouterOS Wi-Fi API path this device supports.

        MikroTik exposes Wi-Fi through one of two mutually exclusive paths:
          /interface/wifi       — WiFiWave2 package (RouterOS 7.13+)
          /interface/wireless   — legacy wireless package

        Some devices have both paths present but only one has actual radio
        interfaces — e.g. /interface/wifi exists but returns 0 rows while
        /interface/wireless has the real wlan1/wlan2 radios.  We prefer
        whichever path returns actual interface rows.

        Returns the working path or None if neither is available (e.g. the
        Chateau variant without Wi-Fi radios, or wAP LTE kit without Wi-Fi).
        Result is cached so we only probe once per connection.
        """
        if self._wifi_probed:
            return self._wifi_api_path
        self._wifi_probed = True
        if not self._routeros:
            return None

        for path in ("/interface/wifi", "/interface/wireless"):
            try:
                rows = self._routeros.get_resource(self._portable_routeros_path(path,path)).get()
                if isinstance(rows, list) and len(rows) > 0:
                    self._wifi_api_path = path
                    log.info("Wi-Fi API detected: %s (%d interfaces)", path, len(rows))
                    return path
            except Exception as exc:
                log.debug("Wi-Fi probe %s failed: %s", path, exc)
        log.info("No Wi-Fi API available on this device (paths exist but no radios)")
        return None

    @staticmethod
    def _classify_wifi_band(row: dict) -> Optional[str]:
        return _classify_wifi_band(row)

    def _read_wifi_status(self) -> tuple:
        """Read Wi-Fi per-band enable state from RouterOS.

        Returns (wifi_enabled, wifi_24ghz_enabled, wifi_5ghz_enabled).
        All Optional[bool] — None if Wi-Fi API unavailable.
        """
        now = time.time()
        last_wifi_status_ts = float(getattr(self, "_last_wifi_status_ts", 0.0) or 0.0)
        wifi_status_interval_s = float(getattr(self, "_wifi_status_interval_s", 5.0) or 5.0)
        cached_wifi_status = tuple(getattr(self, "_last_wifi_status", (None, None, None)))
        if (now - last_wifi_status_ts) < max(1.0, wifi_status_interval_s):
            return cached_wifi_status
        path = self._probe_wifi_api()
        if not path or not self._routeros:
            return None, None, None

        try:
            rows = self._routeros.get_resource(self._portable_routeros_path(path,path)).get()
        except Exception as exc:
            log.debug("Wi-Fi status read failed: %s", exc)
            return cached_wifi_status

        if not rows:
            return cached_wifi_status

        rows = [_decode_row(row) if any(isinstance(key, bytes) for key in row) else row
                for row in rows]
        addresses = ("slow.wifi_enabled", "slow.wifi_24ghz_enabled", "slow.wifi_5ghz_enabled")
        owned = {address for address in addresses if self._definition.has_read(address)}
        replies = {path: rows}
        context = {"native_wifi_path": path, "previous_wifi": (None, None, None)}
        selected = self._definition.available_addresses(replies, context=context, addresses=owned)
        values = decode_native_profile(self._definition, replies, context,
            addresses=selected, issues=[])
        # Keep native behavior for undeclared fields; a declared rule whose
        # selected response is missing must stay unavailable.
        native = routeros_wifi(rows, (None, None, None)) if len(owned) < 3 else (None, None, None)
        current = tuple(values.get(address) if address in owned else native[index]
                        for index, address in enumerate(addresses))
        if all(value is None for value in current):
            return cached_wifi_status
        self._last_wifi_status = current
        self._last_wifi_status_ts = now
        return tuple(self._last_wifi_status)

    def wifi_set_radio(self, radio: str, enabled: bool) -> bool:
        del radio, enabled
        raise RuntimeError(
            "mikrotik_direct_wifi_radio_write_forbidden:"
            "use_execute_physical_stage"
        )

    def _set_wifi_radio_from_typed_stage(
        self,
        *,
        radio: str,
        enabled: bool,
    ) -> bool:
        """Toggle a Wi-Fi radio on or off.

        Args:
            radio: "2.4" or "5" (GHz band to toggle)
            enabled: True to turn on, False to turn off

        Returns True on success.

        Uses RouterOS /interface/wifi or /interface/wireless — whichever
        was detected during probe.  Sets the 'disabled' property on the
        matching interface.
        """
        if radio not in ("2.4", "5"):
            log.error("wifi_set_radio: invalid radio %r (expected '2.4' or '5')", radio)
            return False

        path = self._probe_wifi_api()
        if not path or not self._routeros:
            log.warning("wifi_set_radio: no Wi-Fi API available")
            return False

        try:
            rows = self._routeros.get_resource(path).get()
        except (RouterOsTransportError, RouterOsProtocolError) as exc:
            log.warning("wifi_set_radio: cannot read interfaces: %s", exc)
            if isinstance(exc, RouterOsTransportError):
                self._mark_transport_failure("wifi_read", exc)
            return False

        # Find the interface(s) matching the requested band
        target_ids = []
        for raw_row in rows:
            row = _decode_row(raw_row) if any(isinstance(k, bytes) for k in raw_row) else raw_row
            band = self._classify_wifi_band(row)
            if band == radio:
                iface_id = row.get("id") or row.get(".id")
                if iface_id:
                    target_ids.append(iface_id)

        if not target_ids:
            log.warning("wifi_set_radio: no interface matched radio=%s", radio)
            return False

        # RouterOS: disabled=true → off, disabled=false → on
        new_disabled = "false" if enabled else "true"
        label = "2.4 GHz" if radio == "2.4" else "5 GHz"
        action_word = "ON" if enabled else "OFF"

        success = True
        resource = self._routeros.get_resource(path)
        for iface_id in target_ids:
            try:
                resource.set(id=iface_id, disabled=new_disabled)
                log.info("WiFi: %s radio (%s) set to %s", label, iface_id, action_word)
            except (RouterOsTransportError, RouterOsProtocolError) as exc:
                log.warning("WiFi: failed to set %s radio %s (%s): %s",
                            label, action_word, iface_id, exc)
                if isinstance(exc, RouterOsTransportError):
                    self._mark_transport_failure("wifi_write", exc)
                success = False

        return success

    @staticmethod
    def _parse_uptime(raw: str) -> float:
        """Monitor boundary adapter; missing native durations display as zero."""
        return parse_routeros_duration_seconds(raw) or 0.0
