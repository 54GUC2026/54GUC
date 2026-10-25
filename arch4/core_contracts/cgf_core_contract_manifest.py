"""Exact architecture inventory for active CGF Core boundaries.

This module is declarative.  It does not participate in runtime composition or
change runtime behavior.  Its purpose is to make the Core ownership contract
machine-checkable so a new register, action, customer port, or displaced path
cannot remain undocumented.
"""

from __future__ import annotations

from dataclasses import dataclass


MANIFEST_SCHEMA = "CGFCoreContractManifest.v1"
MANIFEST_OWNER = "54guc.core.architecture_contract"


@dataclass(frozen=True, slots=True)
class RuntimeRegisterContract:
    register_name: str
    owner: str
    producer: str
    consumers: tuple[str, ...]
    event_source: str
    stale_policy: str
    evidence_artifact: str
    public_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionFamilyContract:
    family: str
    action_kind: str
    owner: str
    producer: str
    consumer: str
    event_source: str
    stale_policy: str
    evidence_artifact: str
    disposition: str = "active"


@dataclass(frozen=True, slots=True)
class PortContract:
    port_id: str
    direction: str
    owner: str
    producer: str
    consumers: tuple[str, ...]
    event_source: str
    stale_policy: str
    evidence_artifact: str
    payload_schema: str


@dataclass(frozen=True, slots=True)
class IllegalPathContract:
    path_id: str
    production_symbols: tuple[str, ...]
    competing_truth: str
    canonical_owner: str
    required_disposition: str
    replacement_gate: str


