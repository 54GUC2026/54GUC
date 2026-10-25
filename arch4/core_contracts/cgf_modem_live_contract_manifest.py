"""Executable Wave-0 contract manifest for the CGF Modem Live boundary.

This module is declarative only.  It intentionally does not participate in
runtime composition.  Later architecture-replacement waves use these immutable
rows as the exact migration checklist and anti-reintroduction vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MODEM_LIVE_MATRIX_OWNER = "54guc.modem_live.truth_matrix"
MODEM_LIVE_PHYSICAL_IO_OWNER = "54guc.modem_live"


@dataclass(frozen=True, slots=True)
class MatrixLineContract:
    """One immutable Matrix line and its complete current cell population."""

    name: str
    owner: str
    producer: str
    consumer_classes: tuple[str, ...]
    clock_event_source: str
    stale_invalid_policy: str
    evidence_artifacts: tuple[str, ...]
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatrixCellContract:
    """One exact cell row inheriting its line's declared boundary contract."""

    identity: str
    line: str
    field: str
    owner: str
    producer: str
    consumer_classes: tuple[str, ...]
    clock_event_source: str
    stale_invalid_policy: str
    evidence_artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsumerClassContract:
    """One legal class of Matrix reader."""

    identity: str
    owner: str
    boundary: str
    permission: str
    may_contact_modem: bool
    may_mutate_matrix: bool


@dataclass(frozen=True, slots=True)
class IoBoundaryContract:
    """One physical or adjacent I/O category and its sole legal owner."""

    identity: str
    owner: str
    boundary: str
    modem_contact: bool
    may_write_modem: bool
    result_contract: str


@dataclass(frozen=True, slots=True)
class DurableWriterRule:
    """One explicit durable-write permission or prohibition."""

    identity: str
    owner: str
    allowed: bool
    bounded_required: bool
    mechanism: str
    evidence_artifact: str


@dataclass(frozen=True, slots=True)
class StartupReadinessContract:
    """One mandatory startup/profile contract category."""

    identity: str
    owner: str
    requirement: str
    failure_state: str
    blocks_page_ready: bool
    blocks_monitor_process: bool


@dataclass(frozen=True, slots=True)
class DisplacedPathContract:
    """One path declared illegal by the manual architecture audit."""

    identity: str
    locations: tuple[str, ...]
    competing_meaning: str
    canonical_replacement: str
    disposition: str
    implementation_wave: str


