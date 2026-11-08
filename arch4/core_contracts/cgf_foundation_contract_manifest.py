"""Executable Wave-0 contract freeze for active CGF Foundation boundaries.

This module is intentionally data-only.  It does not import Foundation runtime
implementations, create services, read artifacts, or change production
behavior.  Later replacement waves use these declarations as the mandatory
owner, writer, and deletion checklist.

``active`` means the declared owner is the legal current owner.
``planned_wave_4`` means the authority is required by the approved replacement
plan but is not implemented yet.
``replacement_required`` on a writer means that the writer remains present in
the Wave-0 baseline but is architecturally illegal after the named cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


FOUNDATION_CONTRACT_REQUIRED_METADATA: tuple[str, ...] = (
    "contract_id",
    "kind",
    "owner",
    "producer",
    "consumers",
    "event_source",
    "validity_freshness_policy",
    "evidence_artifacts",
    "implementation",
    "status",
)

FOUNDATION_CONTRACT_KINDS = frozenset(
    {"store", "journal", "report", "service", "boundary_adapter"}
)
FOUNDATION_CONTRACT_STATUSES = frozenset({"active", "planned_wave_4"})

WRITER_ATOMICITY_CLASSES = frozenset(
    {
        "atomic_replace",
        "atomic_archive_replace",
        "append_then_compact",
        "sqlite_transaction",
    }
)
WRITER_BOUNDEDNESS_CLASSES = frozenset(
    {
        "fixed_snapshot",
        "fixed_private_replica_set",
        "bounded_records_and_bytes",
        "one_shot_receipt",
        "current_run_retention_only",
        "retention_window",
        "pending_only",
        "exact_idempotency_state",
    }
)
WRITER_CORRECTNESS_RETENTION_CLASSES = frozenset(
    {"exact_idempotency_state"}
)
WRITER_DISPOSITIONS = frozenset(
    {"canonical", "permanent_boundary_adapter", "replacement_required"}
)
SAFE_ATOMICITY_CLASSES = frozenset(
    {"atomic_replace", "atomic_archive_replace", "sqlite_transaction"}
)
SAFE_BOUNDEDNESS_CLASSES = frozenset(
    {
        "fixed_snapshot",
        "fixed_private_replica_set",
        "bounded_records_and_bytes",
        "one_shot_receipt",
        "retention_window",
        "pending_only",
    }
)
ILLEGAL_REQUIRED_DISPOSITIONS = frozenset({"delete", "hard_fail"})


@dataclass(frozen=True, slots=True)
class FoundationArchitectureContract:
    """One Foundation truth or service authority domain."""

    contract_id: str
    kind: str
    owner: str
    producer: str
    consumers: tuple[str, ...]
    event_source: str
    validity_freshness_policy: str
    evidence_artifacts: tuple[str, ...]
    implementation: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class FoundationDurableWriterContract:
    """One durable writer mechanism visible at a Foundation/evidence boundary."""

    writer_id: str
    contract_id: str
    owner: str
    implementation: str
    artifacts: tuple[str, ...]
    mechanism: str
    atomicity: str
    boundedness: str
    disposition: str
    replacement_finding_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FoundationIllegalPathContract:
    """One displaced path that a later wave must delete or hard-fail."""

    finding_id: str
    path_id: str
    implementation: str
    competing_with: str
    reason: str
    legal_replacement: str
    required_disposition: str
    wave: int = 4


FOUNDATION_ARCHITECTURE_CONTRACTS: tuple[FoundationArchitectureContract, ...] = (
    FoundationArchitectureContract(
        contract_id="product_lifecycle",
        kind="service",
        owner="54guc.product_supervisor",
        producer="ProductSupervisor ordered startup, service, and shutdown",
        consumers=("Core", "Foundation", "NGUI launcher", "operator evidence"),
        event_source="product startup, canonical runtime clock, shutdown request",
        validity_freshness_policy=(
            "one process instance owns lifecycle; component failure is reported "
            "without converting failed state to healthy"
        ),
        evidence_artifacts=("shutdown_receipt.json", "runtime_health.json"),
        implementation="arch4.product_supervisor:ProductSupervisor",
    ),
    FoundationArchitectureContract(
        contract_id="foundation_composition",
        kind="service",
        owner="54guc.foundation.composition",
        producer="FoundationCompositionRoot",
        consumers=("ProductSupervisor", "Foundation child services"),
        event_source="startup, independent service events, canonical runtime clock",
        validity_freshness_policy=(
            "service failures are isolated and retain one incident identity; "
            "unrelated services continue"
        ),
        evidence_artifacts=("runtime_health.json", "cgf_evidence.sqlite3"),
        implementation="arch4.foundation.composition:FoundationCompositionRoot",
    ),
    FoundationArchitectureContract(
        contract_id="foundation_operational_root",
        kind="service",
        owner="54guc.foundation.operational_root",
        producer="FoundationOperationalRoot",
        consumers=("NGUI operational intent gateway",),
        event_source="typed operator request",
        validity_freshness_policy=(
            "one instance of each typed desktop service; no modem or "
            "Intelligence ownership"
        ),
        evidence_artifacts=(
            "site_report_failure_receipt.json",
            "support_bundle_failure_receipt.json",
        ),
        implementation="arch4.foundation.operational_root:FoundationOperationalRoot",
    ),
    FoundationArchitectureContract(
        contract_id="operational_lifecycle",
        kind="store",
        owner="54guc.product.operational_transaction",
        producer="ProductOperationalTransactionAuthority and NGUIOperationalOutbox",
        consumers=("Product operational dispatcher", "NGUI runtime transport"),
        event_source="typed operational request, terminal receipt, presentation acknowledgement",
        validity_freshness_policy=(
            "Product retains exact idempotency state; NGUI retains only "
            "requests pending Product acknowledgement"
        ),
        evidence_artifacts=(
            "product_operational_transactions.json",
            "ngui_operational_outbox.json",
        ),
        implementation=(
            "arch4.product_operational_transaction:"
            "ProductOperationalTransactionAuthority"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="operator_settings",
        kind="store",
        owner="arch4.foundation.operator_settings",
        producer="Foundation OperatorConfigService",
        consumers=(
            "Foundation data-budget reader",
            "settings handoff",
            "settings health",
        ),
        event_source="typed operator settings, traffic-plan or APN-profile commit",
        validity_freshness_policy=(
            "exact schema and exact modem/provider/profile scope; explicit zero "
            "is preserved; configured and enabled are separate; APN profiles "
            "use exact subscription/network/name scope and protected credentials"
        ),
        evidence_artifacts=("foundation_operator_settings.json",),
        implementation=(
            "arch4.foundation.operator_settings:FoundationOperatorSettingsStore"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="license_credentials",
        kind="store",
        owner="arch4.license_credential_store",
        producer="Foundation LicenseService through validated credential store",
        consumers=("license status", "settings handoff private projection"),
        event_source="typed license save/import/reconcile request",
        validity_freshness_policy=(
            "identity-bound nonce/key validation precedes commit; credentials "
            "remain private and are excluded from shareable support evidence"
        ),
        evidence_artifacts=(
            "license_credentials.json",
            "license_credentials.previous.json",
            "license.dat",
        ),
        implementation="arch4.license_credential_store:save_license_credentials",
    ),
    FoundationArchitectureContract(
        contract_id="schedule_lifecycle",
        kind="store",
        owner="54guc.foundation.schedule",
        producer="FoundationScheduleService",
        consumers=("Core Command Authority", "NGUI Foundation extension"),
        event_source=(
            "typed schedule/sequence intent, accepted Core observation, "
            "Core command outcome"
        ),
        validity_freshness_policy=(
            "one active plan; admission validates time; cancellation binds the "
            "exact plan and Core command; occurrence timestamps never restamp"
        ),
        evidence_artifacts=(
            "foundation_schedule_state.json",
            "54guc_foundation_schedule_replay.jsonl",
        ),
        implementation="arch4.foundation.schedule_service:FoundationScheduleService",
    ),
    FoundationArchitectureContract(
        contract_id="automation_configuration",
        kind="service",
        owner="54guc.foundation.automation",
        producer="FoundationAutomationService",
        consumers=("Core active configuration", "SMS runtime"),
        event_source="typed SMS-control configuration intent",
        validity_freshness_policy=(
            "one configuration generation commits only after named consumers "
            "acknowledge; invalid values are rejected, never defaulted"
        ),
        evidence_artifacts=("54guc_foundation_automation_replay.jsonl",),
        implementation=(
            "arch4.foundation.automation_service:FoundationAutomationService"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="antenna_guide",
        kind="service",
        owner="54guc.foundation.antenna_guide",
        producer="FoundationAntennaGuideService",
        consumers=("NGUI Dashboard antenna guide",),
        event_source="accepted Core observation and typed operator control",
        validity_freshness_policy=(
            "one explicit in-memory user session consumes accepted observation "
            "identity and sample time; application restart ends the session"
        ),
        evidence_artifacts=(
            "Foundation antenna-guide runtime register",
            "typed operational receipt",
        ),
        implementation="arch4.foundation.antenna_guide:FoundationAntennaGuideService",
    ),
    FoundationArchitectureContract(
        contract_id="connectivity_transition",
        kind="service",
        owner="54guc.foundation.connectivity_transition",
        producer="FoundationConnectivityTransitionProjector",
        consumers=("Foundation diagnostic journal", "NGUI Foundation extension"),
        event_source="accepted Core runtime health transition",
        validity_freshness_policy=(
            "one occurrence timestamp and sequence per real transition; "
            "unchanged state does not generate a new event"
        ),
        evidence_artifacts=("cgf_evidence.sqlite3", "ngui_page_truth.json"),
        implementation=(
            "arch4.foundation.connectivity_transition:"
            "FoundationConnectivityTransitionProjector"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="modem_diagnostic_projection",
        kind="boundary_adapter",
        owner="foundation.modem_diagnostic",
        producer="FoundationModemDiagnosticProjector",
        consumers=("Foundation diagnostic journal",),
        event_source="new Modem Live Matrix slow-line diagnostic sequence",
        validity_freshness_policy=(
            "validated Matrix only; each backend diagnostic sequence is "
            "recorded once and cannot become current modem truth"
        ),
        evidence_artifacts=("cgf_evidence.sqlite3",),
        implementation=(
            "arch4.foundation.modem_diagnostic:FoundationModemDiagnosticProjector"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="operator_data_budget",
        kind="service",
        owner="54guc.foundation.operator_data_budget",
        producer="FoundationOperatorDataBudgetReader and projection",
        consumers=("Foundation NGUI extension", "site report"),
        event_source="accepted qualified traffic Matrix generation",
        validity_freshness_policy=(
            "exact operator scope and source sample identity; unconfigured "
            "defaults remain drafts and are not enabled truth"
        ),
        evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
        implementation=(
            "arch4.foundation.operator_data_budget_reader:"
            "FoundationOperatorDataBudgetReader"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="foundation_ngui_extension",
        kind="boundary_adapter",
        owner="54guc.foundation.ngui_extension",
        producer="FoundationNGUIExtensionPublisher",
        consumers=("Core extension hook", "NGUI Presentation Authority"),
        event_source="typed Foundation service state or terminal receipt",
        validity_freshness_policy=(
            "publication and local cache commit are one operation; occurrence "
            "time belongs to the source transition"
        ),
        evidence_artifacts=("current_state.json", "ngui_page_truth.json"),
        implementation=(
            "arch4.foundation.ngui_extension:FoundationNGUIExtensionPublisher"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="core_current_state_evidence",
        kind="store",
        owner="arch4.foundation.core_current_state_publisher",
        producer="FoundationCurrentStateClient and CoreCurrentStatePublisher",
        consumers=(
            "Foundation evidence snapshot",
            "runtime diagnostics",
            "support/report projections",
        ),
        event_source="accepted Core runtime snapshot publication",
        validity_freshness_policy=(
            "atomic complete snapshot with source sequence and publication time; "
            "no weaker direct reader may promote defaults"
        ),
        evidence_artifacts=("current_state.json",),
        implementation="arch4.foundation.runtime_artifacts:CoreCurrentStatePublisher",
    ),
    FoundationArchitectureContract(
        contract_id="runtime_health_evidence",
        kind="store",
        owner="arch4.foundation.runtime_health_publisher",
        producer="FoundationRuntimeHealthService and publisher",
        consumers=(
            "ProductSupervisor",
            "NGUI status",
            "Foundation evidence snapshot",
        ),
        event_source="worker, fault, writer, lifecycle, or recovery event",
        validity_freshness_policy=(
            "derived from every required worker, fault and writer; change "
            "identity includes all nested states"
        ),
        evidence_artifacts=("runtime_health.json",),
        implementation=(
            "arch4.foundation.runtime_artifacts:FoundationRuntimeHealthService"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="diagnostic_evidence",
        kind="journal",
        owner="arch4.foundation.cgf_evidence_store",
        producer="CgfEvidenceStore",
        consumers=(
            "Foundation evidence snapshot",
            "site report",
            "support bundle",
        ),
        event_source="typed incident, transition, action, or diagnostic event",
        validity_freshness_policy=(
            "sequence owns order; wall time is evidence and may move backward; "
            "one atomic bounded commit preserves sequence on failure"
        ),
        evidence_artifacts=("cgf_evidence.sqlite3",),
        implementation=(
            "arch4.foundation.runtime_artifacts:CgfEvidenceStore"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="settings_handoff",
        kind="service",
        owner="54guc.foundation.settings_handoff",
        producer="Foundation settings handoff publisher/importer",
        consumers=(
            "next compatible CGF launch",
            "operator settings",
            "license store",
            "NGUI connection hints",
        ),
        event_source="canonical owner-store commit and clean shutdown",
        validity_freshness_policy=(
            "same protected modem identity; validated Matrix reader only; "
            "settings-only archive; no password or history import"
        ),
        evidence_artifacts=(
            "_portable_next_start_handoff/"
            "next_start_settings_handoff__<backend>__<immutable-id>.zip",
            "portable_confirmed_handoff_settings_import.json",
        ),
        implementation=(
            "arch4.foundation.settings_handoff:"
            "publish_next_start_settings_handoff"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="external_settings_import",
        kind="service",
        owner="54guc.foundation.external_settings_import",
        producer="Foundation external settings transaction",
        consumers=(
            "Core runtime defaults store",
            "NGUI settings store",
            "Foundation operator settings store",
        ),
        event_source="one explicit startup import source",
        validity_freshness_policy=(
            "intent receipt precedes mutations; source hash owns idempotent "
            "resume or rollback"
        ),
        evidence_artifacts=("foundation_external_settings_import_receipt.json",),
        implementation=(
            "arch4.foundation.external_settings_import:"
            "import_foundation_external_settings_once"
        ),
    ),
    FoundationArchitectureContract(
        contract_id="site_report",
        kind="report",
        owner="arch4.foundation.site_report",
        producer="Foundation Site Report service",
        consumers=("operator",),
        event_source="explicit typed operator report request",
        validity_freshness_policy=(
            "one coherent evidence snapshot identity and current run; report "
            "retention is enforced at one global report root"
        ),
        evidence_artifacts=(
            "site_report_*.html",
            "site_report_failure_receipt.json",
        ),
        implementation="arch4.foundation.site_report:generate_foundation_site_report",
    ),
    FoundationArchitectureContract(
        contract_id="site_report_job",
        kind="boundary_adapter",
        owner="arch4.foundation.site_report_job",
        producer="Foundation child-process report job",
        consumers=("Foundation ReportService", "operator failure evidence"),
        event_source="typed report request and child-process terminal result",
        validity_freshness_policy=(
            "bounded process lifetime and one terminal receipt correlated to "
            "the request and evidence snapshot"
        ),
        evidence_artifacts=("site_report_failure_receipt.json",),
        implementation="arch4.foundation.site_report_job:run_site_report_job",
    ),
    FoundationArchitectureContract(
        contract_id="support_bundle",
        kind="report",
        owner="arch4.foundation.support_bundle",
        producer="Foundation SupportService through Windows archive adapter",
        consumers=("operator", "support recipient"),
        event_source="explicit typed operator support request",
        validity_freshness_policy=(
            "deny-by-default coherent evidence snapshot; private credentials "
            "excluded; final ZIP reopened, manifest verified, and secret scanned"
        ),
        evidence_artifacts=(
            "54GUC_support_*.zip",
            "support_bundle_failure_receipt.json",
        ),
        implementation="arch4.windows_native_launcher:create_support_bundle",
    ),
    FoundationArchitectureContract(
        contract_id="support_bundle_job",
        kind="boundary_adapter",
        owner="arch4.support_bundle_job",
        producer="Foundation support bundle job",
        consumers=("Foundation SupportService", "operator failure evidence"),
        event_source="typed support request and exporter terminal result",
        validity_freshness_policy=(
            "one terminal receipt correlated to the request; exporter failure "
            "cannot crash sibling runtime owners"
        ),
        evidence_artifacts=("support_bundle_failure_receipt.json",),
        implementation="arch4.support_bundle_job:run_support_bundle_job",
    ),
    FoundationArchitectureContract(
        contract_id="support_privacy",
        kind="boundary_adapter",
        owner="arch4.privacy_scrub",
        producer="semantic path-aware support artifact scrubber",
        consumers=("support bundle snapshot/export service",),
        event_source="one captured support evidence snapshot",
        validity_freshness_policy=(
            "structure and semantic source identity drive redaction; counts "
            "measure substitutions; known current-run secrets cannot survive"
        ),
        evidence_artifacts=("bundle_manifest.json", "PRIVACY_NOTE.txt"),
        implementation="arch4.privacy_scrub:scrub_support_artifact",
    ),
    FoundationArchitectureContract(
        contract_id="foundation_evidence_snapshot",
        kind="service",
        owner="54guc.foundation.evidence_snapshot",
        producer="FoundationEvidenceSnapshotService",
        consumers=("site report", "support bundle", "runtime inspection"),
        event_source="explicit snapshot request over captured owner cursors",
        validity_freshness_policy=(
            "one immutable snapshot binds current state, health, incidents, "
            "commands, Scout evidence, and failure receipts"
        ),
        evidence_artifacts=("FoundationEvidenceSnapshot.v1",),
        implementation="arch4.foundation.evidence_snapshot:FoundationEvidenceSnapshotService",
    ),
    FoundationArchitectureContract(
        contract_id="foundation_incident_receipt",
        kind="service",
        owner="54guc.foundation.incident_receipt",
        producer="FoundationIncidentAuthority",
        consumers=("fault supervisor", "health", "journal", "service results"),
        event_source="one owner failure or recovery occurrence",
        validity_freshness_policy=(
            "one immutable incident identity and occurrence time shared by all "
            "projections"
        ),
        evidence_artifacts=("FoundationIncidentReceipt.v1",),
        implementation="arch4.foundation.incident_receipt:FoundationIncidentAuthority",
    ),
    FoundationArchitectureContract(
        contract_id="atomic_bounded_journal",
        kind="journal",
        owner="54guc.foundation.atomic_bounded_journal",
        producer="AtomicBoundedJournal",
        consumers=("Foundation diagnostics", "future NGUI parent evidence adapter"),
        event_source="typed evidence commit",
        validity_freshness_policy=(
            "append and retention are one atomic commit; sequence advances only "
            "after commit; wall clock does not own ordering"
        ),
        evidence_artifacts=("cgf_evidence.sqlite3",),
        implementation="arch4.foundation.atomic_bounded_journal:AtomicBoundedJournal",
    ),
)


FOUNDATION_DURABLE_WRITERS: tuple[FoundationDurableWriterContract, ...] = (
    FoundationDurableWriterContract(
        "operator_settings_json",
        "operator_settings",
        "arch4.foundation.operator_settings",
        "arch4.foundation.operator_settings:FoundationOperatorSettingsStore.save",
        ("foundation_operator_settings.json",),
        "typed owner-store JSON commit",
        "atomic_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "license_private_replicas",
        "license_credentials",
        "arch4.license_credential_store",
        "arch4.license_credential_store:save_license_credentials",
        (
            "license_credentials.json",
            "license_credentials.previous.json",
            "license.dat",
        ),
        "validated private credential commit and named projection",
        "atomic_replace",
        "fixed_private_replica_set",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "schedule_state_json",
        "schedule_lifecycle",
        "54guc.foundation.schedule",
        "arch4.foundation.schedule_service:FoundationScheduleService._publish",
        ("foundation_schedule_state.json",),
        "typed schedule state commit",
        "atomic_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "product_operational_transactions",
        "operational_lifecycle",
        "54guc.product.operational_transaction",
        (
            "arch4.product_operational_transaction:"
            "ProductOperationalTransactionAuthority._commit"
        ),
        ("product_operational_transactions.json",),
        "typed request-to-terminal-receipt transaction snapshot",
        "atomic_replace",
        "exact_idempotency_state",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "ngui_operational_outbox",
        "operational_lifecycle",
        "54guc.ngui.operational_outbox",
        (
            "arch4.ngui.operational_outbox:"
            "NGUIOperationalOutbox._commit"
        ),
        ("ngui_operational_outbox.json",),
        "typed pre-transport submission retained through visible acknowledgement",
        "atomic_replace",
        "pending_only",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "external_settings_import_receipt",
        "external_settings_import",
        "54guc.foundation.external_settings_import",
        (
            "arch4.foundation.external_settings_import:"
            "import_foundation_external_settings_once"
        ),
        ("foundation_external_settings_import_receipt.json",),
        "one-shot import completion receipt",
        "atomic_replace",
        "one_shot_receipt",
        "replacement_required",
        ("F-11",),
    ),
    FoundationDurableWriterContract(
        "settings_handoff_archive",
        "settings_handoff",
        "54guc.foundation.settings_handoff",
        (
            "arch4.foundation.settings_handoff:"
            "publish_next_start_settings_handoff"
        ),
        (
            "next_start_settings_handoff__<backend>__<immutable-id>.zip",
        ),
        "one exact-device staged settings ZIP then replace",
        "atomic_archive_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "settings_handoff_import_receipt",
        "settings_handoff",
        "54guc.foundation.settings_handoff",
        (
            "arch4.foundation.settings_handoff:"
            "import_confirmed_next_start_handoff_settings"
        ),
        ("portable_confirmed_handoff_settings_import.json",),
        "one-shot confirmed import receipt",
        "atomic_replace",
        "one_shot_receipt",
        "replacement_required",
        ("F-10", "F-11"),
    ),
    FoundationDurableWriterContract(
        "current_state_json",
        "core_current_state_evidence",
        "arch4.foundation.core_current_state_publisher",
        "arch4.foundation.runtime_artifacts:CoreCurrentStatePublisher.publish",
        ("current_state.json",),
        "complete current-state snapshot replacement",
        "atomic_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "runtime_health_json",
        "runtime_health_evidence",
        "arch4.foundation.runtime_health_publisher",
        "arch4.foundation.runtime_artifacts:FoundationRuntimeHealthPublisher.publish",
        ("runtime_health.json",),
        "complete runtime-health snapshot replacement",
        "atomic_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "foundation_cgf_evidence_store",
        "diagnostic_evidence",
        "arch4.foundation.cgf_evidence_store",
        "arch4.foundation.runtime_artifacts:CgfEvidenceStore.log_event",
        ("cgf_evidence.sqlite3",),
        "single-owner SQLite transaction with WAL recovery",
        "sqlite_transaction",
        "bounded_records_and_bytes",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "foundation_evidence_snapshot_json",
        "foundation_evidence_snapshot",
        "54guc.foundation.evidence_snapshot",
        (
            "arch4.foundation.evidence_snapshot:"
            "FoundationEvidenceSnapshotService.capture"
        ),
        ("foundation_evidence_snapshot.json",),
        "stable owner-cursor capture committed as one atomic snapshot",
        "atomic_replace",
        "fixed_snapshot",
        "canonical",
    ),
    FoundationDurableWriterContract(
        "site_report_html",
        "site_report",
        "arch4.foundation.site_report",
        "arch4.foundation.site_report:generate_foundation_site_report",
        ("site_report_*.html",),
        "explicit user-requested report commit",
        "atomic_replace",
        "current_run_retention_only",
        "replacement_required",
        ("F-21", "F-22"),
    ),
    FoundationDurableWriterContract(
        "site_report_failure_receipt",
        "site_report_job",
        "arch4.foundation.site_report_job",
        "arch4.foundation.site_report_job:run_site_report_job",
        ("site_report_failure_receipt.json",),
        "terminal report-job failure receipt",
        "atomic_replace",
        "one_shot_receipt",
        "replacement_required",
        ("F-23", "F-26"),
    ),
    FoundationDurableWriterContract(
        "support_bundle_archive",
        "support_bundle",
        "arch4.foundation.support_bundle",
        "arch4.windows_native_launcher:create_support_bundle",
        ("54GUC_support_*.zip",),
        "staged support ZIP through permanent Windows archive adapter",
        "atomic_archive_replace",
        "retention_window",
        "replacement_required",
        ("F-01", "F-02", "F-03", "F-23"),
    ),
    FoundationDurableWriterContract(
        "support_bundle_failure_receipt",
        "support_bundle_job",
        "arch4.support_bundle_job",
        "arch4.support_bundle_job:run_support_bundle_job",
        ("support_bundle_failure_receipt.json",),
        "terminal support-job failure receipt",
        "atomic_replace",
        "one_shot_receipt",
        "replacement_required",
        ("F-23", "F-26"),
    ),
    FoundationDurableWriterContract(
        "ngui_page_truth_json",
        "foundation_evidence_snapshot",
        "arch4.ngui.debug_surface.NGUIPageTruthLogWriter",
        "arch4.ngui.debug_surface:NGUIPageTruthLogWriter.publish",
        ("ngui_page_truth.json",),
        "explicit parent-process evidence boundary snapshot",
        "atomic_replace",
        "fixed_snapshot",
        "permanent_boundary_adapter",
    ),
    FoundationDurableWriterContract(
        "ngui_fault_journal",
        "atomic_bounded_journal",
        "54guc.ngui.fault_evidence",
        "arch4.ngui.fault_evidence:NGUIFaultEvidenceJournal.log_event",
        ("ngui_faults.jsonl",),
        "durable append followed by conditional compaction",
        "append_then_compact",
        "bounded_records_and_bytes",
        "replacement_required",
        ("N-30",),
    ),
    FoundationDurableWriterContract(
        "ngui_connection_hints_json",
        "settings_handoff",
        "arch4.ngui.settings_store",
        "arch4.ngui.settings_store:NGUISettingsStore.save",
        ("ngui_settings.json",),
        "non-secret connection-hint store",
        "atomic_replace",
        "fixed_snapshot",
        "permanent_boundary_adapter",
    ),
)


FOUNDATION_ILLEGAL_PATHS: tuple[FoundationIllegalPathContract, ...] = (
    FoundationIllegalPathContract(
        "F-01",
        "support_raw_license_artifact",
        "arch4.foundation.support_bundle:LIGHT_RUNTIME_ARTIFACT_FILENAMES",
        "support_privacy",
        "raw license credentials are selected for a shareable bundle",
        "exclude credentials and export only typed non-secret license status",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-02",
        "support_staging_only_privacy_check",
        "arch4.windows_native_launcher:_support_bundle_privacy_integrity",
        "support_privacy",
        "staged size/hash verification is not final ZIP privacy proof",
        "reopen final ZIP through its shared reader; verify CRC, schema, member counts and known secrets without rescrubbing",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-03",
        "support_independent_live_rereads",
        "arch4.windows_native_launcher:create_support_bundle",
        "foundation_evidence_snapshot",
        "selection, validation, and staging can observe different generations",
        "export one captured Foundation evidence snapshot",
        "hard_fail",
    ),
    FoundationIllegalPathContract(
        "F-04",
        "foundation_append_then_compact",
        "arch4.foundation.runtime_artifacts:CgfEvidenceStore.log_event",
        "atomic_bounded_journal",
        "append may commit while compaction and sequence commit fail",
        "one atomic bounded journal commit",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-05",
        "diagnostic_wall_clock_order",
        (
            "arch4.foundation.runtime_artifacts:"
            "foundation_diagnostic_event_record_errors"
        ),
        "diagnostic_evidence",
        "wall-clock rollback can reject all later journal records",
        "sequence owns ordering and wall time remains evidence only",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-06",
        "partial_runtime_health_derivation",
        "arch4.foundation.runtime_artifacts:FoundationRuntimeHealthService.mark",
        "runtime_health_evidence",
        "nested worker, fault, and writer state can be absent from degraded truth",
        "derive health and dedup identity from every required component",
        "hard_fail",
    ),
    FoundationIllegalPathContract(
        "F-07",
        "weaker_json_artifact_reader",
        "arch4.foundation.artifact_io:read_json_artifact",
        "foundation_evidence_snapshot",
        "direct defaults and unbounded reads compete with the canonical live reader",
        "Validated live reader plus captured evidence snapshot",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-08",
        "settings_handoff_raw_matrix_parser",
        (
            "arch4.foundation.settings_handoff:"
            "settings_handoff_identity_from_current_state"
        ),
        "settings_handoff",
        "raw lines/cells/value parsing bypasses ValidatedTruthMatrixReader",
        "validated Matrix reader or typed accepted Core observation only",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-09",
        "zero_allowance_default_substitution",
        (
            "arch4.foundation.settings_handoff:"
            "import_confirmed_next_start_handoff_settings"
        ),
        "operator_settings",
        "truthful zero allowance is converted to the numeric default",
        "preserve explicit zero and separate configured/enabled state",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-10",
        "clean_exit_only_handoff",
        "arch4.ngui.orchestrator:NGUIOrchestrator.stop",
        "settings_handoff",
        "latest canonical settings can be lost on forced termination",
        "coalesced handoff after owner-store commits plus shutdown publication",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-11",
        "sequential_external_import_mutation",
        (
            "arch4.foundation.external_settings_import:"
            "import_foundation_external_settings_once"
        ),
        "external_settings_import",
        "three owner stores can commit before the completion receipt",
        "intent-first source-hash transaction with resumable completion",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-12",
        "active_schedule_replacement",
        "arch4.foundation.schedule_service:FoundationScheduleService._publish",
        "schedule_lifecycle",
        "a new plan can overwrite an armed or executing plan",
        "reject conflicting active plans at the sole schedule authority",
        "hard_fail",
    ),
    FoundationIllegalPathContract(
        "F-13",
        "schedule_state_only_cancel",
        (
            "arch4.foundation.schedule_service:"
            "FoundationScheduleService.cancel_band_schedule"
        ),
        "schedule_lifecycle",
        "schedule state can say canceled while the Core command continues",
        "cancel exact plan and exact command under declared terminal semantics",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-14",
        "invalid_schedule_time_armed",
        "arch4.foundation.schedule_service:_activation_due_ts",
        "schedule_lifecycle",
        "invalid syntax can become an armed plan that never activates",
        "validate syntax and reject at admission",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-15",
        "sms_queue_time_ack_delete",
        "arch4.sms_receiver:SmsRuntimeBoundary.acknowledge_command_lifecycle",
        "automation_configuration",
        "message may be acknowledged or deleted before terminal Core outcome",
        "correlate message to Command Authority terminal policy",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-16",
        "configuration_commit_before_consumer_ack",
        (
            "arch4.foundation.automation_service:"
            "FoundationAutomationService._submit_operator_config"
        ),
        "automation_configuration",
        "Core config can commit before SMS side effects succeed",
        "one config generation with named consumer acknowledgments",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-17",
        "silent_watchdog_defaults",
        "arch4.foundation.modem_watchdog:normalize_modem_watchdog_configuration",
        "automation_configuration",
        "invalid values and unavailable cache operations can be hidden",
        "reject invalid values and publish unsupported operations",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-19",
        "schedule_state_restamp",
        (
            "arch4.foundation.ngui_extension:"
            "FoundationNGUIExtensionPublisher.record_schedule_state"
        ),
        "foundation_ngui_extension",
        "unchanged schedule state receives the current service-cycle timestamp",
        "retain the actual schedule transition occurrence time",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-20",
        "ngui_extension_cache_before_publication",
        "arch4.foundation.ngui_extension:FoundationNGUIExtensionPublisher._record",
        "foundation_ngui_extension",
        "local values can advance when external publication fails",
        "atomic publish-and-cache commit or rollback",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-21",
        "site_report_independent_artifact_reads",
        "arch4.foundation.site_report:load_foundation_site_report_snapshot",
        "foundation_evidence_snapshot",
        "current state, health, events, and settings have no shared snapshot identity",
        "generate only from one captured Foundation evidence snapshot",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-22",
        "site_report_current_run_retention",
        "arch4.foundation.site_report:enforce_foundation_site_report_retention",
        "site_report",
        "retention does not cover older run directories",
        "one global report root and retention authority",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-23",
        "failure_receipts_omitted_from_support",
        "arch4.foundation.support_bundle:LIGHT_RUNTIME_ARTIFACT_FILENAMES",
        "support_bundle",
        "support and report failures are missing from the evidence export",
        "include bounded correlated terminal failure receipts",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-24",
        "unconfigured_plan_presented_enabled",
        "arch4.foundation.data_budget:project_foundation_data_budget",
        "operator_data_budget",
        "draft defaults can be rendered as configured enabled truth",
        "publish explicit configured and enabled fields",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-25",
        "wide_serial_foundation_service_cycle",
        "arch4.foundation.composition:FoundationCompositionRoot.service_once",
        "foundation_composition",
        "independent services share one wide serial orchestration train",
        "independent canonical clock/event jobs with explicit dependencies only",
        "delete",
    ),
    FoundationIllegalPathContract(
        "F-26",
        "uncorrelated_failure_representations",
        (
            "arch4.foundation.composition:"
            "FoundationCompositionRoot._record_service_job_completion"
        ),
        "foundation_incident_receipt",
        "service failure, journal event, fault and health use separate identities",
        "one incident receipt with declared projections",
        "delete",
    ),
    FoundationIllegalPathContract(
        "N-30",
        "ngui_append_then_compact",
        "arch4.ngui.fault_evidence:NGUIFaultEvidenceJournal.log_event",
        "atomic_bounded_journal",
        "NGUI parent evidence repeats Foundation sequence/compaction defects",
        "the same atomic bounded journal through a permanent parent adapter",
        "delete",
    ),
)


FOUNDATION_CONTRACT_BY_ID: Mapping[
    str, FoundationArchitectureContract
] = MappingProxyType(
    {contract.contract_id: contract for contract in FOUNDATION_ARCHITECTURE_CONTRACTS}
)
FOUNDATION_WRITER_BY_ID: Mapping[
    str, FoundationDurableWriterContract
] = MappingProxyType(
    {writer.writer_id: writer for writer in FOUNDATION_DURABLE_WRITERS}
)
FOUNDATION_ILLEGAL_PATH_BY_ID: Mapping[
    str, FoundationIllegalPathContract
] = MappingProxyType(
    {path.path_id: path for path in FOUNDATION_ILLEGAL_PATHS}
)


__all__ = (
    "FOUNDATION_ARCHITECTURE_CONTRACTS",
    "FOUNDATION_CONTRACT_BY_ID",
    "FOUNDATION_CONTRACT_KINDS",
    "FOUNDATION_CONTRACT_REQUIRED_METADATA",
    "FOUNDATION_CONTRACT_STATUSES",
    "FOUNDATION_DURABLE_WRITERS",
    "FOUNDATION_ILLEGAL_PATHS",
    "FOUNDATION_ILLEGAL_PATH_BY_ID",
    "FOUNDATION_WRITER_BY_ID",
    "ILLEGAL_REQUIRED_DISPOSITIONS",
    "SAFE_ATOMICITY_CLASSES",
    "SAFE_BOUNDEDNESS_CLASSES",
    "WRITER_ATOMICITY_CLASSES",
    "WRITER_BOUNDEDNESS_CLASSES",
    "WRITER_CORRECTNESS_RETENTION_CLASSES",
    "WRITER_DISPOSITIONS",
    "FoundationArchitectureContract",
    "FoundationDurableWriterContract",
    "FoundationIllegalPathContract",
)