RUNTIME_REGISTERS: tuple[RuntimeRegisterContract, ...] = (
    RuntimeRegisterContract(
        register_name="modem_live_matrix",
        owner="54guc.modem_live.truth_matrix",
        producer="ModemLiveModule live-register publication",
        consumers=(
            "SignalProcessingMatrix",
            "CoreRuntimeSnapshotPublisher",
            "Core accepted-root publication transaction",
            "future Core customers through accepted-root outbox",
        ),
        event_source="Modem Live matrix generation publication",
        stale_policy=(
            "generation-qualified; last complete generation may be held stale, "
            "but no profile or direct-read value may replace it"
        ),
        evidence_artifact="ModemLiveTruthMatrix.v4",
    ),
    RuntimeRegisterContract(
        register_name="latency",
        owner="signal_processing_matrix",
        producer="SignalProcessingMatrix accepted-generation projection",
        consumers=(
            "Core congestion computation",
            "CoreRuntimeSnapshotPublisher",
            "NGUI",
            "future accepted-observation customers",
        ),
        event_source="accepted modem generation plus independent RTT probe result",
        stale_policy=(
            "source timestamp and sequence retained; unavailable or stale probe "
            "must not be re-stamped fresh"
        ),
        evidence_artifact="CoreRuntimeSnapshot.v3:latency",
        public_fields=(
            "rtt_raw_ms",
            "rtt_raw_available",
            "rtt_raw_failure_reason",
            "rtt_raw_observed_at",
            "rtt_raw_sample_sequence",
            "rtt_raw_freshness",
            "rtt_raw_probe_status",
            "rtt_probe_ok",
            "rtt_low_load_latency_valid",
            "rtt_low_load_latency_reason",
            "rtt_low_load_latency_mbps",
            "jitter_formula",
            "rtt_ms",
            "rtt_sliding_avg",
            "rtt_baseline",
            "rtt_baseline_ms",
            "rtt_effective_baseline",
            "rtt_quiet_delta",
            "rtt_quiet_held",
            "rtt_delta",
            "rtt_delta_valid",
            "rtt_delta_display_ms",
            "rtt_delta_display_valid",
            "rtt_delta_display_state",
            "rtt_delta_display_reason",
            "rtt_delta_display_held",
            "rtt_delta_display_live_ts",
            "rtt_dl_jitter_pct",
            "rtt_dl_jitter_valid",
            "rtt_dl_jitter_reason",
            "rtt_dl_jitter_trimmed_ms",
            "rtt_dl_jitter_bucket",
            "rtt_dl_jitter_baseline_ms",
            "rtt_dl_jitter_delta_ms",
            "rtt_active_jitter_mad_ms",
        ),
    ),
    RuntimeRegisterContract(
        register_name="quality",
        owner="signal_processing_matrix",
        producer="SignalProcessingMatrix accepted-generation projection",
        consumers=(
            "Core action status and diagnostics",
            "CoreRuntimeSnapshotPublisher",
            "NGUI",
            "future accepted-observation customers",
        ),
        event_source="accepted modem generation",
        stale_policy=(
            "generation-qualified computed truth; held display values expose "
            "their held/valid/source timestamps"
        ),
        evidence_artifact="CoreRuntimeSnapshot.v3:quality",
        public_fields=(
            "congestion_weights",
            "congestion_pct",
            "congestion_pct_valid",
            "congestion_display_pct",
            "congestion_display_valid",
            "congestion_supported",
            "congestion_support_reason",
            "congestion_bucket",
            "congestion_state_bucket",
            "congestion_state_avg_pct",
            "congestion_kind_hint",
            "congestion_raw_pct",
            "congestion_trend_display",
            "congestion_flags_valid",
            "congestion_bad_now",
            "congestion_pressure_high",
            "congestion_pressure_moderate",
            "congestion_fast_mode",
            "congestion_hold_mode",
            "congestion_trusted_rtt",
            "congestion_rtt_base_pct",
            "congestion_trusted_rsrq",
            "congestion_trusted_active_jitter",
            "congestion_active_jitter_pct",
            "congestion_rsrq_available",
            "congestion_rsrq_contributes",
            "congestion_rsrq_reason",
            "congestion_rsrq_pct",
            "congestion_rsrq_gap_db",
            "congestion_rsrq_gap_threshold_db",
            "congestion_internal_bucket",
            "congestion_qam_gap",
            "congestion_slope",
            "congestion_rising",
            "congestion_source",
            "quality_model", "quality_capacity_fraction", "quality_total_bw_mhz",
            "quality_radio_inputs", "quality_valid", "quality_reason",
            "bq_cell",
            "bq_cell_valid",
            "bq_cell_source",
            "cq_cell",
            "cq_cell_valid",
            "cq_cell_source",
            "cq_cell_display",
            "cq_cell_display_valid",
            "cqb_cell",
            "cqb_cell_valid",
            "cqb_cell_source",
            "cqb_coverage",
            "rsrq_baseline",
            "rsrq_baseline_valid",
            "rsrq_gap",
            "rsrq_gap_valid",
            "rsrq_baseline_display_db",
            "rsrq_baseline_display_valid",
            "rsrq_baseline_display_held",
            "rsrq_baseline_display_live_ts",
            "rsrq_delta_display_db",
            "rsrq_delta_display_valid",
            "rsrq_delta_display_held",
            "rsrq_delta_display_live_ts",
            "cqi",
            "cqi0",
            "cqi1",
            "mcs_dl0",
            "mcs_dl1",
            "mcs_ul",
            "mcs_collapse",
            "qam_dl0",
            "qam_dl1",
            "qam_ul",
            "me0",
            "me1",
            "me_ul",
            "ri",
            "ri_valid",
            "ri_source",
            "scheduler_zero_authoritative",
            "scheduler_stream1_available",
            "ri_stability_pct",
            "ri_stability_valid",
            "ri_activity_per_hour",
            "nr_secondary_active",
            "nr_transition_state",
        ),
    ),
    RuntimeRegisterContract(
        register_name="ca",
        owner="signal_processing_matrix",
        producer="SignalProcessingMatrix accepted-generation projection",
        consumers=(
            "CoreRuntimeSnapshotPublisher",
            "NGUI",
            "future accepted-observation customers",
        ),
        event_source="accepted modem generation",
        stale_policy=(
            "generation-qualified; load-qualified and composition fields expose "
            "validity/reason instead of inferred current truth"
        ),
        evidence_artifact="CoreRuntimeSnapshot.v3:ca",
        public_fields=(
            "ca_signature",
            "ca_band_signature",
            "ca_realtime_state",
            "ca_stat_pct",
            "ca_stat_ld_pct",
            "ca_stat_ld_valid",
            "ca_stat_scope_key",
            "ca_change_per_hour",
            "ca_ld_change_per_hour",
            "cart",
            "cast",
            "cart_ld",
            "cart_ld_state",
            "cart_ld_reason",
            "cart_ld_capability",
            "cart_activity_per_hour",
            "cart_ld_activity",
            "ca_component_band_set",
            "ca_component_band_signature",
            "ca_component_count",
            "ca_composition_state",
            "ca_composition_reason",
            "ca_composition_capability",
            "ca_composition_signature",
            "ca_source",
        ),
    ),
    RuntimeRegisterContract(
        register_name="health",
        owner="54guc.modem_live.current_health",
        producer="Modem Live typed current-health publication port",
        consumers=(
            "CoreRuntimeSnapshotPublisher",
            "Core typed health decisions",
            "Foundation diagnostics",
            "NGUI through accepted runtime projection",
        ),
        event_source="Modem Live typed current-health transition",
        stale_policy=(
            "owner observation timestamp retained; malformed or foreign "
            "namespace facts fail before the Core register"
        ),
        evidence_artifact="ModemLiveCurrentHealthPublication.v1",
        public_fields=("facts",),
    ),
    RuntimeRegisterContract(
        register_name="runtime_clock",
        owner="runtime_clock",
        producer="Canonical Runtime Clock",
        consumers=("Core scheduled jobs", "CoreRuntimeSnapshotPublisher", "NGUI"),
        event_source="100 ms canonical runtime clock",
        stale_policy="monotonic sequence; a missing update is stale, never replaced by GUI time",
        evidence_artifact="CoreRuntimeSnapshot.v3:runtime_clock",
        public_fields=("uptime_s", "clock_ms", "started_at"),
    ),
    RuntimeRegisterContract(
        register_name="mutation",
        owner="action_runtime",
        producer="Core action mutation guard",
        consumers=("Core command admission", "Core action status", "NGUI"),
        event_source="mutation lease/guard transition",
        stale_policy="session-bound; cleared on terminal receipt or session replacement",
        evidence_artifact="CoreRuntimeSnapshot.v3:mutation",
        public_fields=("action_allowed", "reason", "sources"),
    ),
    RuntimeRegisterContract(
        register_name="action",
        owner="command_authority_projection",
        producer="CommandAuthority lifecycle projection",
        consumers=("Core action status", "NGUI", "command-outcome customers"),
        event_source="command_authority.event.v12",
        stale_policy="root/session-bound; old terminal or old-session status is rejected",
        evidence_artifact="CoreCommandStatus.v2",
        public_fields=("core_command_status",),
    ),
    RuntimeRegisterContract(
        register_name="action_auxiliary",
        owner="action_runtime_auxiliary",
        producer="CoreActionStatusService",
        consumers=("Core command admission", "NGUI"),
        event_source="committed generation or action lifecycle transition",
        stale_policy=(
            "must retain source generation and source timestamp; misaligned "
            "readback is stale/unavailable"
        ),
        evidence_artifact=(
            "CoreActionCapabilities.v1; CoreActionInternetPause.v1"
        ),
        public_fields=(
            "action_capabilities",
            "band_locked",
            "band_locked_valid",
            "band_locked_reason",
            "cell_locked",
            "cell_locked_valid",
            "cell_locked_reason",
            "manual_cell_lock_timer",
            "internet_pause",
        ),
    ),
    RuntimeRegisterContract(
        register_name="operational_status",
        owner="operational_service_status",
        producer="typed Core/Foundation operational services",
        consumers=("CoreRuntimeSnapshotPublisher", "NGUI"),
        event_source="terminal OperationalServiceReceipt.v1",
        stale_policy="terminal receipt retained and de-duplicated by request ID",
        evidence_artifact="OperationalServiceReceipt.v1",
        public_fields=("latest", "by_operation"),
    ),
    RuntimeRegisterContract(
        register_name="downstream_health",
        owner="downstream_health",
        producer="Core downstream-health monitor",
        consumers=("Core diagnostics", "CoreRuntimeSnapshotPublisher", "NGUI"),
        event_source="configured independent probe cycle",
        stale_policy="probe timestamp retained; missing probe is unavailable, not zero",
        evidence_artifact="CoreRuntimeSnapshot.v3:downstream_health",
        public_fields=(
            "message",
            "enabled",
            "ok",
            "target",
            "ping_count",
            "interval_s",
            "last_rtt_ms",
        ),
    ),
    RuntimeRegisterContract(
        register_name="internet_reachability",
        owner="54guc.core.action_reachability",
        producer="CoreReachabilityTimeline typed current-view publisher",
        consumers=(
            "Core emergency reachability",
            "CoreRuntimeSnapshotPublisher",
            "NGUI graphs",
            "Product Intelligence Matrix",
        ),
        event_source="Modem Live RTT register revision",
        stale_policy=(
            "Core publishes reachable, outage, or unavailable; consumers may "
            "not derive another Internet timeline from radio samples"
        ),
        evidence_artifact="CoreInternetReachabilityRegister.v3",
        public_fields=(
            "disposition",
            "outage_s",
            "outage_started_at",
            "latest_completed_outage",
            "latest_coverage_gap",
            "latest_probe_at",
            "register_sequence",
            "reason",
        ),
    ),
)


