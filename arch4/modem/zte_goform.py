"""
arch4/modem/zte_goform.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — ZTE goform Modem Backend (Passive-first)

Read-only modem adapter for ZTE WebUI/goform-class routers such as MF296R.
This first pass focuses on:
  - HTTP goform status reads
  - translation into shared brain/system vocabulary
  - honest capability limits (no actuation yet)

No vendor-specific names leave this module.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
import base64
import ipaddress
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit, urljoin

import requests

from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.modem_live.startup_trace import traced_http

from arch4.interfaces import (
    CarrierInfo,
    LeverType,
    LeverAction,
    LeverResult,
    ModemInput,
    NeighborCell,
    PROVIDER_PLMN,
    band_signature,
    normalize_band_list,
    get_capabilities,
)
from arch4.core_contracts.command_targets import (
    CoreNetworkSettingsTarget,
    CoreWifiRadioTarget,
    CoreBandAutoTarget,
    CoreBandSetTarget,
    CoreCellLockTarget,
    CoreNrBandTarget,
    CoreRadioModeTarget,
    CorePBandTarget,
    CoreRestartTarget,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.control.band_resolver import auto_band_release_rejection_reason, is_auto_band_release
from arch4.lte_mcs import estimate_qm_from_sinr, representative_cqi_for_qm
from arch4.modem.base import (
    AuthenticationError,
    LiveAcquisitionContract,
    ModemInterface,
    TargetGuardError,
    build_public_modem_status,
    evaluate_target_guard,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.backend_acquisition_cells import (
    CONFIGURED_BAND_CELLS,
    IDENTITY_CELLS,
    SLOW_CELLS,
    TRAFFIC_VOLUME_CELLS,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt
from arch4.modem.lock_confirmation import (
    build_lock_confirmation,
    configured_band_match,
    effective_band_match,
    effective_cell_match,
    normalized_bands,
)
from arch4.modem.zte_parsers import (
    CONNECTED_PPP_STATES,
    NEGATIVE_STATES,
    TRUTHY_STRINGS,
    ZTE_BAND_MASKS,
    decode_band_mask,
    dedupe_bands,
    first_non_zero,
    is_5g_only_mode,
    is_limited_service,
    normalize_imsi,
    parse_antenna_mode_text,
    parse_band_values,
    parse_dbm_metric,
    parse_duration,
    parse_float,
    parse_int,
    parse_metric_float,
    parse_nr_band,
    parse_wifi_band_enabled,
    parse_wifi_enabled,
)
from arch4.modem.lte_frequency import lte_frequency_mhz_from_dl_earfcn
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)
from arch4.modem.catalog import (
    ZTE_GOFORM_ROUTER_MANAGED_STATIC_CONFIG,
    PROVIDER_BAND_POOL_FIELDS,
)
from arch4.operator_profiles import resolve_provider_name

log = logging.getLogger("arch4.modem.zte")

# Independent native inputs; derived scores, pool policy and inferred CQI stay
# with their existing owners. This mapping is shared with receipt freshness.
_GOFORM_PROFILE_INPUTS = {'identity.provider_plmn': 'provider_plmn',
 'radio.ca_active': 'ca_active',
 'radio.serving_band': 'serving_band',
 'traffic.session_uptime_s': 'session_uptime_s',
 'radio.network_type': 'network_type',
 'health.radio_connected': 'radio_connected',
 'slow.wan_ip': 'wan_ip',
 'slow.wifi_enabled': 'wifi_enabled',
 'slow.wifi_24ghz_enabled': 'wifi_24ghz_enabled',
 'slow.wifi_5ghz_enabled': 'wifi_5ghz_enabled',
 'radio.tx_power': 'tx_power',
 'slow.temperature_modem_5g_c': 'temperature_modem_5g_c',
 'profile.temperature_ambient_c': 'temperature_ambient_c',
 'profile.temperature_modem_4g_c': 'temperature_modem_4g_c',
 'profile.temperature_pa1_c': 'temperature_pa1_c',
 'profile.temperature_wifi_c': 'temperature_wifi_c',
 'traffic.traffic_month_download_bytes': 'traffic_month_download_bytes',
 'traffic.traffic_month_upload_bytes': 'traffic_month_upload_bytes'}

_GOFORM_CANONICAL_SOURCE_GROUPS: tuple[
    tuple[frozenset[str], frozenset[str]], ...
] = (
    (
        frozenset(
            {
                "network_type",
            }
        ),
        frozenset(
            {
                "network_type", "current_network_mode",
            }
        ),
    ),
    (
        frozenset(
            {
                "traffic_month_download_bytes",
                "traffic_accounting_source",
            }
        ),
        frozenset({"monthly_rx_bytes"}),
    ),
    (frozenset({"traffic_month_upload_bytes", "traffic_accounting_source"}),
     frozenset({"monthly_tx_bytes"})),
    (
        frozenset(
            {
                "serving_band",
                "serving_band_signature", "active_band_set",
                "active_band_set_signature", "band_set", "band_set_signature",
            }
        ),
        frozenset(
            {
                "wan_active_band", "lte_ca_pcell_band",
                "ZCELLINFO_band", "lte_band",
            }
        ),
    ),
    (frozenset({"ca_active", "ca_state_text"}), frozenset({"wan_lte_ca"})),
    (
        frozenset(
            {
                "selected_band_set", "selected_band_set_signature",
                "configured_band_pool", "configured_band_pool_signature",
                "configured_band_pool_source", "configured_band_pool_status",
                "configured_band_pool_age_s",
                "configured_band_pool_last_sample_ts",
                "configured_ca_pool_display",
            }
        ),
        frozenset({"lte_band_lock"}),
    ),
    (frozenset({"sinr_db"}),
     frozenset({"lte_snr", "sinr", "ecio"})),
    (frozenset({"rsrp_dbm"}), frozenset({"lte_rsrp", "lte_rsrp_1"})),
    (frozenset({"rsrq_db"}), frozenset({"lte_rsrq"})),
    (frozenset({"rssi_dbm"}), frozenset({"lte_rssi", "rssi"})),
    (frozenset({"pci"}), frozenset({"lte_pci", "Z_PCI"})),
    (
        frozenset({"earfcn", "earfcn_dl", "dl_freq_mhz", "ul_freq_mhz"}),
        frozenset(
            {
                "lte_earfcn", "earfcn", "Z_dl_earfcn",
                "lte_ca_pcell_arfcn", "lte_ca_pcell_freq",
                "wan_active_channel",
            }
        ),
    ),
    (frozenset({"cell_id", "enb_id"}), frozenset({"cell_id"})),
    (frozenset({"tac"}), frozenset({"tac", "lac_code"})),
    (frozenset({"dl_bandwidth_mhz"}),
     frozenset({"lte_ca_pcell_bandwidth", "bandwidth"})),
    (
        frozenset({"provider_name"}),
        frozenset(
            {
                "network_provider",
                "network_provider_fullname",
            }
        ),
    ),
    (frozenset({"provider_plmn"}), frozenset({"mccmnc"})),
    (frozenset({"model", "router_model"}),
     frozenset({"model_name", "product_name"})),
    (frozenset({"hardware_version"}), frozenset({"hardware_version"})),
    (frozenset({"webui"}), frozenset({"web_version"})),
    (frozenset({"firmware"}),
     frozenset({"wa_inner_version", "cr_version"})),
    (frozenset({"serial"}), frozenset({"serial_number"})),
    (frozenset({"imei"}), frozenset({"imei"})),
    (frozenset({"imsi"}), frozenset({"sim_imsi", "imsi"})),
    (frozenset({"lan_mac"}), frozenset({"mac_address"})),
    (frozenset({"wan_ip"}), frozenset({"wan_ipaddr"})),
    (frozenset({"antenna"}),
     frozenset({"antenna_status", "antenna_type", "antenna_mode"})),
    (frozenset({"tx_power"}), frozenset({"tx_power"})),
    (frozenset({"nsa_active", "sa_active", "nr5g_active", "nr_mode", "nr_state_text"}),
     frozenset({"network_type", "current_network_mode"})),
    (frozenset({"nr_band", "nr5g_band"}),
     frozenset({"nr_ca_pcell_band", "nr5g_action_nsa_band", "nr5g_action_band", "Z5g_CELLINFO_band"})),
    (frozenset({"nr_pci", "nr5g_pci"}), frozenset({"nr5g_pci", "Z5g_PCI"})),
    (frozenset({"nr_cell_id", "nr5g_cell_id"}), frozenset({"nr5g_cell_id", "Z5g_CELL_ID"})),
    (frozenset({"nr_arfcn", "nr5g_arfcn"}), frozenset({"nr_ca_pcell_freq", "nr5g_action_channel", "Z5g_dlEarfcn"})),
    (frozenset({"nr_rsrp_dbm"}), frozenset({"5g_rx0_rsrp", "5g_rx1_rsrp", "Z5g_rsrp"})),
    (frozenset({"nr_rsrq_db"}), frozenset({"Z5g_rsrq"})),
    (frozenset({"nr_sinr_db"}), frozenset({"Z5g_SINR", "Z5g_snr"})),
    (frozenset({"nr_bandwidth_mhz"}), frozenset({"nr5g_nsa_bandwidth"})),
    (
        frozenset({"wifi", "wifi_enabled"}),
        frozenset(
            {
                "wifiEnabled", "wifi_onoff_state",
                "wifi_chip1_ssid1_switch_onoff",
                "wifi_chip2_ssid1_switch_onoff",
            }
        ),
    ),
    (frozenset({"wifi_24ghz_enabled"}), frozenset({"wifi_chip1_ssid1_switch_onoff", "wifiEnabled", "wifi_onoff_state"})),
    (frozenset({"wifi_5ghz_enabled"}), frozenset({"wifi_chip2_ssid1_switch_onoff", "wifiEnabled", "wifi_onoff_state"})),
    (frozenset({"temperature_modem_5g_c"}),
     frozenset({"pm_sensor_5g", "pm_modem_5g"})),
    (frozenset({"neighbors", "neighbor_count"}),
     frozenset({"ngbr_cell_info"})),
)


GOFORM_READ_FIELDS: tuple[str, ...] = (
    "network_type",
    "current_network_mode",
    "modem_main_state",
    "ppp_status",
    "ppp_uptime",
    "wan_active_band",
    "wan_active_channel",
    "wan_lte_ca",
    "lte_ca_pcell_band",
    "lte_ca_pcell_freq",
    "lte_ca_scell_band",
    "lte_ca_pcell_bandwidth",
    "lte_ca_scell_bandwidth",
    "lte_multi_ca_scell_info",
    "lte_multi_ca_scell_sig_info",
    "lte_band",
    "bandwidth",
    "signalbar",
    "lte_rsrp",
    "lte_rsrq",
    "lte_rssi",
    "lte_snr",
    "rssi",
    "rscp",
    "lte_rsrp_1",
    "lte_rsrp_2",
    "lte_rsrp_3",
    "lte_rsrp_4",
    "lte_snr_1",
    "lte_snr_2",
    "lte_snr_3",
    "lte_snr_4",
    "lte_pci",
    "Z_PCI",
    "lte_earfcn",
    "Z_dl_earfcn",
    "earfcn",
    "cell_id",
    "tac",
    "lac_code",
    "mccmnc",
    "rmcc",
    "rmnc",
    "network_provider",
    "network_provider_fullname",
    "lte_band_lock",
    "lte_freq_lock",
    "lte_pci_lock",
    "lte_earfcn_lock",
    "ZCELLINFO_band",
    "sinr",
    "ecio",
    "wan_ipaddr",
    "wan_connected",
    "wan_iface_name",
    "imei",
    "iccid",
    "sim_imsi",
    "imsi",
    "sim_status",
    "serial_number",
    "model_name",
    "product_name",
    "hardware_version",
    "wa_inner_version",
    "web_version",
    "cr_version",
    "upgrade_result",       # MC801A B12/B16 firmware update loop detector
    "tx_power",
    "antenna_status",
    "antenna_type",
    "antenna_mode",
    "ngbr_cell_info",
    "lte_ca_pcell_arfcn",
    "lte_ca_scell_arfcn",
    "lte_ca_scell_info",
    "5g_rx0_rsrp",
    "5g_rx1_rsrp",
    "Z5g_rsrp",
    "Z5g_rsrq",
    "Z5g_SINR",
    "Z5g_snr",
    "Z5g_PCI",
    "network_Z5g_PCI",
    "Z5g_CELL_ID",
    "Z5g_CELLINFO_band",
    "network_Z5g_CELLINFO_band",
    "Z5g_dlEarfcn",
    "nr5g_cell_id",
    "nr5g_pci",
    "nr5g_action_channel",
    "nr5g_action_band",
    "nr5g_action_nsa_band",
    "nr_ca_pcell_band",
    "nr_ca_pcell_freq",
    "nr_multi_ca_scell_info",
    "nr5g_nsa_bandwidth",
    "nr5g_sa_band_lock",
    "nr5g_nsa_band_lock",
    "pm_sensor_ambient",
    "pm_sensor_mdm",
    "pm_sensor_5g",
    "pm_sensor_pa1",
    "pm_modem_5g",
    "wifi_chip_temp",
    "wan_rx_bytes",
    "wan_tx_bytes",
    "rx_bytes",
    "tx_bytes",
    "realtime_rx_bytes",
    "realtime_tx_bytes",
    "realtime_time",
    "realtime_rx_thrpt",
    "realtime_tx_thrpt",
    "monthly_rx_bytes",
    "monthly_tx_bytes",
    "monthly_time",
    "wifiEnabled",
    "wifi_onoff_state",
    "wifi_chip1_ssid1_switch_onoff",
    "wifi_chip2_ssid1_switch_onoff",
    "mac_address",
)

GOFORM_STATIC_BOOT_FIELDS: frozenset[str] = frozenset(
    {
        "imei",
        "iccid",
        "sim_imsi",
        "imsi",
        "serial_number",
        "model_name",
        "product_name",
        "hardware_version",
        "wa_inner_version",
        "web_version",
        "cr_version",
        "upgrade_result",
        "antenna_status",
        "antenna_type",
        "antenna_mode",
        "mac_address",
    }
)

GOFORM_MAINTENANCE_READ_FIELDS: tuple[str, ...] = tuple(
    field for field in GOFORM_READ_FIELDS if field not in GOFORM_STATIC_BOOT_FIELDS
)

# Protected live lane for the clock-driven actionable snapshot.
# Keep this under the GoAhead chunk threshold so LIVE MODULE can fetch it in a
# single request while maintenance owns heavier/static/config fields.
GOFORM_FAST_READ_FIELDS: tuple[str, ...] = (
    "network_type",
    "current_network_mode",
    "modem_main_state",
    "ppp_status",
    "wan_active_band",
    "wan_active_channel",
    "wan_lte_ca",
    "lte_ca_pcell_band",
    "lte_ca_pcell_freq",
    "lte_ca_scell_band",
    "lte_ca_pcell_bandwidth",
    "lte_ca_scell_bandwidth",
    "lte_multi_ca_scell_info",
    "lte_multi_ca_scell_sig_info",
    "lte_ca_pcell_arfcn",
    "lte_rsrp",
    "lte_rsrq",
    "lte_rssi",
    "lte_snr",
    "rssi",
    "lte_rsrp_1",
    "sinr",
    "ecio",
    "lte_pci",
    "Z_PCI",
    "lte_earfcn",
    "Z_dl_earfcn",
    "earfcn",
    "cell_id",
    "tac",
    "mccmnc",
    "network_provider",
    "Z5g_rsrp",
    "Z5g_rsrq",
    "Z5g_snr",
    "Z5g_PCI",
    "Z5g_CELL_ID",
    "Z5g_CELLINFO_band",
    "Z5g_dlEarfcn",
    "wan_rx_bytes",
    "wan_tx_bytes",
    "realtime_rx_bytes",
    "realtime_tx_bytes",
    "realtime_rx_thrpt",
    "realtime_tx_thrpt",
)

# ZTE 5G profiles need NR bandwidth in the same current-state transaction as
# band/cell/PCI. Keep the reviewed one-request budget by dropping one duplicate
# LTE RSRP alias from this 5G-only lane.
ZTE_5G_FAST_READ_FIELDS: tuple[str, ...] = tuple(
    field for field in GOFORM_FAST_READ_FIELDS if field != "lte_rsrp_1"
) + ("nr5g_nsa_bandwidth",)

# MF296R is an LTE-only profile. Its protected lane reads only current
# radio/RF/traffic facts. Full startup owns initial WAN/Wi-Fi completeness,
# maintenance refreshes those slow facts, and post-write acquisition owns
# immediate Wi-Fi confirmation.
MF296R_FAST_READ_FIELDS: tuple[str, ...] = tuple(
    field
    for field in GOFORM_FAST_READ_FIELDS
    if not field.startswith("Z5g_") and field != "tac"
) + ("lac_code", "loginfo")

ZTE_EMPTY_RF_DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "network_type",
    "current_network_mode",
    "modem_main_state",
    "ppp_status",
    "wan_lte_ca",
    "lte_rsrp",
    "lte_rsrq",
    "lte_rssi",
    "lte_snr",
    "rssi",
    "rscp",
    "sinr",
    "ecio",
    "signalbar",
    "wan_active_band",
    "lte_band",
    "lte_ca_pcell_band",
    "lte_ca_scell_band",
    "ZCELLINFO_band",
    "lte_pci",
    "Z_PCI",
    "lte_earfcn",
    "Z_dl_earfcn",
    "earfcn",
    "cell_id",
    "lte_ca_pcell_arfcn",
    "lte_ca_scell_arfcn",
    "lte_ca_scell_info",
    "lte_multi_ca_scell_info",
    "lte_multi_ca_scell_sig_info",
    "wa_inner_version",
    "web_version",
    "cr_version",
    "model_name",
    "product_name",
)

# Identity is stable within one authenticated device session. Live values are
# never taken from this retained context; Matrix owns missing-value retention.
GOFORM_RETAINED_IDENTITY_FIELDS = frozenset({
    "model_name", "product_name", "serial_number", "imei", "hardware_version",
    "wa_inner_version", "web_version", "cr_version", "mac_address", "iccid",
    "sim_imsi", "imsi",
})

ZTE_RF_OR_CELL_PAYLOAD_FIELDS: tuple[str, ...] = (
    "lte_rsrp",
    "lte_rsrq",
    "lte_rssi",
    "lte_snr",
    "rssi",
    "rscp",
    "sinr",
    "ecio",
    "wan_active_band",
    "lte_band",
    "lte_ca_pcell_band",
    "lte_ca_scell_band",
    "ZCELLINFO_band",
    "lte_pci",
    "Z_PCI",
    "lte_earfcn",
    "Z_dl_earfcn",
    "earfcn",
    "cell_id",
    "lte_ca_pcell_arfcn",
    "lte_ca_scell_arfcn",
    "lte_ca_scell_info",
    "lte_multi_ca_scell_info",
    "lte_multi_ca_scell_sig_info",
)

ZTE_SENSITIVE_DIAGNOSTIC_FIELDS = {
    "imei",
    "imsi",
    "sim_imsi",
    "serial_number",
    "password",
    "AD",
    "LD",
    "RD",
}

ZTE_EMPTY_RF_DIAGNOSTIC_THROTTLE_S = 60.0

ZTE_LOGIN_DATA_FIELDS: tuple[str, ...] = (
    "modem_main_state",
    "puknumber",
    "pinnumber",
    "opms_wan_mode",
    "psw_fail_num_str",
    "login_lock_time",
    "SleepStatusForSingleChipCpe",
)

ZTE_LOGIN_TOKEN_FIELDS: tuple[str, ...] = (
    "wa_inner_version",
    "cr_version",
    "RD",
    "LD",
)

ZTE_WRITE_OK_RESULTS = {"0", "4", "ok", "success", "true"}
ZTE_EXTERNAL_ANTENNA_PROFILE_IDS = {
    "zte_mc801a_bg_provisional",
    "zte_mc801a_hk_b18",
}
ZTE_5G_ONLY_PROFILE_IDS = {
    "zte_mc801a_bg_provisional",
}


class ZteGoformModem(ModemInterface):
    """Passive-first ZTE goform modem adapter."""

    physical_request_authority_required = True

    def __init__(
        self,
        host: str = "192.168.1.1",
        username: str = "admin",
        password: str = "",
        ping_interface: Optional[str] = None,
        backend_name: str = "mf296r",
        expected_model: str = "",
        expected_serial: str = "",
        expected_firmware: str = "",
        expected_imei: str = "",
        profile_id: str = "",
    ):
        self._username = str(username or "").strip()
        self._password = str(password or "")
        self._ping_iface = str(ping_interface or "").strip() or None
        self._backend_name = str(backend_name or "mf296r").strip().lower()
        self._profile_field_issues = {}
        self._portable_rf_values = {}
        self._access_id_algorithm = None
        if self._backend_name == "g5b":
            from arch4.modem_live.profile_catalog import modem_profile_record
            if not profile_id:
                raise ValueError("g5b_exact_selected_profile_required")
            selected = modem_profile_record(profile_id)
            if selected.get("backend") != "g5b" or selected.get("authentication_contract") != "goform_g5b_b03_md5_assumption_v1":
                raise ValueError("g5b_selected_authentication_contract_required")
            selected_model = str(selected["target_guard_model"])
            selected_firmware = str(selected["firmware_exact"])
            if expected_model and expected_model != selected_model or expected_firmware and expected_firmware != selected_firmware:
                raise ValueError("g5b_selected_identity_guard_conflict")
            expected_model, expected_firmware = selected_model, selected_firmware
            self._access_id_algorithm = "md5"
        from arch4.modem_live.profile_catalog import modem_profile_definition
        default_profile = {
            'mf296r': 'zte_mf296r_a1srb_b04',
            'mc888': 'zte_mc888_goform',
            'zte_zwrt': 'zte_zwrt_ubus_baseline',
            'g5tc': 'zte_g5tc_zwrt_model',
        }.get(self._backend_name, 'zte_webui_goform_baseline')
        # The public factory passes its exact selected profile. Direct legacy
        # transport construction keeps the existing generic GoForm contract.
        # It still loads an external file; this is not an embedded fallback.
        self._definition = modem_profile_definition(profile_id or default_profile)
        host = self._definition.http_address(host)
        self._base_url = self._selected_origin(host)
        self._host = urlsplit(self._base_url).netloc
        self._auto_https = "://" not in str(host or "")
        self._verify_router_tls = self._router_tls_verification(self._base_url)
        self._goform_profile_radio_keys = tuple(self._definition.native_source_keys(
            addresses, reply='goform') for addresses in (
                {'radio.serving_band', 'rf.pci', 'rf.cell_id', 'rf.earfcn_dl'},
                {'radio.nr_band', 'radio.nr_pci', 'radio.nr_cell_id', 'radio.nr_arfcn'}))
        self._goform_profile_sources = {}
        self._goform_native_sources = {}
        if self._definition.has_request('goform'):
            for address,item in self._definition.fields.items():
                if item.get('read') is not None:
                    self._goform_profile_sources[address] = self._definition.native_source_keys({address},reply='goform')
            for canonical,native in _GOFORM_CANONICAL_SOURCE_GROUPS:
                for address in matrix_cells_for_canonical_fields(canonical):
                    self._goform_native_sources.setdefault(address,set()).update(native)
            for direction in ('rx','tx'):
                self._goform_native_sources['traffic.'+direction+'_bytes_per_sec'] = {
                    f'wan_{direction}_bytes',f'{direction}_bytes',f'realtime_{direction}_bytes',
                    f'traffic_{direction}',f'total_{direction}_bytes',
                    f'realtime_{direction}_thrpt',f'{direction}_thrpt',f'{direction}_rate'}
            self._goform_native_sources['health.radio_connected'] = {
                'network_type','current_network_mode','ppp_status','modem_main_state'}
            self._goform_native_sources['traffic.session_uptime_s'] = {'ppp_uptime'}
            # Existing native fallbacks: CA can be inferred from active bands;
            # missing reported RSRQ can be estimated from RSRP/RSSI/bandwidth.
            self._goform_native_sources['radio.ca_active'].update(
                self._goform_native_sources['radio.serving_band'] | {'lte_ca_scell_band'})
            for address in ('rf.rsrp_dbm','rf.rssi_dbm','slow.dl_bandwidth_mhz'):
                self._goform_native_sources['rf.rsrq_db'].update(self._goform_native_sources[address])
        self._access_profile = modem_live_access_profile_for(
            backend=self._backend_name,
            profile_id=self._definition.profile_id,
            backend_family=(
                "zte_zwrt_ubus_like"
                if self._backend_name in {"zte_zwrt", "g5tc"}
                else "zte_goform_like"
            ),
        )
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(
            self._access_profile
        )
        self._connect_timeout = max(
            0.05,
            float(self._access_profile.maintenance_connect_timeout_s),
        )
        self._read_timeout = max(
            0.05,
            float(self._access_profile.fast_signal_read_timeout_s),
        )
        self._maintenance_read_timeout = max(
            0.05,
            float(self._access_profile.maintenance_read_timeout_s),
        )
        self._read_retry_limit = max(
            0,
            int(self._access_profile.read_retry_limit),
        )
        self._expected_model = str(expected_model or "").strip()
        self._expected_serial = str(expected_serial or "").strip()
        self._expected_firmware = str(expected_firmware or "").strip()
        self._expected_imei = str(expected_imei or "").strip()
        self._session = self._new_session(
            connection_mode=self._access_profile.http_connection_mode,
        )
        self._connected = False
        self._authenticated = False
        self._last_status: Dict[str, Any] = {}
        self._last_raw: Dict[str, Any] = {}
        self._last_empty_rf_diag_ts: float = 0.0
        self._empty_rf_diag_sequence: int = 0
        self._last_empty_rf_diagnostic: Dict[str, Any] = {}
        self._traffic_counter_samples: Dict[str, tuple[int, float, str]] = {}
        self._traffic_rates_proven: set[str] = set()
        self._session_checked_at = time.monotonic()
        self._session_maintenance_attempt_at = time.monotonic()
        self._session_maintenance_status: Dict[str, Any] = {}
        self._synthetic_session_started_at: Optional[float] = None
        self._firmware_band_lock_risk: bool = False
        self._firmware_version_raw: str = ""
        # Cached AD token components — avoid 2 HTTP requests per write
        self._cached_wa_inner_version: str = ""
        self._cached_cr_version: str = ""
        self._static_config_overrides: Dict[str, bool] = {}
        self._goform_api_timing: Dict[str, Dict[str, Any]] = {}
        self._maintenance_chunk_cursor: int = 0
        self._supported_lte_bands: tuple[int, ...] = ()
        self._supported_lte_bands_source: str = ""
        self._supported_lte_bands_observed_at: float = 0.0
        # Existing boot-static capability lifetime: immutable firmware choices,
        # not a poll cache or a second recovery owner.
        self._goform_radio_mode_options: frozenset[str] = frozenset()
        self.radio_mode_preferred_key = None

    @staticmethod
    def _selected_origin(host: str) -> str:
        value = str(host or "192.168.1.1").strip()
        try:
            parts = urlsplit(value if "://" in value else "http://" + value)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.path not in {"", "/"} or parts.query or parts.fragment
                    or any(char.isspace() for char in value) or "\\" in value):
                raise ValueError
            _ = parts.port
        except ValueError:
            raise ValueError("zte_router_origin_invalid") from None
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    def _router_tls_verification(origin: str) -> bool:
        # Existing ZWRT local-router policy, chosen before I/O; DNS/public hosts
        # keep verification. Never retry a certificate failure with weaker TLS.
        try:
            address = ipaddress.ip_address(urlsplit(origin).hostname)
            return not any(address in ipaddress.ip_network(net) for net in (
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
            ))
        except ValueError:
            return True

    def _bootstrap_goform_homepage(self) -> requests.Response:
        url = self._base_url
        for hop in range(4):
            try:
                response = self._zte_http_get(url, operation="homepage", lane="startup",
                                              timeout=(self._connect_timeout, self._read_timeout))
            except requests.ConnectionError as exc:
                if (hop != 0 or not self._auto_https or urlsplit(url).scheme != "http"
                        or isinstance(exc, (requests.Timeout, requests.exceptions.SSLError))):
                    raise
                # Some ZTE firmware resets port80 instead of redirecting. This
                # single credential-free probe precedes login; never replay POST.
                self._base_url = "https://" + urlsplit(url).netloc
                url = self._base_url
                continue
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location", "")
            try:
                destination, previous = urlsplit(urljoin(url, location)), urlsplit(url)
                _ = destination.port
            except ValueError:
                raise requests.TooManyRedirects("zte_bootstrap_redirect_forbidden") from None
            if (hop == 3 or not location or destination.hostname != previous.hostname
                    or destination.scheme not in {"http", "https"}
                    or (previous.scheme == "https" and destination.scheme != "https")
                    or destination.username is not None or destination.password is not None
                    or destination.path not in {"", "/", "/index.html"}
                    or destination.query or destination.fragment or "\\" in location):
                raise requests.TooManyRedirects("zte_bootstrap_redirect_forbidden")
            self._base_url = f"{destination.scheme}://{destination.netloc}"
            url = self._base_url + (destination.path or "/")
        raise requests.TooManyRedirects("zte_bootstrap_redirect_limit")

    def physical_request_lane_scope(self, lane: str):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self) -> Dict[str, Any]:
        return self._physical_request_authority.snapshot()

    def _zte_http_get(
        self,
        url: str,
        *,
        operation: str,
        timeout: float | tuple[float, float],
        lane: str = "slow_live",
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.update(allow_redirects=False, verify=self._verify_router_tls)
        physical_lane = self._physical_request_authority.current_lane(
            default=lane
        )
        with self._physical_request_authority.request(
            lane=physical_lane,
            operation=f"zte:http:get:{operation}",
        ):
            return traced_http(self._session.get,
                url,
                timeout=timeout,
                **kwargs,
            )

    def _zte_http_post(
        self,
        url: str,
        *,
        operation: str,
        timeout: float,
        lane: str = "event_write",
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.update(allow_redirects=False, verify=self._verify_router_tls)
        physical_lane = self._physical_request_authority.current_lane(
            default=lane
        )
        with self._physical_request_authority.request(
            lane=physical_lane,
            operation=f"zte:http:post:{operation}",
        ):
            response = traced_http(self._session.post,
                url,
                timeout=timeout,
                **kwargs,
            )
            if 300 <= response.status_code < 400:
                raise requests.TooManyRedirects("zte_write_redirect_forbidden")
            return response

    @staticmethod
    def _imei_production_serial(imei: Any) -> str:
        digits = "".join(character for character in str(imei or "") if character.isdigit())
        if len(digits) != 15:
            return ""
        return digits[8:14]

    def _derive_profile_identity(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        derived = dict(identity or {})
        if self._backend_name == "mf296r" and not str(derived.get("serial") or "").strip():
            serial = self._imei_production_serial(derived.get("imei"))
            if serial:
                derived["serial"] = serial
                derived["serial_source"] = "imei_snr"
        return derived

    @staticmethod
    def _mf296r_device_config_path(config_source: str) -> str:
        match = re.search(
            r"\bDEVICE\s*:\s*['\"]([^'\"]+)['\"]",
            str(config_source or ""),
        )
        if match is None:
            return ""
        device = str(match.group(1) or "").strip().strip("/")
        if (
            not device
            or ".." in device
            or re.fullmatch(r"[A-Za-z0-9_./-]+", device) is None
        ):
            return ""
        return f"/js/config/{device}/config.js"

    @staticmethod
    def _mf296r_supported_lte_bands_from_config(
        device_config_source: str,
    ) -> tuple[int, ...]:
        table = re.search(
            r"\bFREQUENCY_TYPE_4G\s*:\s*\[(.*?)\]",
            str(device_config_source or ""),
            re.DOTALL,
        )
        if table is None:
            return ()
        bands: set[int] = set()
        for item in re.findall(r"\{([^{}]*)\}", table.group(1)):
            name = re.search(r"\bname\s*:\s*['\"](\d+)['\"]", item)
            value = re.search(
                r"\bvalue\s*:\s*['\"]0x([0-9A-Fa-f]+)['\"]",
                item,
            )
            if name is None or value is None:
                continue
            band = int(name.group(1))
            mask = int(value.group(1), 16)
            if band <= 0 or mask != 1 << (band - 1):
                continue
            bands.add(band)
        return tuple(sorted(bands))

    def _refresh_mf296r_supported_lte_bands(self) -> bool:
        self._supported_lte_bands = ()
        self._supported_lte_bands_source = ""
        self._supported_lte_bands_observed_at = 0.0
        if self._backend_name != "mf296r":
            return False
        try:
            base = self._zte_http_get(
                f"{self._base_url}/js/config/config.js",
                operation="mf296r_config_root",
                lane="boot_static",
                headers=self._goform_headers(),
                timeout=self._maintenance_read_timeout,
            )
            if base.status_code != 200:
                return False
            device_path = self._mf296r_device_config_path(base.text)
            if not device_path:
                return False
            device = self._zte_http_get(
                f"{self._base_url}{device_path}",
                operation="mf296r_device_config",
                lane="boot_static",
                headers=self._goform_headers(),
                timeout=self._maintenance_read_timeout,
            )
            if device.status_code != 200:
                return False
            bands = self._mf296r_supported_lte_bands_from_config(device.text)
            if not bands:
                return False
        except requests.RequestException as exc:
            log.warning("ZTE MF296R firmware band catalog read failed: %s", exc)
            return False
        self._supported_lte_bands = bands
        self._supported_lte_bands_source = (
            f"firmware_asset:{device_path}:FREQUENCY_TYPE_4G"
        )
        self._supported_lte_bands_observed_at = time.time()
        return True

    def _target_guard_enabled(self) -> bool:
        return bool(self._expected_model or self._expected_serial or self._expected_firmware or self._expected_imei)

    @staticmethod
    def _canonical_identity_from_goform(
        observed: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Translate ZTE Goform identity names into the Modem Live contract."""
        raw = dict(observed or {})
        model = str(raw.get("model_name") or raw.get("product_name") or "").strip()
        hardware_version = str(raw.get("hardware_version") or "").strip()
        web_version = str(raw.get("web_version") or "").strip()
        inner_version = str(raw.get("wa_inner_version") or "").strip()
        config_version = str(raw.get("cr_version") or "").strip()
        software_version = web_version or inner_version
        firmware = inner_version or config_version
        return {
            "model": model or None,
            "router_model": model or None,
            "hardware_version": hardware_version or None,
            "software": software_version or None,
            "software_version": software_version or None,
            "webui": web_version or None,
            "firmware": firmware or None,
            "config_version": config_version or None,
            "configuration_file_version": config_version or None,
            "serial": str(raw.get("serial_number") or "").strip() or None,
            "imei": str(raw.get("imei") or "").strip() or None,
            "lan_mac": str(raw.get("mac_address") or "").strip() or None,
        }

    def _profile_identity_from_raw(self, raw, *, retain=False):
        identity = self._canonical_identity_from_goform(raw)
        identity['imsi'] = self._normalize_imsi((raw or {}).get('sim_imsi') or (raw or {}).get('imsi'))
        replies = self._profile_native_replies(raw)
        addresses = ({'identity.' + field for field in identity} | {'profile.config_version'}) & self._definition.available_addresses(replies)
        values = self._decode_profile_values(replies, addresses)
        for address, value in values.items():
            field = address.split('.', 1)[1]
            if retain and value in (None, ''):
                value = self._last_status.get(field)
            identity[field] = value
            if address=='profile.config_version':identity['configuration_file_version']=value
        # This is the existing explicit operator identity policy, not a native
        # reply. It must remain separate from profile decoding and fresh cells.
        if (raw or {}).get('physical_identity_source') == 'operator_supplied_serial':
            identity['serial'] = raw.get('serial_number')
        return identity

    def _apply_target_guard(self, observed: Optional[Dict[str, Any]]) -> bool:
        vendor_identity_keys = {
            "model_name",
            "product_name",
            "serial_number",
            "wa_inner_version",
            "web_version",
            "cr_version",
        }
        if isinstance(observed, dict) and vendor_identity_keys.intersection(observed):
            raise ValueError("zte_target_guard_canonical_identity_required")
        ok, reason = evaluate_target_guard(
            expected_model=self._expected_model,
            expected_serial=self._expected_serial,
            expected_firmware=self._expected_firmware,
            expected_imei=self._expected_imei,
            observed=observed,
        )
        if isinstance(observed, dict):
            observed["target_guard_ok"] = bool(ok)
            observed["target_guard_reason"] = str(reason or "")
        if ok:
            return True
        self._connected = False
        log.error("ZTE target guard rejected current router: %s", reason)
        raise TargetGuardError(
            f"Target guard failed for ZTE at {self._host}: {reason}",
            reason=str(reason or ""),
        )

    def connect(self) -> bool:
        self._last_raw = {}
        self._last_status = {}
        self._traffic_counter_samples.clear()
        self._synthetic_session_started_at = None
        self._goform_radio_mode_options = frozenset()
        try:
            resp = self._bootstrap_goform_homepage()
            if resp.status_code != 200:
                log.warning("ZTE homepage returned %d", resp.status_code)
                self._connected = False
                return False
            self._authenticated = False
            if self._password:
                self._authenticated = self._try_login()
                if not self._authenticated:
                    raise AuthenticationError(
                        f"Authentication failed for ZTE at {self._host} — check password"
                    )
            else:
                # No password provided — ZTE public fields are unusable
                raise AuthenticationError(
                    f"ZTE at {self._host} requires a password but none was provided"
                )
            if (
                self._backend_name == "mf296r"
                and not self._refresh_mf296r_supported_lte_bands()
            ):
                log.warning(
                    "ZTE MF296R firmware did not publish a usable LTE band catalog"
                )
            # Connection owns authentication and target identity only.  The
            # startup_full acquisition immediately following this call is the
            # sole complete current-state read; do not download it twice.
            probe = self._read_goform_for_acquisition(
                tuple(sorted(GOFORM_STATIC_BOOT_FIELDS))
            )
            self._connected = bool(probe)
            if self._connected and isinstance(probe, dict):
                self._last_raw = dict(probe)
            if self._connected and self._target_guard_enabled():
                self._apply_target_guard(
                    self._derive_profile_identity(
                        self._profile_identity_from_raw(probe)
                    )
                )  # raises TargetGuardError on mismatch
            if self._connected:
                from arch4.modem.zte_goform_radio_mode import read_native_mode_options
                self._goform_radio_mode_options = read_native_mode_options(self)
            if self._connected:
                self._session_checked_at = time.monotonic()
                self._session_maintenance_attempt_at = self._session_checked_at
                log.info("Connected to ZTE goform router at %s (authenticated)", self._host)
            else:
                log.warning("ZTE goform probe returned no usable data from %s", self._host)
            return self._connected
        except (AuthenticationError, TargetGuardError):
            raise
        except requests.RequestException as exc:
            log.error("ZTE connection failed: %s", exc)
            self._connected = False
            return False

    def disconnect(self) -> None:
        if self._authenticated:
            try:
                self._post_goform(
                    {"isTest": "false", "goformId": "LOGOFF"}
                )
            except Exception as exc:
                log.debug("ZTE logoff suppressed during disconnect: %s", exc)
        self._session.close()
        self._connected = False
        self._authenticated = False
        self._last_raw = {}
        self._traffic_counter_samples.clear()
        self._cached_wa_inner_version = ""
        self._cached_cr_version = ""
        self._supported_lte_bands = ()
        self._supported_lte_bands_source = ""
        self._supported_lte_bands_observed_at = 0.0
        self._goform_radio_mode_options = frozenset()
        log.info("Disconnected from ZTE goform router")

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        try:
            status = self._read_goform(("loginfo",))
            if "loginfo" not in status:
                raise ConnectionError("zte_session_status_unavailable")
            lost = self._authenticated_session_explicitly_lost(status)
            return not lost and str(status.get("loginfo") or "").strip().lower() == "ok"
        except requests.RequestException as exc:
            raise ConnectionError("zte_session_status_unavailable") from exc

    def _goform_chunk_count(self, fields: List[str] | tuple[str, ...]) -> int:
        return len(self._goform_request_chunks(fields))

    def _record_goform_api_timing(
        self,
        lane: str,
        *,
        elapsed_s: float,
        fields: List[str] | tuple[str, ...],
        ok: bool,
    ) -> None:
        lane_key = str(lane or "").strip() or "unknown"
        self._goform_api_timing[lane_key] = {
            "elapsed_ms": round(max(0.0, float(elapsed_s or 0.0)) * 1000.0, 2),
            "field_count": len([str(field).strip() for field in fields if str(field).strip()]),
            "chunk_count": self._goform_chunk_count(fields),
            "single_request": self._goform_chunk_count(fields) == 1,
            "ok": bool(ok),
        }

    def _goform_fast_read_profile(self) -> Dict[str, Any]:
        fields = self._actionable_read_fields()
        chunk_count = self._goform_chunk_count(fields)
        return {
            "lane": "protected_fast_read",
            "acquisition_class": "aligned_split",
            "field_count": len(fields),
            "chunk_count": chunk_count,
            "single_request": chunk_count == 1,
            "maintenance_field_count": len(GOFORM_MAINTENANCE_READ_FIELDS),
            "maintenance_chunk_count": self._goform_chunk_count(GOFORM_MAINTENANCE_READ_FIELDS),
            "cached_fields": [
                "selected_band_set",
                "wifi_enabled",
                "model",
                "firmware",
                "temperature_modem_5g_c",
            ],
        }

    def _inject_goform_lane_debug_status(self, status: Dict[str, Any]) -> None:
        if not isinstance(status, dict):
            return
        status["goform_fast_read"] = self._goform_fast_read_profile()
        status["goform_api_timing"] = {
            key: dict(value)
            for key, value in self._goform_api_timing.items()
            if isinstance(value, dict)
        }
        if self._session_maintenance_status:
            status["goform_session_maintenance"] = dict(self._session_maintenance_status)

    def _build_modem_input_from_raw(
        self, raw: Dict[str, Any], *, traffic_observed_at: float | None = None,
        expect_rf: bool = True,
    ) -> Optional[ModemInput]:
        previous_diagnostic = self._last_status.get('backend_diagnostic')
        fresh_raw = dict(raw or {})
        effective_raw = {
            key: value for key, value in self._last_raw.items()
            if key in GOFORM_RETAINED_IDENTITY_FIELDS
        }
        # A newly observed model alias must win over a retained other alias.
        if any(self._goform_source_observed(fresh_raw, key) for key in ("model_name", "product_name")):
            effective_raw.pop("model_name", None)
            effective_raw.pop("product_name", None)
        effective_raw.update(fresh_raw)
        self._last_raw = dict(effective_raw)
        raw = effective_raw
        if not self._connected:
            return None
        ts = time.time()
        traffic_at = time.monotonic() if traffic_observed_at is None else traffic_observed_at
        rf = self._decode_profile_rf(raw, traffic_observed_at=traffic_at)
        network_type = (rf.get("radio.network_type") or "") if "radio.network_type" in rf else self._normalize_network_type(raw.get("network_type") or raw.get("current_network_mode"))
        ppp_status = str(raw.get("ppp_status") or raw.get("modem_main_state") or "").strip()
        connection_evidence = self._raw_radio_connected(raw)
        if "health.radio_connected" in rf:
            connection_evidence = rf["health.radio_connected"]
        radio_connected = connection_evidence is True

        provider_plmn = (rf.get("identity.provider_plmn") or "") if "identity.provider_plmn" in rf else self._resolve_plmn(raw)
        provider_name = self._resolve_provider_name(raw, provider_plmn)
        # Operator-name lookup depends on the accepted PLMN, while a native
        # reported name remains an independent editable source.
        names = {code:resolve_provider_name(code) for code in
                 {provider_plmn,self._resolve_plmn(raw)} if code}
        name_context = {"provider_plmn":provider_plmn,"provider_names":{
            code:name for code,name in names.items() if name and name != "Unknown"}}
        name_replies = self._profile_native_replies(raw)
        name_addresses = self._definition.available_addresses(name_replies,
            context=name_context, addresses={"identity.provider_name"})
        name_values = self._decode_profile_values(name_replies, name_addresses, context=name_context)
        if "identity.provider_name" in name_values:
            provider_name = name_values["identity.provider_name"] or "Unknown"
            self._portable_rf_values.update(name_values)
        profile = self._resolve_firmware_profile()
        provider_pool = profile.provider_band_pool if profile is not None else ()
        selected_bands = (
            list(provider_pool) if provider_pool
            else self._decode_band_mask(raw.get("lte_band_lock"))
        )
        pool_source = (
            "profile_provider_band_pool" if provider_pool
            else "zte_goform_lte_band_lock" if selected_bands else None
        )

        active_bands = self._resolve_active_bands(raw)
        if 'radio.serving_band' in rf:
            primary = int(rf['radio.serving_band'] or 0)
            active_bands = self._dedupe_bands([primary,*active_bands[1:]]) if primary>0 else []
        canonical_active_bands = normalize_band_list(active_bands)
        serving_band = int(active_bands[0] if active_bands else 0)
        ca_active = self._is_ca_active(raw, active_bands)
        if 'radio.ca_active' in rf:ca_active=rf['radio.ca_active']is True
        ca_signature = self._build_ca_signature(active_bands, serving_band, ca_active)
        if provider_pool:
            ca_signature = band_signature(selected_bands)
        carrier_bandwidths = self._resolve_carrier_bandwidths(raw, active_bands)
        # PCell width is independent of whether this reply includes its band.
        dl_bandwidth_mhz = max(0.0, self._parse_metric_float(
            raw.get("lte_ca_pcell_bandwidth") or raw.get("bandwidth")
        ))

        pci = int(rf.get('rf.pci') or 0)
        earfcn = int(rf.get('rf.earfcn_dl') or 0)
        dl_freq_mhz, ul_freq_mhz = lte_frequency_mhz_from_dl_earfcn(earfcn)
        cell_id = int(rf.get('rf.cell_id') or 0)
        tac = int(rf.get('slow.tac') or 0)
        enb_id = int(rf.get('rf.enb_id', cell_id // 256 if cell_id > 255 else 0) or 0)

        # ModemInput's legacy numeric defaults do not certify a fresh field.
        # The receipt below uses the optional decoded values, not these defaults.
        sinr = rf.get('rf.sinr_db') or 0.0
        rsrp = rf.get('rf.rsrp_dbm') or 0.0
        rssi = rf.get('rf.rssi_dbm') or 0.0
        rsrq = rf.get('rf.rsrq_db') or 0.0
        rsrq_source = self._definition.state_value('rsrq_source', self._profile_native_replies(raw))
        has_connection_evidence = (connection_evidence is not None or self._goform_source_observed(raw, "ppp_uptime")
            or self._definition.has_read('traffic.session_uptime_s'))
        session_uptime_s = (
            self._resolve_session_uptime(raw, ts, connection_evidence)
            if has_connection_evidence else 0.0
        )
        rx_bps, tx_bps = self._read_traffic(
            raw, traffic_at,
        )

        identity = self._derive_profile_identity(
            self._profile_identity_from_raw(raw, retain=True)
        )
        model = str(identity.get("model") or "")
        hardware_version = str(identity.get("hardware_version") or "")
        web_version = str(identity.get("webui") or "")
        software_version = str(identity.get("software_version") or "")
        firmware = str(identity.get("firmware") or "")
        config_version = str(identity.get("config_version") or "")
        inner_version = str(raw.get("wa_inner_version") or "").strip()
        self._firmware_version_raw = inner_version
        self._firmware_band_lock_risk = self._check_firmware_band_lock_risk(inner_version)
        imei = str(identity.get("imei") or "")
        imsi = str(identity.get('imsi') or '')
        serial = str(identity.get("serial") or "")
        lan_mac = str(identity.get("lan_mac") or "")
        wan_ip = str(raw.get("wan_ipaddr") or "").strip()
        upgrade_result = str(raw.get("upgrade_result") or "").strip().lower()

        lte_cells = self._build_lte_cells(raw, serving_band, pci, earfcn, sinr, rsrp, rsrq, rssi)
        carriers = self._build_carriers(
            lte_cells=lte_cells,
            active_bands=active_bands,
            carrier_bandwidths=carrier_bandwidths,
            serving_band=serving_band,
            earfcn=earfcn,
            pci=pci,
            sinr=sinr,
            rsrp=rsrp,
        )
        nr5g_status = self._parse_nr5g_status(raw, network_type, profile_values=rf)

        self._last_status = {
            "ts": ts,
            "provider_name": provider_name or None,
            "provider_plmn": provider_plmn or None,
            "provider": provider_name or None,
            "plmn": provider_plmn or None,
            "network_provider_fullname": self._normalize_provider_fullname(raw, provider_plmn),
            "network_type": network_type or None,
            "radio_connected": bool(radio_connected),
            "radio_state_text": "Connected" if radio_connected else "Disconnected",
            "ca_active": bool(ca_active),
            "ca_state_text": "ACTIVE" if ca_active else "INACTIVE",
            "band": serving_band or None,
            "serving_band": serving_band or None,
            "serving_band_signature": f"B{serving_band}" if serving_band else "",
            "selected_band_set": list(selected_bands) if selected_bands else None,
            "selected_band_set_signature": band_signature(selected_bands) if selected_bands else None,
            "configured_band_pool": list(selected_bands) if selected_bands else None,
            "configured_band_pool_signature": band_signature(selected_bands) if selected_bands else None,
            "configured_band_pool_source": pool_source,
            "configured_band_pool_status": "available" if selected_bands else "unavailable",
            "configured_band_pool_age_s": 0.0 if selected_bands else None,
            "configured_band_pool_last_sample_ts": ts if selected_bands else None,
            "configured_ca_pool_display": band_signature(selected_bands) if selected_bands else None,
            "supported_lte_bands": (
                list(self._supported_lte_bands)
                if self._supported_lte_bands
                else None
            ),
            "supported_lte_bands_source": (
                self._supported_lte_bands_source or None
            ),
            "supported_lte_bands_observed_at": (
                self._supported_lte_bands_observed_at or None
            ),
            "active_band_set": canonical_active_bands,
            "active_band_set_signature": band_signature(canonical_active_bands) if canonical_active_bands else "",
            "band_set": canonical_active_bands,
            "band_set_signature": band_signature(canonical_active_bands) if canonical_active_bands else "",
            "ca_signature": ca_signature or None,
            "pci": pci or None,
            "earfcn_dl": earfcn or None,
            "earfcn_ul": None,
            "enb_id": enb_id or None,
            "cell_id": cell_id or None,
            "tac": tac or None,
            "dl_freq_mhz": dl_freq_mhz or None,
            "ul_freq_mhz": ul_freq_mhz or None,
            "dl_bandwidth_mhz": dl_bandwidth_mhz or None,
            "tx_power": self._parse_metric_float(raw.get("tx_power")),
            "mcs_dl0": 0,
            "mcs_dl1": 0,
            "mcs_ul": 0,
            "mcs_ul_supported": False,
            "tbs_dl0": 0,
            "tbs_dl1": 0,
            "tbs_ul": 0,
            "qm_dl0": 0,
            "qm_dl1": 0,
            "qm_ul": 0,
            "qam_dl0": "",
            "qam_dl1": "",
            "qam_ul": "",
            "qam_source": "unknown",
            "qam_confidence": 0.0,
            "cqi": 0,
            "cqi0": 0,
            "cqi1": 0,
            "ri": 0,
            "session_uptime_s": session_uptime_s,
            "rtt_ms": None,
            "model": model or None,
            "router_model": model or None,
            "hardware_version": hardware_version or None,
            "software": software_version or None,
            "software_version": software_version or None,
            "webui": web_version or None,
            "firmware": firmware or None,
            "upgrade_result": upgrade_result or None,
            "config_version": config_version or None,
            "configuration_file_version": config_version or None,
            "serial": serial or None,
            "serial_source": (
                str(identity.get("serial_source") or "reported")
                if serial
                else None
            ),
            "lan_mac": lan_mac or None,
            "imsi": imsi or None,
            "wan_ip": wan_ip or None,
            "antenna": self._parse_antenna_mode_text(raw) or None,
            "imei": imei or None,
            "neighbors": [],
            "lte_cells": lte_cells,
            # The display contract describes observed component carriers, not
            # a configured pool or fabricated per-carrier RF measurements.
            "carrier_details": [
                {"band": cell["band"], "is_pcell": cell["is_pcell"],
                 "state": 0 if cell["is_pcell"] else 1}
                for cell in lte_cells
            ] or None,
            "cell_locked": self._configured_cell_lock(raw),
            "nr5g_active": nr5g_status["active"],
            "nr5g_mode": nr5g_status["mode"] or None,
            "nr5g_state_text": nr5g_status["state_text"] or None,
            "nr5g_cell_id": nr5g_status["cell_id"],
            "nr5g_pci": nr5g_status["pci"],
            "nr5g_arfcn": nr5g_status["arfcn"],
            "nr5g_band": nr5g_status["band"],
            "nr5g_band_set": nr5g_status["band_set"],
            "nr5g_rsrp_dbm": nr5g_status["rsrp_dbm"],
            "nr5g_rsrq_db": nr5g_status["rsrq_db"],
            "nr5g_sinr_db": nr5g_status["sinr_db"],
            "nr5g_rx0_rsrp_dbm": nr5g_status["rx0_rsrp_dbm"],
            "nr5g_rx1_rsrp_dbm": nr5g_status["rx1_rsrp_dbm"],
            "nr5g_nsa_bandwidth": nr5g_status["bandwidth_mhz"],
            "nr5g_cells": nr5g_status["cells"],
            "nr5g_sa_band_lock": nr5g_status["sa_band_lock"],
            "nr5g_nsa_band_lock": nr5g_status["nsa_band_lock"],
            "temperature_ambient_c": self._parse_metric_float(raw.get("pm_sensor_ambient")),
            "temperature_modem_4g_c": self._parse_metric_float(raw.get("pm_sensor_mdm")),
            "temperature_modem_5g_c": self._parse_metric_float(raw.get("pm_sensor_5g") or raw.get("pm_modem_5g")),
            "temperature_pa1_c": self._parse_metric_float(raw.get("pm_sensor_pa1")),
            "temperature_wifi_c": self._parse_metric_float(raw.get("wifi_chip_temp")),
            "wifi_enabled": self._parse_wifi_enabled(raw),
            "wifi_24ghz_enabled": self._parse_wifi_band_enabled(
                raw,
                band="2.4",
            ),
            "wifi_5ghz_enabled": self._parse_wifi_band_enabled(
                raw,
                band="5",
            ),
        }
        # Convert before ModemInput/receipt publication, under the same native
        # read. A malformed human correction cannot refresh the old raw field.
        for address, field in _GOFORM_PROFILE_INPUTS.items():
            if address not in rf or field in {"radio_connected", "network_type", "provider_plmn", "ca_active"}:
                continue
            value = rf[address]
            self._last_status[field] = value if value is not None else (
                None if field.startswith("wifi") or field == "wan_ip" else 0)
        # These aliases describe this same accepted rate, not a second native
        # measurement. Do not let typed counter defaults publish a false zero.
        for direction, rate, field in (
            ("rx", rx_bps, "traffic_current_download_rate_bps"),
            ("tx", tx_bps, "traffic_current_upload_rate_bps"),
        ):
            if direction in self._traffic_rates_proven:
                self._last_status[field] = rate
        if self._traffic_rates_proven == {"rx", "tx"}:
            self._last_status["traffic_rate_source"] = "+".join(
                sorted(set(self._traffic_rate_sources.values()))
            )
        self._inject_goform_lane_debug_status(self._last_status)
        empty_rf_diagnostic = self._build_empty_rf_diagnostic(
            raw=raw,
            radio_connected=radio_connected and expect_rf,
            canonical={
                "sinr_db": sinr,
                "rsrp_dbm": rsrp,
                "rsrq_db": rsrq,
                "rssi_dbm": rssi,
                "serving_band": serving_band,
                "active_bands": list(active_bands),
                "pci": pci,
                "earfcn": earfcn,
                "cell_id": cell_id,
                "network_type": network_type,
                "ppp_status": ppp_status,
            },
        )
        if empty_rf_diagnostic:
            self._last_status["backend_diagnostic"] = empty_rf_diagnostic
        from arch4.modem_live.profile_diagnostics import field_diagnostic
        diagnostic = field_diagnostic(self._definition, self._profile_field_issues,
            native=self._last_status.get('backend_diagnostic'), previous=previous_diagnostic)
        if diagnostic is not None:
            self._last_status['backend_diagnostic'] = diagnostic
        if self._target_guard_enabled():
            try:
                self._apply_target_guard(self._last_status)
            except TargetGuardError:
                return None

        # Synthetic QAM/CQI path for modems that do not report scheduler data.
        # We infer the coarse QAM bucket from SINR, then publish a
        # representative CQI band-center only for canonical consumers.
        qam_dl_primary = estimate_qm_from_sinr(sinr)
        qam_source = "inferred" if qam_dl_primary else "unknown"
        qam_confidence = 0.6 if qam_dl_primary else 0.0
        cqi_val = 0
        cqi_source = "unavailable"
        if qam_dl_primary:
            cqi_val, cqi_source = self._estimate_cqi_from_sinr(sinr)

        mi = ModemInput(
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            rsrq_db=rsrq,
            rssi_dbm=rssi,
            mcs_dl=0,
            mcs_ul=0,
            cqi=cqi_val,
            ri=0,
            carriers=carriers,
            ca_active=ca_active,
            ca_band_signature=ca_signature,
            primary_band=serving_band,
            pci=pci,
            earfcn=earfcn,
            enb_id=enb_id,
            cell_id=cell_id,
            radio_connected=radio_connected,
            session_uptime_s=session_uptime_s,
            rx_bytes_per_sec=rx_bps,
            tx_bytes_per_sec=tx_bps,
            traffic_month_download_bytes=self._last_status.get("traffic_month_download_bytes", self._parse_int(raw.get("monthly_rx_bytes"))),
            traffic_month_upload_bytes=self._last_status.get("traffic_month_upload_bytes", self._parse_int(raw.get("monthly_tx_bytes"))),
            traffic_accounting_source=(
                "modem_native"
                if raw.get("monthly_rx_bytes") not in (None, "")
                or raw.get("monthly_tx_bytes") not in (None, "")
                else ""
            ),
            selected_band_set=list(selected_bands),
            configured_band_pool=list(selected_bands),
            configured_band_pool_signature=band_signature(selected_bands),
            configured_band_pool_source=pool_source or "",
            configured_band_pool_status="available" if selected_bands else "",
            configured_band_pool_age_s=0.0 if selected_bands else None,
            configured_band_pool_last_sample_ts=ts if selected_bands else 0.0,
            neighbors=[],
            timestamp=ts,
            mod0=qam_dl_primary,
            qam_dl_primary=qam_dl_primary,
            qam_source=qam_source,
            qam_confidence=qam_confidence,
            nsa_active=bool(nr5g_status["active"] and nr5g_status["mode"] == "NSA"),
            sa_active=bool(nr5g_status["active"] and nr5g_status["mode"] == "SA"),
            nr_mode=str(nr5g_status["mode"] or ""),
            nr_state_text=str(nr5g_status["state_text"] or ""),
            nr_cells=list(nr5g_status["cells"] or []),
            nr_band=int(nr5g_status["band"] or 0),
            nr_pci=int(nr5g_status["pci"] or 0),
            nr_arfcn=int(nr5g_status["arfcn"] or 0),
            nr_cell_id=int(nr5g_status["cell_id"] or 0),
            nr_rsrp_dbm=float(nr5g_status["rsrp_dbm"] or 0.0),
            nr_sinr_db=float(nr5g_status["sinr_db"] or 0.0),
            nr_rsrq_db=float(nr5g_status["rsrq_db"] or 0.0),
            nr_bandwidth_mhz=float(
                nr5g_status["bandwidth_mhz"] or 0.0
            ),
            temperature_modem_5g_c=float(self._last_status["temperature_modem_5g_c"] or 0),
            wifi_enabled=self._last_status["wifi_enabled"],
            wifi_24ghz_enabled=self._last_status["wifi_24ghz_enabled"],
            wifi_5ghz_enabled=self._last_status["wifi_5ghz_enabled"],
            lan_mac=lan_mac,
            rsrq_source=rsrq_source,
            cqi_source=cqi_source,
        )
        return mi

    def _profile_native_replies(self, raw):
        if self._backend_name in {'g5tc', 'zte_zwrt'}:
            return dict(getattr(self, '_portable_reply_groups', {}))
        return {'goform':raw}

    def _decode_profile_rf(self, raw, *, traffic_observed_at=None):
        replies = self._profile_native_replies(raw)
        addresses = {'rf.sinr_db','rf.rsrp_dbm','rf.rsrq_db','rf.rssi_dbm',
                     'rf.pci','rf.cell_id','rf.earfcn_dl','rf.enb_id','slow.tac',
                     'radio.nr_pci','radio.nr_band','radio.nr_cell_id','radio.nr_arfcn',
                     'radio.nr_rsrp_dbm','radio.nr_rsrq_db','radio.nr_sinr_db',
                     'slow.nr_bandwidth_mhz','profile.nr5g_rx0_rsrp_dbm',
                     'profile.nr5g_rx1_rsrp_dbm'} | set(_GOFORM_PROFILE_INPUTS)
        # Session time depends on the existing clock owner, below the RF read.
        addresses.discard('traffic.session_uptime_s')
        # Supply current native source fields, never pre-decoded output that
        # could bypass an edited rule. Other RPC fields remain available.
        context = {"native_inputs":{key:raw.get(key) for key in
                   ("network_type","current_network_mode","ppp_status",
                    "modem_main_state","wan_connected")}}
        addresses &= self._definition.available_addresses(replies, context=context)
        values = self._decode_profile_values(replies, addresses, context=context)
        self._portable_rf_values = values
        return values

    def _decode_profile_values(self, replies, addresses, *, context=None):
        issues = []
        values = decode_native_profile(self._definition, replies, context, addresses=addresses, issues=issues)
        for address in addresses:
            self._profile_field_issues.pop(address, None)
        for issue in issues:
            self._profile_field_issues[issue.address] = issue
        return values

    @staticmethod
    def _goform_source_observed(raw: Dict[str, Any], field: str) -> bool:
        if field not in raw:
            return False
        value = raw[field]
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() not in {"", "null", "undefined", "n/a", "na", "--", "nan", "inf", "-inf", "-999"}
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            return bool(value)
        return True

    @staticmethod
    def _configured_cell_lock(raw: Dict[str, Any]) -> bool | None:
        """Decode the configured pair; omission/partial data is not unlock."""
        if any(key not in raw or raw[key] is None for key in ("lte_pci_lock", "lte_earfcn_lock")):
            return None
        pci, earfcn = (str(raw[key]).strip() for key in ("lte_pci_lock", "lte_earfcn_lock"))
        if pci == earfcn == "":
            return False
        if not pci.isdecimal() or not earfcn.isdecimal():
            return None
        return True if 0 <= int(pci) <= 503 and int(earfcn) > 0 else None

    def _canonical_fields_proven_by_raw(
        self,
        raw: Dict[str, Any],
        modem: Optional[ModemInput],
    ) -> frozenset[str]:
        """Return only canonical facts physically supported by this response."""

        proven: set[str] = set()
        if self._configured_cell_lock(raw) is not None:
            proven.add("cell_locked")
        if self._last_status.get("carrier_details") and any(
            self._goform_source_observed(raw, key) for key in
            ("lte_ca_pcell_band", "ZCELLINFO_band", "lte_band", "lte_ca_scell_band",
             "lte_multi_ca_scell_info", "lte_ca_scell_info")
        ):
            proven.add("carrier_details")
        for canonical_fields, vendor_fields in _GOFORM_CANONICAL_SOURCE_GROUPS:
            if any(
                self._goform_source_observed(raw, field)
                for field in vendor_fields
            ):
                proven.update(canonical_fields)
        # Some NG replies report active component bands rather than the
        # GoForm CA flag. Use the same positive evidence as the value parser.
        # A lone PCell or an omitted/blank flag cannot prove fresh inactivity;
        # configured pool size is never CA-activity evidence.
        if self._is_ca_active(raw, self._resolve_active_bands(raw)):
            proven.update({"ca_active", "ca_state_text"})
        # A connection flag cannot prove the RAT, and an absent flag is not
        # a fresh disconnect. Parser, diagnostic and receipt share this decoder.
        connection = self._raw_radio_connected(raw)
        if connection is not None:
            proven.update({"radio_connected", "radio_state_text", "session_uptime_s"})
        if self._goform_source_observed(raw, "ppp_uptime"):
            proven.add("session_uptime_s")
        # The exact profile supplies this fixed pool. Bind it to an observed
        # radio/identity/pool read; Wi-Fi-only maintenance cannot refresh it.
        if self._last_status.get("configured_band_pool_source") == "profile_provider_band_pool":
            if proven.intersection({"serving_band", "firmware", "configured_band_pool"}):
                proven.update(PROVIDER_BAND_POOL_FIELDS)
            else:
                proven.difference_update(PROVIDER_BAND_POOL_FIELDS)
        if all(self._goform_source_observed(raw, key) for key in ("rmcc", "rmnc")):
            proven.add("provider_plmn")
        if "provider_plmn" in proven and self._last_status.get("provider_name"):
            proven.add("provider_name")
        # Retained boot identity may validate the target, but cannot certify a
        # different fresh source (for example retained WebUI versus fresh FW).
        software_source = "web_version" if self._last_raw.get("web_version") else "wa_inner_version"
        if self._goform_source_observed(raw, software_source):
            proven.update({"software", "software_version"})
        firmware_source = "wa_inner_version" if self._last_raw.get("wa_inner_version") else "cr_version"
        if not self._goform_source_observed(raw, firmware_source):
            proven.discard("firmware")
        for direction, fields in (
            ("rx", {"rx_bytes_per_sec", "throughput_dl_mbps", "dl_mbps", "traffic_current_download_rate_bps"}),
            ("tx", {"tx_bytes_per_sec", "throughput_ul_mbps", "ul_mbps", "traffic_current_upload_rate_bps"}),
        ):
            if direction in self._traffic_rates_proven:
                proven.update(fields)
        if self._traffic_rates_proven == {"rx", "tx"}:
            proven.update({"traffic_valid", "has_traffic", "traffic_rate_source"})
        if {"traffic_month_download_bytes", "traffic_month_upload_bytes"}.issubset(proven):
            proven.update({"traffic_valid", "traffic_volume"})
        nr_members = {
            "band": {"nr_band", "nr5g_band"}, "pci": {"nr_pci", "nr5g_pci"},
            "cell_id": {"nr_cell_id", "nr5g_cell_id"}, "arfcn": {"nr_arfcn", "nr5g_arfcn"},
            "rsrp_dbm": {"nr_rsrp_dbm"}, "rsrq_db": {"nr_rsrq_db"},
            "sinr_db": {"nr_sinr_db"}, "nsa_bandwidth": {"nr_bandwidth_mhz"},
        }
        for name, members in nr_members.items():
            if self._last_status.get("nr5g_" + name) is None:
                proven.difference_update(members)
        if self._last_status.get("nr5g_cells"):
            proven.update({"nr_cells", "nr5g_cells"})
        if not self._last_status.get("nr5g_mode") and not any(
            key.startswith(("nr", "Z5g_", "5g_")) and self._goform_source_observed(raw, key)
            for key in raw
        ):
            proven.difference_update({"nsa_active", "sa_active", "nr5g_active", "nr_mode", "nr_state_text"})
        if (
            modem is not None
            and modem.rsrq_source == "estimated"
            and "rsrp_dbm" in proven
            and "rssi_dbm" in proven
            and "dl_bandwidth_mhz" in proven
        ):
            proven.add("rsrq_db")
        if (
            modem is not None
            and str(getattr(modem, "serial", "") or "").strip()
            and self._backend_name == "mf296r"
            and self._goform_source_observed(raw, "imei")
        ):
            proven.add("serial")
        # Editable RF selectors own both value and same-attempt freshness.
        fresh_identity = self._profile_identity_from_raw(raw)
        available = self._definition.available_addresses(self._profile_native_replies(raw))
        for field, value in fresh_identity.items():
            if 'identity.' + field not in available:
                continue
            proven.discard(field)
            if value not in (None, ''):
                proven.add(field)
        for address in ('rf.sinr_db','rf.rsrp_dbm','rf.rsrq_db','rf.rssi_dbm',
                        'rf.pci','rf.cell_id','rf.earfcn_dl','rf.enb_id','slow.tac'):
            if address not in available:
                continue
            field = address.split('.', 1)[1]
            proven.discard(field)
            if self._portable_rf_values.get(address) is not None:
                proven.add(field)
        # NR primary values and their aliases come from this same read. A
        # malformed edited scalar must not retain the old raw alias as fresh.
        for address in ('radio.nr_pci','radio.nr_band','radio.nr_cell_id','radio.nr_arfcn',
                        'radio.nr_rsrp_dbm','radio.nr_rsrq_db','radio.nr_sinr_db',
                        'slow.nr_bandwidth_mhz'):
            if address not in available:
                continue
            field = address.split('.',1)[1]
            alias = 'nr5g_' + field[3:] if field.startswith('nr_') else field
            proven.difference_update({field,alias})
            if field == 'nr_bandwidth_mhz':
                proven.discard('nr5g_nsa_bandwidth')
            final_name = 'nr5g_nsa_bandwidth' if field == 'nr_bandwidth_mhz' else alias
            if (self._portable_rf_values.get(address) is not None
                    and self._last_status.get(final_name) is not None):
                proven.add(field)
        if "identity.provider_name" in available:
            proven.discard("provider_name")
            if self._portable_rf_values.get("identity.provider_name") not in (None, "", "Unknown"):
                proven.add("provider_name")
        for address, field in _GOFORM_PROFILE_INPUTS.items():
            # Profile-plane sensor details remain backend diagnostics, not
            # new Matrix cells. Preserve the existing canonical contract.
            if address.startswith("profile.") or address not in available:
                continue
            proven.discard(field)
            if field == "radio_connected":
                proven.discard("radio_state_text")
            if field == "ca_active":proven.discard('ca_state_text')
            value = self._portable_rf_values.get(address)
            if value is not None and (field != "provider_plmn" or bool(str(value).strip())):
                proven.add(field)
                if field == "radio_connected":
                    proven.add("radio_state_text")
                if field == "ca_active":proven.add('ca_state_text')
        return frozenset(proven)

    def _read_snapshot_with_evidence(
        self,
        fields: List[str] | tuple[str, ...],
        *,
        lane: str,
    ) -> tuple[Optional[ModemInput], frozenset[str]]:
        if not self._connected:
            return None, frozenset()
        if self._backend_name not in {'g5tc','zte_zwrt'} and tuple(fields) == GOFORM_READ_FIELDS:
            fields = tuple(self._definition.request('goform',protocol='zte_goform')['startup_fields'])
        started = time.perf_counter()
        if lane == "fast_read":
            self._maintain_goform_session_if_due()
        request_timeout_s = (
            self._maintenance_read_timeout
            if lane in {"maintenance_read", "full_read"}
            else self._read_timeout
        )
        raw = self._read_goform_for_acquisition(
            fields,
            timeout_s=request_timeout_s,
        )
        traffic_observed_at = time.monotonic()
        elapsed_s = time.perf_counter() - started
        if raw and self._authenticated_session_explicitly_lost(raw, requested_fields=fields):
            self._record_goform_api_timing(
                lane,
                elapsed_s=time.perf_counter() - started,
                fields=fields,
                ok=False,
            )
            return None, frozenset()
        expect_rf = lane != "maintenance_read"
        authenticated_blank_rf = expect_rf and bool(raw) and self._is_authenticated_blank_rf_read(raw)
        self._record_goform_api_timing(
            lane,
            elapsed_s=elapsed_s,
            fields=fields,
            ok=bool(raw) and not authenticated_blank_rf,
        )
        if not raw:
            if self._last_status:
                self._inject_goform_lane_debug_status(self._last_status)
            return None, frozenset()
        modem = self._build_modem_input_from_raw(
            raw, traffic_observed_at=traffic_observed_at,
            expect_rf=expect_rf,
        )
        if lane in {"fast_read", "post_write_read"} and authenticated_blank_rf:
            self._last_status["backend_session"] = {
                "expired": False,
                "partial_response": True,
                "lane": "fast",
                "reason": "authenticated_blank_rf",
            }
            log.debug(
                "ZTE authenticated response omitted RF/cell fields; "
                "publishing present facts and retaining absent Matrix cells"
            )
        return modem, self._canonical_fields_proven_by_raw(raw, modem)

    def _read_snapshot_for_lane(
        self,
        fields: List[str] | tuple[str, ...],
        *,
        lane: str,
    ) -> Optional[ModemInput]:
        modem, _ = self._read_snapshot_with_evidence(
            fields,
            lane=lane,
        )
        return modem

    def _capture_goform_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
        canonical_fields: frozenset[str],
        failure_reason: str,
    ) -> BackendAcquisitionReceipt:
        acquired_cells = matrix_cells_for_canonical_fields(
            canonical_fields
        )
        if modem is not None and not acquired_cells:
            modem = None
            failure_reason = "zte_goform_no_usable_acquired_fields"
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason=failure_reason if modem is None else "",
        )

    def read_modem_input(self) -> Optional[ModemInput]:
        return self._read_snapshot_for_lane(
            GOFORM_READ_FIELDS,
            lane="full_read",
        )

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        return self._read_snapshot_for_lane(
            self._actionable_read_fields(),
            lane="fast_read",
        )

    def _actionable_read_fields(self) -> tuple[str, ...]:
        if self._backend_name in {'g5tc','zte_zwrt'}:
            # Compatibility diagnostic names only; ZWRT owns its actual ubus reads.
            return GOFORM_FAST_READ_FIELDS
        return tuple(self._definition.request('goform',protocol='zte_goform')['fast_fields'])

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "zte_goform_startup_full_acquisition_lane_required"
            )
        modem, canonical_fields = self._read_snapshot_with_evidence(
            GOFORM_READ_FIELDS,
            lane="full_read",
        )
        canonical_fields = self._capture_radio_mode_status(canonical_fields)
        if self._backend_name == "mf296r" and self._supported_lte_bands:
            canonical_fields = canonical_fields | frozenset(
                {
                    "supported_lte_bands",
                    "supported_lte_bands_source",
                    "supported_lte_bands_observed_at",
                }
            )
        return self._capture_goform_receipt(
            request=request,
            modem=modem,
            canonical_fields=canonical_fields,
            failure_reason="zte_goform_startup_acquisition_unavailable",
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "zte_goform_fast_live_acquisition_lane_required"
            )
        modem, canonical_fields = self._read_snapshot_with_evidence(
            self._actionable_read_fields(),
            lane="fast_read",
        )
        return self._capture_goform_receipt(
            request=request,
            modem=modem,
            canonical_fields=canonical_fields,
            failure_reason="zte_goform_actionable_acquisition_unavailable",
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "post_write":
            raise ValueError(
                "zte_goform_post_write_acquisition_lane_required"
            )
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError(
                "zte_goform_post_write_stage_evidence_required"
            )
        if not stage.permits_target_verification:
            raise ValueError(
                "zte_goform_post_write_accepted_stage_required"
            )
        if isinstance(stage.target, CoreNetworkSettingsTarget):
            return self._acquire_network_settings_readback(request)
        modem, canonical_fields = self._read_snapshot_with_evidence(
            GOFORM_READ_FIELDS,
            lane="post_write_read",
        )
        if isinstance(stage.target, CoreRadioModeTarget):
            canonical_fields = self._capture_radio_mode_status(canonical_fields)
        return self._capture_goform_receipt(
            request=request,
            modem=modem,
            canonical_fields=canonical_fields,
            failure_reason="zte_goform_post_write_acquisition_unavailable",
        )

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("zte_goform_typed_maintenance_required")

    def _iter_network_settings_observation(self):
        from arch4.modem.zte_goform_network_settings import iter_network_settings
        return iter_network_settings(self)

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        from arch4.modem.zte_goform_read_diagnostics import GoformReadDiagnosticAdapter
        if not self._connected or not self._authenticated:
            raise ReadDiagnosticUnavailable("Connect to the router before running read diagnostics.")
        return GoformReadDiagnosticAdapter(self)

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "slow_live":
            raise ValueError(
                "zte_goform_slow_live_acquisition_lane_required"
            )
        maintenance_fields = self._next_maintenance_chunk()
        modem, canonical_fields = self._read_snapshot_with_evidence(
            maintenance_fields,
            lane="maintenance_read",
        )
        canonical_fields = self._capture_radio_mode_status(canonical_fields)
        return self._capture_goform_receipt(
            request=request,
            modem=modem,
            canonical_fields=canonical_fields,
            failure_reason="zte_goform_maintenance_acquisition_unavailable",
        )

    def _capture_radio_mode_status(self, canonical_fields):
        from arch4.modem.zte_goform_radio_mode import contract_enabled, mode_status
        if not contract_enabled(self):
            return canonical_fields
        status = mode_status(self)
        self._last_status.update(status)
        return canonical_fields | ({"radio_mode"} if status["radio_mode"] is not None else set())

    def prepare_radio_mode_stages(self, target):
        from arch4.modem.zte_goform_radio_mode import prepare_mode_stages
        return prepare_mode_stages(self, target)

    def radio_mode_stage_ready(self, plan):
        from arch4.modem.zte_goform_radio_mode import stage_ready
        return stage_ready(self, plan)

    def _next_maintenance_chunk(self) -> tuple[str, ...]:
        chunks = self._goform_request_chunks(GOFORM_MAINTENANCE_READ_FIELDS)
        if not chunks:
            raise RuntimeError("zte_goform_maintenance_fields_required")
        index = self._maintenance_chunk_cursor % len(chunks)
        self._maintenance_chunk_cursor = (index + 1) % len(chunks)
        return tuple(chunks[index])

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        from arch4.modem.zte_goform_radio_mode import contract_enabled
        mode_writes = ("net_mode_write",) if contract_enabled(self) else ()
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=2.0,
            live_register_stale_s=6.0,
            profile_name="zte_goform_chunked_status",
            has_explicit_actionable_snapshot=True,
            has_explicit_maintenance_refresh=True,
            backend_io_parallel_safe=False,
            io_serial_required=True,
            startup_parallel_allowed=False,
            runtime_action_requires_io_lock=True,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=1.0,
            maintenance_holds_io_lock=False,
            generic_neighbor_scan_enabled=False,
            fast_read_groups=("radio_status", "rf_quality", "traffic_presence", "traffic_volume", "nr_status"),
            slow_read_groups=("ca_details", "wifi_status", "quota_stats", "network_mode"),
            static_capability_groups=(
                "device_identity",
                "firmware",
                "supported_bands",
                "antenna_capability",
                "qam_capability",
                "sms_capability",
                "wifi_capability",
            ),
            control_write_groups=("band_selection", "cell_lock", "nr_band_lock", "restart") + mode_writes,
            export_groups=("sidecars", "support_bundle", "history"),
            static_capability_refresh_policy="identity_or_operator_refresh_only",
        )

    def _resolve_firmware_profile(self):
        from arch4.modem.catalog import match_firmware_profile

        # Use the current canonical identity, not the previous sample's status.
        identity = self._derive_profile_identity(
            self._profile_identity_from_raw(self._last_raw, retain=True)
        )
        model = str(identity.get("model") or "").strip()
        firmware = str(identity.get("firmware") or "").strip()
        if not model and not firmware:
            return None
        try:
            return match_firmware_profile(
                backend=self._backend_name or "mf296r",
                model=model,
                firmware=firmware,
            )
        except Exception:
            return None

    def _read_research_static_status(self) -> Dict[str, Any]:
        raw = dict(self._last_raw or {})
        if not self._connected:
            return raw
        try:
            live = self._read_goform_for_acquisition(
                ("antenna_mode", "antenna_type", "antenna_status", "network_type", "current_network_mode"),
                multi_data=False,
            )
        except Exception:
            live = {}
        if isinstance(live, dict) and live:
            raw.update(live)
            self._last_raw.update(live)
        return raw

    @staticmethod
    def _parse_antenna_mode_text(raw: Dict[str, Any]) -> str:
        return parse_antenna_mode_text(raw)

    @classmethod
    def _is_external_antenna_enabled(cls, raw: Dict[str, Any]) -> bool:
        return cls._parse_antenna_mode_text(raw) == "External"

    @staticmethod
    def _is_5g_only_mode(raw: Dict[str, Any]) -> bool:
        return is_5g_only_mode(raw)

    def _apply_external_antenna_mode(self) -> bool:
        raise RuntimeError(
            "zte_goform_static_antenna_write_requires_typed_target"
        )

    def _apply_5g_only_mode(self) -> bool:
        raise RuntimeError(
            "zte_goform_static_5g_mode_write_requires_typed_target"
        )

    def _build_empty_rf_diagnostic(
        self,
        *,
        raw: Dict[str, Any],
        radio_connected: bool,
        canonical: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self._is_connected_but_empty_rf(radio_connected=radio_connected, canonical=canonical):
            self._last_empty_rf_diagnostic = {}
            return None
        now = time.time()
        if now - self._last_empty_rf_diag_ts < ZTE_EMPTY_RF_DIAGNOSTIC_THROTTLE_S:
            return dict(self._last_empty_rf_diagnostic or {}) or None
        self._last_empty_rf_diag_ts = now

        debug_values = self._build_empty_rf_debug_values(raw)
        self._empty_rf_diag_sequence += 1
        diagnostic_data = {
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "authenticated": bool(self._authenticated),
            "radio_connected": bool(radio_connected),
            "fields_requested": len(GOFORM_READ_FIELDS),
            "raw_key_count": len(raw),
            "fields_present": len(debug_values),
            "fields_nonempty": sum(1 for value in debug_values.values() if str(value or "").strip()),
            "sensitive_fields_present": [
                field for field in sorted(ZTE_SENSITIVE_DIAGNOSTIC_FIELDS) if field in raw
            ],
            "canonical": dict(canonical),
            "raw_subset": debug_values,
        }
        snapshot = ModemBackendDiagnostic(
            owner="54guc.modem.zte_goform",
            sequence=self._empty_rf_diag_sequence,
            observed_at=now,
            kind="zte_goform_empty_rf",
            backend=self._backend_name or "mf296r",
            severity="WARNING",
            data=diagnostic_data,
        ).to_payload()
        self._last_empty_rf_diagnostic = dict(snapshot)
        log.warning(
            "ZTE connected but RF/cell fields are empty: fields_present=%d fields_nonempty=%d",
            int(diagnostic_data["fields_present"]),
            int(diagnostic_data["fields_nonempty"]),
        )
        return dict(snapshot)

    def _read_goform_for_acquisition(
        self,
        fields: List[str] | tuple[str, ...],
        *,
        multi_data: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Perform one protected Goform acquisition without nested relogin.

        Connected-but-blank fields are partial physical evidence.  The caller
        retains the prior Matrix cells and the next normal Modem Live cycle
        tries again.  Relogin/retry work is deliberately not nested inside a
        completed fast sample.  The selected access profile continues to own
        the transport's existing ConnectionError retry policy.
        """
        return self._read_goform(
            fields,
            multi_data=multi_data,
            timeout_s=timeout_s,
        )

    def _authenticated_session_explicitly_lost(
        self,
        raw: Dict[str, Any],
        *,
        requested_fields: tuple[str, ...] | List[str] = (),
    ) -> bool:
        """Only explicit login evidence can end or renew an authenticated session.

        Public PPP, provider and RF fields are not authentication evidence.
        MF296R supplies loginfo in its normal compact fast read. Other Goform
        profiles check at their profile interval or after missing measurements.
        ZWRT's explicit ubus adapter owns its different authentication protocol.
        Re-login is still performed only by Modem Live's admitted repair path.
        """
        if not self._authenticated or not isinstance(raw, dict) or not raw:
            return False
        if self._access_profile.backend_family != "zte_goform_like":
            return False
        now = time.monotonic()
        status = raw
        if "loginfo" not in status:
            traffic_fields = {"realtime_rx_bytes", "realtime_tx_bytes", "wan_rx_bytes", "wan_tx_bytes",
                              "realtime_rx_thrpt", "realtime_tx_thrpt"}
            missing_traffic = bool(traffic_fields.intersection(requested_fields)) and not any(
                self._goform_source_observed(raw, field) for field in traffic_fields
            )
            due = now - self._session_checked_at >= self._access_profile.proactive_session_refresh_interval_s
            if not missing_traffic and not due:
                return False
            status = self._read_goform(("loginfo",))
        if "loginfo" not in status:
            log.warning("ZTE session check returned no login status; session validity is unknown")
            return False
        if str(status.get("loginfo") or "").strip().lower() == "ok":
            self._session_checked_at = now
            return False
        self._authenticated = False
        self._connected = False
        self._last_raw = {}
        self._last_status["backend_session"] = {
            "expired": True,
            "partial_response": False,
            "lane": "authentication",
            "reason": "session_ownership_lost",
            "ts": time.time(),
        }
        log.warning(
            "ZTE authenticated session ownership was lost; "
            "the next Modem Live acquisition will reconnect"
        )
        return True

    def _maintain_goform_session_if_due(self) -> None:
        """MF296R WebUI keepalive, inside the existing acquired I/O ownership.

        One due attempt, no catch-up loop and no login. The following fast read
        checks loginfo from the same session. A failed GET is not itself proof
        of Internet loss or a reason to create another reconnect owner.
        """
        profile = self._access_profile
        if (not self._authenticated or not profile.proactive_session_refresh_enabled
                or profile.session_refresh_method != "zte_mf296r_ld_sha256_session"):
            return
        now = time.monotonic()
        interval = max(profile.proactive_session_refresh_interval_s, profile.proactive_session_refresh_min_gap_s)
        if now - self._session_maintenance_attempt_at < interval:
            return
        self._session_maintenance_attempt_at = now
        result: Dict[str, Any] = {"attempted_at": time.time(), "ok": False,
                                  "method": "mf296r_webui_template", "interval_s": interval}
        try:
            response = self._zte_http_get(
                self._base_url + "/tmpl/network/apn_setting.html",
                operation="session_keepalive", lane="session",
                headers=self._goform_headers(), params={"v": int(time.time())},
                timeout=self._maintenance_read_timeout, allow_redirects=False,
            )
            result.update(http_status=response.status_code, ok=response.status_code == 200)
        except requests.RequestException as exc:
            result["failure"] = type(exc).__name__
        self._session_maintenance_status = result
        if not result["ok"]:
            log.warning("ZTE session keepalive failed; explicit login readback remains authoritative")

    def _is_authenticated_blank_rf_read(self, raw: Dict[str, Any]) -> bool:
        if not self._authenticated or not isinstance(raw, dict) or not raw:
            return False
        if not self._raw_radio_connected(raw):
            return False
        return not self._has_rf_or_cell_payload(raw)

    def _raw_radio_connected(self, raw: Dict[str, Any]) -> Optional[bool]:
        """Decode this reply only; None means no usable connection evidence."""
        for key in ("ppp_status", "modem_main_state", "wan_connected"):
            if not self._goform_source_observed(raw, key):
                continue
            state = str(raw[key]).strip().lower()
            if state in CONNECTED_PPP_STATES or (key == "wan_connected" and state in {"1", "true"}):
                return True
            if state in NEGATIVE_STATES or (key == "wan_connected" and state in {"0", "false"}):
                return False
        for key in ("network_type", "current_network_mode"):
            if not self._goform_source_observed(raw, key):
                continue
            if str(raw[key]).strip().lower() in NEGATIVE_STATES or is_limited_service(raw[key]):
                return False
            if self._normalize_network_type(raw[key]):
                return True
        return None

    @classmethod
    def _has_rf_or_cell_payload(cls, raw: Dict[str, Any]) -> bool:
        for field in ZTE_RF_OR_CELL_PAYLOAD_FIELDS:
            text = str(raw.get(field) or "").strip()
            if text and text.lower() not in {"null", "undefined", "n/a", "na", "--"}:
                return True
        return False

    @classmethod
    def _is_connected_but_empty_rf(cls, *, radio_connected: bool, canonical: Dict[str, Any]) -> bool:
        if not radio_connected:
            return False
        rf_values = (
            canonical.get("sinr_db"),
            canonical.get("rsrp_dbm"),
            canonical.get("rsrq_db"),
            canonical.get("rssi_dbm"),
        )
        cell_values = (
            canonical.get("serving_band"),
            canonical.get("pci"),
            canonical.get("earfcn"),
            canonical.get("cell_id"),
        )
        active_bands = canonical.get("active_bands") or []
        return (
            all(abs(cls._parse_float(value)) < 0.0001 for value in rf_values)
            and all(cls._parse_int(value) == 0 for value in cell_values)
            and not [band for band in active_bands if cls._parse_int(band) > 0]
        )

    @classmethod
    def _build_empty_rf_debug_values(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field in ZTE_EMPTY_RF_DIAGNOSTIC_FIELDS:
            if field not in raw:
                continue
            out[field] = cls._diagnostic_value(field, raw.get(field))
        return out

    @staticmethod
    def _diagnostic_value(field: str, value: Any) -> Any:
        if field in ZTE_SENSITIVE_DIAGNOSTIC_FIELDS:
            return "<redacted>"
        if value is None or isinstance(value, (int, float, bool)):
            return value
        text = str(value)
        if len(text) > 240:
            return text[:240] + "...<truncated>"
        return text

    def read_neighbors(self) -> List[NeighborCell]:
        return []

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        return measure_host_ping_rtt(
            target=target, count=count, ping_interface=self._ping_iface,
            timeout_s=timeout_s,
        )

    def _refresh_after_lock_write(self, ok: bool) -> None:
        if not ok:
            return
        was_connected = bool(getattr(self, "_connected", False))
        try:
            self.read_modem_input()
        except Exception as exc:
            log.debug("ZTE lock confirmation refresh suppressed: %s", exc)
        finally:
            if was_connected and not bool(getattr(self, "_connected", False)):
                self._connected = True

    def _zte_band_lock_confirmation(
        self,
        *,
        lock_type: str,
        requested: Dict[str, Any],
        accepted: bool,
        requested_bands: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        status = dict(getattr(self, "_last_status", {}) or {})
        raw = dict(getattr(self, "_last_raw", {}) or {})
        selected_bands = normalized_bands(status.get("selected_band_set") or status.get("band_set") or [])
        effective = {}
        if accepted and status:
            effective = {
                "band": status.get("serving_band"),
                "pci": status.get("pci"),
                "earfcn": status.get("earfcn_dl"),
                "cell_id": status.get("cell_id"),
                "network_type": status.get("network_type"),
                "ca_active": status.get("ca_active"),
            }
        readback = {
            "available": bool(raw or status),
            "selected_band_set": selected_bands,
            "lte_band_lock": raw.get("lte_band_lock"),
            "lte_pci_lock": raw.get("lte_pci_lock"),
            "lte_earfcn_lock": raw.get("lte_earfcn_lock"),
            "nr5g_sa_band_lock": raw.get("nr5g_sa_band_lock"),
            "nr5g_nsa_band_lock": raw.get("nr5g_nsa_band_lock"),
            "source": "goform_status",
        }
        requested_bands = normalized_bands(requested_bands or [])
        observed_bands = normalized_bands(status.get("active_band_set") or [])
        if not observed_bands and effective.get("band") not in (None, ""):
            observed_bands = normalized_bands([effective.get("band")])
        return build_lock_confirmation(
            lock_type=lock_type,
            requested=requested,
            accepted=bool(accepted),
            readback=readback,
            effective=effective,
            effective_cell_match_value=effective_cell_match(effective, requested),
            effective_band_match_value=effective_band_match(observed_bands, requested_bands),
            configured_band_match_value=configured_band_match(selected_bands, requested_bands),
            source="zte_goform",
        )

    def _lte_band_write_payload(self, mask: int) -> Dict[str, Any]:
        """Translate one canonical LTE mask into the exact backend command."""
        if self._backend_name == "mf296r":
            return {
                "isTest": "false",
                "goformId": "SET_LTE_BAND_LOCK",
                "lte_band_lock": f"0x{int(mask):011x}",
            }
        return {
            "isTest": "false",
            "goformId": "BAND_SELECT",
            "is_gw_band": "0",
            "gw_band_mask": "0",
            "is_lte_band": "1",
            "lte_band_mask": f"0x{int(mask):X}",
        }

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("zte_goform_physical_stage_plan_required")
        target = plan.target
        if isinstance(target, CoreWifiRadioTarget):
            raise NotImplementedError("zte_goform_per_band_wifi_control_not_qualified")
        if self._backend_name == "g5b" and (
            not isinstance(target, (CoreBandSetTarget, CoreNrBandTarget, CoreRadioModeTarget))
            or plan.stage_kind not in {"ca_pool", "band_selection", "band_lock", "nr_band_lock",
                                       "radio_disconnect", "radio_mode", "radio_reconnect"}
        ):
            raise NotImplementedError("g5b_physical_control_not_qualified")
        if not self._connected:
            raise RuntimeError("zte_goform_not_connected")
        if (
            isinstance(target, (CoreBandSetTarget, CorePBandTarget, CoreNrBandTarget))
            and getattr(self, "_firmware_band_lock_risk", False)
        ):
            raise RuntimeError("zte_goform_firmware_band_lock_risk")
        payload: Dict[str, Any] | None = None
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem.zte_goform_radio_mode import mode_payload
            payload = mode_payload(self, plan)
        elif isinstance(target, CoreNetworkSettingsTarget):
            from arch4.modem.zte_goform_network_settings import network_payload
            payload = network_payload(self, plan)
        elif isinstance(target, CoreBandSetTarget):
            mask = 0
            for band in target.bands:
                bit = ZTE_BAND_MASKS.get(int(band))
                if bit is None:
                    break
                mask |= int(bit)
            else:
                payload = self._lte_band_write_payload(mask)
        elif isinstance(target, CorePBandTarget):
            if target.pband == 0:
                raise RuntimeError("zte_auto_restore_target_unqualified")
            bit = ZTE_BAND_MASKS.get(int(target.pband))
            if bit:
                payload = self._lte_band_write_payload(int(bit))
        elif isinstance(target, CoreBandAutoTarget):
            raise RuntimeError("zte_auto_restore_target_unqualified")
        elif isinstance(target, CoreCellLockTarget):
            payload = {
                "isTest": "false",
                "goformId": "LTE_LOCK_CELL_SET",
                "lte_pci_lock": "" if target.unlock else str(target.pci),
                "lte_earfcn_lock": "" if target.unlock else str(target.earfcn),
            }
        elif isinstance(target, CoreNrBandTarget):
            payload = {
                "isTest": "false",
                "goformId": "BAND_SELECT",
                "nr5g_nsa_band_lock": ",".join(str(b) for b in target.nr_bands),
            }
        elif isinstance(target, CoreRestartTarget):
            payload = {"isTest": "false", "goformId": "REBOOT_DEVICE"}
        if payload is None:
            raise RuntimeError("zte_goform_typed_target_unsupported")
        body = {str(key): value for key, value in payload.items()}
        definition=getattr(self,'_definition',None)
        if definition is not None:
            parameters=definition.prepare_native_write_fields('goform:'+str(body['goformId']),
                {key:value for key,value in body.items()if key not in {'goformId','isTest'}})
            body={**{key:value for key,value in body.items()if key in {'goformId','isTest'}},**parameters}
        if isinstance(target, CoreRadioModeTarget) and body != payload:
            raise RuntimeError("zte_goform_radio_mode_native_template_changed_target")
        if self._target_guard_enabled():
            self._apply_target_guard(
                self._derive_profile_identity(
                    self.read_fresh_device_identity()
                )
            )
        accessible_id = self._build_accessible_id()
        if isinstance(target, CoreRadioModeTarget) and not accessible_id:
            raise RuntimeError("zte_goform_radio_mode_fresh_access_id_required")
        if accessible_id:
            body["AD"] = accessible_id
        started = time.time()
        accepted = False
        reason = "zte_goform_write_rejected"
        outcome = "rejected"
        detail: Dict[str, Any] = {}
        try:
            response = self._zte_http_post(
                f"{self._base_url}/goform/goform_set_cmd_process",
                operation=str(body.get("goformId") or "physical_stage"),
                data=body,
                headers=self._goform_headers(),
                timeout=self._read_timeout,
            )
            returned = time.time()
            detail["http_status"] = response.status_code
            if response.status_code == 200:
                result = self._parse_goform_payload(response.text)
                accepted = self._write_ok(result)
                if isinstance(target, CoreRadioModeTarget):
                    # The stock mode/PPP pages require their native success
                    # enum. Login's numeric success states are not mode ACKs.
                    accepted = bool(result) and all(result[key] == "success"
                        for key in ("result", "Result") if key in result) and any(
                        key in result for key in ("result", "Result"))
                # Keep only a bounded status, never the reply body or AD token.
                code = result.get("result", result.get("Result"))
                text_code = str(code).strip().lower() if code is not None else ""
                detail["response_code"] = (
                    text_code if text_code in ZTE_WRITE_OK_RESULTS
                    or (text_code.isascii() and text_code.isdecimal()
                        and len(text_code) <= 4)
                    else "missing" if code is None else "unrecognized"
                )
                if accepted:
                    reason = "accepted"
                    outcome = "accepted"
                elif (
                    self._backend_name == "mf296r"
                    and body.get("goformId") == "SET_LTE_BAND_LOCK"
                ):
                    reason = "zte_mf296r_developer_authorization_required"
                else:
                    reason = "zte_goform_write_rejected"
            else:
                reason = f"zte_goform_http_{response.status_code}"
        except requests.RequestException as exc:
            returned = time.time()
            reason = f"zte_goform_transport_error:{type(exc).__name__}"
            outcome = "response_unavailable"
        return ModemLivePhysicalStageResult(
            plan=plan,
            request_dispatched_at=started,
            transport_returned_at=returned,
            outcome=outcome,
            reason=reason,
            radio_drop_detected=False,
            detail=detail,
        )

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("zte_goform_generic_execute_lever_forbidden")

    def verify_static_config(self) -> Dict[str, bool]:
        result = dict(ZTE_GOFORM_ROUTER_MANAGED_STATIC_CONFIG)
        profile = self._resolve_firmware_profile()
        if not profile:
            return result
        raw = self._read_research_static_status()
        if profile.profile_id in ZTE_EXTERNAL_ANTENNA_PROFILE_IDS:
            result["antenna_external"] = self._static_config_overrides.get(
                "antenna_external",
                self._is_external_antenna_enabled(raw),
            )
        if profile.profile_id in ZTE_5G_ONLY_PROFILE_IDS:
            result["nr5g_mode_5g_only"] = self._static_config_overrides.get(
                "nr5g_mode_5g_only",
                self._is_5g_only_mode(raw),
            )
        return result

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError("zte_goform_direct_static_config_write_forbidden")

    def verify_ue_capability(self) -> Dict[str, Any]:
        return {}

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError("zte_goform_direct_ue_capability_write_forbidden")

    def restart_modem(self) -> bool:
        raise RuntimeError("zte_goform_direct_restart_write_forbidden")

    def read_fresh_device_identity(self) -> Dict[str, str]:
        raw = self._read_goform(tuple(sorted(GOFORM_RETAINED_IDENTITY_FIELDS)))
        fresh = self._profile_identity_from_raw(raw)
        # Reuse this response. Do not request boot identity again on the next
        # live sample, or keep the previous software version in that sample.
        self._last_raw.update({k: v for k, v in raw.items() if v not in (None, "")})
        self._last_status.update({k: v for k, v in fresh.items() if v not in (None, "")})
        return {k: str(v) for k, v in fresh.items() if v not in (None, "")}

    def get_modem_info(self) -> Dict[str, str]:
        if not self._last_status:
            self.read_modem_input()
        status = self._last_status
        return {
            "model": str(status.get("model") or ""),
            "router_model": str(status.get("router_model") or ""),
            "hardware_version": str(status.get("hardware_version") or ""),
            "software_version": str(status.get("software_version") or ""),
            "firmware": str(status.get("firmware") or ""),
            "imei": str(status.get("imei") or ""),
            "imsi": str(status.get("imsi") or ""),
            "serial": str(status.get("serial") or ""),
        }

    def get_current_bands(self) -> List[int]:
        if not self._last_status:
            self.read_modem_input()
        return [int(b) for b in (self._last_status.get("band_set") or []) if int(b or 0) > 0]

    def get_capabilities(self):
        return get_capabilities(self._backend_name or "mf296r")

    def get_last_status(self) -> Dict[str, Any]:
        status = build_public_modem_status(self._last_status)
        status["firmware_band_lock_risk"] = self._firmware_band_lock_risk
        return status

    def read_plmn(self) -> Optional[str]:
        """Return cached PLMN from last modem read.

        ZTE goform gets PLMN from the same goform endpoint as all other
        fields, so it is only available after the first read_modem_input().
        The engine calls read_plmn() at init, but for ZTE this may return
        None until the first sample populates _last_status.
        """
        plmn = str(self._last_status.get("provider_plmn") or "").strip()
        return plmn if plmn else None

    @staticmethod
    def _check_firmware_band_lock_risk(inner_version: str) -> bool:
        """Detect firmware versions known to break band lock API.

        ZTE firmware B19+ removes the goform BAND_SELECT endpoint for band
        locking. External field-validation notes also indicate that later
        firmware updates may break locking across older devices such as
        MC7010, MC888, and MC889.

        The wa_inner_version string typically looks like:
          BD_ATMC889V1.0.0B19       (Austrian MC889 B19 — broken)
          BD_ATMC888AULTRAV1.0.0B17 (Austrian MC888A Ultra B17 — OK)
          BD_H3GUKMC888V1.0.0B09    (Three UK MC888 B09 — OK)

        We extract the build number after the last 'B' and flag risk
        if >= 19 for any known older ZTE model.
        """
        ver = str(inner_version or "").strip().upper()
        if not ver:
            return False
        # Any older ZTE 5G CPE model in the version string
        is_older_zte = any(m in ver for m in ("MC889", "MC888", "MC7010"))
        if not is_older_zte:
            return False
        # Extract build number: last B followed by digits
        match = re.search(r"B(\d{2,})\b", ver)
        if not match:
            return False
        build_num = int(match.group(1))
        return build_num >= 19

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        return []

    def read_serving_cell(self) -> Dict[str, Any]:
        if not self._last_status:
            self.read_modem_input()
        return {
            "band": self._last_status.get("serving_band"),
            "pci": self._last_status.get("pci"),
            "earfcn": self._last_status.get("earfcn_dl"),
            "cell_id": self._last_status.get("cell_id"),
            "network_type": self._last_status.get("network_type"),
            "ca_active": self._last_status.get("ca_active"),
        }

    def read_band_priority(self) -> List[int]:
        return []

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError(
            "zte_goform_direct_band_priority_write_forbidden"
        )

    def set_auto_bands(self) -> bool:
        raise RuntimeError(
            "zte_goform_direct_band_auto_write_forbidden"
        )

    def set_nr_band_lock(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError(
            "zte_goform_direct_nr_band_write_forbidden"
        )

    # Maximum fields per single goform request. ZTE GoAhead servers can
    # become unresponsive when too many fields are requested at once.
    # Community reports recommend staying under ~40 fields per call.
    _GOFORM_MAX_FIELDS_PER_REQUEST = 45

    def _profile_goform_fields(self, fields):
        """Add edited selectors to the same native lane as their field.

        Existing optional firmware aliases keep their explicit native request
        plan. A new selector must not need a second edit to the query list.
        """
        sources = getattr(self, '_goform_profile_sources', {})
        if not sources:return fields
        native = self._goform_native_sources
        requested = set(fields)
        startup = self._definition.request('goform',protocol='zte_goform')['startup_fields']
        full = requested == set(startup) or requested == set(GOFORM_READ_FIELDS)
        selected = set(sources) if full else {
            address for address,keys in native.items() if requested.intersection(keys)}
        known = set(GOFORM_READ_FIELDS).union(*native.values())
        # These are shared native context inputs, not a reason to expand the
        # existing fast poll for every dependent RF/provider formula.
        context = {'network_type','current_network_mode','mccmnc','rmcc','rmnc','bandwidth'}
        added = set()
        for address in selected:
            keys = sources.get(address,frozenset())
            added.update(keys if len(keys)==1 else keys - native.get(address,known) - context)
        result = (*fields,*sorted(added-requested))
        if len(result)>512:raise ValueError('zte_goform_profile_field_budget_exceeded')
        return result

    def _goform_request_chunks(self, fields: List[str] | tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
        """Keep the observed band/PCI/cell/channel in one native request."""
        clean = tuple(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
        clean = tuple(self._profile_goform_fields(clean))
        if not clean:
            return ()
        limit = self._goform_max_fields_per_request(len(clean))
        if len(clean) <= limit:
            return (clean,)
        lte_fields: set[str] = set()
        nr_fields: set[str] = set()
        for canonical, native in _GOFORM_CANONICAL_SOURCE_GROUPS:
            if canonical.intersection({"nr_band", "nr_pci", "nr_cell_id", "nr_arfcn"}):
                nr_fields.update(native)
            elif canonical.intersection({"serving_band", "pci", "earfcn", "earfcn_dl",
                                         "cell_id"}):
                lte_fields.update(native)
        nr_fields.update(field for field in clean if field.startswith("network_Z5g_"))
        profile_keys = getattr(self, '_goform_profile_radio_keys', ((), ()))
        lte_fields.update(profile_keys[0])
        nr_fields.update(profile_keys[1])
        optional_cohorts: list[set[str]] = []
        metadata = self._definition.metadata
        if (metadata.get('family') == 'zte_webui_like'
                and metadata.get('model_contains') == ['MC801A']):
            # Competing optional aliases and session timers must describe one
            # response. Keep the existing planner and its size/order limits.
            sources = self._goform_profile_sources
            rsrq_pair = {'Z5g_rsrq', 'Z5g_RSRQ'}
            if rsrq_pair <= sources.get('radio.nr_rsrq_db', frozenset()):
                nr_fields.update(rsrq_pair)
            timer_pair = {'ppp_uptime', 'realtime_time'}
            if timer_pair <= sources.get('traffic.session_uptime_s', frozenset()):
                optional_cohorts.append(timer_pair)
        # A human selector can serve both RATs. It must be read once, together
        # with both identities, or rejected before sending an oversized packet.
        if lte_fields.intersection(nr_fields).intersection(clean):
            lte_fields.update(nr_fields)
            nr_fields.clear()
        groups = (lte_fields, nr_fields, *optional_cohorts)
        cohorts = [tuple(field for field in clean if field in group)
                   for group in groups]
        if any(len(cohort) > limit for cohort in cohorts):
            raise RuntimeError("zte_goform_radio_cohort_exceeds_request_limit")
        grouped = set().union(*groups)
        cohorts.extend((field,) for field in clean if field not in grouped)
        chunks: list[tuple[str, ...]] = []
        pending: tuple[str, ...] = ()
        for cohort in cohorts:
            if len(pending) + len(cohort) > limit:
                chunks.append(pending)
                pending = ()
            pending += cohort
        if pending:
            chunks.append(pending)
        return tuple(chunks)

    def _read_goform(
        self,
        fields: List[str] | tuple[str, ...],
        *,
        multi_data: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        chunks = self._goform_request_chunks(fields)
        # A bounded packet contains its whole requested radio cohort; merging
        # independent traffic/static packets cannot split a serving identity.
        merged: Dict[str, Any] = {}
        for chunk in chunks:
            kwargs: Dict[str, Any] = {"multi_data": multi_data}
            if timeout_s is not None:
                kwargs["timeout_s"] = timeout_s
            result = self._read_goform_single(list(chunk), **kwargs)
            if result:
                merged.update(result)
        return merged

    def _read_goform_single(
        self,
        fields: List[str],
        *,
        multi_data: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        request = self._definition.request('goform',protocol='zte_goform')
        params = dict(request['parameters'])
        params[request['field_parameter']] = ','.join(fields)
        if self._backend_name == "mf296r":
            params["_"] = str(int(time.time() * 1000))
        if multi_data:
            params["multi_data"] = "1"
        else:
            params.pop('multi_data', None)
        url = self._base_url + request['path']
        headers = self._goform_headers()
        request_timeout_s = max(
            0.05,
            float(self._read_timeout if timeout_s is None else timeout_s),
        )
        for attempt in range(self._read_retry_limit + 1):
            try:
                resp = self._zte_http_get(
                    url,
                    operation="goform_read",
                    params=params,
                    headers=headers,
                    timeout=request_timeout_s,
                )
            except (ConnectionError, requests.ConnectionError) as exc:
                if attempt < self._read_retry_limit:
                    log.debug("ZTE goform read connection dropped, retrying: %s", exc)
                    continue
                log.warning("ZTE goform read failed after retry: %s", exc)
                return {}
            except requests.RequestException as exc:
                log.warning("ZTE goform read failed: %s", exc)
                return {}
            if resp.status_code != 200:
                log.warning("ZTE goform read returned %d", resp.status_code)
                return {}
            return self._parse_goform_payload(resp.text)
        return {}

    def _post_goform(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = {str(k): v for k, v in dict(payload or {}).items()}
        goform_id = str(body.get("goformId") or "").strip()
        if self._backend_name in {"mc888", "g5b"} and goform_id == "LOGIN_MULTI_USER":
            raise RuntimeError(f"zte_{self._backend_name}_multi_user_login_forbidden")
        if goform_id in {
            "REBOOT_DEVICE", "BAND_SELECT", "SET_LTE_BAND_LOCK", "LTE_LOCK_CELL_SET",
            "SET_NETWORK", "SET_CONNECTION_MODE", "APN_PROC", "APN_PROC_EX",
            "SET_BEARER_PREFERENCE", "DISCONNECT_NETWORK", "CONNECT_NETWORK",
        }:
            raise RuntimeError("zte_goform_typed_physical_stage_required")
        if self._target_guard_enabled() and goform_id not in {"LOGIN", "LOGIN_MULTI_USER", "LOGOFF", "SET_WEB_LANGUAGE"}:
            self._apply_target_guard(
                self._derive_profile_identity(
                    self.read_fresh_device_identity()
                )
            )
        if goform_id and goform_id not in {"LOGIN", "SET_WEB_LANGUAGE"}:
            ad = self._build_accessible_id()
            if ad:
                body["AD"] = ad
        try:
            resp = self._zte_http_post(
                f"{self._base_url}/goform/goform_set_cmd_process",
                operation=goform_id or "goform_write",
                lane=(
                    "session"
                    if goform_id in {"LOGIN", "LOGIN_MULTI_USER", "LOGOFF"}
                    else "event_write"
                ),
                data=body,
                headers=self._goform_headers(),
                timeout=self._read_timeout,
            )
        except requests.RequestException as exc:
            log.warning("ZTE goform write failed: %s", exc)
            return {}
        if resp.status_code != 200:
            log.warning("ZTE goform write returned %d", resp.status_code)
            return {}
        return self._parse_goform_payload(resp.text)

    @staticmethod
    def _zte_env_str(name: str, default: str = "") -> str:
        value = os.getenv(name)
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    @classmethod
    def _goform_max_fields_per_request(cls, total_fields: int | None = None) -> int:
        raw = cls._zte_env_str("ARCH_ZTE_MAX_FIELDS_PER_REQUEST", "")
        if not raw:
            return cls._GOFORM_MAX_FIELDS_PER_REQUEST
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return cls._GOFORM_MAX_FIELDS_PER_REQUEST
        if value <= 0:
            return max(int(total_fields or 0), cls._GOFORM_MAX_FIELDS_PER_REQUEST)
        return max(1, value)

    @classmethod
    def _new_session(
        cls,
        *,
        connection_mode: str = "close",
    ) -> requests.Session:
        """Create the exact Modem Live profile's HTTP connection contract."""

        mode = str(connection_mode or "").strip().lower()
        if mode == "persistent":
            session = requests.Session()
            session.trust_env = False
            return session
        if mode != "close":
            raise ValueError(f"zte_http_connection_mode_invalid:{mode or '<empty>'}")
        session = requests.Session()
        session.trust_env = False
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers["Connection"] = "close"
        return session

    def _goform_headers(self) -> Dict[str, str]:
        referer_mode = self._zte_env_str("ARCH_ZTE_REFERER_MODE", "index").lower()
        if referer_mode == "root":
            return {"Referer": f"{self._base_url}/"}
        return {"Referer": f"{self._base_url}/index.html"}

    @staticmethod
    def _required_login_field(reply: Dict[str, Any], key: str) -> str:
        # A stopped/restarting router can return nothing. That does not mean
        # it rejected the password. Modem Live owns the next recovery attempt.
        value = reply.get(key) if isinstance(reply, dict) else None
        if type(value) not in (str, int):
            raise ConnectionError(f"zte_login_{key}_unavailable")
        text = str(value).strip()
        if key == "result" and not text:
            raise ConnectionError("zte_login_result_unavailable")
        return text

    def _try_login(self) -> bool:
        if not self._password:
            return False
        login_status = self._read_goform(("loginfo",))
        if self._required_login_field(login_status, "loginfo").lower() == "ok":
            return True
        if self._backend_name == "mf296r":
            ld = self._required_login_field(self._read_goform(("LD",)), "LD")
            result = self._post_goform(
                {
                    "isTest": "false",
                    "goformId": "LOGIN",
                    "password": self._sha256_hex(
                        self._sha256_hex(self._password) + ld
                    ),
                }
            )
            code = self._required_login_field(result, "result")
            if code not in {"0", "4"}:
                log.warning("ZTE MF296R login rejected with result=%s", code)
                return False
            status_after = self._read_goform(("loginfo",))
            if self._required_login_field(status_after, "loginfo").lower() == "ok":
                return True
            raise ConnectionError("zte_login_accepted_but_session_not_confirmed")
        tokens = self._read_goform(ZTE_LOGIN_TOKEN_FIELDS)
        ld = self._required_login_field(tokens, "LD")
        rd = str(tokens.get("RD") or "").strip()
        rd0 = str(tokens.get("wa_inner_version") or "").strip()
        rd1 = str(tokens.get("cr_version") or "").strip()
        if self._backend_name == "g5b" and (not rd or rd0 != self._expected_firmware):
            raise ConnectionError("g5b_selected_login_challenge_unavailable_or_wrong_firmware")
        if self._backend_name == "mc888" and not (rd and (rd0 or rd1)):
            raise ConnectionError("zte_mc888_login_ad_challenge_unavailable")
        # Cache version tokens for _build_accessible_id
        if rd0 or rd1:
            self._cached_wa_inner_version = rd0
            self._cached_cr_version = rd1
        payload = {
            "isTest": "false",
            "goformId": "LOGIN",
            "password": self._sha256_hex(self._sha256_hex(self._password) + ld),
        }
        if rd and (rd0 or rd1):
            payload["AD"] = self._build_accessible_id_from_tokens(rd0, rd1, rd, algorithm=self._access_id_algorithm)
        result = self._post_goform(payload)
        code = self._required_login_field(result, "result")
        if self._backend_name == "mc888":
            if code == "3":
                return False
            if code != "0":
                raise ConnectionError(f"zte_mc888_login_result:{code}")
            status_after = self._read_goform(("loginfo",))
            if self._required_login_field(status_after, "loginfo").lower() == "ok":
                return True
            raise ConnectionError("zte_login_accepted_but_session_not_confirmed")
        # Other ZTE profile families retain their reviewed result-code path.
        # Same-session post-login `loginfo` remains authoritative.
        status_after = self._read_goform(("loginfo",))
        if self._required_login_field(status_after, "loginfo").lower() == "ok":
            return True
        if self._backend_name == "g5b":
            log.warning("ZTE G5B login result=%s but login status did not confirm", code)
            return False
        multi_user_ok = self._try_login_multi_user(tokens)
        if multi_user_ok:
            return True
        log.warning("ZTE login result=%s but login status did not confirm", code)
        return False

    def _try_login_multi_user(self, tokens: Optional[Dict[str, Any]] = None) -> bool:
        if self._backend_name in {"mc888", "g5b"}:
            raise RuntimeError(f"zte_{self._backend_name}_multi_user_login_forbidden")
        if not self._password:
            return False
        params = dict(tokens if tokens is not None else self._read_goform(ZTE_LOGIN_TOKEN_FIELDS))
        rd = str(params.get("RD") or "").strip()
        rd0 = str(params.get("wa_inner_version") or "").strip()
        rd1 = str(params.get("cr_version") or "").strip()
        if not (rd and (rd0 or rd1)):
            raise ConnectionError("zte_login_multi_user_tokens_unavailable")
        ad = self._build_accessible_id_from_tokens(rd0, rd1, rd, algorithm=self._access_id_algorithm)
        payload = {
            "isTest": "false",
            "goformId": "LOGIN_MULTI_USER",
            "user": self._username or "admin",
            "password": base64.b64encode(self._password.encode("utf-8")).decode("ascii"),
            "AD": ad,
        }
        result = self._post_goform(payload)
        code = self._required_login_field(result, "result")
        if code not in {"0", "4"}:
            return False
        status_after = self._read_goform(("loginfo",))
        if self._required_login_field(status_after, "loginfo").lower() != "ok":
            raise ConnectionError("zte_login_accepted_but_session_not_confirmed")
        return True

    def _build_accessible_id(self) -> str:
        rd0 = self._cached_wa_inner_version
        rd1 = self._cached_cr_version
        if not (rd0 or rd1):
            # Cache miss — fetch once
            rd_params = self._read_goform(("Language", "cr_version", "wa_inner_version"))
            rd0 = str(rd_params.get("wa_inner_version") or "").strip()
            rd1 = str(rd_params.get("cr_version") or "").strip()
            if rd0 or rd1:
                self._cached_wa_inner_version = rd0
                self._cached_cr_version = rd1
        if not (rd0 or rd1):
            return ""
        # RD is a nonce — must be fetched fresh each time
        rd_resp = self._read_goform(("RD",), multi_data=False)
        rd = str(rd_resp.get("RD") or "").strip()
        if not rd:
            return ""
        return self._build_accessible_id_from_tokens(rd0, rd1, rd, algorithm=self._access_id_algorithm)

    @classmethod
    def _build_accessible_id_from_tokens(cls, wa_inner_version: str, cr_version: str, rd: str, *, algorithm: str | None = None) -> str:
        rd0 = str(wa_inner_version or "").strip()
        rd1 = str(cr_version or "").strip()
        nonce = str(rd or "").strip()
        if not nonce or not (rd0 or rd1):
            return ""
        seed = rd0 + rd1
        # Existing reviewed non-G5B version-marker adapter. Selected G5B never
        # enters inference or retries another algorithm after a failed login.
        selected = algorithm if algorithm is not None else ("sha256" if cls._uses_sha256_access_id(rd0, rd1) else "md5")
        if selected not in {"md5", "sha256"}:
            raise ValueError("zte_goform_access_id_algorithm_unqualified")
        encoder = cls._sha256_hex if selected == "sha256" else cls._md5_hex
        return encoder(encoder(seed) + nonce)

    @staticmethod
    def _uses_sha256_access_id(wa_inner_version: str, cr_version: str = "") -> bool:
        marker = f"{wa_inner_version or ''} {cr_version or ''}".upper()
        # Verified from this MF296R firmware's own service.js and util.js.
        # Do not extend the rule to unverified MF296R firmware variants.
        return (
            "MC888" in marker or "MC889" in marker
            or str(wa_inner_version or "").strip().upper()
            == "BD_A1SRBMF296RMODV1.0.0B04"
        )

    @staticmethod
    def _write_ok(result: Dict[str, Any]) -> bool:
        if not result:
            return False
        codes = [result[key] for key in ("result", "Result") if key in result]
        return bool(codes) and all(
            type(code) in (str, int, bool)
            and str(code).strip().lower() in ZTE_WRITE_OK_RESULTS
            for code in codes
        )

    def _set_cell_lock(self, pci: Optional[int], earfcn: Optional[int]) -> bool:
        del pci, earfcn
        raise RuntimeError("zte_goform_direct_cell_lock_write_forbidden")

    @staticmethod
    def _sha256_hex(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _md5_hex(value: str) -> str:
        return hashlib.md5(str(value or "").encode("utf-8")).hexdigest()

    def _parse_goform_payload(self, payload: str) -> Dict[str, Any]:
        text = str(payload or "").strip()
        if not text:
            return {}

        def _load_dict(candidate: str) -> Dict[str, Any]:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items()}
            except json.JSONDecodeError as exc:
                log.debug("_parse_goform_payload: suppressed %s", exc)
            return {}

        data = _load_dict(text)
        if data:
            return data

        repaired = self._repair_malformed_json_commas(text)
        if repaired != text:
            data = _load_dict(repaired)
            if data:
                return data

        callback_match = re.match(r"^[^(]+\((\{.*\})\)\s*;?\s*$", text, re.DOTALL)
        if callback_match:
            body = callback_match.group(1)
            data = _load_dict(body)
            if data:
                return data
            repaired_body = self._repair_malformed_json_commas(body)
            if repaired_body != body:
                data = _load_dict(repaired_body)
                if data:
                    return data

        # Do not parse malformed JSON as querystring; parse_qsl would turn the
        # entire JSON object into one bogus key and hide real RF/cell fields.
        if text.startswith("{") or callback_match:
            return {}

        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return {str(k): v for k, v in pairs}
        return {}

    @staticmethod
    def _repair_malformed_json_commas(text: str) -> str:
        """Repair A1 MF296R goform JSON with empty comma slots: ``..., , ...``."""
        repaired = str(text or "")
        while True:
            next_text = re.sub(r",\s*,", ",", repaired)
            if next_text == repaired:
                break
            repaired = next_text
        repaired = re.sub(r"\{\s*,", "{", repaired)
        repaired = re.sub(r",\s*\}", "}", repaired)
        return repaired

    def _resolve_plmn(self, raw: Dict[str, Any]) -> str:
        direct = str(raw.get("mccmnc") or "").strip()
        digits = "".join(ch for ch in direct if ch.isdigit())
        if len(digits) >= 6:
            return digits[:6]
        if len(digits) == 5:
            return digits
        mcc = "".join(ch for ch in str(raw.get("rmcc") or "").strip() if ch.isdigit())
        mnc = "".join(ch for ch in str(raw.get("rmnc") or "").strip() if ch.isdigit())
        if mcc and mnc:
            if len(mnc) == 1:
                mnc = f"0{mnc}"
            return f"{mcc}{mnc}"
        return ""

    def _resolve_provider_name(self, raw: Dict[str, Any], plmn: str) -> str:
        name = self._normalize_provider_fullname(raw, plmn) or str(raw.get("network_provider") or "").strip()
        if name and name.lower() not in {"unknown", "n/a"}:
            return name
        return resolve_provider_name(plmn)

    def _normalize_provider_fullname(self, raw: Dict[str, Any], plmn: str) -> Optional[str]:
        text = str(raw.get("network_provider_fullname") or "").strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if text and text.lower() not in {"unknown", "n/a"} and not (digits and digits == text and digits == plmn):
            return text
        provider = str(raw.get("network_provider") or "").strip()
        if provider and provider.lower() not in {"unknown", "n/a"}:
            return provider
        return resolve_provider_name(plmn) or None

    def _normalize_network_type(self, value: Any) -> str:
        if is_limited_service(value):
            return ""
        text = str(value or "").strip()
        upper = text.upper()
        if upper in {"ENDC", "EN-DC", "LTE-NSA"} or ("NSA" in upper and ("5G" in upper or "NR" in upper)):
            return "NR NSA (5G)"
        if upper == "SA" or ("5G" in upper and "SA" in upper):
            return "NR SA (5G)"
        if upper in {"LTE_A", "LTE-A", "LTEA"}:
            return "LTE (4G)"
        if "LTE" in upper and "CA" in upper:
            return "LTE (4G)"
        if upper in {"4G", "LTE"} or "LTE" in upper:
            return "LTE (4G)"
        return ""

    @staticmethod
    def _normalize_imsi(value: Any) -> str:
        return normalize_imsi(value)

    # ─── Wi-Fi status and control ──────────────────────────

    @staticmethod
    def _parse_wifi_enabled(raw: Dict[str, Any]) -> Optional[bool]:
        return parse_wifi_enabled(raw)

    @staticmethod
    def _parse_wifi_band_enabled(
        raw: Dict[str, Any],
        *,
        band: str,
    ) -> Optional[bool]:
        return parse_wifi_band_enabled(raw, band=band)

    def wifi_set_radio(self, radio: str, enabled: bool) -> bool:
        """The retired global toggle cannot represent a per-band command."""
        raise RuntimeError("zte_goform_direct_wifi_write_forbidden")

    def _is_ca_active(self, raw: Dict[str, Any], active_bands: List[int]) -> bool:
        raw_flag = str(raw.get("wan_lte_ca") or "").strip().lower()
        if raw_flag in TRUTHY_STRINGS:
            return True
        if len(active_bands) > 1:
            return True
        net_type = str(raw.get("network_type") or raw.get("current_network_mode") or "").upper()
        return "CA" in net_type

    def _resolve_active_bands(self, raw: Dict[str, Any]) -> List[int]:
        pcell = []
        for key in ("lte_ca_pcell_band", "wan_active_band", "ZCELLINFO_band", "lte_band"):
            pcell = self._parse_band_values(raw.get(key))
            if pcell:
                break
        if not pcell:
            return []
        scells = self._parse_band_values(raw.get("lte_ca_scell_band"))
        return self._dedupe_bands(pcell + scells)

    # ── RSRQ estimation for modems that don't report it natively ──

    # LTE resource blocks per bandwidth (3GPP TS 36.104 Table 5.6-1)
    _BW_TO_NRB: Dict[int, int] = {
        1: 6, 3: 15, 5: 25, 10: 50, 15: 75, 20: 100,
    }

    def _estimate_rsrq(
        self,
        rsrp: float,
        rssi: float,
        raw: Dict[str, Any],
    ) -> tuple[float, str]:
        """Estimate RSRQ from RSRP, RSSI, and bandwidth when native RSRQ is unavailable.

        Formula: RSRQ = 10*log10(N_rb) + RSRP - RSSI
        where N_rb = number of resource blocks for the channel bandwidth.

        Returns (rsrq_value, source) where source is "estimated" or "unavailable".
        """
        import math
        if abs(rsrp) < 0.001 or abs(rssi) < 0.001:
            return 0.0, "unavailable"
        bw_mhz = self._parse_metric_float(
            raw.get("lte_ca_pcell_bandwidth") or raw.get("bandwidth")
        )
        bw_int = int(round(bw_mhz)) if bw_mhz > 0 else 20
        n_rb = self._BW_TO_NRB.get(bw_int, 100)
        estimated = 10.0 * math.log10(n_rb) + rsrp - rssi
        # Clamp to valid RSRQ range: -19.5 to -3.0 dB (3GPP TS 36.133)
        estimated = max(-19.5, min(-3.0, estimated))
        return round(estimated, 1), "estimated"

    @staticmethod
    def _estimate_cqi_from_sinr(sinr_db: float) -> tuple[int, str]:
        """Estimate representative CQI from inferred SINR QAM bucket.

        This intentionally avoids pretending we know an exact vendor CQI ladder.
        The returned CQI is the center of the inferred 4-bucket modulation band.
        """
        qm = estimate_qm_from_sinr(sinr_db)
        if qm <= 0:
            return 0, "unavailable"
        return representative_cqi_for_qm(qm), "estimated"

    def _resolve_carrier_bandwidths(self, raw: Dict[str, Any], active_bands: List[int]) -> List[float]:
        if not active_bands:
            return []
        pcell = self._parse_metric_float(raw.get("lte_ca_pcell_bandwidth") or raw.get("bandwidth"))
        widths: List[float] = [max(0.0, pcell)]
        scell = self._parse_metric_float(raw.get("lte_ca_scell_bandwidth"))
        if scell > 0.0:
            widths.append(scell)
        while len(widths) < len(active_bands):
            widths.append(0.0)
        return widths[: len(active_bands)]

    def _build_ca_signature(self, active_bands: List[int], serving_band: int, ca_active: bool) -> str:
        if len(active_bands) > 1:
            return band_signature(normalize_band_list(active_bands))
        if serving_band > 0:
            return f"B{serving_band}"
        return ""

    def _build_carriers(
        self,
        lte_cells: List[Dict[str, Any]],
        active_bands: List[int],
        carrier_bandwidths: List[float],
        serving_band: int,
        earfcn: int,
        pci: int,
        sinr: float,
        rsrp: float,
    ) -> List[CarrierInfo]:
        if lte_cells:
            carriers: List[CarrierInfo] = []
            for idx, cell in enumerate(lte_cells):
                carriers.append(
                    CarrierInfo(
                        band=int(cell.get("band") or 0),
                        earfcn=cell.get("earfcn"),
                        pci=cell.get("pci"),
                        bandwidth_mhz=cell.get("bandwidth_mhz"),
                        sinr_db=cell.get("sinr_db"),
                        rsrp_dbm=cell.get("rsrp_dbm"),
                        state=0 if idx == 0 else 1,
                        is_pcell=bool(cell.get("is_pcell", idx == 0)),
                    )
                )
            return carriers
        bands = list(active_bands)
        if not bands and serving_band > 0:
            bands = [serving_band]
        carriers: List[CarrierInfo] = []
        for idx, band in enumerate(bands):
            carriers.append(
                CarrierInfo(
                    band=int(band),
                    earfcn=earfcn if idx == 0 else 0,
                    pci=pci if idx == 0 else 0,
                    bandwidth_mhz=carrier_bandwidths[idx] if idx < len(carrier_bandwidths) else 0.0,
                    sinr_db=sinr if idx == 0 else 0.0,
                    rsrp_dbm=rsrp if idx == 0 else 0.0,
                    state=0 if idx == 0 else 1,
                    is_pcell=(idx == 0),
                )
            )
        return carriers

    def _build_lte_cells(
        self,
        raw: Dict[str, Any],
        serving_band: int,
        pci: int,
        earfcn: int,
        sinr: float,
        rsrp: float,
        rsrq: float,
        rssi: float,
    ) -> List[Dict[str, Any]]:
        # Only selected GoForm5G contracts use optional component values here.
        # The inherited ZWRT/4G parser keeps its established dialect/defaults.
        metadata = self._definition.metadata
        strict_optional = (metadata.get("family") == "zte_webui_like"
                           and metadata.get("device_radio_class") == "5g")
        missing = None if strict_optional else 0.0

        def component_int(value: Any, *, maximum: int | None = None) -> int | None:
            if not strict_optional:
                return self._parse_int(value)
            if value is None or isinstance(value, bool):
                return None
            text = str(value).strip()
            if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
                return None
            try:
                number = int(text, 16 if text.lower().startswith("0x") else 10)
            except ValueError:
                return None
            return number if maximum is None or number <= maximum else None

        def component_metric(value: Any, unit: str) -> float | None:
            if not strict_optional:
                return self._parse_metric_float(value)
            if value is None or isinstance(value, bool):
                return None
            match = re.fullmatch(r"(-?[0-9]+(?:\.[0-9]+)?)\s*(?:" + re.escape(unit) + r")?",
                                 str(value).strip(), flags=re.IGNORECASE)
            if not match:
                return None
            number = float(match.group(1))
            return number if math.isfinite(number) else None

        cells: List[Dict[str, Any]] = []
        pcell_band = self._parse_band_values(
            raw.get("lte_ca_pcell_band") or raw.get("ZCELLINFO_band") or raw.get("lte_band")
        )
        main_band = int((pcell_band or [serving_band])[0] if (pcell_band or [serving_band]) else 0)
        if self._portable_rf_values.get('radio.serving_band') is not None:
            main_band = serving_band
        main_earfcn = earfcn if 'rf.earfcn_dl' in self._portable_rf_values else self._parse_int(
            raw.get("lte_ca_pcell_freq")
            or raw.get("lte_ca_pcell_arfcn")
            or raw.get("Z_dl_earfcn")
            or raw.get("wan_active_channel")
            or earfcn
        )
        main_bw = self._parse_metric_float(raw.get("lte_ca_pcell_bandwidth") or raw.get("bandwidth"))
        cells.append(
            {
                "role": "PCell",
                "is_pcell": True,
                "band": main_band,
                "pci": pci,
                "earfcn": main_earfcn,
                "bandwidth_mhz": main_bw,
                "rsrp_dbm": self._first_non_zero(
                    self._parse_metric_float(raw.get("lte_rsrp_1")),
                    rsrp,
                ),
                "rsrq_db": rsrq,
                "sinr_db": self._first_non_zero(
                    self._parse_metric_float(raw.get("lte_snr_1")),
                    sinr,
                ),
                "rssi_dbm": rssi,
            }
        )
        scell_infos = [
            part
            for part in str(raw.get("lte_multi_ca_scell_info") or raw.get("lte_ca_scell_info") or "").split(";")
            if part.strip()
        ]
        scell_sig_infos = [
            part
            for part in str(raw.get("lte_multi_ca_scell_sig_info") or raw.get("lte_ca_scell_sig_info") or "").split(";")
            if part.strip()
        ]
        for idx, info in enumerate(scell_infos):
            parts = [part.strip() for part in info.split(",")]
            if len(parts) < 6:
                continue
            sig_parts = [part.strip() for part in scell_sig_infos[idx].split(",")] if idx < len(scell_sig_infos) else []
            rsrp_s = component_metric(sig_parts[0], "dBm") if len(sig_parts) >= 1 else missing
            if rsrp_s is not None and abs(rsrp_s + 44.0) < 0.01:
                rsrp_s = missing
            cells.append(
                {
                    "role": f"SCell{idx + 1}",
                    "is_pcell": False,
                    "pci": component_int(parts[1], maximum=503),
                    "band": self._parse_nr_band(parts[3]),
                    "earfcn": component_int(parts[4]),
                    "bandwidth_mhz": component_metric(parts[5], "MHz"),
                    "rsrp_dbm": rsrp_s,
                    "rsrq_db": component_metric(sig_parts[1], "dB") if len(sig_parts) >= 2 else missing,
                    "sinr_db": component_metric(sig_parts[2], "dB") if len(sig_parts) >= 3 else missing,
                    "rssi_dbm": missing,
                }
            )
        if len(cells) == 1:
            summary_scells = self._parse_band_values(raw.get("lte_ca_scell_band"))
            summary_bw = component_metric(raw.get("lte_ca_scell_bandwidth"), "MHz")
            for idx, band in enumerate(summary_scells):
                cells.append(
                    {
                        "role": f"SCell{idx + 1}",
                        "is_pcell": False,
                        "pci": None if strict_optional else 0,
                        "band": int(band),
                        "earfcn": None if strict_optional else 0,
                        "bandwidth_mhz": summary_bw,
                        "rsrp_dbm": missing,
                        "rsrq_db": missing,
                        "sinr_db": missing,
                        "rssi_dbm": missing,
                    }
                )
        return [cell for cell in cells if int(cell.get("band") or 0) > 0]

    def _parse_nr5g_status(self, raw: Dict[str, Any], network_type: str, *, profile_values=None) -> Dict[str, Any]:
        raw_type = str(raw.get("network_type") or raw.get("current_network_mode") or "").strip().upper()
        is_nsa = raw_type in {"ENDC", "EN-DC", "LTE-NSA"}
        is_sa = raw_type == "SA" or ("5G" in raw_type and "SA" in raw_type)
        active = is_sa or (is_nsa and raw_type != "LTE-NSA")
        mode = "NSA" if is_nsa else ("SA" if is_sa else "")
        def first_present(*keys: str) -> Any:
            for key in keys:
                if key in raw and raw[key] not in (None, ""):
                    return raw[key]
            return None

        def optional_int(value: Any, *, maximum: int | None = None) -> int | None:
            if value in (None, "") or isinstance(value, bool):
                return None
            text = str(value).strip()
            try:
                parsed = int(text, 16 if text.lower().startswith("0x") else 10)
            except ValueError:
                return None
            if parsed < 0 or (maximum is not None and parsed > maximum):
                return None
            return parsed

        def optional_metric(
            value: Any, *, minimum: float, maximum: float
        ) -> float | None:
            if value in (None, "") or isinstance(value, bool):
                return None
            text = re.sub(r"\s*(?:MHz|dBm|dB)$", "", str(value).strip(), flags=re.I)
            try:
                parsed = float(text)
            except ValueError:
                return None
            if not minimum <= parsed <= maximum:
                return None
            return parsed

        cell_id = optional_int(first_present("nr5g_cell_id", "Z5g_CELL_ID"))
        pci = optional_int(
            first_present("nr5g_pci", "network_Z5g_PCI", "Z5g_PCI"),
            maximum=1007,
        )
        arfcn = optional_int(
            first_present(
                "nr_ca_pcell_freq",
                "nr5g_action_channel",
                "Z5g_dlEarfcn",
            ),
            maximum=3_279_165,
        )
        band_value = first_present(
            "nr_ca_pcell_band",
            "nr5g_action_nsa_band" if is_nsa else "nr5g_action_band",
            "nr5g_action_band",
            "network_Z5g_CELLINFO_band",
            "Z5g_CELLINFO_band",
        )
        parsed_band = self._parse_nr_band(band_value)
        band = parsed_band if parsed_band > 0 else None
        rx0 = optional_metric(
            first_present("5g_rx0_rsrp", "Z5g_rsrp"),
            minimum=-160.0,
            maximum=-20.0,
        )
        rx1 = optional_metric(
            first_present("5g_rx1_rsrp"),
            minimum=-160.0,
            maximum=-20.0,
        )
        rsrp = optional_metric(
            first_present("Z5g_rsrp", "5g_rx0_rsrp", "5g_rx1_rsrp"),
            minimum=-160.0,
            maximum=-20.0,
        )
        rsrq = optional_metric(
            first_present("Z5g_rsrq"), minimum=-50.0, maximum=10.0
        )
        sinr = optional_metric(
            first_present("Z5g_SINR", "Z5g_snr"),
            minimum=-50.0,
            maximum=100.0,
        )
        bandwidth_mhz = optional_metric(
            first_present("nr5g_nsa_bandwidth"),
            minimum=0.01,
            maximum=400.0,
        )
        values = profile_values or {}
        # Import at the native translation boundary, before PCell/summary
        # construction. Callers that inspect raw protocol data remain pure.
        cell_id = values.get('radio.nr_cell_id',cell_id)
        pci = values.get('radio.nr_pci',pci)
        arfcn = values.get('radio.nr_arfcn',arfcn)
        band = values.get('radio.nr_band',band)
        rsrp = values.get('radio.nr_rsrp_dbm',rsrp)
        rsrq = values.get('radio.nr_rsrq_db',rsrq)
        sinr = values.get('radio.nr_sinr_db',sinr)
        bandwidth_mhz = values.get('slow.nr_bandwidth_mhz',bandwidth_mhz)
        rx0 = values.get('profile.nr5g_rx0_rsrp_dbm',rx0)
        rx1 = values.get('profile.nr5g_rx1_rsrp_dbm',rx1)
        cells: List[Dict[str, Any]] = []
        # LTE-only firmware returns zero-filled NR slots. They are not a cell.
        # In an explicit NR mode, a reported PCI/NCI/ARFCN of zero is still valid.
        identity = (band, arfcn, pci, cell_id)
        has_cell_evidence = any(value not in (None, 0) for value in identity) or (
            bool(mode) and any(value is not None for value in identity)
        )
        if has_cell_evidence:
            cells.append(
                {
                    "role": "PCell",
                    "band": band,
                    "arfcn": arfcn,
                    "pci": pci,
                    "bandwidth_mhz": bandwidth_mhz,
                    "rsrp_dbm": rsrp,
                    "rsrq_db": rsrq,
                    "sinr_db": sinr,
                    "rx0_rsrp_dbm": rx0,
                    "rx1_rsrp_dbm": rx1,
                }
            )
        for idx, cell in enumerate(part for part in str(raw.get("nr_multi_ca_scell_info") or "").split(";") if part.strip()):
            parts = [part.strip() for part in cell.split(",")]
            if len(parts) < 10:
                continue
            cell_band = self._parse_nr_band(parts[3])
            if cell_band <= 0:
                continue
            cells.append(
                {
                    "role": f"SCell{idx + 1}",
                    "band": cell_band,
                    "arfcn": optional_int(parts[4], maximum=3_279_165),
                    "pci": optional_int(parts[1], maximum=1007),
                    "bandwidth_mhz": optional_metric(parts[5], minimum=0.01, maximum=400.0),
                    "rsrp_dbm": optional_metric(parts[7], minimum=-160.0, maximum=-20.0),
                    "rsrq_db": optional_metric(parts[8], minimum=-50.0, maximum=10.0),
                    "sinr_db": optional_metric(parts[9], minimum=-50.0, maximum=100.0),
                }
            )
        band_set = self._dedupe_bands([int(cell.get("band") or 0) for cell in cells])
        if band is None and band_set:
            band = int(band_set[0])
        state_text = ""
        if mode == "NSA" and active:
            state_text = "ACTIVE"
        elif mode == "NSA":
            state_text = "AVAILABLE"
        elif mode == "SA":
            state_text = "ACTIVE"
        elif "5G" in network_type.upper():
            state_text = "AVAILABLE"
        return {
            "active": bool(active),
            "mode": mode,
            "state_text": state_text,
            "cell_id": cell_id,
            "pci": pci,
            "arfcn": arfcn,
            "band": band,
            "band_set": band_set,
            "rsrp_dbm": rsrp,
            "rsrq_db": rsrq,
            "sinr_db": sinr,
            "rx0_rsrp_dbm": rx0,
            "rx1_rsrp_dbm": rx1,
            "bandwidth_mhz": bandwidth_mhz,
            "cells": cells,
            "sa_band_lock": str(raw.get("nr5g_sa_band_lock") or "").strip(),
            "nsa_band_lock": str(raw.get("nr5g_nsa_band_lock") or "").strip(),
        }

    def _read_traffic(self, raw: Dict[str, Any], now: float) -> tuple[float, float]:
        # A maintenance/partial response cannot reset the other direction's
        # baseline. A first counter is not a measured zero throughput.
        self._traffic_rates_proven = set()
        self._traffic_rate_sources = {}
        previous_samples = dict(self._traffic_counter_samples)
        def number(key: str) -> float | None:
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            return value if math.isfinite(value) and value >= 0.0 else None

        rates = []
        for direction in ("rx", "tx"):
            total_key = next((key for key in (
                f"wan_{direction}_bytes", f"{direction}_bytes", f"realtime_{direction}_bytes",
                f"traffic_{direction}", f"total_{direction}_bytes",
            ) if number(key) is not None), None)
            rate_key = next((key for key in (
                f"realtime_{direction}_thrpt", f"{direction}_thrpt", f"{direction}_rate",
            ) if number(key) is not None), None)
            rate = 0.0
            if rate_key:
                rate = number(rate_key)
                self._traffic_rates_proven.add(direction)
                self._traffic_rate_sources[direction] = "native_current_rate"
            if total_key:
                total = int(number(total_key))
                previous = self._traffic_counter_samples.get(direction)
                if previous is None or now > previous[1]:
                    # Reported rates own their measurement interval. Counter
                    # derivation is only for replies without a usable rate.
                    if rate_key is None and previous is not None and previous[2] == total_key and total >= previous[0]:
                        rate = (total - previous[0]) / (now - previous[1])
                        self._traffic_rates_proven.add(direction)
                        self._traffic_rate_sources[direction] = "counter_delta"
                    self._traffic_counter_samples[direction] = (total, now, total_key)
            rates.append(rate)
        # The existing owner advances counter baselines exactly once. Apply
        # an imported correction before its rates and proof leave this owner.
        context = {"traffic_previous":previous_samples,"monotonic_at":now,
                   "native_inputs":{key:raw.get(key) for direction in ("rx","tx")
                       for key in (f"wan_{direction}_bytes",f"{direction}_bytes",
                           f"realtime_{direction}_bytes",f"traffic_{direction}",
                           f"total_{direction}_bytes",f"realtime_{direction}_thrpt",
                           f"{direction}_thrpt",f"{direction}_rate")}}
        replies = self._profile_native_replies(raw)
        addresses = self._definition.available_addresses(replies, context=context,
            addresses={"traffic.rx_bytes_per_sec","traffic.tx_bytes_per_sec"})
        values = self._decode_profile_values(replies, addresses, context=context)
        for index,direction in enumerate(("rx","tx")):
            address = "traffic." + direction + "_bytes_per_sec"
            if address not in values:
                continue
            value = values[address]
            if value is None or value < 0:
                rates[index] = 0.0
                self._traffic_rates_proven.discard(direction)
                self._traffic_rate_sources.pop(direction, None)
            else:
                rates[index] = float(value)
                self._traffic_rates_proven.add(direction)
                self._traffic_rate_sources.setdefault(direction, "native_current_rate")
        return rates[0], rates[1]

    def _resolve_session_uptime(self, raw: Dict[str, Any], now: float, radio_connected: Optional[bool]) -> float:
        raw_uptime = raw.get("ppp_uptime")
        address = 'traffic.session_uptime_s'
        if self._definition.has_read(address):
            # Propose the existing elapsed fallback without advancing its
            # clock twice. The admitted rule still reads the actual response.
            start = self._synthetic_session_started_at
            fallback = (None if radio_connected is None else 0.0 if not radio_connected
                else max(0.0,now-(start if start is not None else now)))
            replies = self._profile_native_replies(raw)
            context = {'native_session_fallback':fallback}
            selected = self._definition.available_addresses(replies,context=context,addresses={address})
            values = self._decode_profile_values(replies,selected,context=context)
            if address in values:
                self._portable_rf_values.update(values)
                value = values[address]
                if value is None:
                    if radio_connected is False:self._synthetic_session_started_at=None
                    return 0.0
                keys = self._goform_profile_sources.get(address,frozenset())
                reported = any(self._goform_source_observed(raw,key)for key in keys)
                if reported:
                    raw_uptime = value
                else:
                    if not radio_connected:self._synthetic_session_started_at=None
                    elif start is None:self._synthetic_session_started_at=now
                    return float(value)
        text = str(raw_uptime if raw_uptime is not None else "").strip()
        if text:
            uptime_s = self._parse_duration(raw_uptime)
            if radio_connected and uptime_s > 0.0:
                self._synthetic_session_started_at = max(0.0, now - uptime_s)
            elif radio_connected is False:
                self._synthetic_session_started_at = None
            return uptime_s
        if not radio_connected:
            self._synthetic_session_started_at = None
            return 0.0
        if self._synthetic_session_started_at is None:
            self._synthetic_session_started_at = now
        return max(0.0, now - self._synthetic_session_started_at)

    @staticmethod
    def _decode_band_mask(value: Any) -> List[int]:
        return decode_band_mask(value)

    @staticmethod
    def _parse_band_values(value: Any) -> List[int]:
        return parse_band_values(value)

    @staticmethod
    def _dedupe_bands(values: List[int]) -> List[int]:
        return dedupe_bands(values)

    @staticmethod
    def _parse_float(value: Any) -> float:
        return parse_float(value)

    @classmethod
    def _parse_metric_float(cls, value: Any) -> float:
        return parse_metric_float(value)

    @classmethod
    def _parse_dbm_metric(cls, value: Any) -> float:
        return parse_dbm_metric(value)

    @staticmethod
    def _parse_nr_band(value: Any) -> int:
        return parse_nr_band(value)

    @staticmethod
    def _first_non_zero(*values: float) -> float:
        return first_non_zero(*values)

    @staticmethod
    def _parse_int(value: Any) -> int:
        return parse_int(value)

    @staticmethod
    def _parse_duration(value: Any) -> float:
        return parse_duration(value)
