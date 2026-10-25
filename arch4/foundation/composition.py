"""Foundation composition for the CGF product."""

from __future__ import annotations

import os
import logging
from pathlib import Path
from threading import RLock
from time import time
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.architecture_contracts.application_lifecycle import (
    CoreStartupCandidate,
    FoundationStartupDisposition,
    FoundationStartupReceipt,
    ProductChildTerminationReceipt,
    ProductRuntimeAdmissionAuthority,
    ProductStartupReceipt,
    ProductTerminationDisposition,
)
from arch4.core_contracts.clients import CoreClientManifest, CoreClientRole
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.accepted_generation_root import (
    CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
    FOUNDATION_NGUI_EXTENSION_HOOK_ID,
    FOUNDATION_NGUI_EXTENSION_SCHEMA,
)
from arch4.core.command_outcome_stream import CORE_COMMAND_OUTCOME_HOOK_ID
from arch4.command_transactions import COMMAND_TERMINAL_RECEIPT_SCHEMA
from arch4.foundation.automation_service import FoundationAutomationService
from arch4.foundation.band_auto_policy import ProviderBandProfileAuthority
from arch4.customer_contracts.network_suggestions import NetworkSuggestions
from arch4.foundation.connectivity_transition import (
    FoundationConnectivityTransitionProjector,
)
from arch4.foundation.antenna_guide import (
    FOUNDATION_ANTENNA_GUIDE_CLIENT_ID,
    FoundationAntennaGuideService,
)
from arch4.foundation.incident_receipt import FoundationIncidentAuthority
from arch4.foundation.modem_diagnostic import (
    FOUNDATION_MODEM_DIAGNOSTIC_CLIENT_ID,
    FoundationModemDiagnosticProjector,
)
from arch4.foundation.data_budget import (
    FOUNDATION_DATA_BUDGET_CLIENT_ID,
    FoundationDataBudgetProjection,
)
from arch4.foundation.ngui_extension import (
    FOUNDATION_NGUI_PROVIDER_ID,
    FoundationPersistencePresentationReceipt,
    FoundationNGUIExtensionPublisher,
    FoundationRuntimeHealthPresentationReceipt,
)
from arch4.foundation.operator_data_budget_reader import (
    FoundationOperatorDataBudgetReader,
    project_operator_data_budget_from_accepted_root,
)
from arch4.foundation.traffic_accounting import (
    FoundationTrafficAccountingAuthority,
    resolve_traffic_accounting_root,
)
from arch4.foundation.runtime_artifacts import (
    EVENT_OWNER,
    FOUNDATION_CURRENT_STATE_CLIENT_ID,
    CoreCurrentStatePublisher,
    FoundationCurrentStateClient,
    CgfEvidenceStore,
    FoundationRuntimeHealthPublisher,
    FoundationRuntimeHealthService,
)
from arch4.foundation.schedule_service import (
    FOUNDATION_SCHEDULE_CLIENT_ID,
    FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID,
    FoundationScheduleService,
)
from arch4.foundation.service_job_fabric import (
    FoundationServiceJobAdmission,
    FoundationServiceJobCompletion,
    FoundationServiceJobFabric,
    FoundationServiceJobSpec,
    require_complete_job_calls,
)
from arch4.foundation.sms_automation import (
    FOUNDATION_SMS_OUTCOME_CLIENT_ID,
    drain_sms_core_command_outcomes,
    submit_sms_core_commands,
)
from arch4.sms_receiver import (
    SMS_COMMAND_RECEIPTS_FILENAME,
    SmsCommandPolicyAuthority,
    SmsInboxAuthority,
    SmsRuntimeBoundary,
    build_sms_transport,
)
FOUNDATION_OWNER = "54guc.foundation.composition"
log = logging.getLogger("arch4.foundation.composition")

FOUNDATION_SERVICE_JOB_SPECS: tuple[FoundationServiceJobSpec, ...] = (
    FoundationServiceJobSpec(
        "connectivity_projection",
        "connectivity_projection",
    ),
    FoundationServiceJobSpec(
        "modem_diagnostic_projection",
        "modem_diagnostic_projection",
    ),
    FoundationServiceJobSpec("sms_inbox_pipeline", "sms_runtime"),
    FoundationServiceJobSpec(
        "sms_outcome_progression",
        "sms_runtime",
    ),
    FoundationServiceJobSpec("schedule_pipeline", "schedule_pipeline"),
    FoundationServiceJobSpec("antenna_pipeline", "antenna_pipeline"),
    FoundationServiceJobSpec(
        "watchdog_presentation",
        "watchdog_presentation",
    ),
    FoundationServiceJobSpec("data_budget_pipeline", "data_budget_pipeline"),
    FoundationServiceJobSpec(
        "runtime_health_pipeline",
        "runtime_health_pipeline",
    ),
    FoundationServiceJobSpec(
        "current_state_publication",
        "current_state_publication",
    ),
)