CELLULAR_ACTIONS: tuple[ActionFamilyContract, ...] = tuple(
    ActionFamilyContract(
        family="cellular_lever",
        action_kind=action_kind,
        owner="arch4.command_transactions.CommandAuthority",
        producer="CoreCommandGateway",
        consumer="Modem Live physical command port",
        event_source="CoreCommandRequest.v2",
        stale_policy="exact session/identity/epoch/source-generation admission",
        evidence_artifact="CommandTerminalReceipt.v3",
        disposition=disposition,
    )
    for action_kind, disposition in (
        ("BAND_SELECTION", "active_mutation"),
        ("BAND_LOCK", "active_mutation"),
        ("CELL_LOCK", "active_mutation"),
        ("TRAFFIC_PRESENCE", "observation_only"),
        ("TRAFFIC_VOLUME", "observation_only"),
        ("UE_CAPABILITY", "observation_only"),
        ("RESTART", "active_mutation"),
        ("PBAND", "active_mutation"),
        ("NR_BAND_LOCK", "active_mutation"),
        ("WIFI_RADIO", "active_mutation"),
        ("NETWORK_SETTINGS", "active_mutation"),
        ("MOBILE_DATA", "active_mutation"),
        ("RADIO_MODE", "active_mutation"),
    )
)


