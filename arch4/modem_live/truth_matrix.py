"""Immutable Matrix contract for one Modem Live generation.

Only Modem Live writes it. Core, NGUI, Foundation, and other consumers may read
and validate it. They must not add aliases, copy a missing cell from an older
generation, or change its state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)

MODEM_LIVE_TRUTH_MATRIX_SCHEMA = "ModemLiveTruthMatrix.v4"
MODEM_LIVE_TRUTH_MATRIX_OWNER = "54guc.modem_live.truth_matrix"
MODEM_LIVE_TRUTH_CELL_SCHEMA = "ModemLiveTruthCell.v4"
MODEM_LIVE_TRUTH_LINE_SCHEMA = "ModemLiveTruthLine.v4"
MODEM_LIVE_TRUTH_RETENTION_SCHEMA = "ModemLiveTruthCellRetentionReceipt.v1"
MODEM_LIVE_TRUTH_RETENTION_OWNER = "54guc.modem_live.truth_matrix_retention"
RETENTION_CURRENT_NAMESPACE = "retained_current_namespace"
RETENTION_SAME_DEVICE_RECONNECT = "retained_same_device_reconnect"
RETENTION_DISPOSITIONS = frozenset(
    {
        RETENTION_CURRENT_NAMESPACE,
        RETENTION_SAME_DEVICE_RECONNECT,
    }
)

CELL_AVAILABLE = "available"
CELL_MISSING = "missing"
CELL_INVALID = "invalid"
CELL_UNSUPPORTED = "unsupported"
CELL_STATES = frozenset(
    {CELL_AVAILABLE, CELL_MISSING, CELL_INVALID, CELL_UNSUPPORTED}
)

PROFILE_CELL_EXPECTATIONS = frozenset(
    {"supported", "synthetic", "unsupported", "untested"}
)


MODEM_LIVE_TRUTH_FIELD_CATALOG: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "radio": (
            "network_type",
            "network_type_ex",
            "cell_locked",
            "carrier_count",
            "carrier_details",
            "carrier_rows",
            "configured_ca_pool_display",
            "ca_active",
            "ca_signature",
            "ca_band_signature",
            "ca_state_text",
            "ca_source",
            "ca_component_band_set",
            "ca_component_band_signature",
            "ca_component_count",
            "ca_composition_signature",
            "tx_power",
            "tx_power_support",
            "tx_power_dbm",
            "nsa_active",
            "sa_active",
            "nr5g_active",
            "nr_band",
            "nr_pci",
            "nr_arfcn",
            "nr_cell_id",
            "nr_sinr_db",
            "nr_rsrp_dbm",
            "nr_rsrq_db",
            "nr_ri", "nr_cqi", "nr_mcs_dl", "nr_mcs_ul",
            "nr_qam_dl", "nr_qam_ul", "nr_tx_power_dbm", "nr_num_rbs", "lte_num_rbs",
            "nr_mode",
            "nr_state_text",
            "serving_band",
            "serving_band_signature",
            "serving_band_available",
            "serving_band_unavailable_reason",
            "selected_band_set",
            "selected_band_set_signature",
            "active_band_set",
            "active_band_set_signature",
            "band_set",
            "band_set_signature",
            "configured_band_pool",
            "configured_band_pool_source",
            "configured_band_pool_signature",
            "configured_band_pool_status",
            "configured_band_pool_age_s",
            "configured_band_pool_last_sample_ts",
        ),
        "rf": (
            "sinr_db",
            "rsrp_dbm",
            "rsrq_db",
            "rssi_dbm",
            "pci",
            "earfcn_dl",
            "earfcn_ul",
            "cell_id",
            "enb_id",
            "cqi",
            "cqi0",
            "cqi1",
            "mcs_dl",
            "mcs_dl0",
            "mcs_dl1",
            "mcs_ul",
            "ri",
            "ri_valid",
            "ri_source",
            "qam_dl0",
            "qam_dl1",
            "qam_ul",
            "scheduler_zero_authoritative",
            "scheduler_stream1_available",
            "scheduler_source",
            "cqi_source",
            "scheduler_field_trust",
            "scheduler_unsupported_fields",
        ),
        "traffic": (
            "throughput_dl_mbps",
            "throughput_ul_mbps",
            "dl_mbps",
            "ul_mbps",
            "rx_bytes_per_sec",
            "tx_bytes_per_sec",
            "has_traffic",
            "traffic_active",
            "traffic_valid",
            "traffic_state_detail",
            "traffic_invalid_reason",
            "packet_session_connected",
            "connection_status_code",
            "session_uptime_s",
            "session_uptime_source",
            "traffic_presence_active",
            "traffic_presence_interval_s",
            "traffic_current_download_rate_bps",
            "traffic_current_upload_rate_bps",
            "traffic_rate_source",
            "traffic_day_download_bytes",
            "traffic_day_upload_bytes",
            "traffic_day_used_bytes",
            "traffic_month_download_bytes",
            "traffic_month_upload_bytes",
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
            "traffic_accounting_month_key",
            "traffic_accounting_day_key",
            "traffic_session_duration_s",
            "traffic_day_duration_s",
            "traffic_month_duration_s",
            "traffic_total_duration_s",
            "traffic_volume_poll_interval_s",
            "traffic_volume_last_sample_ts",
            "traffic_volume_next_sample_ts",
            "traffic_volume_age_s",
            "traffic_quota_start_day",
            "traffic_quota_limit_label",
            "traffic_quota_left_bytes",
            "traffic_quota_limit_bytes",
            "traffic_quota_month_threshold_pct",
            "traffic_quota_day_threshold_pct",
        ),
        "slow": (
            "rrc",
            "trans_mode",
            "tac",
            "dl_freq_mhz",
            "ul_freq_mhz",
            "dl_bandwidth_mhz",
            "ul_bandwidth_mhz",
            "neighbors",
            "neighbor_count",
            "nr_bandwidth_mhz",
            "nr_ul_bandwidth_mhz", "nr_dl_freq_mhz", "nr_ul_freq_mhz",
            "nr_scs_khz",
            "nr_cells",
            "temperature_modem_5g_c",
            "temperature_cpu_c",
            "router_cpu_load_pct", "router_memory_used_pct", "router_system_uptime_s",
            "antenna",
            "wan_ip",
            "wifi",
            "wifi_enabled",
            "wifi_24ghz_enabled",
            "wifi_5ghz_enabled",
            "wifi_6ghz_enabled",
            "backend_diagnostic",
            "read_diagnostic",
            "network_settings",
            "mobile_data_enabled",
            "radio_mode",
        ),
        "timing": (
            "modem_comm_alive",
            "modem_sample_interval_s",
            "modem_sample_expected_interval_s",
            "modem_sample_slo_exceeded",
            "modem_read_ms",
        ),
        "health": (
            "radio_connected",
            "radio_state_text",
            "stale",
            "modem_read_missed",
            "modem_live_io_quiet_active",
            "modem_live_io_quiet_remaining_s",
            "live_register_quiet_hold",
            "live_register_hold_reason",
            "live_register_stale",
            "target_guard_ok",
            "target_guard_reason",
            "backend_session",
            "lte_interface_disabled",
            "device_recovery_state",
            "session_transition",
            "connectivity_receipt",
            "startup_live_baseline",
            "acquisition_lane_state",
        ),
        "identity": (
            "decode_catalog_snapshot",
            "access_type_contract",
            "modem_identity_key",
            "identity_publication_order",
            "schema",
            "owner",
            "code_namespace",
            "identity_code",
            "identity_status",
            "catalog_version",
            "catalog_checksum",
            "combination_guard",
            "access_type_id",
            "access_type_fingerprint",
            "modem_live_access_profile",
            "manufacturer",
            "hardware_sku",
            "firmware_version",
            "backend",
            "backend_family",
            "vendor",
            "model",
            "router_model",
            "firmware",
            "serial",
            "serial_number",
            "imei",
            "imsi",
            "sim_network",
            "profile_id",
            "hardware_version",
            "hw_version",
            "software_version",
            "software",
            "webui",
            "access_type",
            "lan_mac",
            "provider_name",
            "provider_plmn",
            "runtime_identity",
            "resolved_identity_card",
            "capability_profile",
            "static_modem_truth",
        ),
        "capability": (
            "device_radio_class",
            "session_band_capability",
            "runtime_capabilities",
            "session_lte_status",
            "session_lte_bands",
            "session_nr_status",
            "session_nr_bands",
            "session_capability_comparison",
            "session_capability_id",
            "pband_control",
            "ca_pool_control",
            "band_lock",
            "cell_lock",
            "restart",
            "max_tcp_payload",
            "ttl_ip",
            "keepalive_enabled",
            "dl_256qam",
            "pci_lock",
            "ul_64qam",
            "keepalive",
            "ue_category",
            "ue_info",
            "traffic_volume",
            "ca_details",
            "neighbors_support",
            "plmn_read",
            "band_priority",
            "action_band_select_state",
            "action_band_select_allowed",
            "action_band_lock_state",
            "action_band_lock_allowed",
            "action_cell_lock_state",
            "action_cell_lock_allowed",
            "action_restart_state",
            "action_restart_allowed",
            "action_pband_state",
            "action_pband_allowed",
            "action_nr_band_lock_state",
            "action_nr_band_lock_allowed",
            "action_wifi_control_state",
            "action_wifi_control_allowed",
            "action_network_settings_state",
            "action_network_settings_allowed",
            "action_mobile_data_state",
            "action_mobile_data_allowed",
            "action_radio_mode_state",
            "action_radio_mode_allowed",
            "read_traffic_presence_state",
            "read_traffic_volume_state",
            "read_ue_capability_state",
            "read_neighbors_state",
            "read_ca_details_state",
            "read_plmn_state",
            "read_band_priority_state",
            "sms_state",
            "sms_supported",
        ),
    }
)


# Settings and diagnostic evidence retain their owner's acquisition time.
# They are not RF/traffic measurements, so a contact-loss measurement cutoff
# must not erase them or make them fresh.
MODEM_LIVE_NON_MEASUREMENT_CELL_ADDRESSES = frozenset(
    {
        ("radio", "tx_power_support"),
        ("slow", "backend_diagnostic"),
        ("slow", "read_diagnostic"),
        ("radio", "cell_locked"),
        ("radio", "configured_ca_pool_display"),
        ("radio", "selected_band_set"),
        ("radio", "selected_band_set_signature"),
        ("radio", "configured_band_pool"),
        ("radio", "configured_band_pool_source"),
        ("radio", "configured_band_pool_signature"),
        ("radio", "configured_band_pool_status"),
        ("radio", "configured_band_pool_age_s"),
        ("radio", "configured_band_pool_last_sample_ts"),
        ("slow", "antenna"),
        ("slow", "wifi"),
        ("slow", "mobile_data_enabled"),
        ("slow", "radio_mode"),
        ("slow", "wifi_enabled"),
        ("slow", "wifi_24ghz_enabled"),
        ("slow", "wifi_5ghz_enabled"),
        ("slow", "wifi_6ghz_enabled"),
    }
)


# These modem-neutral facts fill the NGUI Device page. Modem Live maps facts to
# Matrix addresses. Presentation code may format them but cannot choose another
# source or alias.
MODEM_LIVE_DEVICE_FACT_ADDRESSES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "device.model": ("identity", "model"),
        "device.serial": ("identity", "serial"),
        "device.imei": ("identity", "imei"),
        "device.firmware": ("identity", "firmware"),
        "device.webui": ("identity", "webui"),
        "device.antenna": ("slow", "antenna"),
        "device.router_cpu_load_pct": ("slow", "router_cpu_load_pct"),
        "device.router_memory_used_pct": ("slow", "router_memory_used_pct"),
        "device.router_system_uptime_s": ("slow", "router_system_uptime_s"),
        "device.wan_ip": ("slow", "wan_ip"),
        "device.lan_mac": ("identity", "lan_mac"),
        "provider.name": ("identity", "provider_name"),
        "radio.pband": ("radio", "serving_band"),
        "radio.ca_pool": ("radio", "configured_band_pool"),
        "radio.cell_id": ("rf", "cell_id"),
        "radio.pci": ("rf", "pci"),
        "radio.earfcn": ("rf", "earfcn_dl"),
        "radio.enb_id": ("rf", "enb_id"),
        "radio.tac": ("slow", "tac"),
        "nr.band": ("radio", "nr_band"),
        "nr.cell_id": ("radio", "nr_cell_id"),
        "nr.pci": ("radio", "nr_pci"),
        "nr.arfcn": ("radio", "nr_arfcn"),
        "device.mobile_data_enabled": ("slow", "mobile_data_enabled"),
        "device.wifi_24ghz_enabled": ("slow", "wifi_24ghz_enabled"),
        "device.wifi_5ghz_enabled": ("slow", "wifi_5ghz_enabled"),
        "device.wifi_6ghz_enabled": ("slow", "wifi_6ghz_enabled"),
        "capability.pband_control": ("capability", "pband_control"),
        "capability.ca_pool_control": ("capability", "ca_pool_control"),
        "capability.band_lock": ("capability", "band_lock"),
        "capability.cell_lock": ("capability", "cell_lock"),
        "capability.restart": ("capability", "restart"),
        "device.access_type": ("identity", "access_type"),
        "capability.max_tcp_payload": ("capability", "max_tcp_payload"),
        "capability.ttl_ip": ("capability", "ttl_ip"),
        "capability.keepalive_enabled": ("capability", "keepalive_enabled"),
        "capability.dl_256qam": ("capability", "dl_256qam"),
        "capability.pci_lock": ("capability", "pci_lock"),
        "capability.ul_64qam": ("capability", "ul_64qam"),
        "capability.keepalive": ("capability", "keepalive"),
        "capability.ue_category": ("capability", "ue_category"),
        "capability.ue_info": ("capability", "ue_info"),
        "capability.traffic_volume": ("capability", "traffic_volume"),
        "capability.ca_details": ("capability", "ca_details"),
        "capability.neighbors": ("capability", "neighbors_support"),
        "capability.plmn_read": ("capability", "plmn_read"),
        "capability.band_priority": ("capability", "band_priority"),
    }
)


CANONICAL_NETWORK_TYPE_TEXT = frozenset(
    {
        "No Service",
        "LTE (4G)",
        "NR NSA (5G)",
        "NR SA (5G)",
    }
)
CANONICAL_CA_STATE_TEXT = frozenset({"ACTIVE", "INACTIVE", "PENDING"})
CANONICAL_RRC_TEXT = frozenset({"Connected", "Idle"})
CANONICAL_RADIO_STATE_TEXT = frozenset(
    {"Connected", "Disconnected", "Reconnecting", "Unavailable"}
)
CANONICAL_NR_MODE_TEXT = frozenset({"NSA", "SA", "Unavailable"})
CANONICAL_NR_STATE_TEXT = frozenset(
    {"ACTIVE", "AVAILABLE", "INACTIVE", "UNAVAILABLE"}
)
CANONICAL_SCHEDULER_TRUST_TEXT = frozenset(
    {"trusted", "unsupported", "non_useful", "untrusted", "missing"}
)
CANONICAL_REASON_TEXT = re.compile(r"^[a-z0-9][a-z0-9_:.+-]*$")
CANONICAL_BAND_SIGNATURE_TEXT = re.compile(r"^B[0-9]+(?:\+B[0-9]+)*$")
CANONICAL_CA_COMPOSITION_SIGNATURE_TEXT = re.compile(
    r"^(?:P|CA)(?:@B[0-9]+)?(?:@earfcn=[0-9]+)?(?:@pci=[0-9]+)?"
    r"(?: \+ (?:P|CA)(?:@B[0-9]+)?(?:@earfcn=[0-9]+)?(?:@pci=[0-9]+)?)*$"
)
CANONICAL_TRANSMISSION_MODE_TEXT = re.compile(
    r"^TM\[[1-9]\](?: \((?:SISO|TX Diversity|OL-MIMO|CL-MIMO|Beamforming)\))?$"
)
CANONICAL_QAM_TEXT = frozenset({"QPSK", "16QAM", "64QAM", "256QAM"})
CANONICAL_CAPABILITY_PROOF_STATE_TEXT = frozenset(
    {"supported", "estimated", "unsupported", "non_useful", "unknown"}
)
CANONICAL_CAPABILITY_SESSION_STATE_TEXT = frozenset(
    {"proven", "seeded", "unavailable", "unsupported", "unknown"}
)
CANONICAL_CAPABILITY_PRESENTATION_TEXT = frozenset(
    {
        "YES",
        "NO",
        "N/A",
        "UNKNOWN",
        "PROBING",
        "UNSUPPORTED",
        "BEST EFFORT",
        "OK",
        "MISS",
    }
)

BOOL_VALUE_FIELDS = frozenset(
    {
        ("radio", "cell_locked"),
        ("radio", "ca_active"),
        ("radio", "nsa_active"),
        ("radio", "sa_active"),
        ("radio", "nr5g_active"),
        ("radio", "serving_band_available"),
        ("rf", "ri_valid"),
        ("rf", "scheduler_zero_authoritative"),
        ("rf", "scheduler_stream1_available"),
        ("traffic", "has_traffic"),
        ("traffic", "traffic_active"),
        ("traffic", "traffic_valid"),
        ("traffic", "packet_session_connected"),
        ("traffic", "traffic_presence_active"),
        ("timing", "modem_comm_alive"),
        ("timing", "modem_sample_slo_exceeded"),
        ("health", "radio_connected"),
        ("health", "stale"),
        ("health", "modem_read_missed"),
        ("health", "modem_live_io_quiet_active"),
        ("health", "live_register_quiet_hold"),
        ("health", "live_register_stale"),
        ("health", "target_guard_ok"),
        ("health", "lte_interface_disabled"),
        ("slow", "mobile_data_enabled"),
        ("slow", "wifi_enabled"),
        ("slow", "wifi_24ghz_enabled"),
        ("slow", "wifi_5ghz_enabled"),
        ("slow", "wifi_6ghz_enabled"),
        ("capability", "action_band_select_allowed"),
        ("capability", "action_band_lock_allowed"),
        ("capability", "action_cell_lock_allowed"),
        ("capability", "action_restart_allowed"),
        ("capability", "action_pband_allowed"),
        ("capability", "action_nr_band_lock_allowed"),
        ("capability", "action_wifi_control_allowed"),
        ("capability", "action_network_settings_allowed"),
        ("capability", "action_mobile_data_allowed"),
        ("capability", "action_radio_mode_allowed"),
        ("capability", "sms_supported"),
    }
)

LIST_VALUE_FIELDS = frozenset(
    {
        ("radio", "carrier_rows"),
        ("radio", "carrier_details"),
        ("radio", "ca_component_band_set"),
        ("radio", "selected_band_set"),
        ("radio", "active_band_set"),
        ("radio", "band_set"),
        ("radio", "configured_band_pool"),
        ("slow", "neighbors"),
        ("slow", "nr_cells"),
        ("rf", "scheduler_unsupported_fields"),
        ("identity", "identity_publication_order"),
        ("capability", "session_lte_bands"),
        ("capability", "session_nr_bands"),
    }
)

MAPPING_VALUE_FIELDS = frozenset(
    {
        ("rf", "scheduler_field_trust"),
        ("slow", "wifi"),
        ("slow", "backend_diagnostic"),
        ("slow", "read_diagnostic"),
        ("slow", "network_settings"),
        ("slow", "radio_mode"),
        ("identity", "sim_network"),
        ("health", "backend_session"),
        ("health", "device_recovery_state"),
        ("health", "session_transition"),
        ("health", "connectivity_receipt"),
        ("health", "startup_live_baseline"),
        ("health", "acquisition_lane_state"),
        ("identity", "decode_catalog_snapshot"),
        ("identity", "access_type_contract"),
        ("identity", "modem_identity_key"),
        ("identity", "modem_live_access_profile"),
        ("identity", "runtime_identity"),
        ("identity", "resolved_identity_card"),
        ("identity", "capability_profile"),
        ("radio", "tx_power_support"),
        ("identity", "static_modem_truth"),
        ("capability", "session_band_capability"),
        ("capability", "runtime_capabilities"),
    }
)

INT_VALUE_FIELDS = frozenset(
    {
        ("radio", "carrier_count"),
        ("radio", "ca_component_count"),
        ("radio", "nr_band"),
        ("radio", "nr_pci"),
        ("radio", "nr_arfcn"),
        ("radio", "nr_cell_id"),
        ("radio", "nr_ri"), ("radio", "nr_cqi"),
        ("radio", "nr_mcs_dl"), ("radio", "nr_mcs_ul"),
        ("radio", "nr_num_rbs"),
        ("radio", "lte_num_rbs"),
        ("slow", "router_cpu_load_pct"), ("slow", "router_memory_used_pct"),
        ("slow", "router_system_uptime_s"),
        ("radio", "serving_band"),
        ("rf", "pci"),
        ("rf", "earfcn_dl"),
        ("rf", "earfcn_ul"),
        ("rf", "cell_id"),
        ("rf", "enb_id"),
        ("rf", "cqi"),
        ("rf", "cqi0"),
        ("rf", "cqi1"),
        ("rf", "mcs_dl"),
        ("rf", "mcs_dl0"),
        ("rf", "mcs_dl1"),
        ("rf", "mcs_ul"),
        ("rf", "ri"),
        ("slow", "neighbor_count"),
        ("traffic", "traffic_day_download_bytes"),
        ("traffic", "traffic_day_upload_bytes"),
        ("traffic", "traffic_day_used_bytes"),
        ("traffic", "traffic_month_download_bytes"),
        ("traffic", "traffic_month_upload_bytes"),
        ("traffic", "traffic_total_download_bytes"),
        ("traffic", "traffic_total_upload_bytes"),
        ("traffic", "traffic_session_download_bytes"),
        ("traffic", "traffic_session_upload_bytes"),
        ("traffic", "traffic_device_download_counter_bytes"),
        ("traffic", "traffic_device_upload_counter_bytes"),
        ("traffic", "traffic_quota_start_day"),
        ("traffic", "traffic_quota_left_bytes"),
        ("traffic", "traffic_quota_limit_bytes"),
        ("identity", "catalog_version"),
    }
)

POSITIVE_FLOAT_VALUE_FIELDS = frozenset(
    {
        ("slow", "dl_freq_mhz"),
        ("slow", "ul_freq_mhz"),
        ("slow", "dl_bandwidth_mhz"),
        ("slow", "ul_bandwidth_mhz"),
        ("slow", "nr_bandwidth_mhz"),
        ("slow", "nr_ul_bandwidth_mhz"), ("slow", "nr_dl_freq_mhz"),
        ("slow", "nr_ul_freq_mhz"),
        ("slow", "nr_scs_khz"),
    }
)


FLOAT_VALUE_FIELDS = frozenset(
    {
        ("radio", "tx_power_dbm"),
        ("radio", "nr_sinr_db"),
        ("radio", "nr_rsrp_dbm"),
        ("radio", "nr_rsrq_db"),
        ("radio", "nr_tx_power_dbm"),
        ("radio", "configured_band_pool_age_s"),
        ("radio", "configured_band_pool_last_sample_ts"),
        ("rf", "sinr_db"),
        ("rf", "rsrp_dbm"),
        ("rf", "rsrq_db"),
        ("rf", "rssi_dbm"),
        ("traffic", "throughput_dl_mbps"),
        ("traffic", "throughput_ul_mbps"),
        ("traffic", "dl_mbps"),
        ("traffic", "ul_mbps"),
        ("traffic", "rx_bytes_per_sec"),
        ("traffic", "tx_bytes_per_sec"),
        ("traffic", "session_uptime_s"),
        ("traffic", "traffic_presence_interval_s"),
        ("traffic", "traffic_current_download_rate_bps"),
        ("traffic", "traffic_current_upload_rate_bps"),
        ("traffic", "traffic_session_duration_s"),
        ("traffic", "traffic_day_duration_s"),
        ("traffic", "traffic_month_duration_s"),
        ("traffic", "traffic_total_duration_s"),
        ("traffic", "traffic_volume_poll_interval_s"),
        ("traffic", "traffic_volume_last_sample_ts"),
        ("traffic", "traffic_volume_next_sample_ts"),
        ("traffic", "traffic_volume_age_s"),
        ("traffic", "traffic_counter_period_started_at"),
        ("traffic", "traffic_counter_interface_started_at"),
        ("traffic", "traffic_quota_month_threshold_pct"),
        ("traffic", "traffic_quota_day_threshold_pct"),
        ("slow", "temperature_modem_5g_c"),
        ("slow", "temperature_cpu_c"),
        ("timing", "modem_sample_interval_s"),
        ("timing", "modem_sample_expected_interval_s"),
        ("timing", "modem_read_ms"),
        ("health", "modem_live_io_quiet_remaining_s"),
    }
)

EXACT_TEXT_VALUE_FIELDS: Mapping[tuple[str, str], frozenset[str]] = MappingProxyType(
    {
        ("radio", "network_type"): CANONICAL_NETWORK_TYPE_TEXT,
        ("radio", "ca_state_text"): CANONICAL_CA_STATE_TEXT,
        ("radio", "nr_mode"): CANONICAL_NR_MODE_TEXT,
        ("radio", "nr_state_text"): CANONICAL_NR_STATE_TEXT,
        ("radio", "nr_qam_dl"): frozenset({"QPSK", "16QAM", "64QAM", "256QAM"}),
        ("radio", "nr_qam_ul"): frozenset({"QPSK", "16QAM", "64QAM", "256QAM"}),
        ("slow", "rrc"): CANONICAL_RRC_TEXT,
        ("health", "radio_state_text"): CANONICAL_RADIO_STATE_TEXT,
    }
)

EXPLICIT_VALUE_CONTRACTS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("radio", "network_type_ex"): "code_text",
        ("radio", "configured_ca_pool_display"): "band_signature",
        ("radio", "ca_source"): "reason_text",
        ("radio", "ca_composition_signature"): "ca_composition_signature",
        ("radio", "tx_power"): "tx_power_text",
        ("radio", "serving_band_unavailable_reason"): "reason_text",
        ("radio", "configured_band_pool_source"): "reason_text",
        ("radio", "configured_band_pool_status"): "reason_text",
        ("rf", "ri_source"): "reason_text",
        ("rf", "scheduler_source"): "reason_text",
        ("rf", "cqi_source"): "reason_text",
        ("traffic", "traffic_state_detail"): "reason_text",
        ("traffic", "traffic_invalid_reason"): "reason_text",
        ("traffic", "traffic_rate_source"): "reason_text",
        ("traffic", "connection_status_code"): "connection_status_code",
        ("traffic", "session_uptime_source"): "reason_text",
        ("traffic", "traffic_accounting_source"): "reason_text",
        ("traffic", "traffic_accounting_status"): "reason_text",
        ("traffic", "traffic_accounting_month_key"): "identifier_text",
        ("traffic", "traffic_accounting_day_key"): "identifier_text",
        ("traffic", "traffic_quota_limit_label"): "quota_label_text",
        ("slow", "tac"): "tac",
        ("slow", "antenna"): "antenna_text",
        ("slow", "wan_ip"): "ip_address",
        ("health", "live_register_hold_reason"): "reason_text",
        ("health", "target_guard_reason"): "reason_text",
        ("identity", "schema"): "identifier_text",
        ("identity", "owner"): "owner_text",
        ("identity", "code_namespace"): "identifier_text",
        ("identity", "identity_code"): "identity_code",
        ("identity", "identity_status"): "identity_status",
        ("identity", "catalog_checksum"): "checksum_text",
        ("identity", "combination_guard"): "checksum_text",
        ("identity", "access_type_id"): "identifier_text",
        ("identity", "access_type_fingerprint"): "checksum_text",
        ("identity", "manufacturer"): "human_text",
        ("identity", "hardware_sku"): "version_text",
        ("identity", "firmware_version"): "version_text",
        ("identity", "backend"): "identifier_text",
        ("identity", "backend_family"): "identifier_text",
        ("identity", "vendor"): "human_text",
        ("identity", "model"): "human_text",
        ("identity", "router_model"): "human_text",
        ("identity", "firmware"): "version_text",
        ("identity", "serial"): "serial_text",
        ("identity", "serial_number"): "serial_text",
        ("identity", "imei"): "imei",
        ("identity", "imsi"): "imsi",
        ("identity", "profile_id"): "identifier_text",
        ("identity", "hardware_version"): "version_text",
        ("identity", "hw_version"): "version_text",
        ("identity", "software_version"): "version_text",
        ("identity", "software"): "version_text",
        ("identity", "webui"): "version_text",
        ("identity", "access_type"): "access_type_text",
        ("identity", "lan_mac"): "mac_address",
        ("identity", "provider_name"): "human_text",
        ("identity", "provider_plmn"): "plmn",
        ("capability", "session_lte_status"): "capability_session_state",
        ("capability", "device_radio_class"): "identifier_text",
        ("capability", "session_nr_status"): "capability_session_state",
        ("capability", "session_capability_comparison"): "reason_text",
        ("capability", "session_capability_id"): "identifier_text",
        ("capability", "pband_control"): "capability_presentation_text",
        ("capability", "ca_pool_control"): "capability_presentation_text",
        ("capability", "band_lock"): "capability_presentation_text",
        ("capability", "cell_lock"): "capability_presentation_text",
        ("capability", "restart"): "capability_presentation_text",
        ("capability", "max_tcp_payload"): "capability_presentation_text",
        ("capability", "ttl_ip"): "capability_presentation_text",
        ("capability", "keepalive_enabled"): "capability_presentation_text",
        ("capability", "dl_256qam"): "capability_presentation_text",
        ("capability", "pci_lock"): "capability_presentation_text",
        ("capability", "ul_64qam"): "capability_presentation_text",
        ("capability", "keepalive"): "capability_presentation_text",
        ("capability", "ue_category"): "capability_ue_category_text",
        ("capability", "ue_info"): "capability_presentation_text",
        ("capability", "traffic_volume"): "capability_presentation_text",
        ("capability", "ca_details"): "capability_presentation_text",
        ("capability", "neighbors_support"): "capability_presentation_text",
        ("capability", "plmn_read"): "capability_presentation_text",
        ("capability", "band_priority"): "capability_presentation_text",
        ("capability", "action_band_select_state"): "capability_proof_state",
        ("capability", "action_band_lock_state"): "capability_proof_state",
        ("capability", "action_cell_lock_state"): "capability_proof_state",
        ("capability", "action_restart_state"): "capability_proof_state",
        ("capability", "action_pband_state"): "capability_proof_state",
        ("capability", "action_nr_band_lock_state"): "capability_proof_state",
        ("capability", "action_wifi_control_state"): "capability_proof_state",
        ("capability", "action_network_settings_state"): "capability_proof_state",
        ("capability", "action_mobile_data_state"): "capability_proof_state",
        ("capability", "action_radio_mode_state"): "capability_proof_state",
        ("capability", "read_traffic_presence_state"): "capability_proof_state",
        ("capability", "read_traffic_volume_state"): "capability_proof_state",
        ("capability", "read_ue_capability_state"): "capability_proof_state",
        ("capability", "read_neighbors_state"): "capability_proof_state",
        ("capability", "read_ca_details_state"): "capability_proof_state",
        ("capability", "read_plmn_state"): "capability_proof_state",
        ("capability", "read_band_priority_state"): "capability_proof_state",
        ("capability", "sms_state"): "capability_proof_state",
    }
)


def _truth_catalog_paths() -> set[tuple[str, str]]:
    return {
        (line, field)
        for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
        for field in fields
    }


def _truth_value_contract_kind(line: str, field: str) -> str:
    if type(line) is not str or type(field) is not str:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_contract_path_type_invalid"
        )
    path = (line, field)
    explicit = EXPLICIT_VALUE_CONTRACTS.get(path)
    if explicit:
        return explicit
    if path in BOOL_VALUE_FIELDS:
        return "bool"
    if path in INT_VALUE_FIELDS:
        return "int"
    if path in POSITIVE_FLOAT_VALUE_FIELDS:
        return "positive_float"
    if path in FLOAT_VALUE_FIELDS:
        return "float"
    if path in LIST_VALUE_FIELDS:
        return "list"
    if path in MAPPING_VALUE_FIELDS:
        return "mapping"
    if path in EXACT_TEXT_VALUE_FIELDS:
        return "exact_text"
    if path == ("slow", "trans_mode"):
        return "transmission_mode"
    if path in {
        ("radio", "ca_signature"),
        ("radio", "ca_band_signature"),
        ("radio", "ca_component_band_signature"),
        ("radio", "serving_band_signature"),
        ("radio", "selected_band_set_signature"),
        ("radio", "active_band_set_signature"),
        ("radio", "band_set_signature"),
        ("radio", "configured_band_pool_signature"),
    }:
        return "band_signature"
    if path in {
        ("radio", "serving_band_unavailable_reason"),
        ("radio", "configured_band_pool_source"),
        ("radio", "configured_band_pool_status"),
        ("rf", "ri_source"),
        ("rf", "scheduler_source"),
        ("rf", "cqi_source"),
        ("traffic", "traffic_state_detail"),
        ("traffic", "traffic_invalid_reason"),
        ("traffic", "traffic_rate_source"),
        ("traffic", "traffic_accounting_source"),
        ("traffic", "traffic_accounting_status"),
        ("health", "live_register_hold_reason"),
        ("health", "target_guard_reason"),
        ("identity", "identity_status"),
    }:
        return "reason_text"
    if path in {
        ("rf", "qam_dl0"),
        ("rf", "qam_dl1"),
        ("rf", "qam_ul"),
    }:
        return "qam"
    return ""


def truth_value_contract_coverage() -> dict[str, list[str]]:
    """Return Matrix fields without a declared value-domain class."""

    missing: list[str] = []
    for line, field in sorted(_truth_catalog_paths()):
        if not _truth_value_contract_kind(line, field):
            missing.append(f"{line}.{field}")
    return {"missing": missing}


def _require_text(value: Any, *, path: str, kind: str) -> str:
    if type(value) is not str or value == "":
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_contract:{path}:{kind}"
        )
    return value


def _validate_truth_value_contract(line: str, field: str, value: Any) -> None:
    path = f"{line}.{field}"
    kind = _truth_value_contract_kind(line, field)
    if line == "slow" and field in {"router_cpu_load_pct", "router_memory_used_pct", "router_system_uptime_s"}:
        if type(value) is not int or value < 0 or (field.endswith("_pct") and value > 100):
            raise ModemLiveTruthMatrixError(f"modem_live_truth_matrix_value_contract:{path}:router_resource")
        return
    if (line, field) == ("slow", "neighbors"):
        if type(value) is not list:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:neighbors"
            )
        allowed = {"pci", "band", "earfcn", "rsrp_dbm", "rsrq_db"}
        for row in value:
            if not isinstance(row, Mapping) or not set(row).issubset(allowed):
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_value_contract:{path}:neighbors"
                )
            pci = row.get("pci")
            if type(pci) is not int or pci < 0 or pci > 1007:
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_value_contract:{path}:neighbors"
                )
            for name in ("band", "earfcn"):
                if name in row and (
                    type(row[name]) is not int or int(row[name]) <= 0
                ):
                    raise ModemLiveTruthMatrixError(
                        f"modem_live_truth_matrix_value_contract:{path}:neighbors"
                    )
            for name, lower in (("rsrp_dbm", -200.0), ("rsrq_db", -100.0)):
                if name in row and (
                    type(row[name]) not in {int, float}
                    or type(row[name]) is bool
                    or not math.isfinite(float(row[name]))
                    or not lower <= float(row[name]) < 0.0
                ):
                    raise ModemLiveTruthMatrixError(
                        f"modem_live_truth_matrix_value_contract:{path}:neighbors"
                    )
        return
    if kind == "bool":
        if type(value) is not bool:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:bool"
            )
        return
    if kind == "int":
        if type(value) is not int:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:int"
            )
        return
    if kind == "float":
        if type(value) not in {int, float} or type(value) is bool or not math.isfinite(float(value)):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:float"
            )
        return
    if kind == "positive_float":
        if (
            type(value) not in {int, float}
            or type(value) is bool
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:positive_float"
            )
        return
    if kind == "list":
        if type(value) is not list:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:list"
            )
        return
    if kind == "mapping":
        if not isinstance(value, Mapping):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:mapping"
            )
        if (line, field) == ("identity", "sim_network"):
            from .sim_identity import validate_sim_network_identity
            try:
                validate_sim_network_identity(dict(value))
            except ValueError as exc:
                raise ModemLiveTruthMatrixError(f"modem_live_truth_matrix_value_contract:{path}:sim_network") from exc
        return
    if kind == "exact_text":
        allowed = EXACT_TEXT_VALUE_FIELDS[(line, field)]
        if type(value) is not str or value not in allowed:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:exact_text"
            )
        return
    if kind == "capability_session_state":
        if type(value) is not str or value not in CANONICAL_CAPABILITY_SESSION_STATE_TEXT:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:capability_session_state"
            )
        return
    if kind == "capability_proof_state":
        if type(value) is not str or value not in CANONICAL_CAPABILITY_PROOF_STATE_TEXT:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:capability_proof_state"
            )
        return
    if kind == "capability_presentation_text":
        if type(value) is not str or value not in CANONICAL_CAPABILITY_PRESENTATION_TEXT:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:capability_presentation_text"
            )
        return
    if kind == "capability_ue_category_text":
        if value in CANONICAL_CAPABILITY_PRESENTATION_TEXT:
            return
        if type(value) is str and re.match(r"^[1-9][0-9]{0,2}$", value):
            return
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_contract:{path}:capability_ue_category_text"
        )
    if kind == "transmission_mode":
        if type(value) is not str or not CANONICAL_TRANSMISSION_MODE_TEXT.match(value):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:transmission_mode"
            )
        return
    if kind == "band_signature":
        if type(value) is not str or (
            value != "" and not CANONICAL_BAND_SIGNATURE_TEXT.match(value)
        ):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:band_signature"
            )
        return
    if kind == "ca_composition_signature":
        if type(value) is not str or (
            value != "" and not CANONICAL_CA_COMPOSITION_SIGNATURE_TEXT.match(value)
        ):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:ca_composition_signature"
            )
        return
    if kind == "reason_text":
        if type(value) is not str or (
            value != "" and not CANONICAL_REASON_TEXT.match(value)
        ):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:reason_text"
            )
        return
    if kind == "qam":
        if type(value) is int:
            return
        if type(value) is str and value in CANONICAL_QAM_TEXT:
            return
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_contract:{path}:qam"
        )
    if kind == "state_text":
        text = _require_text(value, path=path, kind=kind)
        if "\n" in text or len(text) > 80:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:state_text"
            )
        return
    if kind == "identifier_text":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[A-Za-z0-9_.:+/@-]+$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:identifier_text"
            )
        return
    if kind == "owner_text":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[a-z0-9_.:-]+$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:owner_text"
            )
        return
    if kind == "code_text":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[A-Za-z0-9_.:+/-]+$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:code_text"
            )
        return
    if kind == "connection_status_code":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9]{3,8}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:connection_status_code"
            )
        return
    if kind == "identity_key":
        text = _require_text(value, path=path, kind=kind)
        if "|" not in text or len(text) > 512:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:identity_key"
            )
        return
    if kind == "identity_code":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^0x[0-9A-Fa-f]+$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:identity_code"
            )
        return
    if kind == "identity_status":
        if type(value) is not str or value not in {"exact", "provisional", "unknown"}:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:identity_status"
            )
        return
    if kind == "checksum_text":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9a-fA-F]{8,128}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:checksum_text"
            )
        return
    if kind == "human_text":
        text = _require_text(value, path=path, kind=kind)
        if "\n" in text or len(text) > 160:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:human_text"
            )
        return
    if kind == "version_text":
        text = _require_text(value, path=path, kind=kind)
        if "\n" in text or len(text) > 128:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:version_text"
            )
        return
    if kind == "serial_text":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[A-Za-z0-9_.:-]{3,96}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:serial_text"
            )
        return
    if kind == "imei":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9]{14,17}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:imei"
            )
        return
    if kind == "imsi":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9]{5,18}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:imsi"
            )
        return
    if kind == "plmn":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9]{5,6}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:plmn"
            )
        return
    if kind == "mac_address":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:mac_address"
            )
        return
    if kind == "ip_address":
        text = _require_text(value, path=path, kind=kind)
        if not re.match(r"^[0-9A-Fa-f:.]+$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:ip_address"
            )
        return
    if kind == "access_type_text":
        text = _require_text(value, path=path, kind=kind)
        if "\n" in text or len(text) > 160:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:access_type_text"
            )
        return
    if kind == "quota_label_text":
        text = _require_text(value, path=path, kind=kind)
        if "\n" in text or len(text) > 48:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:quota_label_text"
            )
        return
    if kind == "tx_power_text":
        if isinstance(value, Mapping):
            if not value:
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_value_contract:{path}:tx_power_text"
                )
            for key, item in value.items():
                if type(key) is not str or not re.match(
                    r"^[A-Za-z][A-Za-z0-9_ -]{0,31}$",
                    key,
                ):
                    raise ModemLiveTruthMatrixError(
                        f"modem_live_truth_matrix_value_contract:{path}:tx_power_text"
                    )
                if type(item) not in {int, float, str} or type(item) is bool:
                    raise ModemLiveTruthMatrixError(
                        f"modem_live_truth_matrix_value_contract:{path}:tx_power_text"
                    )
                item_text = item if type(item) is str else f"{item}"
                item_text = item_text.strip()
                if not re.match(r"^-?[0-9]+(?:\.[0-9]+)?(?:\s*dBm)?$", item_text):
                    raise ModemLiveTruthMatrixError(
                        f"modem_live_truth_matrix_value_contract:{path}:tx_power_text"
                    )
            return
        text = _require_text(value, path=path, kind=kind)
        single = r"-?[0-9]+(?:\.[0-9]+)?\s*dBm"
        component = r"[A-Za-z][A-Za-z0-9_ -]{0,31}\s*[:=]\s*" + single
        if not re.match(rf"^(?:{single}|{component}(?:\s+{component})*)$", text):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:tx_power_text"
            )
        return
    if kind == "antenna_text":
        if type(value) is not str or value not in {"Internal", "External", "Auto"}:
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_value_contract:{path}:antenna_text"
            )
        return
    if kind == "tac":
        if type(value) is int and value > 0:
            return
        if type(value) is str and re.match(r"^[0-9A-Fa-f]{2,8}$", value):
            return
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_contract:{path}:tac"
        )
    if kind:
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_contract_unknown:{path}:{kind}"
        )
    raise ModemLiveTruthMatrixError(
        f"modem_live_truth_matrix_value_contract_missing:{path}"
    )


class ModemLiveTruthMatrixError(RuntimeError):
    pass


def _is_profile_unsupported_inactive_value(
    line: str,
    field: str,
    value: Any,
    *,
    nr_context_observed: bool = False,
) -> bool:
    """Separate inactive or absent values from profile support evidence."""

    # Zero is a valid NR identity or RF measurement, not an inactive flag.
    # It can override an old unsupported profile only with acquired NR context;
    # LTE-only zero-filled slots still do not establish support.
    if nr_context_observed and line == "radio" and field in {
        "nr_pci", "nr_arfcn", "nr_cell_id", "nr_sinr_db", "nr_rsrq_db",
    } and type(value) in {int, float}:
        return False
    if value is False:
        return True
    if type(value) in {int, float} and type(value) is not bool:
        return float(value) == 0.0
    if type(value) is str:
        return value.strip().upper() in {
            "0",
            "---",
            "INACTIVE",
            "N/A",
            "NA",
            "NO",
            "OFF",
            "UNKNOWN",
            "UNSUPPORTED",
        }
    if isinstance(value, (list, tuple, set, frozenset, Mapping)):
        return not bool(value)
    return False


def _canonical_truth_value(value: Any, *, path: str = "$") -> Any:
    """Copy one normalized Modem Live value that is valid strict JSON.

    Vendor enums, dataclasses, namespaces, tuples, sets, and non-string keys
    belong at the translator input. Matrix must not interpret vendor data again.
    """

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_non_finite_value:{path}"
            )
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_non_string_key:{path}"
                )
            if key in output:
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_key_collision:{path}.{key}"
                )
            output[key] = _canonical_truth_value(
                item,
                path=f"{path}.{key}",
            )
        return output
    if type(value) is list:
        return [
            _canonical_truth_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ModemLiveTruthMatrixError(
        "modem_live_truth_matrix_value_type_unsupported:"
        f"{path}:{type(value).__name__}"
    )


def _assert_canonical_truth_value(value: Any, *, path: str = "$") -> None:
    """Reject Matrix values that still depend on Python-only types."""

    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ModemLiveTruthMatrixError(
                f"modem_live_truth_matrix_non_finite_value:{path}"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise ModemLiveTruthMatrixError(
                    f"modem_live_truth_matrix_non_string_key:{path}"
                )
            _assert_canonical_truth_value(item, path=f"{path}.{key}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _assert_canonical_truth_value(item, path=f"{path}[{index}]")
        return
    raise ModemLiveTruthMatrixError(
        "modem_live_truth_matrix_value_not_canonical:"
        f"{path}:{type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class ModemLiveTruthCell:
    line: str
    field: str
    state: str
    value: Any
    reason: str
    sampled_at: float
    acquired_at: float
    acquisition_attempt_id: str
    source_lane: str
    acquisition_namespace: RuntimeGenerationNamespace | None
    retention: Mapping[str, Any] | None
    line_sequence: int
    source_sequence: int
    source: str

    @property
    def available(self) -> bool:
        return self.state == CELL_AVAILABLE


_TRUTH_RETENTION_KEYS = frozenset(
    {
        "schema",
        "owner",
        "disposition",
        "origin_namespace",
        "destination_namespace",
        "retained_at",
        "device_identity_key",
    }
)


def _require_exact_text(
    value: Any,
    *,
    error: str,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ModemLiveTruthMatrixError(error)
    return value


def _require_exact_positive_int(value: Any, *, error: str) -> int:
    if type(value) is not int or value <= 0:
        raise ModemLiveTruthMatrixError(error)
    return value


def _require_exact_timestamp(
    value: Any,
    *,
    error: str,
    allow_zero: bool = False,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ModemLiveTruthMatrixError(error)
    if value < 0.0 or (not allow_zero and value == 0.0):
        raise ModemLiveTruthMatrixError(error)
    return value


def build_truth_cell_retention_receipt(
    *,
    disposition: str,
    origin_namespace: RuntimeGenerationNamespace,
    destination_namespace: RuntimeGenerationNamespace,
    retained_at: float,
    device_identity_key: str = "",
) -> dict[str, Any]:
    exact_disposition = _require_exact_text(
        disposition,
        error="modem_live_truth_retention_disposition_type_invalid",
    )
    exact_retained_at = _require_exact_timestamp(
        retained_at,
        error="modem_live_truth_retention_timestamp_invalid",
    )
    exact_device_identity_key = _require_exact_text(
        device_identity_key,
        error="modem_live_truth_retention_device_key_type_invalid",
        allow_empty=True,
    )
    receipt = {
        "schema": MODEM_LIVE_TRUTH_RETENTION_SCHEMA,
        "owner": MODEM_LIVE_TRUTH_RETENTION_OWNER,
        "disposition": exact_disposition,
        "origin_namespace": origin_namespace.as_dict(),
        "destination_namespace": destination_namespace.as_dict(),
        "retained_at": exact_retained_at,
        "device_identity_key": exact_device_identity_key,
    }
    _validate_retention_receipt(receipt)
    return receipt


def _validate_retention_receipt(
    value: Mapping[str, Any],
) -> tuple[
    RuntimeGenerationNamespace,
    RuntimeGenerationNamespace,
    str,
    float,
    str,
]:
    if not isinstance(value, Mapping):
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_mapping_required"
        )
    if frozenset(value) != _TRUTH_RETENTION_KEYS:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_keys_invalid"
        )
    if value["schema"] != MODEM_LIVE_TRUTH_RETENTION_SCHEMA:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_schema_invalid"
        )
    if value["owner"] != MODEM_LIVE_TRUTH_RETENTION_OWNER:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_owner_invalid"
        )
    disposition = value["disposition"]
    if type(disposition) is not str or disposition not in RETENTION_DISPOSITIONS:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_disposition_invalid"
        )
    origin = RuntimeGenerationNamespace.from_mapping(
        value["origin_namespace"]
    )
    destination = RuntimeGenerationNamespace.from_mapping(
        value["destination_namespace"]
    )
    retained_at = _require_exact_timestamp(
        value["retained_at"],
        error="modem_live_truth_retention_timestamp_invalid",
    )
    device_identity_key = value["device_identity_key"]
    if type(device_identity_key) is not str:
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_device_key_type_invalid"
        )
    if device_identity_key != device_identity_key.strip():
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_retention_device_key_not_canonical"
        )
    if disposition == RETENTION_CURRENT_NAMESPACE:
        if origin != destination or device_identity_key:
            raise ModemLiveTruthMatrixError(
                "modem_live_truth_current_namespace_retention_invalid"
            )
    else:
        if (
            origin == destination
            or origin.source_epoch != destination.source_epoch
            or origin.lifecycle_generation
            != destination.lifecycle_generation
            or not device_identity_key
        ):
            raise ModemLiveTruthMatrixError(
                "modem_live_truth_same_device_retention_invalid"
            )
    return (
        origin,
        destination,
        disposition,
        retained_at,
        device_identity_key,
    )


def _truth_cell_value_present(line: str, field: str, value: Any) -> bool:
    """Apply the availability rule before publishing a cell."""

    if value is None or value == "":
        return False
    if (line, field) in POSITIVE_FLOAT_VALUE_FIELDS:
        if type(value) in {int, float} and type(value) is not bool:
            # ``ModemInput`` uses zero for several missing optional facts. At
            # this boundary zero means missing. Negative values remain so the
            # positive-value check can reject them.
            return float(value) != 0.0
    return True


def build_truth_line(
    *,
    name: str,
    payload: Mapping[str, Any],
    sampled_at: float,
    line_sequence: int,
    source_sequence: int,
    source: str,
    changed: bool,
    cell_evidence: Mapping[str, Mapping[str, Any]],
    current_namespace: RuntimeGenerationNamespace,
    cell_expectations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    line = _require_exact_text(
        name,
        error="modem_live_truth_line_name_type_invalid",
    )
    if line != line.strip():
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_line_name_not_canonical"
        )
    catalog = MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line)
    if catalog is None:
        raise ModemLiveTruthMatrixError(f"unknown Modem Live truth line: {line}")
    if not isinstance(payload, Mapping):
        raise ModemLiveTruthMatrixError(f"{line} payload mapping required")
    raw_values = dict(payload)
    extra = sorted(set(raw_values) - set(catalog))
    if extra:
        raise ModemLiveTruthMatrixError(
            f"{line} contains undeclared truth cells: {','.join(extra)}"
        )
    timestamp = _require_exact_timestamp(
        sampled_at,
        error=f"{line} sampled_at invalid",
    )
    exact_line_sequence = _require_exact_positive_int(
        line_sequence,
        error=f"{line} line_sequence invalid",
    )
    exact_source_sequence = _require_exact_positive_int(
        source_sequence,
        error=f"{line} source_sequence invalid",
    )
    exact_source = _require_exact_text(
        source,
        error=f"{line} source invalid",
    )
    if exact_source != exact_source.strip():
        raise ModemLiveTruthMatrixError(f"{line} source not canonical")
    if type(changed) is not bool:
        raise ModemLiveTruthMatrixError(
            f"{line} changed type invalid"
        )
    if not isinstance(current_namespace, RuntimeGenerationNamespace):
        raise TypeError("modem_live_truth_line_namespace_required")
    if not isinstance(cell_evidence, Mapping):
        raise ModemLiveTruthMatrixError(
            f"{line} truth cell evidence mapping required"
        )
    evidence_by_field = dict(cell_evidence)
    if set(evidence_by_field) != set(catalog):
        raise ModemLiveTruthMatrixError(
            f"{line} truth cell evidence catalog mismatch"
        )
    expectations = dict(cell_expectations or {})
    if not set(expectations).issubset(set(catalog)):
        raise ModemLiveTruthMatrixError(
            f"{line} truth cell expectation catalog mismatch"
        )
    if any(
        type(value) is not str or value not in PROFILE_CELL_EXPECTATIONS
        for value in expectations.values()
    ):
        raise ModemLiveTruthMatrixError(
            f"{line} truth cell expectation invalid"
        )
    cells: dict[str, dict[str, Any]] = {}
    for field in catalog:
        evidence = evidence_by_field[field]
        if not isinstance(evidence, Mapping):
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} acquisition evidence invalid"
            )
        if frozenset(evidence) != frozenset(
            {
                "acquired_at",
                "acquisition_attempt_id",
                "source_lane",
                "source_sequence",
                "acquisition_namespace",
                "retention",
            }
        ):
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} acquisition evidence keys invalid"
            )
        acquired_at_raw = evidence["acquired_at"]
        if type(acquired_at_raw) is not float:
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} acquisition timestamp type invalid"
            )
        acquired_at = _require_exact_timestamp(
            acquired_at_raw,
            error=f"{line}.{field} acquisition timestamp invalid",
            allow_zero=True,
        )
        attempt_raw = evidence["acquisition_attempt_id"]
        lane_raw = evidence["source_lane"]
        sequence_raw = evidence["source_sequence"]
        if (
            type(attempt_raw) is not str
            or type(lane_raw) is not str
            or type(sequence_raw) is not int
        ):
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} acquisition evidence type invalid"
            )
        if attempt_raw != attempt_raw.strip() or lane_raw != lane_raw.strip():
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} acquisition evidence not canonical"
            )
        acquisition_attempt_id = attempt_raw
        source_lane = lane_raw
        evidence_source_sequence = sequence_raw
        namespace_raw = evidence["acquisition_namespace"]
        acquisition_namespace = (
            None
            if namespace_raw is None
            else RuntimeGenerationNamespace.from_mapping(namespace_raw)
        )
        retention_raw = evidence["retention"]
        retention = None
        if retention_raw is not None:
            (
                retention_origin,
                retention_destination,
                _disposition,
                retained_at,
                _device_key,
            ) = _validate_retention_receipt(retention_raw)
            if (
                acquisition_namespace != retention_origin
                or retention_destination != current_namespace
                or retained_at > timestamp
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} retention evidence mismatch"
                )
            retention = copy.deepcopy(dict(retention_raw))
        present = field in raw_values and _truth_cell_value_present(
            line,
            field,
            raw_values[field],
        )
        canonical_value: Any = None
        quarantine_reason = ""
        if present:
            try:
                canonical_value = _canonical_truth_value(
                    raw_values[field],
                    path=f"$.lines.{line}.{field}",
                )
                _validate_truth_value_contract(line, field, canonical_value)
            except Exception as exc:
                quarantine_reason = (
                    "modem_live_cell_contract_invalid:"
                    f"{type(exc).__name__}:{exc}"
                )
        expectation = expectations.get(field, "")
        explicit_unsupported = bool(
            not quarantine_reason
            and expectation == "unsupported"
            and _is_profile_unsupported_inactive_value(
                line,
                field,
                canonical_value,
                nr_context_observed=(
                    raw_values.get("nr_mode") in {"NSA", "SA"}
                    and acquisition_namespace is not None
                    and cell_evidence.get("nr_mode", {}).get("acquisition_namespace") == namespace_raw
                    and cell_evidence.get("nr_mode", {}).get("acquisition_attempt_id") == acquisition_attempt_id
                ),
            )
        )
        state = (
            CELL_INVALID
            if quarantine_reason
            else (
                CELL_UNSUPPORTED
                if explicit_unsupported
                else (
                    CELL_AVAILABLE
                    if present
                    else (
                        CELL_UNSUPPORTED
                        if expectation == "unsupported"
                        else CELL_MISSING
                    )
                )
            )
        )
        has_acquisition = acquisition_namespace is not None
        if has_acquisition:
            if (
                not math.isfinite(acquired_at)
                or acquired_at <= 0.0
                or acquired_at > timestamp
                or not acquisition_attempt_id
                or not source_lane
                or source_lane == "unattempted"
                or evidence_source_sequence <= 0
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} acquisition evidence incomplete"
                )
            if retention is None and acquisition_namespace != current_namespace:
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} foreign acquisition without retention"
                )
        elif (
            acquired_at != 0.0
            or acquisition_attempt_id
            or source_lane
            or evidence_source_sequence != 0
            or retention is not None
        ):
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} unacquired evidence must be empty"
            )
        if state == CELL_AVAILABLE and not has_acquisition:
            raise ModemLiveTruthMatrixError(
                f"{line}.{field} available without acquisition"
            )
        cells[field] = {
            "schema": MODEM_LIVE_TRUTH_CELL_SCHEMA,
            "line": line,
            "field": field,
            "state": state,
            "value": (
                copy.deepcopy(canonical_value)
                if state == CELL_AVAILABLE
                else None
            ),
            "reason": (
                ""
                if state == CELL_AVAILABLE
                else (
                    quarantine_reason
                    if quarantine_reason
                    else (
                        "modem_live_profile_unsupported"
                        if state == CELL_UNSUPPORTED
                        else (
                            "modem_live_profile_supported_not_observed"
                            if expectation in {"supported", "synthetic"}
                            else (
                                "modem_live_profile_untested_not_observed"
                                if expectation == "untested"
                                else "modem_live_not_observed"
                            )
                        )
                    )
                )
            ),
            "sampled_at": acquired_at,
            "acquired_at": acquired_at,
            "acquisition_attempt_id": acquisition_attempt_id,
            "source_lane": source_lane,
            "acquisition_namespace": (
                acquisition_namespace.as_dict()
                if acquisition_namespace is not None
                else None
            ),
            "retention": retention,
            "line_sequence": exact_line_sequence,
            "source_sequence": evidence_source_sequence,
            "source": source_lane,
        }
    derived_valid = not any(
        cell["state"] == CELL_INVALID
        for cell in cells.values()
    )
    return {
        "schema": MODEM_LIVE_TRUTH_LINE_SCHEMA,
        "owner": MODEM_LIVE_TRUTH_MATRIX_OWNER,
        "name": line,
        "sampled_at": timestamp,
        "line_sequence": exact_line_sequence,
        "source_sequence": exact_source_sequence,
        "source": exact_source,
        "valid": derived_valid,
        "changed": changed,
        "cells": cells,
    }


def build_truth_matrix(
    *,
    source: str,
    sampled_at: float,
    sequence: int,
    source_epoch: str,
    session_scope_key: str,
    lifecycle_generation: int,
    lines: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    exact_source = _require_exact_text(
        source,
        error="modem_live_truth_matrix_source_invalid",
    )
    exact_sampled_at = _require_exact_timestamp(
        sampled_at,
        error="modem_live_truth_matrix_sampled_at_invalid",
    )
    exact_sequence = _require_exact_positive_int(
        sequence,
        error="modem_live_truth_matrix_sequence_invalid",
    )
    exact_source_epoch = _require_exact_text(
        source_epoch,
        error="modem_live_truth_matrix_epoch_required",
    )
    exact_session_scope_key = _require_exact_text(
        session_scope_key,
        error="modem_live_truth_matrix_scope_required",
    )
    exact_lifecycle_generation = _require_exact_positive_int(
        lifecycle_generation,
        error="modem_live_truth_matrix_lifecycle_required",
    )
    if any(
        item != item.strip()
        for item in (exact_source, exact_source_epoch, exact_session_scope_key)
    ):
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_text_not_canonical"
        )
    if not isinstance(lines, Mapping):
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_lines_required"
        )
    matrix = {
        "schema": MODEM_LIVE_TRUTH_MATRIX_SCHEMA,
        "owner": MODEM_LIVE_TRUTH_MATRIX_OWNER,
        "source": exact_source,
        "sampled_at": exact_sampled_at,
        "sequence": exact_sequence,
        "source_epoch": exact_source_epoch,
        "session_scope_key": exact_session_scope_key,
        "lifecycle_generation": exact_lifecycle_generation,
        "lines": _canonical_truth_value(
            dict(lines),
            path="$.lines",
        ),
    }
    matrix["revision"] = _matrix_revision(matrix)
    validate_truth_matrix(matrix)
    return matrix


def validate_truth_matrix(value: Mapping[str, Any]) -> None:
    _assert_canonical_truth_value(value)
    if frozenset(value) != frozenset(
        {
            "schema",
            "owner",
            "source",
            "sampled_at",
            "sequence",
            "source_epoch",
            "session_scope_key",
            "lifecycle_generation",
            "lines",
            "revision",
        }
    ):
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_keys_invalid")
    if type(value["schema"]) is not str or value["schema"] != MODEM_LIVE_TRUTH_MATRIX_SCHEMA:
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_schema_invalid")
    if type(value["owner"]) is not str or value["owner"] != MODEM_LIVE_TRUTH_MATRIX_OWNER:
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_owner_invalid")
    source = _require_exact_text(
        value["source"],
        error="modem_live_truth_matrix_source_invalid",
    )
    sequence = _require_exact_positive_int(
        value["sequence"],
        error="modem_live_truth_matrix_sequence_invalid",
    )
    sampled_at = _require_exact_timestamp(
        value["sampled_at"],
        error="modem_live_truth_matrix_sampled_at_invalid",
    )
    source_epoch = _require_exact_text(
        value["source_epoch"],
        error="modem_live_truth_matrix_epoch_required",
    )
    session_scope_key = _require_exact_text(
        value["session_scope_key"],
        error="modem_live_truth_matrix_scope_required",
    )
    lifecycle_generation = _require_exact_positive_int(
        value["lifecycle_generation"],
        error="modem_live_truth_matrix_lifecycle_required",
    )
    if any(
        item != item.strip()
        for item in (source, source_epoch, session_scope_key)
    ):
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_text_not_canonical"
        )
    current_namespace = RuntimeGenerationNamespace.from_mapping(
        {
            "session_scope_key": session_scope_key,
            "source_epoch": source_epoch,
            "lifecycle_generation": lifecycle_generation,
        }
    )
    lines = value.get("lines")
    if not isinstance(lines, Mapping):
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_lines_required")
    if set(lines) != set(MODEM_LIVE_TRUTH_FIELD_CATALOG):
        missing = sorted(set(MODEM_LIVE_TRUTH_FIELD_CATALOG) - set(lines))
        extra = sorted(set(lines) - set(MODEM_LIVE_TRUTH_FIELD_CATALOG))
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_line_catalog_mismatch:"
            f"missing={','.join(missing)};extra={','.join(extra)}"
        )
    for line, catalog in MODEM_LIVE_TRUTH_FIELD_CATALOG.items():
        row = lines[line]
        if not isinstance(row, Mapping):
            raise ModemLiveTruthMatrixError(f"{line} truth line invalid")
        if frozenset(row) != frozenset(
            {
                "schema",
                "owner",
                "name",
                "sampled_at",
                "line_sequence",
                "source_sequence",
                "source",
                "valid",
                "changed",
                "cells",
            }
        ):
            raise ModemLiveTruthMatrixError(f"{line} truth line keys invalid")
        if (
            type(row["schema"]) is not str
            or row["schema"] != MODEM_LIVE_TRUTH_LINE_SCHEMA
            or type(row["owner"]) is not str
            or row["owner"] != MODEM_LIVE_TRUTH_MATRIX_OWNER
            or type(row["name"]) is not str
            or row["name"] != line
        ):
            raise ModemLiveTruthMatrixError(f"{line} truth line contract invalid")
        row_sampled_at = _require_exact_timestamp(
            row["sampled_at"],
            error=f"{line} truth line sampled_at invalid",
        )
        row_line_sequence = _require_exact_positive_int(
            row["line_sequence"],
            error=f"{line} truth line sequence invalid",
        )
        row_source_sequence = _require_exact_positive_int(
            row["source_sequence"],
            error=f"{line} truth line source sequence invalid",
        )
        row_source = _require_exact_text(
            row["source"],
            error=f"{line} truth line source invalid",
        )
        if (
            row_sampled_at > sampled_at
            or row_source != row_source.strip()
            or type(row["valid"]) is not bool
            or type(row["changed"]) is not bool
        ):
            raise ModemLiveTruthMatrixError(
                f"{line} truth line metadata invalid"
            )
        cells = row.get("cells")
        if not isinstance(cells, Mapping) or set(cells) != set(catalog):
            raise ModemLiveTruthMatrixError(
                f"{line} truth cell catalog mismatch"
            )
        for field in catalog:
            cell = cells[field]
            if not isinstance(cell, Mapping):
                raise ModemLiveTruthMatrixError(f"{line}.{field} cell invalid")
            if frozenset(cell) != frozenset(
                {
                    "schema",
                    "line",
                    "field",
                    "state",
                    "value",
                    "reason",
                    "sampled_at",
                    "acquired_at",
                    "acquisition_attempt_id",
                    "source_lane",
                    "acquisition_namespace",
                    "retention",
                    "line_sequence",
                    "source_sequence",
                    "source",
                }
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} cell keys invalid"
                )
            if (
                type(cell["schema"]) is not str
                or cell["schema"] != MODEM_LIVE_TRUTH_CELL_SCHEMA
                or type(cell["line"]) is not str
                or cell["line"] != line
                or type(cell["field"]) is not str
                or cell["field"] != field
                or type(cell["state"]) is not str
                or cell["state"] not in CELL_STATES
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} cell contract invalid"
                )
            state = cell["state"]
            reason = cell["reason"]
            if type(reason) is not str:
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} cell reason type invalid"
                )
            if state == CELL_AVAILABLE:
                if cell["value"] is None or reason != "":
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} available state inconsistent"
                    )
                _validate_truth_value_contract(line, field, cell["value"])
            elif state == CELL_MISSING:
                if cell["value"] is not None or reason not in {
                    "modem_live_not_observed",
                    "modem_live_profile_supported_not_observed",
                    "modem_live_profile_untested_not_observed",
                }:
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} missing state inconsistent"
                    )
            elif state == CELL_UNSUPPORTED:
                if (
                    cell["value"] is not None
                    or reason != "modem_live_profile_unsupported"
                ):
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} unsupported state inconsistent"
                    )
            elif (
                cell["value"] is not None
                or not reason
                or not reason.startswith("modem_live_cell_contract_invalid:")
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} invalid state inconsistent"
                )
            if type(cell["line_sequence"]) is not int or cell["line_sequence"] != row_line_sequence:
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} line sequence mismatch"
                )
            if type(cell["sampled_at"]) is not float or type(cell["acquired_at"]) is not float:
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} acquisition timestamp type invalid"
                )
            if cell["sampled_at"] != cell["acquired_at"]:
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} acquisition timestamp mismatch"
                )
            acquired_at_raw = cell["acquired_at"]
            if (
                type(acquired_at_raw) is not float
                or type(cell["acquisition_attempt_id"]) is not str
                or type(cell["source_lane"]) is not str
                or type(cell["source_sequence"]) is not int
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} acquisition evidence type invalid"
                )
            acquired_at = acquired_at_raw
            namespace_raw = cell["acquisition_namespace"]
            acquisition_namespace = (
                None
                if namespace_raw is None
                else RuntimeGenerationNamespace.from_mapping(namespace_raw)
            )
            retention_raw = cell["retention"]
            if acquisition_namespace is None:
                if (
                    acquired_at != 0.0
                    or cell["acquisition_attempt_id"]
                    or cell["source_lane"]
                    or cell["source_sequence"] != 0
                    or retention_raw is not None
                    or cell["state"] == CELL_AVAILABLE
                ):
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} unacquired evidence invalid"
                    )
                continue
            if (
                not math.isfinite(acquired_at)
                or acquired_at <= 0.0
                or acquired_at > row_sampled_at
                or not cell["acquisition_attempt_id"].strip()
                or not cell["source_lane"].strip()
                or cell["source_lane"] == "unattempted"
                or cell["source_sequence"] <= 0
            ):
                raise ModemLiveTruthMatrixError(
                    f"{line}.{field} acquisition evidence invalid"
                )
            if retention_raw is None:
                if acquisition_namespace != current_namespace:
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} acquisition namespace mismatch"
                    )
            else:
                (
                    origin,
                    destination,
                    _disposition,
                    retained_at,
                    _device_key,
                ) = _validate_retention_receipt(retention_raw)
                if (
                    origin != acquisition_namespace
                    or destination != current_namespace
                    or retained_at > row_sampled_at
                ):
                    raise ModemLiveTruthMatrixError(
                        f"{line}.{field} retention namespace mismatch"
                    )
        derived_valid = not any(
            cell["state"] == CELL_INVALID
            for cell in cells.values()
        )
        if row["valid"] is not derived_valid:
            raise ModemLiveTruthMatrixError(
                f"{line} truth line valid state inconsistent"
            )
    revision = value["revision"]
    if type(revision) is not str:
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_revision_type_invalid")
    if not revision or revision != _matrix_revision(value):
        raise ModemLiveTruthMatrixError("modem_live_truth_matrix_revision_invalid")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTruthMatrixReader(Mapping[str, Any]):
    """Validated detached reader for one Matrix generation."""

    _matrix: Mapping[str, Any]
    _identity: MatrixGenerationIdentity

    def __init__(self, matrix: Mapping[str, Any]) -> None:
        detached = copy.deepcopy(dict(matrix))
        validate_truth_matrix(detached)
        object.__setattr__(self, "_matrix", MappingProxyType(detached))
        object.__setattr__(
            self,
            "_identity",
            MatrixGenerationIdentity.from_truth_matrix(detached),
        )

    @classmethod
    def from_owner_built_matrix(
        cls,
        matrix: dict[str, Any],
    ) -> "ValidatedTruthMatrixReader":
        """Use a Matrix already validated by its builder.

        This is the zero-copy route used by Modem Live after
        ``build_truth_matrix`` returns. External mappings use the validating
        constructor.
        """

        if type(matrix) is not dict:
            raise TypeError("owner_built_truth_matrix_dict_required")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_matrix", MappingProxyType(matrix))
        object.__setattr__(
            instance,
            "_identity",
            MatrixGenerationIdentity.from_truth_matrix(matrix),
        )
        return instance

    @property
    def identity(self) -> MatrixGenerationIdentity:
        return self._identity

    def __getitem__(self, name: str) -> Any:
        return copy.deepcopy(self._matrix[name])

    def __iter__(self):
        return iter(self._matrix)

    def __len__(self) -> int:
        return len(self._matrix)

    def metadata(self, name: str) -> Any:
        if name not in {
            "owner",
            "revision",
            "sampled_at",
            "schema",
            "sequence",
            "session_scope_key",
            "source",
            "source_epoch",
            "lifecycle_generation",
        }:
            raise ModemLiveTruthMatrixError(
                f"truth matrix metadata unavailable: {name}"
            )
        return copy.deepcopy(self._matrix.get(name))

    def to_mapping(self) -> dict[str, Any]:
        """Return a detached strict-JSON copy of this generation."""

        return copy.deepcopy(dict(self._matrix))

    def cell(self, line: str, field: str) -> ModemLiveTruthCell:
        if type(line) is not str or type(field) is not str:
            raise ModemLiveTruthMatrixError(
                "truth cell path type invalid"
            )
        row = self._matrix["lines"].get(line)
        if not isinstance(row, Mapping):
            raise ModemLiveTruthMatrixError(f"truth line unavailable: {line}")
        raw = row["cells"].get(field)
        if not isinstance(raw, Mapping):
            raise ModemLiveTruthMatrixError(
                f"truth cell unavailable: {line}.{field}"
            )
        return ModemLiveTruthCell(
            line=line,
            field=field,
            state=raw["state"],
            value=copy.deepcopy(raw.get("value")),
            reason=raw["reason"],
            sampled_at=raw["sampled_at"],
            acquired_at=raw["acquired_at"],
            acquisition_attempt_id=raw["acquisition_attempt_id"],
            source_lane=raw["source_lane"],
            acquisition_namespace=(
                None
                if raw["acquisition_namespace"] is None
                else RuntimeGenerationNamespace.from_mapping(
                    raw["acquisition_namespace"]
                )
            ),
            retention=(
                None
                if raw["retention"] is None
                else MappingProxyType(
                    copy.deepcopy(dict(raw["retention"]))
                )
            ),
            line_sequence=raw["line_sequence"],
            source_sequence=raw["source_sequence"],
            source=raw["source"],
        )

    def line_values(
        self,
        line: str,
        *,
        available_only: bool = True,
    ) -> dict[str, Any]:
        if type(line) is not str:
            raise ModemLiveTruthMatrixError("truth line path type invalid")
        row = self._matrix["lines"].get(line)
        if not isinstance(row, Mapping):
            raise ModemLiveTruthMatrixError(f"truth line unavailable: {line}")
        output: dict[str, Any] = {}
        for field, raw in row["cells"].items():
            if available_only and raw["state"] != CELL_AVAILABLE:
                continue
            output[field] = copy.deepcopy(raw.get("value"))
        return output

    def line_metadata(self, line: str) -> dict[str, Any]:
        """Return copied generation metadata for one Matrix line."""

        if type(line) is not str:
            raise ModemLiveTruthMatrixError("truth line path type invalid")
        row = self._matrix["lines"].get(line)
        if not isinstance(row, Mapping):
            raise ModemLiveTruthMatrixError(f"truth line unavailable: {line}")
        return {
            name: copy.deepcopy(row[name])
            for name in (
                "schema",
                "owner",
                "name",
                "sampled_at",
                "line_sequence",
                "source_sequence",
                "source",
                "valid",
                "changed",
            )
        }

    def nsa_snapshot(self) -> dict[str, Any]:
        """Return the Modem Live semantic view of this Matrix generation."""

        from .nsa_snapshot import NsaPhysicalSnapshot

        return NsaPhysicalSnapshot.from_matrix(self).as_dict()


def _matrix_revision(value: Mapping[str, Any]) -> str:
    material = _canonical_truth_value({
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key != "revision"
    })
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def truth_value_fingerprint(value: Any) -> str:
    """Return a deterministic fingerprint for one raw value."""

    canonical = _canonical_truth_value(value)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_truth_cell_value(
    line: str,
    field: str,
    value: Any,
) -> Any:
    """Validate and copy one value under the Matrix contract."""

    if field not in MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line, ()):
        raise ModemLiveTruthMatrixError(
            "modem_live_truth_matrix_contract_path_unknown"
        )
    if not _truth_cell_value_present(line, field, value):
        raise ModemLiveTruthMatrixError(
            f"modem_live_truth_matrix_value_unavailable:{line}.{field}"
        )
    canonical = _canonical_truth_value(
        value,
        path=f"$.lines.{line}.{field}",
    )
    _validate_truth_value_contract(line, field, canonical)
    return copy.deepcopy(canonical)


__all__ = (
    "CELL_AVAILABLE",
    "CELL_INVALID",
    "CELL_MISSING",
    "CELL_UNSUPPORTED",
    "MODEM_LIVE_NON_MEASUREMENT_CELL_ADDRESSES",
    "MODEM_LIVE_TRUTH_FIELD_CATALOG",
    "MODEM_LIVE_TRUTH_MATRIX_OWNER",
    "MODEM_LIVE_TRUTH_MATRIX_SCHEMA",
    "ModemLiveTruthCell",
    "ModemLiveTruthMatrixError",
    "ValidatedTruthMatrixReader",
    "build_truth_line",
    "build_truth_cell_retention_receipt",
    "build_truth_matrix",
    "truth_value_fingerprint",
    "validate_truth_cell_value",
    "validate_truth_matrix",
)