MODEM_LIVE_MATRIX_LINES: Mapping[str, MatrixLineContract] = MappingProxyType(
    {
        "radio": MatrixLineContract(
            name="radio",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "core_command_authority",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted fast sample or radio-state event",
            stale_invalid_policy=(
                "invalid cells cannot confirm a target; missing and stale remain explicit"
            ),
            evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
            cells=(
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
                "nr_ri",
                "nr_cqi",
                "nr_mcs_dl",
                "nr_mcs_ul",
                "nr_qam_dl",
                "nr_qam_ul",
                "nr_tx_power_dbm",
                "nr_num_rbs",
                "lte_num_rbs",
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
        ),
        "rf": MatrixLineContract(
            name="rf",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "core_signal_compute",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted fast RF sample",
            stale_invalid_policy=(
                "missing or invalid RF cells cannot be reused as a new sample"
            ),
            evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
            cells=(
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
        ),
        "traffic": MatrixLineContract(
            name="traffic",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "foundation_data_budget",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted fast or traffic-volume sample",
            stale_invalid_policy=(
                "rate, counters, validity and reason remain one Matrix generation"
            ),
            evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
            cells=(
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
        ),
        "slow": MatrixLineContract(
            name="slow",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "foundation_current_state",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted slow or maintenance sample",
            stale_invalid_policy=(
                "missing and unsupported remain explicit and cannot imply action outcome"
            ),
            evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
            cells=(
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
                "nr_ul_bandwidth_mhz",
                "nr_dl_freq_mhz",
                "nr_ul_freq_mhz",
                "nr_scs_khz",
                "nr_cells",
                "temperature_modem_5g_c",
                "temperature_cpu_c",
                "router_cpu_load_pct",
                "router_memory_used_pct",
                "router_system_uptime_s",
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
        ),
        "timing": MatrixLineContract(
            name="timing",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "foundation_health_and_evidence",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "validation_and_proof",
            ),
            clock_event_source="accepted fast sample",
            stale_invalid_policy=(
                "missing timing fails timing proof without blocking sibling truth"
            ),
            evidence_artifacts=("current_state.json", "source_soak_manifest.json"),
            cells=(
                "modem_comm_alive",
                "modem_sample_interval_s",
                "modem_sample_expected_interval_s",
                "modem_sample_slo_exceeded",
                "modem_read_ms",
            ),
        ),
        "health": MatrixLineContract(
            name="health",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator",
            consumer_classes=(
                "core_acceptance",
                "foundation_health_and_evidence",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "validation_and_proof",
            ),
            clock_event_source="accepted sample or Modem Live state event",
            stale_invalid_policy=(
                "communication failures are explicit and isolated from valid sibling lines"
            ),
            evidence_artifacts=("current_state.json", "runtime_health.json"),
            cells=(
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
        ),
        "identity": MatrixLineContract(
            name="identity",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live canonical translator and identity matrix",
            consumer_classes=(
                "core_acceptance",
                "foundation_current_state",
                "foundation_health_and_evidence",
                "foundation_license_identity",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted identity or session event",
            stale_invalid_policy=(
                "serial or IMEI gates completed login and action admission, not monitor survival"
            ),
            evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
            cells=(
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
        ),
        "capability": MatrixLineContract(
            name="capability",
            owner=MODEM_LIVE_MATRIX_OWNER,
            producer="Modem Live effective session capability matrix",
            consumer_classes=(
                "core_acceptance",
                "core_command_authority",
                "foundation_current_state",
                "modem_live_profile_scout",
                "modem_live_capability_report",
                "ngui_runtime_projection",
                "future_intelligence_observation",
                "validation_and_proof",
            ),
            clock_event_source="accepted session capability or profile event",
            stale_invalid_policy=(
                "probe unavailability cannot erase seeded or proven effective capability"
            ),
            evidence_artifacts=("current_state.json", "scout_capability_evidence.jsonl"),
            cells=(
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
        ),
    }
)


MODEM_LIVE_MATRIX_CELLS: Mapping[str, MatrixCellContract] = MappingProxyType(
    {
        f"{line_name}.{field}": MatrixCellContract(
            identity=f"{line_name}.{field}",
            line=line_name,
            field=field,
            owner=line_contract.owner,
            producer=line_contract.producer,
            consumer_classes=line_contract.consumer_classes,
            clock_event_source=line_contract.clock_event_source,
            stale_invalid_policy=line_contract.stale_invalid_policy,
            evidence_artifacts=line_contract.evidence_artifacts,
        )
        for line_name, line_contract in MODEM_LIVE_MATRIX_LINES.items()
        for field in line_contract.cells
    }
)


MODEM_LIVE_LEGAL_CONSUMER_CLASSES: Mapping[
    str, ConsumerClassContract
] = MappingProxyType(
    {
        "core_acceptance": ConsumerClassContract(
            identity="core_acceptance",
            owner="54guc.core.accepted_generation",
            boundary="ValidatedTruthMatrixReader",
            permission="read and atomically accept one immutable generation",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "core_signal_compute": ConsumerClassContract(
            identity="core_signal_compute",
            owner="54guc.core.signal_compute",
            boundary="accepted Matrix observation",
            permission="derive current operational signals",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "core_command_authority": ConsumerClassContract(
            identity="core_command_authority",
            owner="54guc.core.command_authority",
            boundary="accepted Matrix target-readback receipt",
            permission="admit and settle commands from physical readback",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "foundation_current_state": ConsumerClassContract(
            identity="foundation_current_state",
            owner="54guc.foundation.current_state",
            boundary="validated Matrix snapshot",
            permission="project declared current-state fields",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "foundation_data_budget": ConsumerClassContract(
            identity="foundation_data_budget",
            owner="54guc.foundation.data_budget",
            boundary="validated raw traffic counter cells",
            permission="calculate and persist operator traffic accounting",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "foundation_health_and_evidence": ConsumerClassContract(
            identity="foundation_health_and_evidence",
            owner="54guc.foundation.supervision",
            boundary="validated Matrix health/evidence snapshot",
            permission="supervise and preserve bounded operational evidence",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "foundation_license_identity": ConsumerClassContract(
            identity="foundation_license_identity",
            owner="54guc.foundation.license_service",
            boundary="validated Matrix permanent-device identity",
            permission="bind license scope to serial or IMEI",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "modem_live_profile_scout": ConsumerClassContract(
            identity="modem_live_profile_scout",
            owner="54guc.modem_live.scout",
            boundary="validated Matrix plus translator unknown-token receipt",
            permission="record differences and propose profile repair",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "modem_live_capability_report": ConsumerClassContract(
            identity="modem_live_capability_report",
            owner="54guc.modem_live.capability_report",
            boundary="validated Matrix projection",
            permission="format report facts without inference or repair",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "ngui_runtime_projection": ConsumerClassContract(
            identity="ngui_runtime_projection",
            owner="54guc.ngui.presentation",
            boundary="accepted generation runtime frame",
            permission="render declared canonical values",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "future_intelligence_observation": ConsumerClassContract(
            identity="future_intelligence_observation",
            owner="54guc.new_horizon.context_intake",
            boundary="typed optional-customer observation port",
            permission="read accepted observations after attachment",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
        "validation_and_proof": ConsumerClassContract(
            identity="validation_and_proof",
            owner="54guc.cgf.validation",
            boundary="detached validated Matrix copy",
            permission="validate contracts without becoming runtime truth",
            may_contact_modem=False,
            may_mutate_matrix=False,
        ),
    }
)


MODEM_LIVE_IO_BOUNDARIES: Mapping[str, IoBoundaryContract] = MappingProxyType(
    {
        "vendor_protocol_read": IoBoundaryContract(
            identity="vendor_protocol_read",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="protected vendor protocol adapter read lane",
            modem_contact=True,
            may_write_modem=False,
            result_contract="typed raw vendor observation",
        ),
        "vendor_protocol_write": IoBoundaryContract(
            identity="vendor_protocol_write",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="protected physical command executor",
            modem_contact=True,
            may_write_modem=True,
            result_contract="typed physical dispatch receipt",
        ),
        "session_authentication": IoBoundaryContract(
            identity="session_authentication",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="protected access-profile session lane",
            modem_contact=True,
            may_write_modem=True,
            result_contract="typed session/authentication receipt",
        ),
        "post_write_readback": IoBoundaryContract(
            identity="post_write_readback",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="protected priority readback lane",
            modem_contact=True,
            may_write_modem=False,
            result_contract="raw observation translated into a Matrix generation",
        ),
        "diagnostic_probe": IoBoundaryContract(
            identity="diagnostic_probe",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="named read-only diagnostic adapter",
            modem_contact=True,
            may_write_modem=False,
            result_contract="typed diagnostic receipt excluded from current truth",
        ),
        "capability_discovery_probe": IoBoundaryContract(
            identity="capability_discovery_probe",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            boundary="protected Scout discovery read lane",
            modem_contact=True,
            may_write_modem=False,
            result_contract="bounded profile-difference or unknown-token evidence",
        ),
        "external_host_rtt_probe": IoBoundaryContract(
            identity="external_host_rtt_probe",
            owner="54guc.core.diagnostics.rtt",
            boundary="host network diagnostics adapter",
            modem_contact=False,
            may_write_modem=False,
            result_contract="typed steady-state RTT observation",
        ),
    }
)


MODEM_LIVE_DURABLE_WRITER_RULES: Mapping[str, DurableWriterRule] = MappingProxyType(
    {
        "backend_protocol_adapter_filesystem_write": DurableWriterRule(
            identity="backend_protocol_adapter_filesystem_write",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            allowed=False,
            bounded_required=True,
            mechanism="none",
            evidence_artifact="typed observation or receipt handed to Foundation",
        ),
        "modem_live_current_truth_persistence": DurableWriterRule(
            identity="modem_live_current_truth_persistence",
            owner=MODEM_LIVE_MATRIX_OWNER,
            allowed=False,
            bounded_required=True,
            mechanism="none",
            evidence_artifact="Foundation consumes immutable Matrix snapshots",
        ),
        "routeros_backend_traffic_accounting": DurableWriterRule(
            identity="routeros_backend_traffic_accounting",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            allowed=False,
            bounded_required=True,
            mechanism="none",
            evidence_artifact="Foundation data budget owns durable accounting",
        ),
        "matrix_consumer_rewrite": DurableWriterRule(
            identity="matrix_consumer_rewrite",
            owner=MODEM_LIVE_MATRIX_OWNER,
            allowed=False,
            bounded_required=True,
            mechanism="none",
            evidence_artifact="detached validated reads only",
        ),
        "scout_profile_repair_overlay": DurableWriterRule(
            identity="scout_profile_repair_overlay",
            owner="54guc.modem_live.scout",
            allowed=True,
            bounded_required=True,
            mechanism="atomic bounded repair overlay",
            evidence_artifact="modem_live_profile_repair_overlay.json",
        ),
        "scout_investigation_evidence": DurableWriterRule(
            identity="scout_investigation_evidence",
            owner="54guc.modem_live.scout",
            allowed=True,
            bounded_required=True,
            mechanism="atomic bounded evidence journal",
            evidence_artifact="modem_live_scout_evidence.jsonl",
        ),
    }
)


MODEM_LIVE_STARTUP_READINESS_CONTRACTS: Mapping[
    str, StartupReadinessContract
] = MappingProxyType(
    {
        "access_profile_contract": StartupReadinessContract(
            identity="access_profile_contract",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="exact explicit acquisition/cadence/action contract",
            failure_state="profile_contract_invalid",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "communication_ready": StartupReadinessContract(
            identity="communication_ready",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="protected transport and session communication established",
            failure_state="communication_pending_or_degraded",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "required_fast_groups": StartupReadinessContract(
            identity="required_fast_groups",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="all profile-required fast groups valid",
            failure_state="required_fast_group_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "required_slow_groups": StartupReadinessContract(
            identity="required_slow_groups",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="bounded startup sweep completes required slow groups",
            failure_state="required_slow_group_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "required_static_groups": StartupReadinessContract(
            identity="required_static_groups",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="bounded startup sweep completes required static groups",
            failure_state="required_static_group_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "permanent_device_identity": StartupReadinessContract(
            identity="permanent_device_identity",
            owner=MODEM_LIVE_MATRIX_OWNER,
            requirement="manufacturer serial or IMEI available in Matrix identity line",
            failure_state="permanent_identity_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "effective_session_capability": StartupReadinessContract(
            identity="effective_session_capability",
            owner=MODEM_LIVE_MATRIX_OWNER,
            requirement="seeded or live-proven session capability published",
            failure_state="session_capability_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "optional_feature_disposition": StartupReadinessContract(
            identity="optional_feature_disposition",
            owner=MODEM_LIVE_MATRIX_OWNER,
            requirement="each optional startup fact is valid, unsupported, or explicitly pending",
            failure_state="optional_feature_degraded",
            blocks_page_ready=False,
            blocks_monitor_process=False,
        ),
        "baseline_matrix_publication": StartupReadinessContract(
            identity="baseline_matrix_publication",
            owner=MODEM_LIVE_MATRIX_OWNER,
            requirement="one complete validated baseline generation committed",
            failure_state="baseline_matrix_pending",
            blocks_page_ready=True,
            blocks_monitor_process=False,
        ),
        "acquisition_lane_recovery": StartupReadinessContract(
            identity="acquisition_lane_recovery",
            owner=MODEM_LIVE_PHYSICAL_IO_OWNER,
            requirement="healthy, timed-out, wedged, recovering, or replaced state is explicit",
            failure_state="acquisition_lane_degraded",
            blocks_page_ready=False,
            blocks_monitor_process=False,
        ),
    }
)


MODEM_LIVE_DISPLACED_PATHS: Mapping[str, DisplacedPathContract] = MappingProxyType(
    {
        "startup_attempt_equals_ready": DisplacedPathContract(
            identity="startup_attempt_equals_ready",
            locations=(
                "arch4.modem_live.acquisition._establish_startup_live_baseline_from_receipt",
                "arch4.modem_live.acquisition.run_startup_live_phase",
            ),
            competing_meaning="a startup reread can masquerade as receipt-bound readiness",
            canonical_replacement="successful startup_full receipt plus per-profile readiness manifest",
            disposition="hard_fail",
            implementation_wave="wave_1",
        ),
        "model_firmware_identity_lock": DisplacedPathContract(
            identity="model_firmware_identity_lock",
            locations=(
                "arch4.modem_live.runtime_identity_projection.build_verified_modem_identity_section",
                "arch4.modem_live.runtime_identity_projection.runtime_identity_scope_lock_status",
            ),
            competing_meaning="model plus firmware can masquerade as permanent identity",
            canonical_replacement="serial-or-IMEI Matrix identity readiness",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "modem_input_parallel_current_truth": DisplacedPathContract(
            identity="modem_input_parallel_current_truth",
            locations=(
                "arch4.modem_live.snapshots.LiveModemRegisterSnapshot.modem",
                "arch4.modem_live.acquisition._publish_live_register_snapshot",
            ),
            competing_meaning="detached ModemInput remains a current-truth surface",
            canonical_replacement="immutable Truth Matrix generation",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "public_status_promotion": DisplacedPathContract(
            identity="public_status_promotion",
            locations=(
                "arch4.modem_live.acquisition._promote_public_status_to_modem",
            ),
            competing_meaning="selected status fields overwrite or fill ModemInput",
            canonical_replacement="one Modem Live canonical translator",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "live_register_payload_precedence": DisplacedPathContract(
            identity="live_register_payload_precedence",
            locations=("arch4.modem_live.register_store.LiveRegisterStore._pick_payload",),
            competing_meaning="ModemInput and status compete field by field",
            canonical_replacement="one translated Matrix-cell candidate",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "backend_public_status_canonicalization": DisplacedPathContract(
            identity="backend_public_status_canonicalization",
            locations=("arch4.modem.base.build_public_modem_status",),
            competing_meaning="backend canonicalizes CGF language before Modem Live",
            canonical_replacement="vendor observation plus sole Modem Live translation",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "duplicate_matrix_semantics": DisplacedPathContract(
            identity="duplicate_matrix_semantics",
            locations=("arch4.modem_live.truth_matrix.MODEM_LIVE_TRUTH_FIELD_CATALOG",),
            competing_meaning="one semantic fact has multiple Matrix cell identities",
            canonical_replacement="one canonical Matrix cell per semantic fact",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "declared_but_untranslated_alias": DisplacedPathContract(
            identity="declared_but_untranslated_alias",
            locations=(
                "arch4.modem_live.canonical_aliases.DECLARED_MODEM_LIVE_SOURCE_KEYS",
                "arch4.modem_live.canonical_aliases.CANONICAL_ALIAS_SPECS",
            ),
            competing_meaning="known alias can be suppressed without translation",
            canonical_replacement="complete translator registry or Scout unknown-token receipt",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "whole_generation_optional_cell_failure": DisplacedPathContract(
            identity="whole_generation_optional_cell_failure",
            locations=(
                "arch4.modem_live.truth_matrix.build_truth_line",
                "arch4.modem_live.register_store.LiveRegisterStore.publish",
            ),
            competing_meaning="one malformed optional cell rejects sibling truth",
            canonical_replacement="per-cell quarantine plus required-envelope validation",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "silent_generic_acquisition_contract": DisplacedPathContract(
            identity="silent_generic_acquisition_contract",
            locations=(
                "arch4.modem_live.acquisition._coerce_live_acquisition_contract",
                "arch4.modem.base.ModemInterface.read_actionable_modem_input",
            ),
            competing_meaning="invalid certified profile silently selects generic behavior",
            canonical_replacement="explicit certified acquisition contract",
            disposition="hard_fail",
            implementation_wave="wave_1",
        ),
        "unknown_lane_slow_live_coercion": DisplacedPathContract(
            identity="unknown_lane_slow_live_coercion",
            locations=("arch4.modem_live.access_scheduler.ModemLiveAccessScheduler",),
            competing_meaning="unknown source or lane is relabeled slow_live",
            canonical_replacement="declared access lane or typed rejection",
            disposition="hard_fail",
            implementation_wave="wave_1",
        ),
        "provider_cache_cross_device": DisplacedPathContract(
            identity="provider_cache_cross_device",
            locations=(
                "arch4.modem_live.acquisition._merge_modem_info_cache",
                "arch4.modem_live.acquisition.run_startup_live_phase",
            ),
            competing_meaning="old provider state can survive physical identity replacement",
            canonical_replacement="atomic identity/provider/capability session cutover",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "generic_pband_direct_readback": DisplacedPathContract(
            identity="generic_pband_direct_readback",
            locations=(
                "arch4.modem_live.acquisition._pband_camped",
                "arch4.modem_live.acquisition._wait_for_pband_camp",
            ),
            competing_meaning="direct backend status verifies target outside Matrix",
            canonical_replacement="typed stage receipt bound to accepted Matrix generation",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_3",
        ),
        "generic_action_success": DisplacedPathContract(
            identity="generic_action_success",
            locations=("arch4.modem_live.acquisition.execute_lever",),
            competing_meaning="generic LeverResult success may lack target readback",
            canonical_replacement="action-family physical receipt and Matrix confirmation",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_3",
        ),
        "multiple_rtt_truth_writers": DisplacedPathContract(
            identity="multiple_rtt_truth_writers",
            locations=(
                "arch4.modem_live.acquisition._rtt_cadence_loop",
                "arch4.modem_live.acquisition._probe_rtt_once",
                "arch4.modem_live.acquisition.measure_rtt",
            ),
            competing_meaning="diagnostic and toolbox probes can replace steady RTT truth",
            canonical_replacement="one Core host-ping RTT owner plus separate diagnostics",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "routeros_backend_traffic_persistence": DisplacedPathContract(
            identity="routeros_backend_traffic_persistence",
            locations=("arch4.modem.mikrotik.MikroTikModem",),
            competing_meaning="protocol adapter owns durable traffic accounting",
            canonical_replacement="Foundation data budget from raw Matrix counters",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "capability_report_truth_repair": DisplacedPathContract(
            identity="capability_report_truth_repair",
            locations=(
                "arch4.modem_live.capability_report.build_capabilities_report",
                "arch4.modem_live.capability_report._repair_capability_contract",
            ),
            competing_meaning="report reinterprets and repairs current capability truth",
            canonical_replacement="pure validated Matrix projection",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "scout_unknown_token_blind_spot": DisplacedPathContract(
            identity="scout_unknown_token_blind_spot",
            locations=(
                "arch4.modem_live.canonical_aliases.translate_canonical_aliases",
                "arch4.modem_live.scout.ModemLiveScout",
            ),
            competing_meaning="raw unknown vendor token can disappear before Scout evidence",
            canonical_replacement="translator-owned bounded unknown-token receipt",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
        "direct_diagnostic_current_truth_escape": DisplacedPathContract(
            identity="direct_diagnostic_current_truth_escape",
            locations=(
                "arch4.modem_live.acquisition.routeros_at_command",
                "arch4.modem_live.acquisition.zte_goform_read",
                "arch4.modem_live.acquisition.backend_debug_state",
            ),
            competing_meaning="direct diagnostic fact can bypass Matrix current truth",
            canonical_replacement="typed diagnostic receipt excluded from current truth",
            disposition="delete_or_hard_fail",
            implementation_wave="wave_1",
        ),
    }
)


def modem_live_matrix_cell_identities() -> tuple[str, ...]:
    """Return the frozen ``line.cell`` population in manifest order."""

    return tuple(
        f"{line_name}.{cell}"
        for line_name, contract in MODEM_LIVE_MATRIX_LINES.items()
        for cell in contract.cells
    )


__all__ = (
    "ConsumerClassContract",
    "DisplacedPathContract",
    "DurableWriterRule",
    "IoBoundaryContract",
    "MODEM_LIVE_DISPLACED_PATHS",
    "MODEM_LIVE_DURABLE_WRITER_RULES",
    "MODEM_LIVE_IO_BOUNDARIES",
    "MODEM_LIVE_LEGAL_CONSUMER_CLASSES",
    "MODEM_LIVE_MATRIX_CELLS",
    "MODEM_LIVE_MATRIX_LINES",
    "MODEM_LIVE_MATRIX_OWNER",
    "MODEM_LIVE_PHYSICAL_IO_OWNER",
    "MODEM_LIVE_STARTUP_READINESS_CONTRACTS",
    "MatrixLineContract",
    "MatrixCellContract",
    "StartupReadinessContract",
    "modem_live_matrix_cell_identities",
)