OPERATIONAL_ACTIONS: tuple[ActionFamilyContract, ...] = tuple(
    ActionFamilyContract(
        family="operational_service",
        action_kind=action_kind,
        owner=owner,
        producer="NGUI or named Core customer",
        consumer=consumer,
        event_source="typed operational request",
        stale_policy="request-ID replay guard and exact current runtime context",
        evidence_artifact="OperationalServiceReceipt.v1",
        disposition=disposition,
    )
    for action_kind, owner, consumer, disposition in (
        (
            "read_diagnostics", "54guc.core.device_control",
            "Modem Live maintenance admission; diagnostic Matrix report",
            "active_core_service",
        ),
        (
            "inspect_network_settings",
            "54guc.core.device_control",
            "Modem Live demand admission; Matrix observation publication",
            "active_core_service",
        ),
        (
            "wifi_set_radio",
            "arch4.command_transactions.CommandAuthority",
            "CoreCommandGateway WIFI_RADIO boundary",
            "typed_ingress_to_active_cellular_mutation",
        ),
        (
            "antenna_guide",
            "54guc.foundation.antenna_guide",
            "Foundation antenna-guide service",
            "active_foundation_service",
        ),
        (
            "set_downstream_health",
            "54guc.core.diagnostics",
            "Core downstream-health monitor",
            "active_core_service",
        ),
        (
            "configure_sms_command_control",
            "54guc.foundation.sms_command_control",
            "Foundation SMS command service",
            "active_foundation_service",
        ),
        (
            "plan_band_schedule",
            "54guc.foundation.band_schedule",
            "CoreCommandGateway at scheduled activation",
            "active_foundation_service",
        ),
        (
            "plan_band_sequence",
            "54guc.foundation.band_sequence",
            "CoreCommandGateway at each sequence step",
            "active_foundation_service",
        ),
        (
            "cancel_band_schedule",
            "54guc.foundation.band_schedule",
            "Foundation band schedule service",
            "active_foundation_service",
        ),
        (
            "cancel_band_sequence",
            "54guc.foundation.band_sequence",
            "Foundation band sequence service",
            "active_foundation_service",
        ),
        (
            "set_intelligence_autopilot",
            "54guc.product.intelligence_matrix",
            "Product Intelligence Matrix control policy",
            "active_product_service",
        ),
        (
            "set_intelligence_explore",
            "54guc.product.intelligence_matrix",
            "Product Intelligence Matrix Explore policy",
            "active_product_service",
        ),
    )
)


