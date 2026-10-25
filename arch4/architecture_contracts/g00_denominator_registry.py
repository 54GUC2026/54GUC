"""G00 production denominator freeze for CGF architecture replacement.

This module is deliberately data-only.  It changes no owner, lifecycle,
writer, package, launcher, or test behavior.  Its purpose is to make the
current population reviewable in both directions before later cutovers:

* a declared item that no longer exists fails;
* an item that appears in a governed production population without a
  declaration fails.

The test-claim registry is a selection/coverage declaration only.  Membership
never means that a test passed, that a race was executed, or that hardware,
Windows, packaging, or end-to-end behavior was proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from arch4.core_contracts.cgf_foundation_contract_manifest import (
    FOUNDATION_ARCHITECTURE_CONTRACTS,
    FOUNDATION_DURABLE_WRITERS,
)


G00_TEST_CLAIM_LIMIT = (
    "selection_only: a claim names intended test coverage; it is not a pass, "
    "runtime, race, package, Windows, hardware, or end-to-end proof result"
)

G00_DISPOSITIONS = frozenset(
    {
        "current",
        "boundary_adapter",
        "developer_only",
        "replacement_required",
        "hard_fail_tombstone",
    }
)


@dataclass(frozen=True, slots=True)
class G00Route:
    route_id: str
    owner: str
    implementation: str
    disposition: str

    def __post_init__(self) -> None:
        if not self.route_id or not self.owner or not self.implementation:
            raise ValueError("g00_route_metadata_required")
        if self.disposition not in G00_DISPOSITIONS:
            raise ValueError(f"g00_route_disposition_invalid:{self.disposition}")


G00_PRODUCTION_CALLABLES: tuple[G00Route, ...] = tuple(
    G00Route(
        route_id=f"foundation_contract:{row.contract_id}",
        owner=row.owner,
        implementation=row.implementation,
        disposition="current",
    )
    for row in FOUNDATION_ARCHITECTURE_CONTRACTS
) + (
    G00Route(
        "product:startup",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor.startup",
        "current",
    ),
    G00Route(
        "product:service_once",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor.service_once",
        "current",
    ),
    G00Route(
        "product:shutdown",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor.shutdown",
        "current",
    ),
    G00Route(
        "product:request_shutdown",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor.request_shutdown",
        "current",
    ),
    G00Route(
        "product:lifecycle_status",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor.lifecycle_status",
        "current",
    ),
    G00Route(
        "source:run",
        "54guc.cgf_runner",
        "arch4.cgf_runner:CGFRunner.run",
        "current",
    ),
    G00Route(
        "source:stop",
        "54guc.cgf_runner",
        "arch4.cgf_runner:CGFRunner.stop",
        "current",
    ),
    G00Route(
        "source:termination_receipt",
        "54guc.cgf_runner",
        "arch4.cgf_runner:CGFRunner.termination_receipt",
        "current",
    ),
    G00Route(
        "application_lease:acquire",
        "54guc.application_lease",
        "arch4.application_lease:ApplicationLease.acquire",
        "current",
    ),
    G00Route(
        "application_lease:transition",
        "54guc.application_lease",
        "arch4.application_lease:ApplicationLease.transition",
        "current",
    ),
    G00Route(
        "application_lease:release",
        "54guc.application_lease",
        "arch4.application_lease:ApplicationLease.release",
        "current",
    ),
    G00Route(
        "windows:main",
        "54guc.windows_native_launcher",
        "arch4.windows_native_launcher:main",
        "current",
    ),
    G00Route(
        "windows:bundle_validation",
        "54guc.windows_bundle_validation",
        "arch4.windows_bundle_validation:main",
        "boundary_adapter",
    ),
)

G00_DURABLE_WRITER_IDS = tuple(
    row.writer_id for row in FOUNDATION_DURABLE_WRITERS
)

G00_LIFECYCLE_ROUTES: tuple[G00Route, ...] = (
    G00Route(
        "application:lease",
        "54guc.application_lease",
        "arch4.application_lease:ApplicationLease",
        "current",
    ),
    G00Route(
        "product:supervisor",
        "54guc.product_supervisor",
        "arch4.product_supervisor:ProductSupervisor",
        "current",
    ),
    G00Route(
        "source:runner",
        "54guc.cgf_runner",
        "arch4.cgf_runner:CGFRunner",
        "current",
    ),
    G00Route(
        "foundation:runtime_composition",
        "54guc.foundation.composition",
        "arch4.foundation.composition:FoundationCompositionRoot",
        "current",
    ),
    G00Route(
        "foundation:operational_root",
        "54guc.foundation.operational_root",
        "arch4.foundation.operational_root:FoundationOperationalRoot",
        "current",
    ),
    G00Route(
        "foundation:service_job_fabric",
        "54guc.foundation.service_job_fabric",
        "arch4.foundation.service_job_fabric:FoundationServiceJobFabric",
        "current",
    ),
    G00Route(
        "foundation:schedule",
        "54guc.foundation.schedule",
        "arch4.foundation.schedule_service:FoundationScheduleService",
        "current",
    ),
    G00Route(
        "foundation:runtime_health",
        "arch4.foundation.runtime_health_publisher",
        "arch4.foundation.runtime_artifacts:FoundationRuntimeHealthService",
        "current",
    ),
    G00Route(
        "foundation:report_job",
        "arch4.foundation.site_report_job",
        "arch4.foundation.site_report_job:run_site_report_job",
        "boundary_adapter",
    ),
    G00Route(
        "foundation:support_job",
        "arch4.support_bundle_job",
        "arch4.support_bundle_job:run_support_bundle_job",
        "boundary_adapter",
    ),
    G00Route(
        "windows:launcher_process",
        "54guc.windows_native_launcher",
        "arch4.windows_native_launcher:main",
        "current",
    ),
)

G00_FOUNDATION_JOB_IDS = (
    "connectivity_projection",
    "modem_diagnostic_projection",
    "sms_inbox_pipeline",
    "sms_outcome_progression",
    "schedule_pipeline",
    "antenna_pipeline",
    "watchdog_presentation",
    "data_budget_pipeline",
    "runtime_health_pipeline",
    "current_state_publication",
)

G00_RUNTIME_ARTIFACTS: Mapping[str, str] = MappingProxyType(
    {
        "current_state.json": "arch4.foundation.core_current_state_publisher",
        "cgf_evidence.sqlite3": "arch4.foundation.cgf_evidence_store",
        "runtime_health.json": "arch4.foundation.runtime_health_publisher",
    }
)

G00_PACKAGE_HIDDEN_IMPORT_ROOTS = (
    "arch4.b818",
    "arch4.control",
    "arch4.core",
    "arch4.core_contracts",
    "arch4.customer_contracts",
    "arch4.foundation",
    "arch4.modem",
    "arch4.modem_live",
    "arch4.ngui",
    "arch4.new_horizon",
    "arch4.policy",
    "arch4.signal_processing",
    "arch4.startup",
)

G00_PACKAGE_ROOT_DISPOSITIONS: Mapping[str, str] = MappingProxyType(
    {
        "arch4.b818": "current",
        "arch4.control": "current",
        "arch4.core": "current",
        "arch4.core_contracts": "current",
        "arch4.customer_contracts": "current",
        "arch4.foundation": "current",
        "arch4.modem": "current",
        "arch4.modem_live": "current",
        "arch4.ngui": "current",
        "arch4.new_horizon": "current",
        "arch4.policy": "replacement_required",
        "arch4.signal_processing": "current",
        "arch4.startup": "current",
    }
)

G00_PACKAGE_EXCLUDED_ROOTS = (
    "arch4.brain",
    "arch4.intelligence",
    "arch4.smart_loop",
)

G00_PACKAGE_DATA_ROUTES = (
    ("arch4/ngui/fonts/*.ttf", "arch4/ngui/fonts"),
    ("arch4/ngui/fonts/LICENSE-SIL-OFL.txt", "arch4/ngui/fonts"),
    ("config/*.json", "config"),
    ("config/modems/*.env", "config/modems"),
    ("config/ip/*.txt", "config/ip"),
    ("open_source_manifest.json", "."),
    ("third_party/licenses", "licenses"),
    ("third_party/NOTICE.txt", "."),
    # Reviewed first-party GPL package inputs; upstream notice routes stay exact.
    ("LICENSE", "."),
    ("LICENSING.md", "."),
    ('arch4/modem_live/profile_definition/*.json', 'arch4/modem_live/profile_definition'),
    ('arch4/modem_live/profile_definition/codecs/*.json', 'arch4/modem_live/profile_definition/codecs'),
)

G00_PACKAGE_DATA_DISPOSITIONS: Mapping[str, str] = MappingProxyType(
    {
        "arch4/ngui/fonts/*.ttf": "current",
        "arch4/ngui/fonts/LICENSE-SIL-OFL.txt": "current",
        "config/*.json": "replacement_required",
        "config/modems/*.env": "replacement_required",
        "config/ip/*.txt": "replacement_required",
        "open_source_manifest.json": "current",
        "third_party/licenses": "current",
        "third_party/NOTICE.txt": "current",
        "LICENSE": "current",
        "LICENSING.md": "current",
        "arch4/modem_live/profile_definition/*.json": "current",
        "arch4/modem_live/profile_definition/codecs/*.json": "current",
    }
)

G00_PACKAGE_EXPLICIT_HIDDEN_IMPORTS = (
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
    "pyqtgraph",
    "shiboken6",
    "requests",
    "urllib3",
    "idna",
    "charset_normalizer",
    "certifi",
    "routeros_api",
    "regex",
    "regex._regex",
    "routeros_api.api",
    "routeros_api.api_communicator",
    "routeros_api.api_communicator.async_decorator",
    "routeros_api.api_communicator.base",
    "routeros_api.api_communicator.encoding_decorator",
    "routeros_api.api_communicator.exception_decorator",
    "routeros_api.api_communicator.key_cleaner_decorator",
    "routeros_api.api_socket",
    "routeros_api.api_structure",
    "routeros_api.base_api",
    "routeros_api.communication_exception_parsers",
    "routeros_api.exceptions",
    "routeros_api.query",
    "routeros_api.resource",
    "routeros_api.sentence",
    "routeros_api.utils",
)

G00_ROOT_CALLABLES: Mapping[str, str] = MappingProxyType(
    {
        "ARCH5_v4_Start_Live.bat": "hard_fail_tombstone",
        "ARCH5_v4_Start_Observe.bat": "hard_fail_tombstone",
        "ARCH5_v4_Stop_All.bat": "hard_fail_tombstone",
        "GUC_License_Gen.spec": "boundary_adapter",
        "LTE-Ultimate-Controller.spec": "replacement_required",
        "LUC-Windows-Native.spec": "current",
        "probe.py": "hard_fail_tombstone",
        "start.sh": "hard_fail_tombstone",
        "start_b535.sh": "hard_fail_tombstone",
        "start_b818.sh": "hard_fail_tombstone",
        "start_mc888.sh": "hard_fail_tombstone",
        "start_mf296r.sh": "hard_fail_tombstone",
        "start_model.sh": "hard_fail_tombstone",
        "start_modem.sh": "hard_fail_tombstone",
        "start_openwrt.sh": "hard_fail_tombstone",
        "start_zte_zwrt.sh": "hard_fail_tombstone",
    }
)

_RETAINED = "current"
_BOUNDARY = "boundary_adapter"
_DEV = "developer_only"
_REPLACE = "replacement_required"
_TOMBSTONE = "hard_fail_tombstone"

G00_SCRIPT_DISPOSITIONS: Mapping[str, str] = MappingProxyType(
    {
        "active_rtt_jitter_report.py": _REPLACE,
        "agent1_snapshot.sh": _REPLACE,
        "agent1_snapshot_ctl.sh": _REPLACE,
        "aggressiveness_balance_gate.py": _REPLACE,
        "arch5_supervisor.sh": _REPLACE,
        "arch6_observe_supervisor.sh": _TOMBSTONE,
        "g5tc_network_api_probe.js": _DEV,
        "test_g5tc_network_api_probe.cjs": _DEV,
        "audit_backend_fields.py": _DEV,
        "audit_test_suite.py": _DEV,
        "b818_gui_soak_until.sh": _REPLACE,
        "build_guc_license_gen_windows.ps1": _REPLACE,
        "build_log_checker_windows.ps1": _RETAINED,
        "build_support_recovery_windows.ps1": _RETAINED,
        "build_fixed_hour_converter_package.py": _DEV,
        "build_model_replay_profile.py": _DEV,
        "build_win11_bundle.sh": _TOMBSTONE,
        "build_windows_native_bundle.ps1": _REPLACE,
        "build_windows_portable.ps1": _REPLACE,
        "build_windows_self_extract.ps1": _TOMBSTONE,
        "carbon_git_safe.sh": _REPLACE,
        "cgf_active_runtime_validator.py": _BOUNDARY,
        "cgf_architecture_assessor.py": _BOUNDARY,
        "cgf_carbon_full_test.sh": _BOUNDARY,
        "cgf_full_test_runner.py": _RETAINED,
        "cgf_generate_ngui_contract_catalog.py": _DEV,
        "cgf_hp_full_test.ps1": _BOUNDARY,
        "cgf_test_gate.sh": _REPLACE,
        "cgf_windows_path_registry.ps1": _RETAINED,
        "cgf_windows_source_soak.py": _REPLACE,
        "codex_startup.sh": _TOMBSTONE,
        "configure_windows_dev_security_exclusions.ps1": _REPLACE,
        "convert_fixed_hour_history.py": _BOUNDARY,
        "congestion_watch_0900_job.sh": _REPLACE,
        "congestion_watch_finalize.py": _REPLACE,
        "congestion_watch_schedule_stop.sh": _REPLACE,
        "create_legacy_runtime_compat.py": _REPLACE,
        "developer_native_runtime.sh": _DEV,
        "disable_updates_365d.ps1": _REPLACE,
        "disable_updates_365d_linux_root.sh": _REPLACE,
        "export_new_horizon_history.py": _BOUNDARY,
        "export_windows_build.ps1": _REPLACE,
        "full_test_gate.sh": _REPLACE,
        "generate_modem_protocol_matrix.py": _DEV,
        "git_credential_hp.sh": _REPLACE,
        "github_backup.sh": _REPLACE,
        "github_backup_ctl.sh": _REPLACE,
        "gui_start.sh": _REPLACE,
        "hardware_evidence_matrix.py": _BOUNDARY,
        "hilink_cell_capability_probe.py": _DEV,
        "hp_live_b818_observe.sh": _REPLACE,
        "idle_rtt_probe.py": _REPLACE,
        "import_calibration_external_knowledge.py": _REPLACE,
        "install_ngui_visual_tools.sh": _DEV,
        "ip_compliance_check.sh": _REPLACE,
        "live_lane_sanity_check.py": _REPLACE,
        "luc_v1_one_hour_soak.sh": _REPLACE,
        "mf296r_session_refresh_experiment.py": _DEV,
        "migrate_new_horizon_learning_context.py": _BOUNDARY,
        "ngui_live_probe_contract.py": _BOUNDARY,
        "ngui_live_ui_probe.py": _REPLACE,
        "ngui_visual_audit.py": _DEV,
        "ngui_visual_correction_helper.py": _DEV,
        "ngui_visual_gate.sh": _DEV,
        "ngui_visual_manifest.py": _DEV,
        "night_orchestrator.sh": _TOMBSTONE,
        "openwrt_dual_simulator.py": _DEV,
        "openwrt_dual_simulator_control.py": _DEV,
        "package_active_source.sh": _DEV,
        "persistence_watch.py": _REPLACE,
        "persistence_watch_ctl.sh": _REPLACE,
        "plot_calibration_hourly_ranges.py": _REPLACE,
        "pre_beta_acceptance_gate.sh": _REPLACE,
        "pre_beta_artifact_audit.py": _REPLACE,
        "probe_runtime_truth_surfaces.py": _REPLACE,
        "promote_code_valid.sh": _REPLACE,
        "recode_modem_history_identity.py": _REPLACE,
        "release_gate_record.py": _REPLACE,
        "release_lane_preflight.sh": _REPLACE,
        "replay_log_retention.py": _REPLACE,
        "replay_log_retention_ctl.sh": _REPLACE,
        "report_maintenance.py": _REPLACE,
        "rsrq_delta_calibration.py": _REPLACE,
        "run_windows_portable_build_from_wsl_source.ps1": _TOMBSTONE,
        "runtime_git_guard.sh": _REPLACE,
        "runtime_guardian.sh": _REPLACE,
        "runtime_guardian_ctl.sh": _REPLACE,
        "start_t480_b818_source_live_detached.ps1": _TOMBSTONE,
        "start_t480_b818_source_live_detached.sh": _TOMBSTONE,
        "t480_b818_source_live_gui.sh": _TOMBSTONE,
        "t480_live_b818_check.sh": _TOMBSTONE,
        "t480_live_b818_observe.sh": _TOMBSTONE,
        "t480_live_gui_bootstrap.sh": _TOMBSTONE,
        "telemetry_signal_compare.py": _REPLACE,
        "test_lanes.py": _DEV,
        "test_suite_contract_inventory.py": _DEV,
        "zte_mc888_simulator.py": _DEV,
        "test_windows_portable.ps1": _REPLACE,
        "two_hour_watch_ctl.sh": _REPLACE,
        "two_hour_watch_report.py": _REPLACE,
        "tx1_git_safe.sh": _REPLACE,
        "v1_acceptance_gate.sh": _REPLACE,
        "v2_acceptance_gate.sh": _REPLACE,
        "validate_cgf_evidence.py": _BOUNDARY,
        "validate_zte_mc888_simulator_run.py": _DEV,
        "verify_windows_native_path.ps1": _REPLACE,
        "win11_b818_entrypoint.sh": _TOMBSTONE,
        "win11_mc888_entrypoint.sh": _TOMBSTONE,
        "win11_portable_control.sh": _TOMBSTONE,
        "windows_mc888_simulator_control.ps1": _DEV,
        "windows_mc888_simulator_lab.ps1": _DEV,
        "windows_native_bootstrap.ps1": _REPLACE,
        "windows_native_support_bundle.ps1": _BOUNDARY,
        "windows_ngui_debug_autoconnect.ps1": _REPLACE,
        "windows_ngui_debug_b535_232.ps1": _REPLACE,
        "windows_ngui_debug_b535_333.ps1": _REPLACE,
        "windows_ngui_debug_b818.ps1": _REPLACE,
        "windows_ngui_debug_zte_mf296r.ps1": _REPLACE,
        "windows_portable_control.ps1": _RETAINED,
        "windows_portable_dev_start.sh": _TOMBSTONE,
        "workspace_preflight.sh": _REPLACE,
        "wsl_fast_mirror.sh": _DEV,
        "zte_live_probe.sh": _REPLACE,
        "zte_mc888_simulator_control.py": _DEV,
    }
)

G00_TEST_CLAIM_IDS = frozenset(
    {
        "every_active_cgf_source_has_one_reviewed_disposition",
        "accepted_modem_generation_commits_atomically_once",
        "one_typed_physical_command_and_optional_exact_reachability_evidence",
        "foundation_jobs_and_evidence_are_independent_correlated_and_atomic",
        "ngui_capability_presentation_and_utility_owners_are_single",
        "modem_live_matrix_and_capability_have_one_canonical_owner",
        "owner_truth_survives_corrupt_or_foreign_cross_layer_inputs",
        "identity_confirmed_settings_handoff_current_and_previous_archives",
        "product_without_intelligence",
        "product_startup_classification_has_one_typed_owner",
        "central_cgf_architecture_assessment_is_available",
        "every_large_active_code_unit_is_explicitly_reviewed",
        "mandatory_active_cgf_runtime_validation_layer",
        "every_core_register_has_one_exact_reachable_writer",
        "semantic_identity_is_calculated_only_from_canonical_values",
        "old_runtime_vocabulary_is_exactly_reviewed",
        "all_active_filesystem_writers_are_exact_bounded_and_reviewed",
        "dedicated_cgf_state_namespace",
        "real_ngui_application_composition",
        "reproducible_workspace_environment",
        "accepted_generation_to_charts",
        "runtime_frame_is_one_atomic_ngui_input_generation",
        "committed_windows_source_soak_is_strict_and_rtt_truthful",
        "modem_live_is_the_only_modem_io_owner",
        "all_modem_families_share_one_session_identity_and_login_contract",
        "modem_identity_registry_is_append_only",
        "every_runtime_modem_profile_declares_critical_identity_capability_and_provenance",
        "mutable_modem_profiles_pass_owner_validation_and_real_route_proof",
        "routeros_read_write_sequence",
        "mikrotik_rtt_is_host_path_and_never_routeros_io",
        "modem_live_owns_the_exact_bounded_rtt_probe_deadline",
        "operator_nmi_to_modem_and_chart",
        "compound_radio_physical_stages_and_first_chart_are_truthful",
        "command_correlation_and_replay",
        "mechanical_command_safety",
        "command_terminal_outcome",
        "ngui_consumes_typed_gateway_outcome_without_second_owner",
        "accepted_generation_closes_and_restored_command_lifecycle_reconciles",
        "late_command_evidence_expires_before_any_transition_is_accepted",
        "backend_writer_tombstones_have_no_duplicate_dead_implementation",
        "core_action_readiness_and_lock_readback",
        "clock_and_worker_isolation",
        "diagnostic_fault_reporting",
        "light_artifact_boundary",
        "foundation_extension_to_ngui_input_register",
        "all_active_page_buttons_have_typed_owner_routes",
        "wifi_radio_state_is_matrix_owned_and_controlled",
        "old_ngui_and_engine_paths_cannot_return",
        "current_visual_manifests_are_the_only_visual_proof_roots",
        "foundation_antenna_guide_is_live_and_non_actuating",
        "foundation_report_support_and_update_utilities",
        "operator_band_sequence_progresses_once_per_completed_step",
        "band_schedule_and_sequence_preserve_live_observation_outside_writes",
        "priority_post_write_readback_has_one_cgf_boundary_and_exact_stage_evidence",
        "typed_operational_services",
        "worker_health_distinguishes_services_from_finite_jobs",
        "core_configuration_restart_reactivates_only_owned_durable_values",
        "foundation_user_budget_is_the_only_gui_and_report_budget_owner",
        "typed_latency_traffic_qualification_feeds_rtt_and_jitter_once",
        "signal_checkpoint_has_one_current_schema_and_one_congestion_owner",
        "rsrq_live_baseline_has_one_cgf_owner",
        "new_horizon_closed_loop_uses_one_referee_and_decision_history_snapshot",
        "new_horizon_restart_import_corruption_and_foreign_identity_are_bounded",
        "new_horizon_brain_advisers_referee_learning_and_impact_close_end_to_end",
        "intelligence_off_and_emergency_input_do_not_break_cgf_truth",
        "typed_core_current_fields_are_the_only_ngui_signal_meaning",
        "bounded_schema_owned_privacy_scrubbed_cgf_evidence",
        "band_auto_uses_provider_policy_and_exact_session_capability",
        "g00_production_denominators_are_bidirectional_and_selection_only",
        "application_and_product_lifecycle_races_have_exact_generations",
    }
)


def g00_denominator_counts() -> Mapping[str, int]:
    """Return current declared counts without implying proof success."""

    return MappingProxyType(
        {
            "production_callables": len(G00_PRODUCTION_CALLABLES),
            "durable_writers": len(G00_DURABLE_WRITER_IDS),
            "lifecycle_routes": len(G00_LIFECYCLE_ROUTES),
            "foundation_jobs": len(G00_FOUNDATION_JOB_IDS),
            "runtime_artifacts": len(G00_RUNTIME_ARTIFACTS),
            "package_hidden_roots": len(G00_PACKAGE_HIDDEN_IMPORT_ROOTS),
            "package_excluded_roots": len(G00_PACKAGE_EXCLUDED_ROOTS),
            "package_data_routes": len(G00_PACKAGE_DATA_ROUTES),
            "package_explicit_hidden_imports": len(
                G00_PACKAGE_EXPLICIT_HIDDEN_IMPORTS
            ),
            "root_callables": len(G00_ROOT_CALLABLES),
            "scripts": len(G00_SCRIPT_DISPOSITIONS),
            "test_claims": len(G00_TEST_CLAIM_IDS),
        }
    )


__all__ = (
    "G00_DURABLE_WRITER_IDS",
    "G00_FOUNDATION_JOB_IDS",
    "G00_LIFECYCLE_ROUTES",
    "G00_PACKAGE_DATA_DISPOSITIONS",
    "G00_PACKAGE_DATA_ROUTES",
    "G00_PACKAGE_EXCLUDED_ROOTS",
    "G00_PACKAGE_EXPLICIT_HIDDEN_IMPORTS",
    "G00_PACKAGE_HIDDEN_IMPORT_ROOTS",
    "G00_PACKAGE_ROOT_DISPOSITIONS",
    "G00_PRODUCTION_CALLABLES",
    "G00_ROOT_CALLABLES",
    "G00_RUNTIME_ARTIFACTS",
    "G00_SCRIPT_DISPOSITIONS",
    "G00_TEST_CLAIM_IDS",
    "G00_TEST_CLAIM_LIMIT",
    "G00Route",
    "g00_denominator_counts",
)