class FoundationCompositionRoot:
    """One light Foundation owner consuming only declared direct Core ports."""

    def __init__(
        self,
        core: CoreRuntimePort,
        *,
        state_dir: str | Path,
        runtime_dir: str | Path,
        diagnostics: CgfEvidenceStore,
        product_admission: ProductRuntimeAdmissionAuthority,
    ) -> None:
        if core is None:
            raise TypeError("foundation_composition_core_port_required")
        if not isinstance(diagnostics, CgfEvidenceStore):
            raise TypeError("foundation_composition_evidence_store_required")
        if getattr(core, "diagnostics", None) is not diagnostics:
            raise RuntimeError("foundation_diagnostic_writer_identity_mismatch")
        if not isinstance(
            product_admission,
            ProductRuntimeAdmissionAuthority,
        ):
            raise TypeError("foundation_product_admission_authority_required")
        self.core = core
        self._product_admission = product_admission
        self.state_dir = Path(state_dir).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        for path in (self.state_dir, self.runtime_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.diagnostics = diagnostics
        self._resources_prepared = False
        self._quiescing = False
        self._shutdown = False
        self._quiesce_receipt: ProductChildTerminationReceipt | None = None
        self._final_receipt: ProductChildTerminationReceipt | None = None
        self._service_cycles = 0
        self._runtime_namespace: RuntimeGenerationNamespace | None = None
        self._prepared_receipt: FoundationStartupReceipt | None = None
        self._namespace_lock = RLock()
        self.incidents = FoundationIncidentAuthority()
        self._service_jobs = FoundationServiceJobFabric(
            FOUNDATION_SERVICE_JOB_SPECS
        )
        self._service_job_latest: dict[str, Any] = {}

        self.provider_band_profiles = ProviderBandProfileAuthority(NetworkSuggestions(
            core.cfg.network_country_suggestion,
            core.cfg.network_provider_suggestion,
            core.cfg.network_bands_suggestion,
        ))
        bind_band_policy = getattr(
            getattr(core, "command_gateway", None),
            "bind_band_auto_policy_provider",
            None,
        )
        if not callable(bind_band_policy):
            raise RuntimeError("core_band_auto_policy_port_unavailable")
        bind_band_policy(self.provider_band_profiles.resolve_contract)

        self._attach_core_clients()
        self.current_state = FoundationCurrentStateClient(
            snapshot_port=core.runtime_snapshot_publisher,
            current_health_provider=core.current_health_view,
            publisher=CoreCurrentStatePublisher(
                self.runtime_dir,
                fault_reporter=self._report_artifact_fault,
            ),
            fault_reporter=self._report_artifact_fault,
        )
        self.runtime_health = FoundationRuntimeHealthService(
            publisher=FoundationRuntimeHealthPublisher(
                self.runtime_dir,
                fault_reporter=self._report_artifact_fault,
            ),
            modem_backend=str(getattr(core.cfg, "modem_backend", "") or ""),
            probe_mode=bool(core.probe_mode),
            observe_only=bool(core.observe_only),
            check_only=bool(core.check_only),
            instance_id=str(os.environ.get("ARCH_RUNTIME_INSTANCE_ID", "") or ""),
            artifact_status_provider=self._artifact_status,
            incident_status_provider=self.incidents.snapshot,
        )
        self.ngui_extension = FoundationNGUIExtensionPublisher()
        self.connectivity_transitions = FoundationConnectivityTransitionProjector(
            diagnostics=self.diagnostics,
            ngui_extension=self.ngui_extension,
        )
        self.modem_diagnostics = FoundationModemDiagnosticProjector(
            self.diagnostics
        )
        self.operator_data_budget_reader = FoundationOperatorDataBudgetReader(
            self.runtime_dir
        )
        self.traffic_accounting = FoundationTrafficAccountingAuthority(
            resolve_traffic_accounting_root(self.state_dir)
        )

        inbox = SmsInboxAuthority(
            build_sms_transport(core.input_module),
            readiness_provider=self._sms_identity_ready,
            poll_interval_s=float(
                getattr(core.cfg, "sms_inbox_poll_interval_s", 121.0) or 121.0
            ),
            support_provider=self._sms_supported_by_current_matrix,
        )
        policy = SmsCommandPolicyAuthority(
            enabled=bool(getattr(core.cfg, "sms_command_enabled", False)),
            receipt_path=self.state_dir / SMS_COMMAND_RECEIPTS_FILENAME,
            approved_senders=list(
                getattr(core.cfg, "sms_command_approved_senders", ()) or ()
            ),
            code_word=str(getattr(core.cfg, "sms_command_code_word", "reboot") or "reboot"),
            delete_processed=bool(
                getattr(core.cfg, "sms_command_delete_processed", False)
            ),
        )
        self.sms_runtime = SmsRuntimeBoundary(inbox, policy)
        self.automation = FoundationAutomationService(
            core,
            sms_runtime=self.sms_runtime,
            persistence_dir=self.state_dir,
        )
        self.schedule = FoundationScheduleService(
            core,
            persistence_dir=self.state_dir,
            fault_reporter=self._report_artifact_fault,
        )
        self.antenna_guide = FoundationAntennaGuideService(core)

    def _prepare_runtime_namespace(
        self,
        core_candidate: CoreStartupCandidate,
    ) -> bool:
        """Bind Foundation resources to the active Modem Live namespace."""

        if not isinstance(core_candidate, CoreStartupCandidate):
            raise TypeError("foundation_composition_core_candidate_required")
        matrix_readiness = core_candidate.matrix_readiness
        if not matrix_readiness.ready:
            raise RuntimeError("foundation_composition_matrix_not_ready")
        namespace = matrix_readiness.matrix_identity.namespace
        with self._namespace_lock:
            if self._shutdown or self._quiescing:
                raise RuntimeError("foundation_composition_namespace_bind_closed")
            try:
                published_root = self._product_admission.accepted_root()
            except RuntimeError:
                published_root = None
            if published_root is not None:
                raise RuntimeError(
                    "foundation_composition_root_rebind_after_product_admission"
                )
            if self._runtime_namespace == namespace:
                return False
            candidate = self.ngui_extension.prepare_runtime_namespace(namespace)
            if candidate is None:
                raise RuntimeError(
                    "foundation_composition_namespace_state_mismatch"
                )
            # Close the old effect gate first. Work already inside can finish;
            # later work from the old namespace is rejected.
            self._service_jobs.bind_runtime_namespace(namespace)
            self.ngui_extension.commit_runtime_namespace(candidate)
            self._runtime_namespace = namespace
            self._service_job_latest = {}
            return True

    def _require_runtime_namespace(self) -> RuntimeGenerationNamespace:
        namespace = self._runtime_namespace
        if namespace is None:
            raise RuntimeError("foundation_composition_namespace_unbound")
        return namespace

    def _sms_identity_ready(self) -> bool:
        """Adapt the bound runtime namespace to ModemLive's identity owner."""
        namespace = self._runtime_namespace
        return namespace is not None and self.core.input_module.sms_identity_ready(
            namespace=namespace,
        )

    def _sms_supported_by_current_matrix(self) -> bool:
        """Return only current-namespace Modem Live Matrix SMS support truth."""

        namespace = self._runtime_namespace
        root = self.core.accepted_root_outbox.current()
        if (
            namespace is None
            or not isinstance(root, CoreAcceptedGenerationRoot)
            or root.matrix_identity.namespace != namespace
        ):
            return False
        try:
            fact = root.current_facts.exact("capability.sms_supported")
        except KeyError:
            return False
        if fact.disposition != "available":
            return False
        envelope = fact.value.to_mapping()
        return bool(
            envelope.get("present") is True
            and envelope.get("value") is True
        )

    def runtime_namespace(self) -> RuntimeGenerationNamespace:
        """Return the active namespace, or fail while it is not bound."""

        with self._namespace_lock:
            return self._require_runtime_namespace()

    @staticmethod
    def _preparation_receipt(
        namespace: RuntimeGenerationNamespace,
        *,
        prepared_at: float,
    ) -> FoundationStartupReceipt:
        return FoundationStartupReceipt(
            disposition=FoundationStartupDisposition.PREPARED,
            namespace=namespace,
            prepared_at=prepared_at,
        )

    def _require_product_admission(self) -> ProductStartupReceipt:
        receipt = self._product_admission.accepted_root()
        if (
            receipt.foundation is None
            or receipt.foundation != self._prepared_receipt
            or receipt.foundation.namespace != self._require_runtime_namespace()
        ):
            raise RuntimeError("foundation_product_external_admission_closed")
        return receipt

    def validate_product_ready_root(
        self,
        receipt: ProductStartupReceipt,
    ) -> None:
        """Pure pre-publication validation of the prepared Product root."""

        if not isinstance(receipt, ProductStartupReceipt) or not receipt.ready:
            raise TypeError("foundation_product_ready_receipt_required")
        with self._namespace_lock:
            prepared = self._prepared_receipt
            if prepared is None:
                raise RuntimeError("foundation_startup_not_prepared")
            if (
                receipt.foundation != prepared
                or receipt.matrix_readiness is None
                or receipt.matrix_readiness.matrix_identity.namespace
                != prepared.namespace
            ):
                raise RuntimeError(
                    "foundation_product_ready_root_mismatch"
                )

    def run_bound_effect(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity,
        dependency_group: str,
        effect,
    ) -> Any:
        """Final synchronous admission for Foundation commands and writes."""

        with self._namespace_lock:
            self._require_product_admission()
            namespace = self._require_runtime_namespace()
            if matrix_identity.namespace != namespace:
                raise RuntimeError(
                    "foundation_composition_matrix_namespace_not_active"
                )
            return self._service_jobs.run_bound_effect(
                dependency_group=dependency_group,
                admission=FoundationServiceJobAdmission(
                    namespace=namespace,
                    service_cycle=self._service_cycles,
                ),
                effect=effect,
            )

    def _attach_core_clients(self) -> None:
        hooks = self.core.extension_hooks
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_CURRENT_STATE_CLIENT_ID,
                role=CoreClientRole.DIAGNOSTIC,
                contract_versions=(CORE_RUNTIME_SNAPSHOT_SCHEMA,),
                queue_budget=1,
                required=False,
            ),
            hook_id=CORE_RUNTIME_SNAPSHOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_DATA_BUDGET_CLIENT_ID,
                role=CoreClientRole.OBSERVER,
                contract_versions=(CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,),
                queue_budget=8,
                required=False,
            ),
            hook_id="accepted_observation",
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_MODEM_DIAGNOSTIC_CLIENT_ID,
                role=CoreClientRole.DIAGNOSTIC,
                contract_versions=(CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,),
                queue_budget=8,
                required=False,
            ),
            hook_id="accepted_observation",
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID,
                role=CoreClientRole.OPERATOR_AUTOMATION,
                contract_versions=(COMMAND_TERMINAL_RECEIPT_SCHEMA,),
                queue_budget=32,
                required=False,
            ),
            hook_id=CORE_COMMAND_OUTCOME_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_SMS_OUTCOME_CLIENT_ID,
                role=CoreClientRole.OPERATOR_AUTOMATION,
                contract_versions=(COMMAND_TERMINAL_RECEIPT_SCHEMA,),
                queue_budget=32,
                required=False,
            ),
            hook_id=CORE_COMMAND_OUTCOME_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_SCHEDULE_CLIENT_ID,
                role=CoreClientRole.FOUNDATION_SERVICE,
                contract_versions=(CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,),
                queue_budget=8,
                required=False,
            ),
            hook_id="accepted_observation",
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_ANTENNA_GUIDE_CLIENT_ID,
                role=CoreClientRole.FOUNDATION_SERVICE,
                contract_versions=(CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,),
                queue_budget=8,
                required=False,
            ),
            hook_id="accepted_observation",
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        hooks.attach(
            CoreClientManifest(
                client_id=FOUNDATION_NGUI_PROVIDER_ID,
                role=CoreClientRole.FOUNDATION_SERVICE,
                contract_versions=(FOUNDATION_NGUI_EXTENSION_SCHEMA,),
                queue_budget=1,
                required=False,
            ),
            hook_id=FOUNDATION_NGUI_EXTENSION_HOOK_ID,
            mode=CoreHookAttachmentMode.PROVIDER,
        )

    def _report_artifact_fault(
        self,
        owner: str,
        operation: str,
        error: BaseException,
        degraded_behavior: str,
    ) -> None:
        receipt = self._incident_authority().record_failure(
            fault_owner=str(owner or FOUNDATION_OWNER),
            operation=str(operation or "foundation_artifact"),
            generation=self._service_cycles,
            severity="error",
            retryable=True,
            error=error,
            degraded_behavior=str(degraded_behavior or "retain_runtime"),
        )
        if str(owner or "") != EVENT_OWNER:
            self.diagnostics.log_event(
                "foundation_artifact_fault",
                ts=receipt.occurred_at,
                transaction_id=receipt.incident_id,
                event_owner=receipt.fault_owner,
                operation=receipt.operation,
                severity="ERROR",
                detail=receipt.detail,
                degraded_behavior=receipt.degraded_behavior,
                incident=receipt.to_payload(),
            )

    def _incident_authority(self) -> FoundationIncidentAuthority:
        authority = getattr(self, "incidents", None)
        if not isinstance(authority, FoundationIncidentAuthority):
            authority = FoundationIncidentAuthority()
            self.incidents = authority
        return authority

    def _artifact_status(self) -> dict[str, Any]:
        return {
            "current_state": self.current_state.status().to_payload(),
            "diagnostics": self.diagnostics.status().to_payload(),
            "signal_baselines": self.core.signals.baseline_persistence_status(),
        }

    def _publish_runtime_status_presentation(self) -> object:
        namespace = self._require_runtime_namespace()
        snapshot = self.runtime_health.latest()
        if snapshot is None:
            raise RuntimeError("foundation_runtime_health_snapshot_unavailable")
        artifacts = self._artifact_status()
        required = ("current_state", "diagnostics", "signal_baselines")
        if any(not isinstance(artifacts.get(name), Mapping) for name in required):
            raise RuntimeError("foundation_persistence_status_incomplete")
        persistence_healthy = all(
            artifacts[name].get("healthy") is True for name in required
        )
        return self.ngui_extension.record_runtime_status(
            namespace=namespace,
            health=FoundationRuntimeHealthPresentationReceipt(
                status="DEGRADED" if snapshot.degradation_reasons else "OK",
                observed_at=snapshot.observed_at,
            ),
            persistence=FoundationPersistencePresentationReceipt(
                status="OK" if persistence_healthy else "DEGRADED",
                observed_at=snapshot.observed_at,
            ),
        )

    def prepare_startup(
        self,
        core_candidate: CoreStartupCandidate,
    ) -> FoundationStartupReceipt:
        """Prepare Foundation resources for one runtime namespace."""

        with self._namespace_lock:
            if self._shutdown:
                raise RuntimeError("foundation_composition_already_shutdown")
            if self._prepared_receipt is not None:
                if (
                    self._prepared_receipt.namespace
                    != core_candidate.matrix_readiness.matrix_identity.namespace
                ):
                    raise RuntimeError(
                        "foundation_startup_candidate_overlap"
                    )
                return self._prepared_receipt
            self._prepare_runtime_namespace(core_candidate)
            namespace = self._require_runtime_namespace()
            if namespace != core_candidate.matrix_readiness.matrix_identity.namespace:
                raise RuntimeError(
                    "foundation_startup_core_candidate_matrix_mismatch"
                )
            receipt = self._preparation_receipt(
                namespace,
                prepared_at=float(time()),
            )
            self._prepared_receipt = receipt
            return receipt

    def activate_prepared_runtime(
        self,
        product_receipt: ProductStartupReceipt,
    ) -> None:
        """Activate prepared resources inside Product's install transaction."""

        if (
            not isinstance(product_receipt, ProductStartupReceipt)
            or not product_receipt.ready
        ):
            raise TypeError(
                "foundation_runtime_activation_product_receipt_required"
            )
        with self._namespace_lock:
            if self._shutdown or self._quiescing:
                raise RuntimeError(
                    "foundation_runtime_activation_closed"
                )
            prepared = self._prepared_receipt
            if prepared is None:
                raise RuntimeError(
                    "foundation_runtime_activation_not_prepared"
                )
            if (
                product_receipt.foundation != prepared
                or product_receipt.matrix_readiness is None
                or product_receipt.matrix_readiness.matrix_identity.namespace
                != prepared.namespace
            ):
                raise RuntimeError(
                    "foundation_runtime_activation_root_mismatch"
                )
            if self._resources_prepared:
                return
            self._resources_prepared = True

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        if (
            not self._resources_prepared
            or self._quiescing
            or self._shutdown
        ):
            raise RuntimeError("foundation_composition_not_running")
        self._require_product_admission()
        observed_at = float(time() if now is None else now)
        self._service_cycles += 1
        cycle_count = self._service_cycles
        admission = FoundationServiceJobAdmission(
            namespace=self._require_runtime_namespace(),
            service_cycle=cycle_count,
        )
        completed = self._service_jobs.collect()
        for receipt in completed:
            self._record_service_job_completion(receipt)
        calls = require_complete_job_calls(
            FOUNDATION_SERVICE_JOB_SPECS,
            {
                "connectivity_projection": lambda context: (
                    context.run_effect(
                        self._project_connectivity_transition
                    )
                ),
                "modem_diagnostic_projection": lambda context: (
                    context.run_effect(
                        lambda: self._project_modem_diagnostic(
                            observed_at=observed_at
                        )
                    )
                ),
                "sms_inbox_pipeline": lambda context: context.run_effect(
                    lambda: self._service_sms_inbox(
                        observed_at=observed_at
                    )
                ),
                "sms_outcome_progression": lambda context: (
                    context.run_effect(self._service_sms_outcomes)
                ),
                "schedule_pipeline": lambda context: context.run_effect(
                    lambda: self._service_schedule(
                        observed_at=observed_at
                    )
                ),
                "antenna_pipeline": lambda context: context.run_effect(
                    lambda: self._service_antenna(
                        observed_at=observed_at
                    )
                ),
                "watchdog_presentation": lambda context: (
                    context.run_effect(
                        lambda: self._service_watchdog(
                            observed_at=observed_at
                        )
                    )
                ),
                "data_budget_pipeline": lambda context: context.run_effect(
                    lambda: self._service_data_budget(
                        observed_at=observed_at
                    )
                ),
                "runtime_health_pipeline": lambda context: (
                    context.run_effect(
                        lambda: self._service_runtime_health(
                            observed_at=observed_at,
                            cycle_count=cycle_count,
                        )
                    )
                ),
                "current_state_publication": lambda context: (
                    context.run_effect(
                        lambda: self.current_state.publish_if_due(
                            health=dict(self.core.worker_health.status()),
                            metadata={
                                "reason": "foundation_service_job",
                                "cycle_count": cycle_count,
                            },
                            published_at=observed_at,
                        )
                    )
                ),
            },
        )
        dispatches = tuple(
            self._service_jobs.dispatch(
                spec.job_id,
                admission=admission,
                requested_at=observed_at,
                call=calls[spec.job_id],
            )
            for spec in FOUNDATION_SERVICE_JOB_SPECS
        )
        incidents = self._incident_authority().snapshot()
        sms = dict(self._service_job_latest.get("sms_inbox_pipeline") or {})
        return {
            "owner": FOUNDATION_OWNER,
            "status": (
                "degraded"
                if int(incidents.get("active_count", 0) or 0) > 0
                else "ok"
            ),
            "cycle_count": self._service_cycles,
            "sms_commands": int(sms.get("commands", 0) or 0),
            "sms_submitted": int(sms.get("submitted", 0) or 0),
            "schedule": self._service_job_latest.get("schedule_pipeline"),
            "antenna_guide": self._service_job_latest.get(
                "antenna_pipeline"
            ),
            "jobs": self._service_jobs.status(),
            "dispatch": {
                receipt.job_id: receipt.disposition
                for receipt in dispatches
            },
            "completed": tuple(receipt.job_id for receipt in completed),
            "incidents": incidents,
        }

    def _service_sms_inbox(self, *, observed_at: float) -> dict[str, int]:
        namespace = self._require_runtime_namespace()
        try:
            commands = tuple(self.sms_runtime.poll(now=observed_at))
            submitted = (
                submit_sms_core_commands(
                    self.core,
                    commands=commands,
                    sms_runtime=self.sms_runtime,
                    now=observed_at,
                )
                if commands
                else 0
            )
        finally:
            self._publish_sms_events()
        self.ngui_extension.record_sms_snapshot(
            self.sms_runtime.snapshot(),
            namespace=namespace,
            observed_at=observed_at,
        )
        return {"commands": len(commands), "submitted": int(submitted)}

    def _service_sms_outcomes(self) -> int:
        try:
            return drain_sms_core_command_outcomes(
                self.core,
                sms_runtime=self.sms_runtime,
            )
        finally:
            self._publish_sms_events()

    def _publish_sms_events(self) -> None:
        for event in self.sms_runtime.drain_runtime_events():
            data = dict(event)
            kind = data.pop("kind")
            self.diagnostics.log_event(
                kind, event_owner="sms_runtime", **data,
            )

    def _service_schedule(self, *, observed_at: float) -> Mapping[str, Any]:
        namespace = self._require_runtime_namespace()
        result = self.schedule.service_once(now=observed_at)
        state = self.schedule.state()
        if state:
            self.ngui_extension.record_schedule_state(
                state,
                namespace=namespace,
                observed_at=observed_at,
            )
        return dict(result)

    def _service_antenna(self, *, observed_at: float) -> Mapping[str, Any]:
        namespace = self._require_runtime_namespace()
        result = self.antenna_guide.service_once(now=observed_at)
        self.ngui_extension.record_antenna_guide_surface(
            self.antenna_guide.surface(now=observed_at),
            namespace=namespace,
            observed_at=observed_at,
        )
        return dict(result)

    def _service_watchdog(self, *, observed_at: float) -> object:
        return self.automation.service_modem_watchdog(now=observed_at)

    def _service_data_budget(
        self,
        *,
        observed_at: float,
    ) -> FoundationDataBudgetProjection | None:
        projection = self._project_data_budget(observed_at=observed_at)
        if projection is not None:
            self.ngui_extension.record_data_budget_projection(
                projection,
                namespace=self._require_runtime_namespace(),
            )
        return projection

    def _service_runtime_health(
        self,
        *,
        observed_at: float,
        cycle_count: int,
    ) -> object:
        del observed_at
        self.runtime_health.mark(
            phase="running",
            cycle_count=cycle_count,
            workers=dict(self.core.worker_health.status()),
            faults=dict(self.core.fault_supervisor.status()),
        )
        return self._publish_runtime_status_presentation()

    def _project_data_budget(
        self,
        *,
        observed_at: float,
    ) -> FoundationDataBudgetProjection | None:
        del observed_at
        projection: FoundationDataBudgetProjection | None = None
        for candidate in self._project_accepted_roots(
            FOUNDATION_DATA_BUDGET_CLIENT_ID,
            lambda root: project_operator_data_budget_from_accepted_root(
                self.operator_data_budget_reader,
                root,
                traffic_accounting=self.traffic_accounting,
            ),
        ):
            if candidate is not None:
                projection = candidate
        return projection

    def _project_accepted_roots(
        self,
        client_id: str,
        project,
    ) -> tuple[Any, ...]:
        """Acknowledge a root only after its customer update succeeds."""

        results: list[Any] = []
        outbox = self.core.accepted_root_outbox
        while True:
            record = outbox.claim_next(client_id)
            if record is None:
                return tuple(results)
            try:
                root = record.root
                if not isinstance(root, CoreAcceptedGenerationRoot):
                    raise TypeError(
                        "foundation_composition_accepted_root_required"
                    )
                result = project(root)
                outbox.acknowledge(client_id, key=record.key)
            except Exception as exc:
                outbox.release(
                    client_id,
                    key=record.key,
                    error=exc,
                )
                raise
            results.append(result)

    def _current_core_accepted_root(self) -> CoreAcceptedGenerationRoot:
        root = self.core.accepted_root_outbox.current()
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise RuntimeError(
                "foundation_composition_accepted_root_unavailable"
            )
        if root.matrix_identity.namespace != self._require_runtime_namespace():
            raise RuntimeError(
                "foundation_composition_accepted_root_namespace_mismatch"
            )
        return root

    def _project_connectivity_transition(self):
        current_health = self.core.current_health_view()
        if current_health.namespace != self._require_runtime_namespace():
            raise RuntimeError(
                "foundation_connectivity_health_namespace_mismatch"
            )
        return self.connectivity_transitions.observe(current_health)

    def _project_modem_diagnostic(
        self,
        *,
        observed_at: float,
    ):
        latest = None
        for candidate in self._project_accepted_roots(
            FOUNDATION_MODEM_DIAGNOSTIC_CLIENT_ID,
            lambda root: self.modem_diagnostics.observe(root, observed_at=observed_at),
        ):
            if candidate is not None:
                latest = candidate
        return latest

    def _record_service_job_completion(
        self,
        completion: FoundationServiceJobCompletion,
    ) -> None:
        if not isinstance(completion, FoundationServiceJobCompletion):
            raise TypeError("foundation_service_job_completion_required")
        job_id = completion.job_id
        if (
            completion.quarantined
            or completion.namespace != self._require_runtime_namespace()
        ):
            return
        if job_id not in {
            spec.job_id for spec in FOUNDATION_SERVICE_JOB_SPECS
        }:
            raise RuntimeError(
                f"foundation_service_job_completion_undeclared:{job_id}"
            )
        if completion.succeeded:
            self._service_job_latest[job_id] = completion.result
            try:
                recovery = self._incident_authority().record_recovery(
                    fault_owner=FOUNDATION_OWNER,
                    operation=job_id,
                    generation=self._service_cycles,
                )
                if recovery is not None:
                    self.diagnostics.log_event(
                        "foundation_service_job_recovered",
                        ts=recovery.recovered_at,
                        transaction_id=recovery.incident_id,
                        event_owner=FOUNDATION_OWNER,
                        operation=job_id,
                        severity="INFO",
                        detail="recovered",
                        incident=recovery.to_payload(),
                    )
            except Exception as recovery_error:
                # A storage failure cannot undo a completed service result.
                # Report it and let unrelated runtime work continue.
                log.error(
                    "Foundation recovery evidence failed for %s: %s: %s",
                    job_id,
                    type(recovery_error).__name__,
                    recovery_error,
                )
                try:
                    self._report_artifact_fault(
                        FOUNDATION_OWNER,
                        f"{job_id}_recovery_evidence",
                        recovery_error,
                        "retain_completed_service_result_and_retry_evidence",
                    )
                except Exception as report_error:
                    log.error(
                        "Foundation recovery-evidence fault reporting failed: %s: %s",
                        type(report_error).__name__,
                        report_error,
                    )
            return
        error = completion.error
        if not isinstance(error, Exception):
            raise TypeError("foundation_service_job_exception_required")
        receipt = self._incident_authority().record_failure(
            fault_owner=FOUNDATION_OWNER,
            operation=job_id,
            generation=self._service_cycles,
            severity="error",
            retryable=True,
            error=error,
            degraded_behavior=(
                f"foundation_{job_id}_failed_sibling_jobs_continue"
            ),
        )
        try:
            if not self.diagnostics.log_event(
                "foundation_service_job_failed",
                ts=receipt.occurred_at,
                transaction_id=receipt.incident_id,
                event_owner=FOUNDATION_OWNER,
                severity="ERROR",
                operation=job_id,
                cycle_count=self._service_cycles,
                error_type=type(error).__name__,
                error=str(error),
                degraded_behavior=receipt.degraded_behavior,
                incident=receipt.to_payload(),
            ):
                raise RuntimeError(
                    "foundation_evidence_store_commit_failed"
                )
        except Exception as report_error:
            log.error(
                "Foundation diagnostic projection failed for %s: %s: %s",
                job_id,
                type(report_error).__name__,
                report_error,
            )

    def _termination_generation(
        self,
        requested_generation: int | None = None,
    ) -> int:
        if requested_generation is not None and (
            type(requested_generation) is not int
            or requested_generation <= 0
        ):
            raise ValueError("foundation_termination_generation_invalid")
        try:
            receipt = self._product_admission.accepted_root()
        except RuntimeError:
            receipt = None
        if receipt is not None:
            owned_generation = receipt.lifecycle_generation
        elif self._prepared_receipt is not None:
            owned_generation = self._prepared_receipt.namespace.lifecycle_generation
        elif self._runtime_namespace is not None:
            owned_generation = self._runtime_namespace.lifecycle_generation
        elif requested_generation is not None:
            return requested_generation
        else:
            raise RuntimeError("foundation_termination_generation_unavailable")
        if (
            requested_generation is not None
            and requested_generation != owned_generation
        ):
            raise RuntimeError("foundation_termination_generation_mismatch")
        return owned_generation

    def quiesce(
        self,
        *,
        lifecycle_generation: int | None = None,
    ) -> ProductChildTerminationReceipt:
        """Close Foundation admission and account for every accepted job."""

        if self._quiesce_receipt is not None and self._quiesce_receipt.terminated:
            if (
                lifecycle_generation is not None
                and self._quiesce_receipt.lifecycle_generation
                != lifecycle_generation
            ):
                raise RuntimeError("foundation_quiesce_generation_mismatch")
            return self._quiesce_receipt
        if self._shutdown:
            raise RuntimeError("foundation_quiesce_after_finalize")
        self._quiescing = True
        completions = self._service_jobs.shutdown()
        failed_jobs: list[str] = []
        for completion in completions:
            self._record_service_job_completion(completion)
            if not completion.succeeded:
                failed_jobs.append(completion.job_id)
        jobs_status = self._service_jobs.status()
        survivors = tuple(jobs_status["surviving_jobs"])
        if jobs_status["active_effects"] and not survivors:
            survivors = ("foundation_active_effects",)
        receipt = ProductChildTerminationReceipt(
            child_id="foundation_jobs",
            lifecycle_generation=self._termination_generation(
                lifecycle_generation
            ),
            disposition=ProductTerminationDisposition.RETAINED if survivors else ProductTerminationDisposition.COMPLETE,
            survivors=survivors,
            evidence={
                "completed_jobs": tuple(
                    completion.job_id for completion in completions
                ),
                "failed_jobs": tuple(sorted(failed_jobs)),
                "jobs": jobs_status,
            },
        )
        self._quiesce_receipt = receipt
        return receipt

    def finalize(
        self,
        *,
        core_receipt: ProductChildTerminationReceipt,
        shutdown_errors: tuple[str, ...] = (),
    ) -> ProductChildTerminationReceipt:
        """Publish terminal Foundation truth after Core has stopped."""

        if self._final_receipt is not None:
            return self._final_receipt
        if self._quiesce_receipt is None:
            raise RuntimeError("foundation_finalize_before_quiesce")
        if not isinstance(core_receipt, ProductChildTerminationReceipt):
            raise TypeError("foundation_finalize_core_termination_receipt_required")
        if core_receipt.child_id != "core":
            raise RuntimeError("foundation_finalize_core_child_identity_mismatch")
        generation = self._quiesce_receipt.lifecycle_generation
        if core_receipt.lifecycle_generation != generation:
            raise RuntimeError("foundation_finalize_core_generation_mismatch")
        errors = [str(item) for item in shutdown_errors if str(item)]
        if not core_receipt.terminated or core_receipt.survivors:
            receipt = ProductChildTerminationReceipt(
                child_id="foundation",
                lifecycle_generation=generation,
                disposition=ProductTerminationDisposition.RETAINED,
                survivors=("foundation",),
                errors=("core_shutdown_not_complete",),
                evidence={
                    "quiesce": self._quiesce_receipt.as_dict(),
                    "core": core_receipt.as_dict(),
                    "shutdown_errors": tuple(errors),
                },
            )
            return receipt
        if (
            not self._quiesce_receipt.terminated
            or self._quiesce_receipt.survivors
        ):
            receipt = ProductChildTerminationReceipt(
                child_id="foundation",
                lifecycle_generation=generation,
                disposition=ProductTerminationDisposition.RETAINED,
                survivors=("foundation",),
                errors=("foundation_jobs_not_terminated",),
                evidence={
                    "quiesce": self._quiesce_receipt.as_dict(),
                    "core": core_receipt.as_dict(),
                    "shutdown_errors": tuple(errors),
                },
            )
            return receipt
        terminal_health: dict[str, Any] = {}
        current_state: dict[str, Any] = {}
        survivors: list[str] = []
        try:
            terminal_health = self.runtime_health.finalize(
                cycle_count=self._service_cycles,
                degraded=bool(errors),
                workers=dict(self.core.worker_health.status()),
                faults=dict(self.core.fault_supervisor.status()),
                detail={
                    "foundation_quiesce": self._quiesce_receipt.as_dict(),
                    "core_shutdown": core_receipt.as_dict(),
                    "shutdown_errors": tuple(errors),
                },
            )
        except Exception as exc:
            errors.append(
                f"runtime_health_finalize:{type(exc).__name__}:{exc}"
            )
            terminal_health = {
                "owner": "arch4.foundation.runtime_health_service",
                "status": "fault",
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            current_state = self.current_state.close()
        except Exception as exc:
            errors.append(
                f"current_state_close:{type(exc).__name__}:{exc}"
            )
            survivors.append("foundation_current_state")
        try:
            runtime_health_close = self.runtime_health.close()
        except Exception as exc:
            errors.append(
                f"runtime_health_close:{type(exc).__name__}:{exc}"
            )
            survivors.append("foundation_runtime_health")
            runtime_health_close = {
                "owner": "arch4.foundation.runtime_health_service",
                "status": "fault",
                "error": f"{type(exc).__name__}: {exc}",
            }
        receipt = ProductChildTerminationReceipt(
            child_id="foundation",
            lifecycle_generation=generation,
            disposition=(
                ProductTerminationDisposition.DEGRADED
                if survivors
                else ProductTerminationDisposition.COMPLETE
            ),
            survivors=tuple(survivors),
            errors=tuple(errors) if survivors else (),
            evidence={
                "quiesce": self._quiesce_receipt.as_dict(),
                "current_state": current_state,
                "runtime_health": terminal_health,
                "runtime_health_close": runtime_health_close,
                "shutdown_errors": tuple(errors),
            },
        )
        if not survivors:
            self._shutdown = True
            self._final_receipt = receipt
        return receipt

    def shutdown(self) -> dict[str, Any]:
        raise RuntimeError(
            "foundation_shutdown_illegal_use_product_supervisor"
        )


__all__ = (
    "FOUNDATION_OWNER",
    "FOUNDATION_SERVICE_JOB_SPECS",
    "FoundationCompositionRoot",
)