EXTENSION_PORTS: tuple[PortContract, ...] = tuple(
    PortContract(
        port_id=port_id,
        direction=direction,
        owner="54guc.core.extension_hook_registry",
        producer=producer,
        consumers=consumers,
        event_source=event_source,
        stale_policy=stale_policy,
        evidence_artifact="CoreExtensionHookStatus.v1",
        payload_schema=payload_schema,
    )
    for (
        port_id,
        direction,
        producer,
        consumers,
        event_source,
        stale_policy,
        payload_schema,
    ) in (
        (
            "accepted_observation",
            "core_to_customer",
            "CoreAcceptedRootOutbox",
            ("observer customers", "Foundation diagnostics"),
            "accepted committed generation",
            "bounded cursor; loss requires explicit resync",
            "CoreAcceptedGenerationRoot.v1",
        ),
        (
            "runtime_snapshot",
            "core_to_customer",
            "CoreRuntimeSnapshotPublisher",
            ("NGUI", "Product Intelligence Matrix adapter", "diagnostic customers"),
            "changed committed runtime register generation",
            "last complete snapshot; generation-qualified",
            "CoreRuntimeSnapshot.v3",
        ),
        (
            "clock_wake_50ms",
            "core_to_customer",
            "CoreClockWakeStream",
            ("diagnostic customers",),
            "50 ms canonical clock lane",
            "wake sequence only; never runtime truth",
            "CoreClockWake.v1",
        ),
        (
            "clock_wake_100ms",
            "core_to_customer",
            "CoreClockWakeStream",
            ("Foundation services", "diagnostic customers"),
            "100 ms canonical clock lane",
            "wake sequence only; never runtime truth",
            "CoreClockWake.v1",
        ),
        (
            "command_outcome",
            "core_to_customer",
            "CoreCommandOutcomeStream",
            (
                "NGUI operator",
                "Foundation schedule automation",
                "Foundation SMS automation",
                "Product Intelligence Matrix adapter",
                "Foundation diagnostics",
            ),
            "command_terminal_receipt.v7",
            "cursor-bound; retained-history loss fails loudly",
            "command_terminal_receipt.v7",
        ),
        (
            "foundation_ngui_extension",
            "customer_to_core",
            "Foundation presentation client",
            ("NGUI presentation extension boundary",),
            "Foundation presentation generation",
            "advisory presentation only; cannot write Core registers",
            "FoundationNGUIExtension.v1",
        ),
    )
)


