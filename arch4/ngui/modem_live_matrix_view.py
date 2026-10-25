"""NGUI addresses in the Modem Live truth matrix.

This is only a read-only view. It does not own values, aliases, normalization,
or hold rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from arch4.core_contracts.current_state import CoreCurrentFieldState
from arch4.modem_live.truth_matrix import (
    MODEM_LIVE_DEVICE_FACT_ADDRESSES,
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
)


MODEM_LIVE_NGUI_CELL_ADDRESSES: dict[str, tuple[str, ...]] = {
    **MODEM_LIVE_DEVICE_FACT_ADDRESSES,
    "network.settings": ("slow", "network_settings"),
    "read.diagnostic": ("slow", "read_diagnostic"),
    "network.diagnostic": ("slow", "backend_diagnostic"),
    "sim.network": ("identity", "sim_network"),
    "runtime.sequence": ("$matrix", "sequence"),
    "radio.network_type": ("radio", "network_type"),
    "radio.ca_active": ("radio", "ca_active"),
    "radio.carrier_details": ("radio", "carrier_details"),
    "radio.carrier_count": ("radio", "carrier_count"),
    "radio.active_bands": ("radio", "active_band_set"),
    "radio.frequency_set": ("radio", "configured_band_pool"),
    "rf.sinr_db": ("rf", "sinr_db"),
    "rf.rsrp_dbm": ("rf", "rsrp_dbm"),
    "rf.rsrq_db": ("rf", "rsrq_db"),
    "rf.rssi_dbm": ("rf", "rssi_dbm"),
    "rf.tx_power": ("radio", "tx_power"),
    "rf.tx_power_support": ("radio", "tx_power_support"),
    **{f"rf.{field}_support": ("rf", field) for field in ("cqi1", "mcs_dl1", "qam_dl1")},
    "nr.mode": ("radio", "nr_mode"),
    "nr.sinr_db": ("radio", "nr_sinr_db"),
    "nr.rsrp_dbm": ("radio", "nr_rsrp_dbm"),
    "nr.rsrq_db": ("radio", "nr_rsrq_db"),
    "nr.cells": ("slow", "nr_cells"),
    "nr.nsa_active": ("radio", "nsa_active"),
    "nr.sa_active": ("radio", "sa_active"),
    "timing.modem_sample_interval_s": ("timing", "modem_sample_interval_s"),
    "timing.modem_read_ms": ("timing", "modem_read_ms"),
    "radio.rrc": ("slow", "rrc"),
    "radio.transmission_mode": ("slow", "trans_mode"),
    "radio.dl_bandwidth_mhz": ("slow", "dl_bandwidth_mhz"),
    "radio.ul_bandwidth_mhz": ("slow", "ul_bandwidth_mhz"),
    "radio.dl_frequency_mhz": ("slow", "dl_freq_mhz"),
    "radio.ul_frequency_mhz": ("slow", "ul_freq_mhz"),
    "radio.neighbors": ("slow", "neighbors"),
    "radio.neighbor_count": ("slow", "neighbor_count"),
    "device.temperature_5g_c": ("slow", "temperature_modem_5g_c"),
    "device.temperature_cpu_c": ("slow", "temperature_cpu_c"),
    "device.router_cpu_load_pct": ("slow", "router_cpu_load_pct"),
    "device.router_memory_used_pct": ("slow", "router_memory_used_pct"),
    "device.router_system_uptime_s": ("slow", "router_system_uptime_s"),
    "nr.bandwidth_mhz": ("slow", "nr_bandwidth_mhz"),
    "nr.ri": ("radio", "nr_ri"),
    "nr.cqi": ("radio", "nr_cqi"),
    "nr.mcs_dl": ("radio", "nr_mcs_dl"),
    "nr.mcs_ul": ("radio", "nr_mcs_ul"),
    "nr.qam_dl": ("radio", "nr_qam_dl"),
    "nr.qam_ul": ("radio", "nr_qam_ul"),
    "nr.num_rbs": ("radio", "nr_num_rbs"),
    "radio.lte_num_rbs": ("radio", "lte_num_rbs"),
    "nr.tx_power_dbm": ("radio", "nr_tx_power_dbm"),
    "nr.ul_bandwidth_mhz": ("slow", "nr_ul_bandwidth_mhz"),
    "nr.dl_frequency_mhz": ("slow", "nr_dl_freq_mhz"),
    "nr.ul_frequency_mhz": ("slow", "nr_ul_freq_mhz"),
    "device.imsi": ("identity", "imsi"),
    "device.hardware_version": ("identity", "hardware_version"),
    "device.backend": ("identity", "backend"),
    "device.backend_family": ("identity", "backend_family"),
    "provider.plmn": ("identity", "provider_plmn"),
    "profile.detected_model": ("identity", "model"),
    "profile.profile_id": ("identity", "profile_id"),
    "device.modem_status": ("traffic", "packet_session_connected"),
    "capability.session_lte_status": (
        "capability",
        "session_lte_status",
    ),
    "capability.session_lte_bands": (
        "capability",
        "session_lte_bands",
    ),
    "capability.session_capability_comparison": (
        "capability",
        "session_capability_comparison",
    ),
    "capability.session_capability_id": (
        "capability",
        "session_capability_id",
    ),
    "capability.sms_supported": ("capability", "sms_supported"),
    "traffic.dl_mbps": ("traffic", "dl_mbps"),
    "traffic.ul_mbps": ("traffic", "ul_mbps"),
    "traffic.rx_bytes_per_s": ("traffic", "rx_bytes_per_sec"),
    "traffic.tx_bytes_per_s": ("traffic", "tx_bytes_per_sec"),
    "traffic.has_traffic": ("traffic", "has_traffic"),
    "traffic.valid": ("traffic", "traffic_valid"),
    "traffic.state_detail": ("traffic", "traffic_state_detail"),
    "traffic.invalid_reason": ("traffic", "traffic_invalid_reason"),
    "traffic.session_uptime_s": ("traffic", "session_uptime_s"),
    "traffic.session_download_bytes": (
        "traffic",
        "traffic_session_download_bytes",
    ),
    "traffic.session_upload_bytes": (
        "traffic",
        "traffic_session_upload_bytes",
    ),
}


CGF_NGUI_REQUIRED_FIRST_MATRIX_LINES: tuple[str, ...] = tuple(
    MODEM_LIVE_TRUTH_FIELD_CATALOG
)


def ngui_matrix_readiness_violation(matrix: Mapping[str, Any]) -> str:
    del matrix
    raise RuntimeError(
        "ngui_raw_matrix_readiness_retired_use_modem_live_ready_receipt"
    )


def modem_live_matrix_line_for_source(source_id: str) -> str:
    address = MODEM_LIVE_NGUI_CELL_ADDRESSES.get(str(source_id))
    return str(address[0]) if address else ""


@dataclass(frozen=True, slots=True)
class MatrixSourceValue:
    value: Any
    valid: bool
    sampled_at: float
    sample_sequence: int
    reason: str
    fact_state: CoreCurrentFieldState


def source_value_from_matrix(
    matrix: Any,
    source_id: str,
) -> MatrixSourceValue | None:
    del matrix, source_id
    raise RuntimeError(
        "ngui_raw_matrix_reader_retired_use_accepted_root_current_facts"
    )


__all__ = (
    "CGF_NGUI_REQUIRED_FIRST_MATRIX_LINES",
    "MODEM_LIVE_NGUI_CELL_ADDRESSES",
    "MatrixSourceValue",
    "modem_live_matrix_line_for_source",
    "ngui_matrix_readiness_violation",
    "source_value_from_matrix",
)
