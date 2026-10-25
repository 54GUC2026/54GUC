"""Core-only composition root."""

from __future__ import annotations

from arch4.modem_live.startup_trace import StartupModemTrace

import threading
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any, Callable

from arch4.accepted_modem_generation import SignalProcessingMatrix
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.command_transactions import (
    CommandAuthority,
    DEFAULT_WRITE_RECEIPT_DEADLINE_S,
)
from arch4.core.action_source_state_authority import ActionSourceStateAuthority
from arch4.core.action_reachability import CoreReachabilityTimeline
from arch4.core.emergency_reachability import (
    CoreEmergencyReachabilityAuthority,
)
from arch4.core.action_status_service import CoreActionStatusService
from arch4.core.active_configuration_service import CoreActiveConfigurationService
from arch4.core.bouncer import CoreBouncer
from arch4.core.clock_wake_stream import CoreClockWakeStream
from arch4.core.command_gateway import CoreCommandGateway
from arch4.core.command_lifecycle_service import CoreCommandLifecycleService
from arch4.core.command_outcome_stream import CoreCommandOutcomeStream
from arch4.core.config_generation import ActiveConfigGenerationAuthority
from arch4.core.device_control_service import CoreDeviceControlService
from arch4.core.diagnostics_service import CoreDiagnosticsService
from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core.accepted_root_outbox import CoreAcceptedRootOutbox
from arch4.core.persistence import CorePersistenceCoordinator
from arch4.core.runtime_defaults import CoreRuntimeDefaultsError, CoreRuntimeDefaultsStore
from arch4.core.runtime_snapshot import CoreRuntimeSnapshotPublisher
from arch4.core.rsrq_baseline_state import (
    RSRQBaselineStateStore,
    resolve_rsrq_baseline_state_path,
)
from arch4.core_contracts.current_health import CoreCurrentHealthView
from arch4.core_contracts.current_health import (
    ModemLiveCurrentHealthAcceptanceReceipt,
    ModemLiveCurrentHealthPublication,
    ModemLiveCurrentHealthSinkDetachReceipt,
)
from arch4.core_contracts.commands import (
    CoreCommandExecutionAbortReceipt,
    CoreCommandExecutionGate,
    CoreCommandExecutionReceipt,
    CoreCommandGatewayShutdownReceipt,
    CoreCommandRequest,
)
from arch4.core_contracts.core_shutdown import (
    CoreCommandWorkerTerminationDisposition,
    CoreCommandWorkerTerminationReceipt,
    CoreShutdownReceipt,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core.signal_computer import SignalComputer
from arch4.core.action_runtime import (
    arm_mutating_action_lock,
    export_mutating_action_transaction_state,
    extend_auto_action_lockout,
    note_radio_mutation_command_sent,
)
from arch4.core.command_execution import (
    abort_core_cellular_command,
    execute_core_cellular_command,
)
from arch4.core.runtime_clock_dispatcher import (
    advance_runtime_clock,
    advance_runtime_clock_locked,
)
from arch4.event_journal import EventLogPort, ForensicEventJournal
from arch4.fault_supervisor import FaultBoundaryClass, FaultSeverity, FaultSupervisor
from arch4.interfaces import Arch4Config, Constraints, LeverType
from arch4.modem.base import ModemInterface
from arch4.modem_live import ModemLiveModule, create_modem_live_backend
from arch4.modem_live.acquisition import ModemLiveTerminationReceipt
from arch4.radio_transition_authority import RadioTransitionAuthority
from arch4.runtime_clock import (
    ClockedRuntimeService,
    ClockedScheduler,
    core_runtime_clock_jobs,
    core_runtime_clock_lanes,
)
from arch4.runtime_clock_worker_fabric import (
    ClockJobWorkerFabric,
    ClockJobWorkerTerminationReceipt,
)
from arch4.runtime_modem_sample_publication import (
    CoreAcceptedRootPublicationReceipt,
    ModemSamplePublicationFabric,
    ModemSamplePublicationTerminationReceipt,
)
from arch4.runtime_registers import RuntimeRegisterBank
from arch4.runtime_status_register_publishers import (
    publish_modem_live_current_health_status,
)
from arch4.architecture_contracts.application_lifecycle import (
    CorePrivateStartupRootProof,
    CoreStartupCandidate,
    ProductChildTerminationReceipt,
    ProductRuntimeAdmissionAuthority,
    ProductStartupReceipt,
    ProductTerminationDisposition,
)
from arch4.worker_health import WorkerHealthAuthority

class CoreCompositionRoot:
    """One live Core owner with no Foundation or Intelligence branch."""

    def __init__(
        self,
        config: Arch4Config,
        *,
        settings_dir: str | Path,
        state_dir: str | Path,
        runtime_dir: str | Path,
        event_log: EventLogPort,
        startup_wire_trace: StartupModemTrace | None = None,
        wire_trace_sink: Any = None,
        product_admission: ProductRuntimeAdmissionAuthority,
        modem: ModemInterface | None = None,
        verify_only: bool = True,
        check_only: bool = False,
        observe_only: bool = False,
        probe_mode: bool = False,
        device_association_reader: Any = None,
    ) -> None:
        if not isinstance(config, Arch4Config):
            raise TypeError("core_composition_arch4_config_required")
        if not callable(getattr(event_log, "log_event", None)):
            raise TypeError("core_composition_event_log_port_required")
        if not isinstance(product_admission, ProductRuntimeAdmissionAuthority):
            raise TypeError("core_composition_product_admission_required")
        self._product_admission = product_admission
        self.settings_dir = Path(settings_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        for path in (self.settings_dir, self.state_dir, self.runtime_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.verify_only = bool(verify_only)
        self.check_only = bool(check_only)
        self.observe_only = bool(observe_only)
        self.probe_mode = bool(probe_mode)
        self._started = False
        self._shutdown = False
        self._lifecycle_generation = 0
        self._startup_candidate: CoreStartupCandidate | None = None

        try:
            CoreRuntimeDefaultsStore(self.settings_dir).apply_to_config(config)
            self.runtime_defaults_error = ""
        except CoreRuntimeDefaultsError as exc:
            self.runtime_defaults_error = str(exc)
        self.config_generation_authority = ActiveConfigGenerationAuthority(config)

        self.diagnostics = event_log
        self.event_journal = ForensicEventJournal(event_log)
        self.fault_supervisor = FaultSupervisor(self.event_journal)
        self.worker_health = WorkerHealthAuthority()

        self.extension_hooks = CoreExtensionHookRegistry.with_default_catalog()
        self.clock_wake_stream = CoreClockWakeStream(self.extension_hooks)
        self.accepted_root_outbox = CoreAcceptedRootOutbox(
            self.extension_hooks
        )
        self.register_bank = RuntimeRegisterBank(
            identity_replica_path=str(self.state_dir / "identity_decode_replica.json")
        )

        self._modem_backend = modem or create_modem_live_backend(self.cfg)
        rsrq_baseline_path = resolve_rsrq_baseline_state_path(
            self.state_dir,
            profile_id=self.cfg.modem_profile_id,
        )
        self.signals_owner = SignalComputer(
            self.cfg,
            rsrq_baseline_store=(
                RSRQBaselineStateStore(rsrq_baseline_path)
                if rsrq_baseline_path is not None
                else None
            ),
        )
        self.signal_processing_matrix = SignalProcessingMatrix(
            self.signals_owner
        )
        self.signals = self.signal_processing_matrix.signal_facade
        self.modem_sample_publication = ModemSamplePublicationFabric(
            self.signal_processing_matrix,
            self.accepted_root_outbox,
            self.register_bank,
        )
        self.input_module = ModemLiveModule(
            self.cfg,
            self._modem_backend,
            health_checks=None,
            ops_root_dir=str(self.state_dir),
            startup_wire_trace=startup_wire_trace,
            wire_trace_sink=wire_trace_sink,
            native_read_event_sink=event_log.log_event,
            restored_identity_catalog=self.register_bank.identity_decode_catalog_for_owner_restore(),
            device_association_reader=device_association_reader,
        )
        self.input_module.set_live_register_publication_sink(
            self.modem_sample_publication.notify
        )
        self._modem_live_current_health_sink = (
            self._accept_modem_live_current_health
        )
        self.input_module.set_current_health_publication_sink(
            self._modem_live_current_health_sink
        )

        self.constraints = Constraints()

        self.bouncer = CoreBouncer(self.cfg)
        self.action_source_state_authority = ActionSourceStateAuthority()
        self.command_authority = CommandAuthority(persistence_dir=self.state_dir)
        self.reachability_timeline = CoreReachabilityTimeline()
        self.emergency_reachability = CoreEmergencyReachabilityAuthority()
        self.radio_transition_authority = RadioTransitionAuthority()

        jobs = tuple(core_runtime_clock_jobs())
        self.clock = ClockedScheduler(lane_specs=core_runtime_clock_lanes())
        for job in jobs:
            self.clock.register(job)
        self.clock_service = ClockedRuntimeService(self.clock)
        self.clock_job_workers = ClockJobWorkerFabric(
            job_keys=tuple(job.key for job in jobs),
            worker_health=self.worker_health,
            fault_supervisor=self.fault_supervisor,
        )

        self.command_gateway = CoreCommandGateway(
            self,
            extension_hooks=self.extension_hooks,
            accepted_root_outbox=self.accepted_root_outbox,
            fault_reporter=self.fault_supervisor.record,
        )
        self.command_outcome_stream = CoreCommandOutcomeStream(
            self.command_authority,
            self.extension_hooks,
            persistence_dir=self.state_dir,
        )
        self.command_lifecycle = CoreCommandLifecycleService(self)
        self.command_gateway.bind_admission_health_provider(
            self.command_lifecycle.admission_status
        )
        self.runtime_snapshot_publisher = CoreRuntimeSnapshotPublisher(
            self.accepted_root_outbox,
            self.extension_hooks,
            self.register_bank,
        )

        self._initialize_runtime_state()
        self.action_status = CoreActionStatusService(self)
        self.device_control = CoreDeviceControlService(
            self,
            persistence_dir=self.state_dir,
        )
        self.active_configuration = CoreActiveConfigurationService(
            self,
            persistence_dir=self.state_dir,
            evidence_dir=self.runtime_dir,
            refresh_core_consumers=self._refresh_config_consumers,
        )
        self.core_diagnostics = CoreDiagnosticsService(
            self,
            persistence_dir=self.state_dir,
        )
        self.persistence = CorePersistenceCoordinator(
            persistence_dir=str(self.state_dir),
            bouncer=self.bouncer,
            action_source_state=self.action_source_state_authority,
            export_mutation_state=self._export_mutating_action_transaction_state,
            latest_accepted_root=self.accepted_root_outbox.current,
            worker_health_status=self.worker_health.status,
            fault_status=self.fault_supervisor.status,
        )

    def bind_provider_pool_policy(self, resolver) -> None:
        if self._started:
            raise RuntimeError("core_provider_pool_policy_binding_after_start")
        self.input_module.bind_provider_pool_policy(resolver)

    def bind_startup_identity_prompt(
        self,
        prompt: Callable[[], str] | None,
    ) -> None:
        if self._started:
            raise RuntimeError("core_startup_identity_prompt_after_start_forbidden")
        self.input_module.bind_startup_identity_prompt(prompt)

    @property
    def cfg(self) -> Any:
        return self.config_generation_authority.current.config

    def _initialize_runtime_state(self) -> None:
        self._auto_action_lockout_until = 0.0
        self._auto_action_lockout_event_reason = ""
        self._mutating_action_lock_until = 0.0
        self._mutating_action_lock_lever = ""
        self._last_radio_lock_confirmation: dict[str, Any] = {}
        self._accepted_generation_namespace_status = {
            "bound": False,
            "reason": "awaiting_modem_live_ready",
        }
        self._runtime_clock_last_wall_ts = 0.0
        self._runtime_clock_advance_lock = threading.RLock()
        self._runtime_clock_internal_service_active = False
        self._runtime_clock_internal_service_status: dict[str, Any] = {}
        self._runtime_status_decision_lane_last_cycle: dict[str, Any] = {}
        self._runtime_control_faults: dict[str, Any] = {}
        self._action_runtime_faults: dict[str, Any] = {}
        self._core_command_worker_thread = None
        self._core_command_worker_status: dict[str, Any] = {}
        self._core_command_worker_lock = threading.RLock()
    def _refresh_config_consumers(
        self,
        core: Any,
        *,
        config: Any,
        generation_id: str,
        config_fingerprint: str,
    ) -> dict[str, dict[str, Any]]:
        if core is not self:
            raise RuntimeError("core_config_consumer_owner_mismatch")
        if str(getattr(config, "generation_id", "") or "") != generation_id:
            raise RuntimeError("core_config_generation_projection_mismatch")
        if str(getattr(config, "fingerprint", "") or "") != config_fingerprint:
            raise RuntimeError("core_config_fingerprint_projection_mismatch")
        acknowledgements: dict[str, dict[str, Any]] = {}
        for name, owner in (
            ("signals", self.signals),
            ("bouncer", self.bouncer),
            ("core_diagnostics", self.core_diagnostics),
            ("modem_live", self.input_module),
        ):
            owner.bind_config_generation(
                config,
                generation_id=generation_id,
                config_fingerprint=config_fingerprint,
            )
            acknowledgements[name] = {
                "acknowledged": True,
                "generation_id": generation_id,
                "config_fingerprint": config_fingerprint,
            }
        return acknowledgements

    def prepare_startup_candidate(
        self,
        *,
        lifecycle_generation: int,
    ) -> CoreStartupCandidate:
        if self._shutdown:
            raise RuntimeError("core_composition_already_shutdown")
        if self._startup_candidate is not None:
            raise RuntimeError(
                "core_composition_duplicate_startup_candidate_forbidden"
            )
        if type(lifecycle_generation) is not int or lifecycle_generation <= 0:
            raise ValueError(
                "core_composition_lifecycle_generation_required"
            )
        lifecycle = lifecycle_generation
        self._lifecycle_generation = lifecycle
        self.input_module.bind_product_lifecycle_generation(lifecycle)
        readiness = self.input_module.run_startup_live_phase(
            constraints=self.constraints,
            fast=True,
        )
        if readiness.lifecycle_generation != lifecycle:
            raise RuntimeError(
                "core_modem_live_ready_lifecycle_mismatch"
            )
        self.command_lifecycle.reconcile_startup_identity(
            readiness.session_scope_key
        )
        self.modem_sample_publication.bind_runtime_namespace(
            readiness.matrix_identity.namespace
        )
        self.active_configuration.restore_committed()
        self.input_module.release_startup_publication(readiness)
        deadline = monotonic() + 8.0
        private_root = self.modem_sample_publication.private_startup_root()
        while private_root is None:
            result = self.modem_sample_publication.drain(
                self,
                now=float(time()),
            )
            if result.last_error:
                raise RuntimeError(
                    "core_private_startup_root_prepare_failed:"
                    f"{result.last_error}"
                )
            private_root = (
                self.modem_sample_publication.private_startup_root()
            )
            if private_root is not None:
                break
            if monotonic() >= deadline:
                raise RuntimeError(
                    "core_private_startup_root_prepare_timeout"
                )
            sleep(0.01)
        if private_root.matrix_identity != readiness.matrix_identity:
            raise RuntimeError(
                "core_private_startup_root_readiness_mismatch"
            )
        proof = CorePrivateStartupRootProof(
            startup_attempt_id=(
                readiness.startup_sweep.acquisition_attempt_id
            ),
            namespace=readiness.matrix_identity.namespace,
            matrix_identity=readiness.matrix_identity,
            accepted_root_fingerprint=(
                private_root.root_fingerprint
            ),
            private_root_created_at=private_root.prepared_at,
        )
        active = self.active_configuration.active_snapshot()
        candidate = CoreStartupCandidate(
            lifecycle_generation=lifecycle,
            prepared_at=time(),
            config_generation_id=active.generation_id,
            config_fingerprint=active.config_fingerprint,
            matrix_readiness=readiness,
            private_root_proof=proof,
        )
        self._startup_candidate = candidate
        return candidate

    def open_accepted_root_publication(
        self,
        candidate: CoreStartupCandidate,
    ) -> CoreAcceptedRootPublicationReceipt:
        """Publish the retained first root once when Product becomes ready."""

        if not isinstance(candidate, CoreStartupCandidate):
            raise TypeError("core_startup_candidate_required")
        if self._startup_candidate != candidate:
            raise RuntimeError("core_startup_candidate_mismatch")
        proof = candidate.private_root_proof
        receipt = (
            self.modem_sample_publication.open_accepted_root_publication(
                matrix_identity=proof.matrix_identity,
                root_fingerprint=proof.accepted_root_fingerprint,
            )
        )
        return receipt

    def publish_post_product_startup_evidence(
        self,
        *,
        candidate: CoreStartupCandidate,
        product_root: ProductStartupReceipt,
    ) -> dict[str, Any]:
        """Publish advisory evidence only after Product installed RUNNING truth."""

        self.validate_product_ready_root(
            candidate=candidate,
            product_root=product_root,
        )
        if not self._product_admission.admits(product_root):
            raise RuntimeError(
                "core_post_startup_evidence_before_product_admission"
            )
        failures: list[str] = []
        try:
            self.active_configuration.publish_current_run_evidence()
        except Exception as exc:
            failures.append(
                f"active_configuration:{type(exc).__name__}: {exc}"
            )
        try:
            proof = candidate.private_root_proof
            self.diagnostics.log_event(
                "core_startup_root_published",
                matrix_identity=proof.matrix_identity.as_dict(),
                root_fingerprint=proof.accepted_root_fingerprint,
                candidate_digest=candidate.candidate_digest,
            )
        except Exception as exc:
            failures.append(f"diagnostics:{type(exc).__name__}: {exc}")
        return {
            "schema": "CorePostProductStartupEvidenceReceipt.v1",
            "owner": "54guc.core.composition",
            "product_root_digest": product_root.root_digest,
            "published": not failures,
            "failures": failures,
        }

    def validate_product_ready_root(
        self,
        *,
        candidate: CoreStartupCandidate,
        product_root: ProductStartupReceipt,
    ) -> None:
        """Validate the root before Product installs it."""

        if not isinstance(candidate, CoreStartupCandidate):
            raise TypeError("core_product_ready_candidate_required")
        if not isinstance(product_root, ProductStartupReceipt):
            raise TypeError("core_product_ready_root_required")
        if self._shutdown:
            raise RuntimeError("core_product_ready_after_shutdown")
        if self._startup_candidate != candidate:
            raise RuntimeError("core_product_ready_candidate_mismatch")
        if product_root.core_candidate != candidate:
            raise RuntimeError("core_product_ready_root_candidate_mismatch")
        if product_root.matrix_readiness != candidate.matrix_readiness:
            raise RuntimeError("core_product_ready_root_matrix_mismatch")
        if not product_root.ready or not product_root.root_digest:
            raise RuntimeError("core_product_ready_root_not_ready")

    def external_admission_active(
        self,
        *,
        candidate: CoreStartupCandidate,
        product_root: ProductStartupReceipt,
    ) -> bool:
        return bool(
            self._startup_candidate == candidate
            and product_root.core_candidate == candidate
            and self._product_admission.admits(product_root)
            and not self._shutdown
        )

    def shutdown(
        self,
        *,
        lifecycle_generation: int,
    ) -> ProductChildTerminationReceipt:
        if type(lifecycle_generation) is not int or lifecycle_generation <= 0:
            raise ValueError("core_shutdown_lifecycle_generation_required")
        if self._lifecycle_generation not in (0, lifecycle_generation):
            raise RuntimeError("core_shutdown_lifecycle_generation_mismatch")
        if self._lifecycle_generation == 0:
            self._lifecycle_generation = lifecycle_generation
        if self._shutdown:
            return ProductChildTerminationReceipt(
                child_id="core",
                lifecycle_generation=self._lifecycle_generation,
                disposition=ProductTerminationDisposition.ALREADY_COMPLETE,
            )
        shutdown_started_at = float(time())
        command_gateway = self.command_gateway.close(timeout_s=6.0)
        if not isinstance(
            command_gateway,
            CoreCommandGatewayShutdownReceipt,
        ):
            raise TypeError(
                "core_command_gateway_shutdown_receipt_required"
            )
        command_worker = self._core_command_worker_thread
        command_worker_present = command_worker is not None
        command_worker_was_alive = bool(
            command_worker is not None and command_worker.is_alive()
        )
        if command_worker_was_alive:
            # A dispatched compound write must finish its protected stages.
            # Closing the app does not shorten the command owner's allowance.
            command_worker.join(timeout=DEFAULT_WRITE_RECEIPT_DEADLINE_S)
        command_worker_receipt = CoreCommandWorkerTerminationReceipt(
            disposition=(
                CoreCommandWorkerTerminationDisposition.DEGRADED
                if command_worker is not None and command_worker.is_alive()
                else CoreCommandWorkerTerminationDisposition.STOPPED
                if command_worker_was_alive
                else CoreCommandWorkerTerminationDisposition.ALREADY_STOPPED
                if command_worker_present
                else CoreCommandWorkerTerminationDisposition.NOT_STARTED
            ),
            thread_present=command_worker_present,
            alive=bool(
                command_worker is not None and command_worker.is_alive()
            ),
            shutdown_started_at=shutdown_started_at,
            finished_at=float(time()),
        )
        if not command_worker_receipt.terminated:
            error = RuntimeError("core_command_worker_shutdown_timeout")
            self.fault_supervisor.record(
                boundary=FaultBoundaryClass.WORKER,
                owner="54guc.core.composition",
                operation="shutdown_command_worker",
                generation=0,
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior="report_degraded_shutdown_and_retain_diagnostics",
                error=error,
                suppression_budget=0,
            )
            self.diagnostics.log_event(
                "core_shutdown_degraded",
                reason=str(error),
            )
        if not command_gateway.terminated:
            self.diagnostics.log_event(
                "core_shutdown_degraded",
                reason="core_command_gateway_submission_survivor",
                active_submissions=command_gateway.active_submissions,
                waiting_submissions=command_gateway.waiting_submissions,
            )
        workers: ClockJobWorkerTerminationReceipt | None = None
        if (
            command_gateway.terminated
            and command_worker_receipt.terminated
        ):
            workers = self.clock_job_workers.shutdown(
                timeout_s=6.0,
                cancel_futures=True,
            )
            if not isinstance(
                workers,
                ClockJobWorkerTerminationReceipt,
            ):
                raise TypeError(
                    "clock_job_worker_termination_receipt_required"
                )
        health_sink_receipt: (
            ModemLiveCurrentHealthSinkDetachReceipt | None
        ) = None
        if workers is not None and workers.terminated:
            health_sink_receipt = (
                self.input_module.detach_current_health_publication_sink(
                    self._modem_live_current_health_sink
                )
            )
            if not isinstance(
                health_sink_receipt,
                ModemLiveCurrentHealthSinkDetachReceipt,
            ):
                raise TypeError(
                    "modem_live_current_health_sink_detach_receipt_required"
                )
        publication_termination: (
            ModemSamplePublicationTerminationReceipt | None
        ) = None
        if (
            health_sink_receipt is not None
            and health_sink_receipt.detached
            and health_sink_receipt.inflight_callbacks == 0
        ):
            publication_termination = (
                self.modem_sample_publication.terminate_core_owned(
                    self,
                    input_module=self.input_module,
                    lifecycle_generation=self._lifecycle_generation,
                    timeout_s=6.0,
                )
            )
            if not isinstance(
                publication_termination,
                ModemSamplePublicationTerminationReceipt,
            ):
                raise TypeError(
                    "modem_sample_publication_termination_receipt_required"
                )
        input_termination: ModemLiveTerminationReceipt | None = None
        if (
            publication_termination is not None
            and publication_termination.terminated
        ):
            input_termination = self.input_module.shutdown(
                join_timeout_s=6.0
            )
            if not isinstance(
                input_termination,
                ModemLiveTerminationReceipt,
            ):
                raise TypeError(
                    "modem_live_termination_receipt_required"
                )
        if input_termination is not None and input_termination.terminated:
            persistence = self.persistence.maybe_commit(force=True)
        else:
            persistence = {
                "status": "not_committed",
                "reason": "modem_live_termination_unproven",
            }
        aggregate = CoreShutdownReceipt(
            lifecycle_generation=self._lifecycle_generation,
            command_gateway=command_gateway,
            command_worker=command_worker_receipt,
            clock_workers=workers,
            current_health_sink=health_sink_receipt,
            modem_sample_publication=publication_termination,
            modem_live=input_termination,
            persistence=CoreFrozenPayload.from_mapping(persistence),
            shutdown_started_at=shutdown_started_at,
            finished_at=float(time()),
        )
        self._shutdown = aggregate.terminated
        return aggregate.to_product_child_receipt()

    def get_latest_capabilities_report(self) -> dict[str, Any]:
        root = self.accepted_root_outbox.current()
        if root is None:
            return {}
        return root.runtime_view.runtime_capabilities.to_mapping()

    def current_health_view(self) -> CoreCurrentHealthView:
        root = self.accepted_root_outbox.current()
        if root is None:
            raise RuntimeError("core_current_health_accepted_root_unavailable")
        return self.register_bank.read_current_health(
            accepted_namespace=root.matrix_identity.namespace,
        )

    def current_reachability_view(self, *, now: float):
        return self.reachability_timeline.current_view(now=now)

    def current_recovery_timing(self):
        from arch4.core_contracts.emergency_reachability import RecoveryTimingProfile
        root = self.accepted_root_outbox.current()
        if root is None:
            raise RuntimeError("core_recovery_profile_accepted_root_required")
        capabilities = root.runtime_view.runtime_capabilities.to_mapping()
        profile = capabilities["modem_live_access_profile"]
        return RecoveryTimingProfile.from_mapping(profile["recovery_timing"])

    def current_reachability_evidence_view(self, *, now: float):
        return self.reachability_timeline.evidence_view(now=now)

    def current_emergency_reachability_view(self, *, now: float):
        root = self.accepted_root_outbox.current()
        automatic_cellular_actions_allowed = (
            root.runtime_view.cellular_automatic_actions_allowed if root else True
        )
        raw = self.reachability_timeline.current_view(now=now)
        failure_streak, first_failure_at, success_streak = (
            self.reachability_timeline.primary_reachability_streaks(now=now)
        )
        return self.emergency_reachability.observe_primary(
            session_scope_key=raw.session_scope_key,
            identity_code=raw.identity_code,
            failure_streak=failure_streak,
            success_streak=success_streak,
            first_failure_at=first_failure_at,
            observed_at=float(now),
            activity_success_streak=self.reachability_timeline.activity_success_streak(now=now),
            automatic_cellular_actions_allowed=automatic_cellular_actions_allowed,
        )

    def verify_emergency_reachability(self, *, now: float):
        current = self.current_emergency_reachability_view(now=now)
        if current.disposition.value == "normal":
            raise RuntimeError("core_emergency_reachability_not_active")
        namespace = self.input_module.rtt_observation_register_snapshot(now=now).observation.namespace
        result = self.input_module.verify_internet_reachability(
            targets=("1.1.1.1", "8.8.8.8", "9.9.9.9"),
            requested_at=float(now),
        )
        # Traffic or a regular ping can recover service while this blocking
        # check is in flight. A late negative round must not reopen recovery.
        checked_at = max(float(now), result.completed_at)
        self.reachability_timeline.observe_verification(result, namespace=namespace, now=checked_at)
        refreshed = self.current_emergency_reachability_view(now=checked_at)
        verified = (
            refreshed if refreshed.disposition.value == "normal"
            else self.emergency_reachability.accept_verification_round(
                result, observed_at=checked_at,
            )
        )
        self.diagnostics.log_event(
            "core_internet_verification", ts=result.completed_at,
            evidence=result.as_dict(), emergency=verified.as_dict(),
        )
        return verified

    def restart_emergency_reachability_verification(
        self,
        *,
        incident_id: str,
        now: float,
    ):
        return self.emergency_reachability.restart_verification(
            incident_id=incident_id,
            observed_at=float(now),
        )

    def issue_emergency_restart_permit(
        self,
        *,
        request_id: str,
        matrix_identity: MatrixGenerationIdentity,
        identity_code: str,
        incident_id: str,
        now: float,
    ):
        self.current_emergency_reachability_view(now=float(now))
        return self.emergency_reachability.issue_restart_permit(
            request_id=request_id,
            matrix_identity=matrix_identity,
            identity_code=identity_code,
            incident_id=incident_id,
            now=float(now),
        )

    def validate_emergency_restart_permit(self, permit, *, now: float) -> bool:
        self.current_emergency_reachability_view(now=float(now))
        return self.emergency_reachability.validate_restart_permit(
            permit,
            now=float(now),
        )

    def current_device_recovery_state(self) -> CoreFrozenPayload:
        """Adapt the accepted Matrix recovery fact without re-reading hardware."""

        root = self.accepted_root_outbox.current()
        if root is None:
            return CoreFrozenPayload.from_mapping(
                {"available": False, "active": False, "reason": "root_pending"}
            )
        try:
            fact = root.current_facts.exact("health.device_recovery_state")
        except KeyError:
            return CoreFrozenPayload.from_mapping(
                {
                    "available": False,
                    "active": False,
                    "reason": "device_recovery_fact_not_declared",
                }
            )
        if fact.disposition != "available":
            return CoreFrozenPayload.from_mapping(
                {
                    "available": False,
                    "active": False,
                    "reason": fact.reason,
                }
            )
        envelope = fact.value.to_mapping()
        value = envelope.get("value")
        if not isinstance(value, dict):
            raise TypeError("core_device_recovery_matrix_mapping_required")
        if value.get("available") is False:
            return CoreFrozenPayload.from_mapping(
                {"available": False, "active": False,
                 "reason": "device_recovery_observation_unavailable"}
            )
        active = value.get("active")
        if not isinstance(active, bool):
            raise TypeError("core_device_recovery_active_bool_required")
        lte = value.get("lte_interface")
        lte = dict(lte) if isinstance(lte, dict) else {}
        active_jobs = value.get("active_jobs")
        active_jobs = (
            tuple(active_jobs)
            if isinstance(active_jobs, (list, tuple))
            else ()
        )
        state = str(value.get("state") or "unknown").strip().lower()
        return CoreFrozenPayload.from_mapping(
            {
                "available": True,
                "active": active,
                "state": state,
                "observed_at": float(value.get("observed_at", 0.0) or 0.0),
                "active_jobs": list(active_jobs),
                "lte_interface_disabled": lte.get("disabled"),
            }
        )

    def _accept_modem_live_current_health(
        self,
        publication: ModemLiveCurrentHealthPublication,
    ) -> ModemLiveCurrentHealthAcceptanceReceipt:
        if not isinstance(publication, ModemLiveCurrentHealthPublication):
            raise TypeError(
                "core_modem_live_current_health_publication_required"
            )
        root = self.accepted_root_outbox.current()
        if root is None:
            raise RuntimeError(
                "core_modem_live_current_health_root_unavailable"
            )
        if publication.namespace != root.matrix_identity.namespace:
            raise RuntimeError(
                "core_modem_live_current_health_namespace_mismatch"
            )
        register = publish_modem_live_current_health_status(
            self.register_bank,
            publication=publication,
        )
        return ModemLiveCurrentHealthAcceptanceReceipt(
            namespace=publication.namespace,
            observed_at=publication.observed_at,
            register_sequence=register.sequence,
        )

    def get_modem_health_status(self) -> dict[str, Any]:
        raise RuntimeError(
            "direct_modem_live_health_read_illegal:"
            "use_core_current_health_view"
        )

    def disconnect_physical(self) -> None:
        """Delegate the admitted physical disconnect to Modem Live."""

        self.input_module.modem_disconnect()

    def _drain_runtime_events(self, producer: Any, default_kind: str) -> None:
        try:
            drain = getattr(producer, "drain_runtime_events", None)
            if not callable(drain):
                return
            for event in drain():
                payload = dict(event or {})
                kind = str(payload.pop("kind", "") or default_kind)
                self.diagnostics.log_event(kind, **payload)
        except Exception as exc:
            self.fault_supervisor.record(
                boundary=FaultBoundaryClass.WORKER,
                owner="54guc.core.composition",
                operation=f"drain_runtime_events:{default_kind}",
                generation=0,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior="retain_runtime_and_report_event_drain_failure",
                error=exc,
                suppression_budget=3,
            )

    def execute_core_cellular_command(
        self,
        request: CoreCommandRequest,
        action: Any,
        **kwargs: Any,
    ) -> CoreCommandExecutionReceipt:
        return execute_core_cellular_command(self, request, action, **kwargs)

    def abort_core_cellular_command(
        self,
        request: CoreCommandRequest,
        *,
        execution_gate: CoreCommandExecutionGate,
        reason: str,
    ) -> CoreCommandExecutionAbortReceipt:
        return abort_core_cellular_command(
            self,
            request,
            execution_gate=execution_gate,
            reason=reason,
        )

    def _extend_auto_action_lockout(self, duration_s: float, *, reason: str) -> None:
        extend_auto_action_lockout(self, duration_s, reason=reason)

    def _arm_mutating_action_lock(self, lever: LeverType) -> None:
        arm_mutating_action_lock(self, lever)

    def _export_mutating_action_transaction_state(self) -> dict[str, Any]:
        return export_mutating_action_transaction_state(self)

    def _note_radio_mutation_command_sent(
        self,
        *,
        action: Any,
        action_result: Any,
        physical_stages: Any,
    ) -> None:
        note_radio_mutation_command_sent(
            self,
            action=action,
            action_result=action_result,
            physical_stages=physical_stages,
        )

    def _advance_runtime_clock(self) -> None:
        advance_runtime_clock(self)

    def _advance_runtime_clock_locked(self) -> None:
        advance_runtime_clock_locked(self)


__all__ = ("CoreCompositionRoot",)
