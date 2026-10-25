"""
arch4/modem/base.py
═══════════════════════════════════════════════════════════════
CGF — Abstract Modem Interface

Modem Live is the only component allowed to call backend transport writers.
Implementation-specific backends translate one typed physical stage into one
vendor request. No untyped action or direct public writer is part of this
boundary.
═══════════════════════════════════════════════════════════════
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Mapping

from arch4.interfaces import (
    ModemInput, NeighborCell,
    band_signature, normalize_band_list, normalize_band_fields,
)
from arch4.coerce import safe_int as _safe_int
from arch4.runtime_operator_contract import operator_field_names
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    capture_backend_acquisition,
)
from arch4.modem.backend_acquisition_cells import (
    CONFIGURED_BAND_CELLS,
    FAST_COMMON_CELLS,
    IDENTITY_CELLS,
    SLOW_CELLS,
    TRAFFIC_VOLUME_CELLS,
)


from arch4.modem.utils import RttProbeResult


class AuthenticationError(ConnectionError):
    """Raised when modem connection fails due to wrong credentials."""
    pass


class ModemIdentityRequiredError(ConnectionError):
    """Login succeeded, but the modem exposed no stable physical identity."""


class SessionExpiredError(ConnectionError):
    """Authentication is required; only a fresh response bypasses backoff."""

    def __init__(self, *args, confirmed: bool = True):
        if type(confirmed) is not bool:
            raise TypeError("session_expiry_confirmation_bool_required")
        super().__init__(*args)
        self.confirmed = confirmed


class ModemTransportUnavailable(ConnectionError):
    """Expected vendor transport miss, distinct from a protocol/code fault."""

    def __init__(self, reason: str):
        if reason not in {"modem_transport_connection_lost", "modem_transport_timeout",
                          "modem_transport_truncated_response"}:
            raise ValueError("modem_transport_reason_invalid")
        super().__init__(reason)
        self.reason = reason


class TargetGuardError(ConnectionError):
    """Raised when modem connect succeeds but target identity check fails.

    Attributes:
        reason: machine-readable reason code from evaluate_target_guard
                e.g. "target_guard_missing_model", "target_guard_mismatch_imei"
    """
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class LiveAcquisitionContract:
    acquisition_class: str = "aligned_single_read"
    clocked_register_read: bool = False
    live_register_interval_s: Optional[float] = None
    live_register_stale_s: Optional[float] = None
    profile_name: str = ""
    backend_io_parallel_safe: bool = False
    io_serial_required: Optional[bool] = None
    startup_parallel_allowed: Optional[bool] = None
    runtime_action_requires_io_lock: Optional[bool] = None
    has_explicit_actionable_snapshot: bool = False
    has_explicit_maintenance_refresh: bool = False
    maintenance_stepwise: bool = False
    maintenance_completion_ordered: bool = False
    maintenance_step_requires_fast_margin: bool = False
    rtt_holds_io_lock: bool = False
    rtt_min_interval_s: float = 1.0
    maintenance_holds_io_lock: bool = False
    neighbor_reads_hold_io_lock: bool = False
    generic_neighbor_scan_enabled: bool = True
    fast_modem_read_groups: Tuple[str, ...] = ()
    slow_modem_read_groups: Tuple[str, ...] = ()
    fast_publish_fields: Tuple[str, ...] = ()
    slow_publish_fields: Tuple[str, ...] = ()
    fast_read_groups: Tuple[str, ...] = ()
    slow_read_groups: Tuple[str, ...] = ()
    static_capability_groups: Tuple[str, ...] = ()
    control_write_groups: Tuple[str, ...] = ()
    export_groups: Tuple[str, ...] = ()
    static_capability_refresh_policy: str = "identity_or_operator_refresh_only"

    def __post_init__(self) -> None:
        if self.maintenance_completion_ordered and not self.maintenance_stepwise:
            raise ValueError("completion_ordered_maintenance_requires_steps")
        if float(self.rtt_min_interval_s) <= 0.0:
            raise ValueError("live_acquisition_rtt_min_interval_must_be_positive")
        fast_modem = tuple(self.fast_modem_read_groups or self.fast_read_groups or ())
        slow_modem = tuple(self.slow_modem_read_groups or self.slow_read_groups or ())
        object.__setattr__(self, "fast_modem_read_groups", fast_modem)
        object.__setattr__(self, "slow_modem_read_groups", slow_modem)
        object.__setattr__(self, "fast_read_groups", fast_modem)
        object.__setattr__(self, "slow_read_groups", slow_modem)
        object.__setattr__(self, "fast_publish_fields", tuple(self.fast_publish_fields or ()))
        object.__setattr__(self, "slow_publish_fields", tuple(self.slow_publish_fields or ()))

    def effective_fast_modem_read_groups(self) -> Tuple[str, ...]:
        return tuple(self.fast_modem_read_groups or ())

    def effective_slow_modem_read_groups(self) -> Tuple[str, ...]:
        return tuple(self.slow_modem_read_groups or ())

    def publish_field_map(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "fast_publish_fields": tuple(self.fast_publish_fields or ()),
            "slow_publish_fields": tuple(self.slow_publish_fields or ()),
        }

    def lane_group_map(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "fast_modem_read_groups": self.effective_fast_modem_read_groups(),
            "slow_modem_read_groups": self.effective_slow_modem_read_groups(),
            "static_capability_groups": tuple(self.static_capability_groups or ()),
            "control_write_groups": tuple(self.control_write_groups or ()),
            "export_groups": tuple(self.export_groups or ()),
        }

    def global_lane_map(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "fast_live": self.effective_fast_modem_read_groups(),
            "slow_live": self.effective_slow_modem_read_groups(),
            "boot_static": tuple(self.static_capability_groups or ()),
            "event_write": tuple(self.control_write_groups or ()),
            "export": tuple(self.export_groups or ()),
        }

    def lane_group_overlaps(self) -> Dict[str, Tuple[str, ...]]:
        groups = self.lane_group_map()
        overlaps: Dict[str, Tuple[str, ...]] = {}
        names = list(groups)
        for idx, left_name in enumerate(names):
            left_values = set(groups[left_name])
            if not left_values:
                continue
            for right_name in names[idx + 1:]:
                shared = sorted(left_values.intersection(groups[right_name]))
                if shared:
                    overlaps[f"{left_name}:{right_name}"] = tuple(shared)
        return overlaps

    def static_capability_runtime_overlaps(self) -> Dict[str, Tuple[str, ...]]:
        static_values = set(self.static_capability_groups or ())
        overlaps: Dict[str, Tuple[str, ...]] = {}
        if not static_values:
            return overlaps
        for name, values in self.lane_group_map().items():
            if name == "static_capability_groups":
                continue
            shared = sorted(static_values.intersection(values))
            if shared:
                overlaps[name] = tuple(shared)
        return overlaps

    def effective_io_serial_required(self) -> bool:
        if self.io_serial_required is not None:
            return bool(self.io_serial_required)
        return not bool(self.backend_io_parallel_safe)

    def effective_startup_parallel_allowed(self) -> bool:
        if self.startup_parallel_allowed is not None:
            return bool(self.startup_parallel_allowed)
        return bool(self.backend_io_parallel_safe)

    def effective_runtime_action_requires_io_lock(self) -> bool:
        if self.runtime_action_requires_io_lock is not None:
            return bool(self.runtime_action_requires_io_lock)
        return True


PUBLIC_MODEM_STATUS_KEYS = {
    "ts",
    "provider",
    "provider_name",
    "provider_plmn",
    "plmn",
    "plmn_name",
    "network_provider_fullname",
    "network_type",
    "radio_connected",
    "radio_state_text",
    "rrc",
    "trans_mode",
    "trans_mode_source",
    "ca_active",
    "ca_state_text",
    "ca_signature",
    "carrier_details",
    "cell_locked",
    "carrier_rows",
    "ca_component_band_set",
    "ca_component_band_signature",
    "ca_component_count",
    "ca_composition_signature",
    "ca_source",
    "band",
    "serving_band",
    "primary_band",
    "serving_band_signature",
    "selected_band_set",
    "selected_band_set_signature",
    "configured_band_pool",
    "configured_band_pool_signature",
    "configured_band_pool_source",
    "supported_lte_bands",
    "supported_lte_bands_source",
    "supported_lte_bands_observed_at",
    "supported_nr_bands",
    "supported_nr_bands_source",
    "supported_nr_bands_observed_at",
    "active_band_set",
    "active_band_set_signature",
    "band_set",
    "band_set_signature",
    "pci",
    "earfcn_dl",
    "earfcn_ul",
    "enb_id",
    "cell_id",
    "tac",
    "carrier_count",
    "nr_ri", "nr_cqi", "nr_mcs_dl", "nr_mcs_ul", "nr_qam_dl", "nr_qam_ul",
    "nr_num_rbs", "nr_tx_power_dbm", "nr_ul_bandwidth_mhz", "nr_dl_freq_mhz", "nr_ul_freq_mhz",
    "dl_bandwidth",
    "ul_bandwidth",
    "dl_bandwidth_mhz",
    "ul_bandwidth_mhz",
    "dl_freq_mhz",
    "ul_freq_mhz",
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "mcs_ul_supported",
    "mcs_ul_raw",
    "mcs_dl_retx",
    "mcs_dl1_retx",
    "mcs_ul_retx",
    "tbs_dl0",
    "tbs_dl1",
    "tbs_ul",
    "qm_dl0",
    "qm_dl1",
    "qm_ul",
    "qam_dl0",
    "qam_dl1",
    "qam_ul",
    "qam_source",
    "qam_confidence",
    "cqi",
    "cqi0",
    "cqi1",
    "ri",
    "ri_valid",
    "ri_source",
    "tx_power",
    "ims",
    "session_uptime_s",
    "session_uptime_source",
    "rtt_ms",
    "antenna_mode",
    "antenna_mode_source",
    "antenna_switch_api_supported",
    "external_antenna_ports_present",
    "model",
    "firmware",
    "webui",
    "software",
    "software_version",
    "os_name",
    "os_version",
    "router_model",
    "serial",
    "imsi",
    "hardware_version",
    "config_version",
    "configuration_file_version",
    "lan_mac",
    "wan_ip",
    "wan_connected",
    "wan_iface_name",
    "wan_logical_if",
    "antenna",
    "imei",
    "neighbors",
    "lte_cells",
    "nr5g_active",
    "nr5g_mode",
    "nr5g_state_text",
    "nr5g_cell_id",
    "nr5g_pci",
    "nr5g_arfcn",
    "nr5g_band",
    "nr5g_band_signature",
    "nr5g_band_set",
    "nr5g_band_set_signature",
    "nr5g_rsrp_dbm",
    "nr5g_rsrq_db",
    "nr5g_sinr_db",
    "nr5g_rx0_rsrp_dbm",
    "nr5g_rx1_rsrp_dbm",
    "nr5g_nsa_bandwidth",
    "nr5g_cells",
    "nr5g_sa_band_lock",
    "nr5g_nsa_band_lock",
    "temperature_ambient_c",
    "temperature_cpu_c",
    "temperature_cpu_diagnostic",
    "zwrt_traffic_diagnostic",
    "temperature_modem_4g_c",
    "temperature_modem_5g_c",
    "temperature_pa1_c",
    "temperature_wifi_c",
    "wifi_enabled",
    "wifi_24ghz_enabled",
    "wifi_5ghz_enabled",
    "wifi_6ghz_enabled",
    "target_guard_ok",
    "target_guard_reason",
    "at_cmd_latency_ms",
    "at_bus_guard_s",
    "at_blackout_remaining_s",
    "modem_transport",
    "modemmanager_plugin",
    "modemmanager_primary_port",
    "modemmanager_drivers",
    "lte_interface_disabled",
    "device_recovery_state",
    "traffic_presence_active",
    "traffic_presence_interval_s",
    "traffic_session_upload_bytes",
    "traffic_session_download_bytes",
    "traffic_session_duration_s",
    "traffic_total_upload_bytes",
    "traffic_total_download_bytes",
    "traffic_device_download_counter_bytes",
    "traffic_device_upload_counter_bytes",
    "traffic_counter_period_started_at",
    "traffic_counter_interface_started_at",
    "traffic_total_duration_s",
    "traffic_current_download_rate_bps",
    "traffic_current_upload_rate_bps",
    "traffic_rate_source",
    "traffic_valid",
    "traffic_state_detail",
    "traffic_invalid_reason",
    "packet_session_connected",
    "connection_status_code",
    "traffic_month_download_bytes",
    "traffic_month_upload_bytes",
    "traffic_month_duration_s",
    "traffic_month_last_clear_date",
    "traffic_day_used_bytes",
    "traffic_day_duration_s",
    "traffic_quota_start_day",
    "traffic_quota_limit_label",
    "traffic_quota_left_bytes",
    "traffic_quota_limit_bytes",
    "traffic_quota_awoke",
    "traffic_quota_month_threshold_pct",
    "traffic_quota_day_threshold_pct",
    "traffic_quota_set_month_data",
    "traffic_quota_turnoff_data_enable",
    "traffic_quota_turnoff_data_switch",
    "traffic_quota_turnoff_data_flag",
    "backend_fast_read",
    "backend_api_timing",
    "backend_diagnostic",
    "read_diagnostic",
    "network_settings",
    "radio_mode",
    "mobile_data_enabled",
    "backend_session",
    "qnwlock_guard",
} | operator_field_names()

_PUBLIC_MODEM_STATUS_ALIAS_SOURCES = {
    "backend_fast_read": (
        "backend_fast_read",
        "hilink_fast_read",
        "goform_fast_read",
        "zyxel_fast_read",
    ),
    "backend_api_timing": (
        "backend_api_timing",
        "hilink_api_timing",
        "goform_api_timing",
        "zyxel_api_timing",
    ),
}


def build_public_modem_status(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return only canonical brain-language modem status keys."""
    if not isinstance(status, dict) or not status:
        return {}
    out: Dict[str, Any] = {}
    for key in PUBLIC_MODEM_STATUS_KEYS:
        if key in status:
            out[key] = status[key]
    for public_key, source_keys in _PUBLIC_MODEM_STATUS_ALIAS_SOURCES.items():
        if public_key in out:
            continue
        for source_key in source_keys:
            if source_key in status:
                out[public_key] = status[source_key]
                break

    # Extract raw values for unified normalization
    selected = normalize_band_list(out.get("selected_band_set"))
    if not selected:
        selected = normalize_band_list(status.get("selected_band_set"))
    if not selected:
        selected = normalize_band_list(status.get("allowed_bands"))
    
    active = normalize_band_list(out.get("active_band_set"))
    
    serving_band_raw = _safe_int(
        out.get("serving_band") or status.get("serving_band") or out.get("band") or status.get("band"),
        0,
    )
    
    # Use unified band normalization
    nbf = normalize_band_fields(
        serving_band=serving_band_raw,
        selected_band_set=selected,
        active_band_set=active,
        band_set=out.get("band_set"),
        ca_active=bool(out.get("ca_active")),
        serving_band_signature=out.get("serving_band_signature", ""),
        selected_band_set_signature=out.get("selected_band_set_signature", ""),
        active_band_set_signature=out.get("active_band_set_signature", ""),
        band_set_signature=out.get("band_set_signature", ""),
        ca_state_text=out.get("ca_state_text", ""),
    )
    
    # Apply normalized values
    if nbf.selected_band_set:
        out["selected_band_set"] = list(nbf.selected_band_set)
        out.setdefault("selected_band_set_signature", nbf.selected_band_set_signature)
    
    if nbf.serving_band > 0:
        out["serving_band"] = nbf.serving_band
        out.setdefault("primary_band", nbf.serving_band)
        out.setdefault("serving_band_signature", nbf.serving_band_signature)
        out.setdefault("band", nbf.serving_band)
    else:
        out.pop("serving_band", None)
        out.pop("primary_band", None)
        out.pop("serving_band_signature", None)

    qm_dl0 = _safe_int(out.get("qm_dl0") or status.get("qm_dl0") or out.get("mod0") or status.get("mod0") or out.get("qam_dl0") or status.get("qam_dl0"), 0)
    qm_dl1 = _safe_int(out.get("qm_dl1") or status.get("qm_dl1") or out.get("mod1") or status.get("mod1") or out.get("qam_dl1") or status.get("qam_dl1"), 0)
    qm_ul = _safe_int(out.get("qm_ul") or status.get("qm_ul") or out.get("qam_ul") or status.get("qam_ul"), 0)
    if qm_dl0 > 0:
        out["qm_dl0"] = qm_dl0
    else:
        out.pop("qm_dl0", None)
    if qm_dl1 > 0:
        out["qm_dl1"] = qm_dl1
    else:
        out.pop("qm_dl1", None)
    if qm_ul > 0:
        out["qm_ul"] = qm_ul
    else:
        out.pop("qm_ul", None)

    # Apply remaining unified band fields
    if nbf.active_band_set:
        out.setdefault("active_band_set_signature", nbf.active_band_set_signature)

    nr_band_set = normalize_band_list(out.get("nr5g_band_set"))
    if nr_band_set:
        out["nr5g_band_set"] = list(nr_band_set)
        out.setdefault("nr5g_band_set_signature", "+".join(f"n{band}" for band in nr_band_set))

    nr_band = _safe_int(out.get("nr5g_band") or status.get("nr5g_band"), 0)
    if nr_band > 0:
        out["nr5g_band"] = nr_band
        out.setdefault("nr5g_band_signature", f"n{nr_band}")
    else:
        out.pop("nr5g_band", None)
        out.pop("nr5g_band_signature", None)

    # band_set from unified normalization
    if nbf.band_set:
        out["band_set"] = list(nbf.band_set)
        out.setdefault("band_set_signature", nbf.band_set_signature)

    if "provider_name" not in out and "provider" in out:
        out["provider_name"] = out["provider"]
    if "provider" not in out and "provider_name" in out:
        out["provider"] = out["provider_name"]
    if "provider_plmn" not in out and "plmn" in out:
        out["provider_plmn"] = out["plmn"]
    if "plmn" not in out and "provider_plmn" in out:
        out["plmn"] = out["provider_plmn"]

    if "radio_state_text" not in out and "radio_connected" in out:
        out["radio_state_text"] = "Connected" if bool(out.get("radio_connected")) else "Disconnected"
    # ca_state_text from unified normalization
    if "ca_state_text" not in out:
        out["ca_state_text"] = nbf.ca_state_text
    return out