DELIVERY_PORTS: tuple[PortContract, ...] = (
    PortContract(
        port_id="core_command_ingress",
        direction="customer_to_core",
        owner="54guc.core.command_gateway",
        producer="NGUI or attached CoreCommandClientPort",
        consumers=("CommandAuthority", "CoreBouncer"),
        event_source="CoreCommandRequest.v2",
        stale_policy="exact source observation retained and revalidated",
        evidence_artifact="command_terminal_receipt.v7",
        payload_schema="CoreCommandRequest.v2",
    ),
    PortContract(
        port_id="physical_command_dispatch",
        direction="core_to_modem_live",
        owner="modem_live",
        producer="Core command execution",
        consumers=("Modem Live physical actuator", "CommandAuthority evidence journal"),
        event_source="accepted CommandAuthority lease",
        stale_policy="one typed receipt per ordered physical stage",
        evidence_artifact="PhysicalCommandDispatchEvidence.v2",
        payload_schema="PhysicalCommandDispatchEvidence.v2",
    ),
    PortContract(
        port_id="accepted_generation_commit",
        direction="core_internal_fanout",
        owner="54guc.core.accepted_generation_commit",
        producer="ModemSamplePublicationFabric",
        consumers=("derived register projection", "CoreAcceptedRootOutbox"),
        event_source="accepted modem generation plus complete internal acknowledgements",
        stale_policy="bounded latest state with exact loss range and explicit resync",
        evidence_artifact="AcceptedGenerationCommit.v1",
        payload_schema="AcceptedGenerationCommit.v1",
    ),
    PortContract(
        port_id="observation_client_cursor",
        direction="core_to_customer",
        owner="54guc.core.accepted_root_outbox",
        producer="CoreAcceptedRootOutbox",
        consumers=("accepted-root customer clients",),
        event_source="CoreAcceptedGenerationRoot.v1 publication",
        stale_policy="claim/ack cursor; explicit loss/resync",
        evidence_artifact="CoreAcceptedRootOutboxRecord",
        payload_schema="CoreAcceptedGenerationRoot.v1",
    ),
    PortContract(
        port_id="command_outcome_client_cursor",
        direction="core_to_customer",
        owner="54guc.core.command_outcome_stream",
        producer="CoreCommandOutcomeStream",
        consumers=("CoreCommandOutcomeClientPort clients",),
        event_source="CommandAuthority terminal receipt publication",
        stale_policy="claim/ack cursor; history loss fails loudly",
        evidence_artifact="CoreCommandOutcomeRecord",
        payload_schema="command_terminal_receipt.v7",
    ),
    PortContract(
        port_id="intelligence_matrix_cgf_to_intelligence",
        direction="cgf_to_intelligence",
        owner="54guc.product.intelligence_matrix",
        producer="Product Intelligence Matrix CGF input adapter",
        consumers=("New Horizon Intelligence",),
        event_source="accepted root plus coherent constraints, terminal outcome, proposal dispatch result",
        stale_policy="latest observation plus bounded ordered event windows; explicit eviction sequence",
        evidence_artifact="ProductIntelligenceMatrixStatus.v1",
        payload_schema="IntelligenceMatrixInputSnapshot.v1",
    ),
    PortContract(
        port_id="intelligence_matrix_intelligence_to_cgf",
        direction="intelligence_to_cgf",
        owner="54guc.product.intelligence_matrix",
        producer="New Horizon Intelligence Matrix customer",
        consumers=("NGUI presentation", "Product Core proposal adapter"),
        event_source="typed presentation, proposal or Intelligence health publication",
        stale_policy="latest presentation/health; one non-overwritable pending proposal",
        evidence_artifact="ProductIntelligenceMatrixStatus.v1",
        payload_schema="IntelligenceMatrixOutputSnapshot.v1",
    ),
)


