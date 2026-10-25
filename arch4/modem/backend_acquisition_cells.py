"""Explicit canonical Matrix-cell sets proven by backend acquisition lanes."""

from __future__ import annotations

from .backend_acquisition import matrix_cells


FAST_RADIO_CELLS = matrix_cells(
    "radio",
    (
        "network_type",
        "carrier_count",
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
        "nr_qam_dl", "nr_qam_ul", "nr_tx_power_dbm", "nr_num_rbs",
        "nr_mode",
        "nr_state_text",
        "serving_band",
        "serving_band_signature",
        "serving_band_available",
        "serving_band_unavailable_reason",
        "active_band_set",
        "active_band_set_signature",
        "band_set",
        "band_set_signature",
    ),
)

FAST_RF_CELLS = matrix_cells(
    "rf",
    (
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
)

FAST_TRAFFIC_CELLS = matrix_cells(
    "traffic",
    (
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
    ),
)

# Local session/guard/publication state, not physical radio or LTE-interface
# observations. Physical health can only be certified by its backend receipt.
DERIVED_HEALTH_CELLS = matrix_cells(
    "health",
    (
        "target_guard_ok",
        "target_guard_reason",
        "backend_session",
        "session_transition",
        "connectivity_receipt",
        "acquisition_lane_state",
    ),
)

DEVICE_RECOVERY_CELLS = matrix_cells(
    "health",
    (
        "device_recovery_state",
    ),
)

TIMING_CELLS = matrix_cells(
    "timing",
    (
        "modem_comm_alive",
        "modem_sample_interval_s",
        "modem_sample_expected_interval_s",
        "modem_sample_slo_exceeded",
        "modem_read_ms",
    ),
)

SLOW_CELLS = matrix_cells(
    "slow",
    (
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
        "antenna",
        "wan_ip",
        "wifi",
        "wifi_enabled",
        "wifi_24ghz_enabled",
        "wifi_5ghz_enabled",
        "wifi_6ghz_enabled",
        "backend_diagnostic",
        "read_diagnostic",
    ),
)

IDENTITY_CELLS = matrix_cells(
    "identity",
    tuple(
        (
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
        )
    ),
)

CONFIGURED_BAND_CELLS = matrix_cells(
    "radio",
    (
        "carrier_count",
        "configured_ca_pool_display",
        "ca_signature",
        "ca_band_signature",
        "selected_band_set",
        "selected_band_set_signature",
        "configured_band_pool",
        "configured_band_pool_source",
        "configured_band_pool_signature",
        "configured_band_pool_status",
        "configured_band_pool_age_s",
        "configured_band_pool_last_sample_ts",
    ),
)

TRAFFIC_VOLUME_CELLS = matrix_cells(
    "traffic",
    (
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
)

FAST_COMMON_CELLS = (
    FAST_RADIO_CELLS
    | FAST_RF_CELLS
    | FAST_TRAFFIC_CELLS
)