def normalize_identity_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(text.split()).lower()


def target_guard_observed_identity(
    observed: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return the canonical observed value for every target-guard axis."""

    info = dict(observed or {})
    return {
        "model": normalize_identity_value(
            info.get("router_model") or info.get("model")
        ),
        "serial": normalize_identity_value(info.get("serial")),
        "firmware": normalize_identity_value(
            info.get("firmware")
            or info.get("software")
            or info.get("software_version")
            or info.get("config_version")
            or info.get("configuration_file_version")
        ),
        "imei": normalize_identity_value(info.get("imei")),
    }


def evaluate_target_guard(
    *,
    expected_model: Any = "",
    expected_serial: Any = "",
    expected_firmware: Any = "",
    expected_imei: Any = "",
    observed: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Check observed device identity against expected values.

    Each identity axis (imei, serial, model, firmware) is independent.
    No fallback of one identity into another — if serial is expected but
    the backend does not report one, the guard fails cleanly.
    """
    identity = target_guard_observed_identity(observed)
    actual_model = identity["model"]
    actual_serial = identity["serial"]
    actual_firmware = identity["firmware"]
    actual_imei = identity["imei"]
    expected_model_norm = normalize_identity_value(expected_model)
    expected_serial_norm = normalize_identity_value(expected_serial)
    expected_firmware_norm = normalize_identity_value(expected_firmware)
    expected_imei_norm = normalize_identity_value(expected_imei)

    # Each axis checked independently — no cross-axis fallback.

    if expected_imei_norm:
        if not actual_imei:
            return False, "target_guard_missing_imei"
        if actual_imei != expected_imei_norm:
            return False, "target_guard_mismatch_imei"

    if expected_serial_norm:
        if not actual_serial:
            return False, "target_guard_missing_serial"
        if actual_serial != expected_serial_norm:
            return False, "target_guard_mismatch_serial"

    if expected_model_norm:
        if not actual_model:
            return False, "target_guard_missing_model"
        if actual_model != expected_model_norm:
            return False, "target_guard_mismatch_model"

    if expected_firmware_norm:
        if not actual_firmware:
            return False, "target_guard_missing_firmware"
        if actual_firmware != expected_firmware_norm:
            return False, "target_guard_mismatch_firmware"

    return True, ""


class ModemInterface(ABC):
    """
    Standard modem abstraction layer.
    
    Modem Live → typed physical stage → hardware-specific backend
    
    Every method either reads state or executes an authorized typed stage.
    No hardware-specific types cross this boundary.
    """

    # ─── Connection lifecycle ───────────────────────────────

    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to modem management interface.
        Returns True if connected successfully.
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Clean disconnect from modem management."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check the management session, not RF or Internet connectivity.

        True means usable; False means a new session must be established.
        An inconclusive read raises ConnectionError and must not imply logout.
        """
        ...

    # ─── Observation (read modem inputs) ────────────────────

    @abstractmethod
    def read_modem_input(self) -> Optional[ModemInput]:
        """
        Read all observable signals in one atomic snapshot.
        
        Returns None if modem unreachable or data unavailable.
        This is the ONLY method the brain calls to get signal data.
        
        Combines:
          - lte monitor (SINR, RSRP, RSRQ, RSSI, band, PCI, etc.)
          - QCAINFO (CA carriers, per-carrier SINR)
          - traffic counters (rx/tx bytes per sec)
          - session uptime
          - radio state
        
        The backend handles all RouterOS/AT quirks internally:
          - .id selector for at-chat
          - 2.5s delay after AT commands
          - string key decoding
          - SINR format differences (QCAINFO direct dB vs QENG 1/5 dB)
        """
        ...

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        """
        Read the fast actionable modem snapshot used by LIVE MODULE hot ticks.

        Split backends must keep this path limited to RF/cell/traffic truth
        needed for live monitoring and immediate actions. Static identity,
        configured band lists, Wi-Fi state, quota, capability probes, neighbor
        scans, and other detail/enrichment reads belong to startup or
        refresh_live_maintenance(); actionable snapshots may only reuse cached
        values for those fields.

        The permanent aligned-backend adapter is the complete atomic modem
        snapshot. Split backends override this method with their protected
        fast acquisition.
        """
        return self.read_modem_input()

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        """Require a concrete backend to declare exact startup evidence."""

        del request
        raise RuntimeError(
            "backend_explicit_startup_acquisition_required"
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        """Require a concrete backend to declare exact fast evidence."""

        del request
        raise RuntimeError(
            "backend_explicit_fast_acquisition_required"
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        """Return exact target-relevant evidence after one accepted write."""

        if request.lane != "post_write":
            raise ValueError(
                "backend_post_write_acquisition_lane_required"
            )
        modem = self.read_post_write_modem_input(stage)
        target_cells = FAST_COMMON_CELLS
        if stage.stage_kind in {
            "pband_anchor",
            "ca_pool",
            "band_selection",
            "band_lock",
            "band_auto",
        }:
            target_cells = target_cells | CONFIGURED_BAND_CELLS
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=target_cells,
        )

    def read_post_write_modem_input(
        self,
        stage: PhysicalCommandDispatchEvidence,
    ) -> Optional[ModemInput]:
        """Read target-relevant truth after one accepted physical write.

        This is the permanent Modem Live/backend boundary for command
        readback. Aligned backends use their complete atomic observation.
        Split backends whose actionable observation can retain target fields
        must override this method and physically refresh those fields first.
        """

        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError("post_write_physical_stage_evidence_required")
        if not stage.permits_target_verification:
            raise ValueError("post_write_accepted_physical_stage_required")
        return self.read_modem_input()

    def refresh_live_maintenance(self) -> None:
        """
        Refresh slower maintenance-grade modem caches.

        This is called by LIVE MODULE on a separate cadence from the fast
        actionable tick. Default is a no-op for backends that do not need a
        dedicated maintenance refresh path.
        """
        return None

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        """Return exact slow-lane evidence; side-effect-only completion is illegal."""

        del request
        raise RuntimeError(
            "side_effect_only_backend_maintenance_forbidden"
        )

    def iter_acquire_read_diagnostics(self, request, run):
        if request.lane != "slow_live":
            raise ValueError("read_diagnostic_maintenance_lane_required")
        while not run.terminal:
            run.advance()
            yield self._capture_backend_acquisition_receipt(
                request=request, modem=None,
                acquired_cells=frozenset({"slow.read_diagnostic"}),
                status_override={"read_diagnostic": run.envelope()})

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        raise ReadDiagnosticUnavailable("Read diagnostics are not implemented for this router profile.")

    def _iter_network_settings_observation(self):
        raise RuntimeError("network_inspection_unsupported_profile")

    def iter_acquire_network_settings(self, request: BackendAcquisitionRequest):
        from arch4.modem.network_inspection import NetworkSettingsRead, NetworkSettingsReadIncomplete
        if request.lane not in {"slow_live", "post_write"}:
            raise ValueError("network_inspection_lane_invalid")
        result = yield from self._iter_network_settings_observation()
        if not isinstance(result, NetworkSettingsRead):
            raise TypeError("network_inspection_typed_result_required")
        status, cells = {}, set()
        if result.diagnostic is not None:
            from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
            if not isinstance(result.diagnostic, ModemBackendDiagnostic):
                raise TypeError("network_inspection_typed_diagnostic_required")
            status["backend_diagnostic"] = result.diagnostic.to_payload()
            cells.add("slow.backend_diagnostic")
        if result.observation["subscription_key"]:
            status["network_settings"] = result.observation
            cells.add("slow.network_settings")
        elif not cells:
            raise NetworkSettingsReadIncomplete("network_inspection_subscription_unavailable")
        if result.imsi:
            status["imsi"] = result.imsi
            cells.add("identity.imsi")
        yield self._capture_backend_acquisition_receipt(
            request=request, modem=None, acquired_cells=frozenset(cells),
            status_override=status,
        )

    def _acquire_network_settings_readback(self, request: BackendAcquisitionRequest):
        # Explicit post-write adapter: the command owner already holds the
        # serialized readback lane. Never drain an ordinary maintenance sweep.
        steps = self.iter_acquire_network_settings(request)
        receipt = None
        try:
            for step in steps:
                if isinstance(step, BackendAcquisitionReceipt):
                    receipt = step
        finally:
            steps.close()
        if receipt is None:
            raise RuntimeError("network_settings_readback_unavailable")
        return receipt

    def acquire_neighbors(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        """Return exact neighbor evidence without a side-channel cache write."""

        if request.lane != "neighbor":
            raise ValueError("backend_neighbor_acquisition_lane_required")
        neighbors = list(self.read_neighbors() or [])
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=None,
            status_override={
                "neighbors": neighbors,
                "neighbor_count": len(neighbors),
            },
            acquired_cells=frozenset(
                cell
                for cell in SLOW_CELLS
                if cell in {
                    "slow.neighbors",
                    "slow.neighbor_count",
                }
            ),
        )

    def _capture_backend_acquisition_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
        acquired_cells: frozenset[str],
        failure_reason: str = "",
        status_override: Optional[Mapping[str, Any]] = None,
    ) -> BackendAcquisitionReceipt:
        if status_override is None:
            status_reader = getattr(self, "get_last_status", None)
            status = status_reader() if callable(status_reader) else {}
            status = dict(status or {}) if isinstance(status, dict) else {}
        else:
            status = dict(status_override)
        backend = str(
            status.get("backend")
            or getattr(self, "_backend_name", "")
            or getattr(getattr(self, "cfg", None), "modem_backend", "")
            or type(self).__name__
        ).strip()
        profile_id = str(
            getattr(self.get_live_acquisition_contract(), "profile_name", "")
            or type(self).__name__
        ).strip()
        if type(acquired_cells) is not frozenset:
            raise TypeError(
                "backend_explicit_acquired_cells_frozenset_required"
            )
        return capture_backend_acquisition(
            request=request,
            backend=backend,
            profile_id=profile_id,
            modem=modem,
            status=status,
            acquired_cells=acquired_cells,
            failure_reason=failure_reason,
        )

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        """
        Describe how LIVE MODULE should schedule this backend.

        The default is a cheap aligned single-read backend: the full modem read
        is safe to use as the actionable snapshot, there is no dedicated
        maintenance lane, and RTT probing does not require the modem I/O lock.
        """
        return LiveAcquisitionContract()

    def native_diagnostic_privacy(self):
        """Admitted native field labels, never observations or credentials."""
        definition=getattr(self,'_definition',None)
        return ({'keys':definition.native_private_keys,'opaque':definition.opaque_native_privacy}
                if definition is not None else {'keys':(),'opaque':False})

    @abstractmethod
    def read_neighbors(self) -> List[NeighborCell]:
        """
        Query neighbor cell list (AT+QENG="neighbourcell").
        
        This is a separate method because it's on-demand (not every tick).
        Legacy return is a list only. New call paths that need truth about
        unsupported/empty/failed must wrap this with BackendFeatureResult.
        """
        ...

    @abstractmethod
    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        """
        Measure RTT via ICMP ping through the LTE interface.

        Modem Live owns ``timeout_s``. Backends may select the required host
        interface, but must not replace the supplied probe deadline.
        
        Returns the RTT or the explicit transport failure in one receipt.
        """
        ...

    # ─── Typed physical command execution ───────────────────

    @abstractmethod
    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        """
        Translate one immutable Modem Live stage into one vendor write.

        This is the only backend mutation contract. It returns evidence
        captured at the actual transport boundary.
        """
        ...

    # ─── Startup verification ───────────────────────────────

    @abstractmethod
    def verify_static_config(self) -> Dict[str, bool]:
        """
        Check which static configurations are present.
        
        Returns dict like:
          {"ttl_ipv4": True, "ttl_ipv6": True, "mss_ipv4": False, ...}
        
        Maps to Foundation Doc Chapter 5.2.
        """
        ...

    @abstractmethod
    def verify_ue_capability(self) -> Dict[str, Any]:
        """
        Check UE capability flags via AT commands.
        
        Returns dict like:
          {"dl_256qam": True, "ul_64qam": False, "ue_category": 12, ...}
        
        Maps to Foundation Doc Chapter 5.3.
        """
        ...

    # ─── Info ───────────────────────────────────────────────

    def read_fresh_device_identity(self) -> Dict[str, str]:
        """Read permanent identity now, under Modem Live's I/O authority.

        Cached identification is not evidence that a reconnected device is the
        same router. Backends must implement this boundary explicitly.
        """
        raise NotImplementedError("fresh_device_identity_read_required")

    @abstractmethod
    def get_modem_info(self) -> Dict[str, str]:
        """
        Get modem identification (model, firmware, IMEI, etc).
        Used for logging and constraint detection.
        """
        ...

    @abstractmethod
    def get_current_bands(self) -> List[int]:
        """
        Get currently allowed band list.
        """
        ...

    @abstractmethod
    def get_capabilities(self):
        """Return ModemCapabilities for this backend."""
        ...

    # ─── EG120K-EA extended capabilities ─────────────────────

    @abstractmethod
    def scan_neighbors(self) -> List[Dict[str, Any]]:
        """
        Scan neighbor cells via AT+QENG="neighbourcell".
        
        Returns list of dicts: type, earfcn, pci, rsrp, rsrq, band.
        On-demand — not called every tick.
        """
        ...

    @abstractmethod
    def read_serving_cell(self) -> Dict[str, Any]:
        """
        Read serving cell engineering data via AT+QENG="servingcell".
        
        Returns dict with CQI, tx_power, and other fields not in
        RouterOS monitor output.
        """
        ...

    @abstractmethod
    def read_band_priority(self) -> List[int]:
        """
        Read modem band priority order.
        
        Returns list of band numbers in priority order, e.g. [3, 1, 20].
        """
        ...