ILLEGAL_PATHS: tuple[IllegalPathContract, ...] = (
    IllegalPathContract(
        path_id="configured_pool_as_selected_truth",
        production_symbols=("arch4.radio_target_contract.radio_target_facts_from_accepted",),
        competing_truth="configured/requested pool substitutes for physical selected-band readback",
        canonical_owner="54guc.modem_live.truth_matrix selected-band cells",
        required_disposition="delete_substitution_and_hard_fail_missing_readback",
        replacement_gate="missing selected readback can never confirm a target",
    ),
    IllegalPathContract(
        path_id="precommit_command_lifecycle_observation",
        production_symbols=(
            "arch4.runtime_modem_sample_publication.ModemSamplePublicationFabric",
            "arch4.core.command_lifecycle_service.CoreCommandLifecycleService.observe_accepted_generation",
        ),
        competing_truth="precommit accepted object treated as downstream continuation proof",
        canonical_owner="AcceptedGenerationCommit receipt",
        required_disposition="replace_all_callers_then_delete",
        replacement_gate="terminal action follows required committed delivery acknowledgement",
    ),
    IllegalPathContract(
        path_id="split_generation_public_truth",
        production_symbols=("arch4.runtime_modem_sample_publication.ModemSamplePublicationFabric",),
        competing_truth="raw, derived, observation, and presentation generations advance independently",
        canonical_owner="AcceptedGenerationCommit receipt",
        required_disposition="replace_split_publication",
        replacement_gate="no public reader can observe mixed generation IDs",
    ),
    IllegalPathContract(
        path_id="generic_success_without_physical_evidence",
        production_symbols=("arch4.core.physical_command_evidence.physical_command_summary_from_result",),
        competing_truth="generic LeverResult success substitutes for typed physical receipt",
        canonical_owner="Modem Live PhysicalCommandDispatchEvidence",
        required_disposition="hard_failed_no_callers",
        replacement_gate="every mutation has typed per-stage evidence or typed no-op",
    ),
    IllegalPathContract(
        path_id="split_physical_stage_outcomes",
        production_symbols=(
            "arch4.core.physical_command_evidence.PhysicalCommandSummary",
            "arch4.command_transactions.CommandAuthority",
        ),
        competing_truth="stage timestamps and flat outcomes lose their binding",
        canonical_owner="typed per-stage physical receipt",
        required_disposition="replaced_by_PhysicalCommandReceipt_v1",
        replacement_gate="stage outcome and return time survive persistence/restart",
    ),
    IllegalPathContract(
        path_id="reason_text_as_target_proof",
        production_symbols=(
            "arch4.core.command_lifecycle_service.CoreCommandLifecycleService",
        ),
        competing_truth="human reason text substitutes for requested-versus-observed target evidence",
        canonical_owner="TargetConfirmationReceipt.v1",
        required_disposition="replaced_and_old_mapping_rejected",
        replacement_gate="receipt carries full requested and observed target plus generation",
    ),
    IllegalPathContract(
        path_id="stale_action_readback_restamped_fresh",
        production_symbols=(
            "arch4.core.action_status_service.CoreActionStatusService._latest_observation_readback",
            "arch4.core.action_status_service.CoreActionStatusService.publish",
        ),
        competing_truth="old observation receives a current timestamp and valid state",
        canonical_owner="generation-bound Core action status",
        required_disposition="replace_restamping_and_migrate_callers",
        replacement_gate="misaligned observation and matrix remain stale/unavailable",
    ),
    IllegalPathContract(
        path_id="direct_modem_read_bypasses_matrix",
        production_symbols=(
            "deleted:arch4.core.runtime_adapter",
            "arch4.core.action_runtime.verify_manual_action_effect",
        ),
        competing_truth="direct backend/profile reads compete with Modem Live matrix truth",
        canonical_owner="54guc.modem_live.truth_matrix",
        required_disposition="deleted_and_static_audited",
        replacement_gate="refresh requests return only through a new matrix generation",
    ),
    IllegalPathContract(
        path_id="duplicate_observation_stream",
        production_symbols=("deleted:arch4.core.observation_stream",),
        competing_truth="second accepted-observation delivery owner beside CoreAcceptedRootOutbox",
        canonical_owner="54guc.core.accepted_root_outbox",
        required_disposition="deleted_and_static_audited",
        replacement_gate="general Core customers use the outbox; Intelligence receives only the Product Matrix observation cell",
    ),
    IllegalPathContract(
        path_id="stage1_old_intelligence_command_composition",
        production_symbols=(
            "deleted:arch4.intelligence.composition",
            "deleted:arch4.intelligence.command_client",
            "deleted:arch4.intelligence.configuration_client",
            "deleted:arch4.intelligence.outcome_adapter",
            "deleted:arch4.intelligence.clock_adapter",
        ),
        competing_truth="detached old composition or the deleted multi-hook port exposes direct command and configuration authority beside the Matrix",
        canonical_owner="54guc.product.intelligence_matrix",
        required_disposition="deleted_and_static_audited",
        replacement_gate="Intelligence can only latch a typed proposal in the Matrix output bank",
    ),
    IllegalPathContract(
        path_id="manual_customer_fault_recovery",
        production_symbols=(
            "deleted:arch4.core.observation_stream.recover_client",
            "arch4.core.command_outcome_stream.CoreCommandOutcomeStream",
        ),
        competing_truth="external ad hoc caller owns recovery of a Core-attached customer",
        canonical_owner="Core customer lifecycle supervisor",
        required_disposition="replace_manual_recovery_dependency",
        replacement_gate="transient customer fault auto-recovers without duplicate command",
    ),
    IllegalPathContract(
        path_id="status_projection_failure_allows_dispatch",
        production_symbols=(
            "arch4.core.command_gateway.CoreCommandGateway._clear_superseded_command_status",
        ),
        competing_truth="stale terminal status can coexist with a newly dispatched physical action",
        canonical_owner="transaction-bound CommandAuthority pending receipt",
        required_disposition="remove_catch_and_continue",
        replacement_gate="status binding failure produces zero physical dispatch",
    ),
    IllegalPathContract(
        path_id="independent_radio_mutation_trace",
        production_symbols=("arch4.core.action_runtime._radio_mutation_trace",),
        competing_truth="process-global mutable trace duplicates CommandAuthority lifecycle",
        canonical_owner="CommandAuthority typed receipts",
        required_disposition="deleted_active_trace_projection_from_authority_receipts",
        replacement_gate="manual timing is a pure receipt projection across restart",
    ),
    IllegalPathContract(
        path_id="first_nonempty_identity_conflict",
        production_symbols=(
            "arch4.core.command_safety_adapter.core_static_action_identity",
        ),
        competing_truth="conflicting identity facts are silently prioritized",
        canonical_owner="validated Modem Live matrix identity",
        required_disposition="replace_priority_with_conflict_rejection",
        replacement_gate="conflicting nonempty identity facts reject the generation",
    ),
    IllegalPathContract(
        path_id="dormant_radio_identity_projection",
        production_symbols=(
            "arch4.runtime_registers.extract_radio_identity_value",
            "arch4.runtime_registers.apply_radio_identity_register_to_status",
        ),
        competing_truth="dormant identity interpretation can reintroduce split register truth",
        canonical_owner="validated Modem Live matrix identity",
        required_disposition="prove_no_callers_then_delete_or_name_pure_renderer",
        replacement_gate="static audit forbids production truth callers",
    ),
)


ALL_ACTIONS = CELLULAR_ACTIONS + OPERATIONAL_ACTIONS
ALL_PORTS = EXTENSION_PORTS + DELIVERY_PORTS


__all__ = (
    "ALL_ACTIONS",
    "ALL_PORTS",
    "CELLULAR_ACTIONS",
    "DELIVERY_PORTS",
    "EXTENSION_PORTS",
    "ILLEGAL_PATHS",
    "MANIFEST_OWNER",
    "MANIFEST_SCHEMA",
    "OPERATIONAL_ACTIONS",
    "RUNTIME_REGISTERS",
    "ActionFamilyContract",
    "IllegalPathContract",
    "PortContract",
    "RuntimeRegisterContract",
)
