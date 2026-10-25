"""CGF lifecycle supervisor with no combined controller or customer execution."""

from __future__ import annotations

from arch4.modem_live.startup_trace import StartupModemTrace

import logging
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Event, RLock
from time import monotonic, time
from typing import Any, Callable

from arch4.architecture_contracts.application_lifecycle import (
    PRODUCT_STARTUP_RECEIPT_SCHEMA,
    PRODUCT_SUPERVISOR_OWNER,
    CoreStartupCandidate,
    IntelligenceStartupEvidence,
    ProductChildTerminationReceipt,
    ProductLifecycleState,
    ProductLifecycleStatusReceipt,
    ProductRuntimeAdmissionAuthority,
    ProductRuntimeWorkerReplacementRequired,
    ProductShutdownDisposition,
    ProductShutdownReceipt,
    ProductStartupCleanupReceipt,
    ProductStartupDisposition,
    ProductStartupReason,
    ProductStartupReceipt,
    ProductStopRequestReceipt,
    ProductTerminationDisposition,
    ProductZeroSurvivorReceipt,
)
from arch4.architecture_contracts.runtime_termination import (
    RequiredRuntimeWorkerTerminationReceipt,
    RuntimeFrameBoundaryTerminationReceipt,
    RuntimeParticipantAbsenceDisposition,
    RuntimeParticipantAbsenceReceipt,
    RuntimeParticipantStartReceipt,
    RuntimeTerminationBlocker,
    RuntimeTerminationParticipant,
)
from arch4.core.composition import CoreCompositionRoot
from arch4.foundation.composition import FoundationCompositionRoot
from arch4.foundation.runtime_artifacts import CgfEvidenceStore
from arch4.fault_supervisor import FaultBoundaryClass, FaultSeverity
from arch4.interfaces import Arch4Config
from arch4.modem.base import (
    AuthenticationError,
    ModemIdentityRequiredError,
    ModemInterface,
    TargetGuardError,
)
from arch4.runtime_clock_service_thread import (
    INTERNAL_RUNTIME_CLOCK_INTERVAL_S,
    InternalRuntimeClockTerminationReceipt,
    start_internal_runtime_clock,
    stop_internal_runtime_clock,
)
from arch4.runtime_modem_sample_publication import (
    CoreAcceptedRootPublicationReceipt,
    ModemSamplePublicationTerminationReceipt,
    ModemSamplePublicationService,
)
from arch4.core.command_status_projection import (
    bind_core_command_status_wake_listener,
)
from arch4.core.runtime_snapshot import require_core_runtime_snapshot_publisher
from arch4.runtime_registers import require_core_runtime_register_bank
from arch4.core.command_gateway import require_core_command_gateway
from arch4.core_contracts.commands import CoreCommandRequest
from arch4.runtime_clock_service_thread import (
    bind_runtime_clock_wake_listener,
)
from arch4.runtime_modem_sample_publication import (
    require_modem_sample_publication_fabric,
)
from arch4.operational_service_status import publish_operational_service_status
from arch4.operational_service_contracts import (
    AntennaGuideControlRequest,
    BandScheduleCancelRequest,
    BandSchedulePlanRequest,
    BandSequenceCancelRequest,
    BandSequencePlanRequest,
    DownstreamHealthConfigRequest,
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    SmsCommandControlConfigRequest,
    WifiRadioControlRequest,
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
    operational_request_kind,
    operational_request_matrix_identity,
    require_product_operational_effect_permit,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.clients import CoreClientManifest
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.accepted_root_outbox import (
    CoreAcceptedRootOutboxPort,
)
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CLIENT_ID,
    INTELLIGENCE_MATRIX_CONTRACT_VERSION,
    INTELLIGENCE_MATRIX_OWNER,
)
from arch4.product_operational_transaction import (
    ProductOperationalTransactionAuthority,
)
from arch4.product_intelligence import ProductIntelligenceMatrix


log = logging.getLogger("arch4.product_supervisor")


def _require_sms_enable_supported(core: CoreCompositionRoot) -> None:
    """Admit SMS enable only from current Modem Live Matrix capability truth."""

    root = core.accepted_root_outbox.current()
    if root is None:
        raise RuntimeError("product_sms_enable_current_matrix_required")
    try:
        fact = root.current_facts.exact("capability.sms_supported")
    except KeyError as exc:
        raise RuntimeError("product_sms_enable_capability_unproved") from exc
    if fact.disposition != "available":
        raise RuntimeError("product_sms_enable_capability_unproved")
    envelope = fact.value.to_mapping()
    if envelope.get("present") is not True or envelope.get("value") is not True:
        raise RuntimeError("product_sms_enable_unsupported")

PRODUCT_HEADLESS_LIFECYCLE_INTERNAL_ROOT_EXCEPTIONS = (
    "mark_health:startup_and_reconnect_evidence_before_child_activation",
    "terminate:ordered_shutdown_after_product_admission_revoke",
)
PRODUCT_NGUI_LIFECYCLE_INTERNAL_ROOT_EXCEPTIONS = (
    "bind_runtime_clock_wake_listener:"
    "listener_removal_after_product_admission_revoke",
    "bind_command_status_wake_listener:"
    "listener_removal_after_product_admission_revoke",
    "bind_sample_wake_listener:"
    "listener_removal_after_product_admission_revoke",
    "stop_sample_publication:"
    "ordered_shutdown_after_product_admission_revoke",
    "stop_runtime_clock:ordered_shutdown_after_product_admission_revoke",
    "record_runtime_termination:"
    "ordered_shutdown_receipt_after_product_admission_revoke",
    "termination_receipt:"
    "product_shutdown_diagnostic_after_product_admission_revoke",
)


class ProductHeadlessRuntimePort:
    """Typed headless capability whose children remain Product-owned."""

    def __init__(self, product: "ProductSupervisor") -> None:
        self._product = product
        self._root: ProductStartupReceipt | None = None
        self._running = False
        self._sample_publication: ModemSamplePublicationService | None = None
        self._internal_runtime_clock_stop = Event()
        self._internal_runtime_clock_thread = None
        self._internal_runtime_clock_interval_s = (
            INTERNAL_RUNTIME_CLOCK_INTERVAL_S
        )
        self._termination: ProductChildTerminationReceipt | None = None

    def activate(self, root: ProductStartupReceipt) -> None:
        product = self._product
        if not isinstance(root, ProductStartupReceipt):
            raise TypeError("product_headless_ready_root_required")
        def activate_child() -> None:
            if (
                self._running
                or self._root is not None
                or self._sample_publication is not None
                or self._termination is not None
            ):
                raise RuntimeError("product_headless_runtime_overlap")
            sample = ModemSamplePublicationService(
                core=product._core,
                is_running=lambda: (
                    self._running
                    and product._admission.admits(root)
                ),
                report_error=lambda message: self._report_fault(
                    "modem_sample_publication",
                    RuntimeError(message),
                    retryable=True,
                ),
            )
            self._sample_publication = sample
            self._root = root
            self._running = True
            try:
                sample.start()
            except BaseException:
                self._running = False
                self._sample_publication = None
                self._root = None
                raise
            try:
                start_internal_runtime_clock(
                    self,
                    core=product._core,
                    is_running=lambda: (
                        self._running
                        and product._admission.admits(root)
                    ),
                    logger=log,
                    interval_s=self._internal_runtime_clock_interval_s,
                )
            except BaseException as activation_error:
                self._running = False
                try:
                    publication = sample.stop(timeout_s=6.0)
                except BaseException as rollback_error:
                    self._termination = ProductChildTerminationReceipt(
                        child_id="headless_runtime",
                        lifecycle_generation=(
                            product._lifecycle_generation
                        ),
                        disposition=(
                            ProductTerminationDisposition.DEGRADED
                        ),
                        survivors=(
                            "headless_modem_sample_publication",
                        ),
                        errors=(
                            "headless_activation_rollback:"
                            f"{type(rollback_error).__name__}:"
                            f"{rollback_error}",
                        ),
                    )
                    raise RuntimeError(
                        "product_headless_activation_rollback_unproven"
                    ) from activation_error
                if not publication.terminated:
                    self._termination = ProductChildTerminationReceipt(
                        child_id="headless_runtime",
                        lifecycle_generation=(
                            product._lifecycle_generation
                        ),
                        disposition=(
                            ProductTerminationDisposition.DEGRADED
                        ),
                        survivors=(
                            "headless_modem_sample_publication",
                        ),
                        errors=(
                            "headless_activation_rollback:"
                            "publication_not_terminated",
                        ),
                        evidence={
                            "publication": publication.as_dict(),
                        },
                    )
                    raise RuntimeError(
                        "product_headless_activation_rollback_survivor"
                    ) from activation_error
                self._sample_publication = None
                self._root = None
                raise

        product._activate_runtime_child(
            root,
            runtime_kind="headless",
            activate=activate_child,
        )

    def _require_root(self) -> ProductStartupReceipt:
        root = self._root
        if root is None or not self._product._admission.admits(root):
            raise RuntimeError(
                "product_headless_external_admission_closed"
            )
        return root

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        return self._product.service_once(now=now)

    def mark_health(
        self,
        phase: str,
        *,
        error: BaseException | str | None = None,
        cycle_count: int = 0,
        **detail: Any,
    ) -> None:
        # Runtime health is Product-owned lifecycle evidence.  Startup and
        # reconnect phases necessarily precede child activation, so evidence
        # publication must not depend on an activated runtime root.  Service,
        # command and physical-disconnect operations remain root-fenced.
        product = self._product
        product._foundation.runtime_health.mark(
            phase,
            cycle_count=max(0, int(cycle_count)),
            error=error,
            workers=dict(product._core.worker_health.status()),
            faults=dict(product._core.fault_supervisor.status()),
            detail=detail,
        )

    def report_fault(
        self,
        operation: str,
        error: BaseException,
        *,
        retryable: bool,
    ) -> None:
        self._require_root()
        self._report_fault(operation, error, retryable=retryable)

    def _report_fault(
        self,
        operation: str,
        error: BaseException,
        *,
        retryable: bool,
    ) -> None:
        self._product._core.fault_supervisor.record(
            boundary=FaultBoundaryClass.WORKER,
            owner="54guc.cgf_runner",
            operation=operation,
            generation=self._product._lifecycle_generation,
            severity=FaultSeverity.ERROR,
            retryable=retryable,
            degraded_behavior=(
                "product_continues_foundation_retries"
                if retryable
                else "ordered_product_shutdown"
            ),
            error=error,
            suppression_budget=3,
        )

    def disconnect_physical(self) -> None:
        product = self._product
        with product._lifecycle:
            root = self._root
            if (
                root is None
                or product._stop_requested
                or not product._lifecycle_state.admits_service
                or not product._admission.admits(root)
            ):
                raise RuntimeError(
                    "product_headless_disconnect_admission_closed"
                )
            product._service_inflight += 1
        try:
            product._core.disconnect_physical()
        finally:
            with product._lifecycle:
                product._service_inflight -= 1
                if product._service_inflight < 0:
                    raise RuntimeError(
                        "product_headless_disconnect_inflight_underflow"
                    )
                product._lifecycle.notify_all()

    def terminate(
        self,
        *,
        timeout_s: float = 5.0,
    ) -> ProductChildTerminationReceipt:
        if (
            self._termination is not None
            and self._termination.terminated
        ):
            return self._termination
        self._running = False
        errors: list[str] = []
        survivors: list[str] = []
        evidence: dict[str, Any] = {}
        publication = self._sample_publication
        if publication is not None:
            try:
                receipt = publication.stop(timeout_s=timeout_s)
                evidence["publication"] = receipt.as_dict()
                if not receipt.terminated:
                    survivors.append("headless_modem_sample_publication")
            except Exception as exc:
                survivors.append("headless_modem_sample_publication")
                errors.append(f"publication:{type(exc).__name__}:{exc}")
        try:
            clock = stop_internal_runtime_clock(
                self,
                core=self._product._core,
                timeout_s=timeout_s,
            )
            evidence["clock"] = clock.as_dict()
            if not clock.terminated:
                survivors.append("headless_runtime_clock")
        except Exception as exc:
            survivors.append("headless_runtime_clock")
            errors.append(f"clock:{type(exc).__name__}:{exc}")
        self._termination = ProductChildTerminationReceipt(
            child_id="headless_runtime",
            lifecycle_generation=self._product._lifecycle_generation,
            disposition=(
                ProductTerminationDisposition.COMPLETE
                if not survivors and not errors
                else ProductTerminationDisposition.DEGRADED
            ),
            survivors=tuple(survivors),
            errors=tuple(errors),
            evidence=evidence,
        )
        return self._termination


class ProductNGUIRuntimePort:
    """Typed NGUI capability over one exact installed Product root."""

    def pending_device_replacement(self) -> ProductRuntimeWorkerReplacementRequired | None:
        self._require_root()
        return self._product._core.input_module.pending_device_replacement()

    _REQUIRED_RUNTIME_WORKERS = (
        "ngui-core-manual-nmi",
        "ngui-core-command-outcome",
    )
    _CORE_OPERATIONAL_KINDS = frozenset(
        {
            OperationalKind.READ_DIAGNOSTICS,
            OperationalKind.INSPECT_NETWORK_SETTINGS,
            OperationalKind.WIFI_SET_RADIO,
            OperationalKind.SET_DOWNSTREAM_HEALTH,
        }
    )
    _FOUNDATION_OPERATIONAL_KINDS = frozenset(
        {
            OperationalKind.ANTENNA_GUIDE,
            OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL,
            OperationalKind.PLAN_BAND_SCHEDULE,
            OperationalKind.PLAN_BAND_SEQUENCE,
            OperationalKind.CANCEL_BAND_SCHEDULE,
            OperationalKind.CANCEL_BAND_SEQUENCE,
        }
    )
    _PRODUCT_OPERATIONAL_KINDS = frozenset(
        {
            OperationalKind.SET_INTELLIGENCE_AUTOPILOT,
            OperationalKind.SET_INTELLIGENCE_EXPLORE,
        }
    )
    if (
        _CORE_OPERATIONAL_KINDS
        | _FOUNDATION_OPERATIONAL_KINDS
        | _PRODUCT_OPERATIONAL_KINDS
        != frozenset(OperationalKind)
    ):
        raise RuntimeError("product_operational_kind_denominator_incomplete")

    def __init__(self, product: "ProductSupervisor") -> None:
        self._product = product
        self._root: ProductStartupReceipt | None = None
        self._termination: ProductChildTerminationReceipt | None = None
        self._termination_sources: tuple[object, ...] | None = None
        self._sample_publication: ModemSamplePublicationService | None = None
        self._sample_publication_start: (
            RuntimeParticipantStartReceipt | None
        ) = None
        self._sample_publication_start_attempted = False
        self._sample_publication_start_error = ""
        self._sample_publication_stop_error = ""
        self._sample_publication_termination: (
            ModemSamplePublicationTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
            | None
        ) = None
        self._sample_publication_last_termination: (
            ModemSamplePublicationTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
            | None
        ) = None
        self._internal_runtime_clock_stop = Event()
        self._internal_runtime_clock_thread = None
        self._internal_runtime_clock_register_thread = None
        self._internal_core_clock_wake_thread = None
        self._internal_runtime_clock_interval_s = (
            INTERNAL_RUNTIME_CLOCK_INTERVAL_S
        )
        self._runtime_clock_termination: (
            InternalRuntimeClockTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
            | None
        ) = None
        self._runtime_clock_last_termination: (
            InternalRuntimeClockTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
            | None
        ) = None
        self._runtime_clock_start: RuntimeParticipantStartReceipt | None = None
        self._runtime_clock_start_attempted = False
        self._runtime_clock_start_error = ""
        self._runtime_frame_start: RuntimeParticipantStartReceipt | None = None
        self._runtime_frame_entered = False
        self._runtime_frame_last_termination: (
            RuntimeFrameBoundaryTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
            | None
        ) = None
        self._required_workers_last_termination: (
            RequiredRuntimeWorkerTerminationReceipt | None
        ) = None
        self._participant_lock = RLock()
        self._termination_issuer = object()

    def activate(self, root: ProductStartupReceipt) -> None:
        if not isinstance(root, ProductStartupReceipt):
            raise TypeError("product_ngui_ready_root_required")
        def activate_child() -> None:
            if self._root is not None:
                raise RuntimeError("product_ngui_root_overlap")
            self._root = root

        self._product._activate_runtime_child(
            root,
            runtime_kind="ngui",
            activate=activate_child,
        )

    def _require_root(self) -> ProductStartupReceipt:
        root = self._root
        if root is None or not self._product._admission.admits(root):
            raise RuntimeError("product_ngui_external_admission_closed")
        return root

    @contextmanager
    def _runtime_child_operation(self, *, starting: bool):
        """Serialize one Product-owned NGUI child operation with shutdown."""

        product = self._product
        with product._lifecycle:
            root = self._root
            if root is None:
                raise RuntimeError("product_ngui_runtime_root_missing")
            if root.lifecycle_generation != product._lifecycle_generation:
                raise RuntimeError("product_ngui_runtime_generation_retired")
            if starting and (
                product._stop_requested
                or not product._lifecycle_state.admits_service
                or not product._admission.admits(root)
            ):
                raise RuntimeError(
                    "product_ngui_runtime_child_start_admission_closed"
                )
            product._service_inflight += 1
        try:
            yield root
        finally:
            with product._lifecycle:
                product._service_inflight -= 1
                if product._service_inflight < 0:
                    raise RuntimeError(
                        "product_ngui_runtime_child_inflight_underflow"
                    )
                product._lifecycle.notify_all()

    def accepted_root(self) -> ProductStartupReceipt:
        return self._require_root()

    def _require_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        root = self._require_root()
        return self._product._admission.require_runtime_namespace(
            root,
            namespace,
        )

    def attach_core_client(
        self,
        manifest: CoreClientManifest,
        *,
        hook_id: str,
        mode: CoreHookAttachmentMode,
    ) -> None:
        self._require_root()
        self._product._core.extension_hooks.attach(
            manifest,
            hook_id=hook_id,
            mode=mode,
        )

    def detach_core_client(
        self,
        client_id: str,
        *,
        hook_id: str,
    ):
        with self._runtime_child_operation(starting=False):
            return self._product._core.extension_hooks.detach(
                client_id,
                hook_id=hook_id,
            )

    def worker_health_authority(self):
        self._require_root()
        return self._product._core.worker_health

    def fault_supervisor_authority(self):
        self._require_root()
        return self._product._core.fault_supervisor

    def clock_job_workers(self):
        self._require_root()
        return self._product._core.clock_job_workers

    def runtime_snapshot_for(
        self,
        client_id: str,
        *,
        accepted_root=None,
        health: dict[str, Any],
        metadata: dict[str, Any],
        published_at: float,
    ):
        self._require_root()
        return require_core_runtime_snapshot_publisher(
            self._product._core
        ).snapshot_for(
            client_id,
            accepted_root=accepted_root,
            health=health,
            metadata=metadata,
            published_at=published_at,
        )

    def runtime_register_sequence(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> int:
        """Return the O(1) semantic Core register-bank sequence."""

        self._require_namespace(namespace)
        return require_core_runtime_register_bank(
            self._product._core
        ).sequence

    def accepted_root_customer_port(
        self,
    ) -> CoreAcceptedRootOutboxPort:
        """Return only the typed claim/receipt boundary for accepted roots."""

        self._require_root()
        return self._product._core.accepted_root_outbox

    def foundation_extension_latest(
        self,
        namespace: RuntimeGenerationNamespace,
    ):
        self._require_namespace(namespace)
        return self._product._foundation.ngui_extension.latest_for(namespace)

    def publish_runtime_health(
        self,
        *,
        phase: str,
        cycle_count: int,
        error: BaseException | str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        self._require_root()
        return self._product._foundation.runtime_health.mark(
            phase,
            cycle_count=max(0, int(cycle_count)),
            error=error,
            workers=dict(self._product._core.worker_health.status()),
            faults=dict(self._product._core.fault_supervisor.status()),
            detail=dict(detail or {}),
        )

    def bind_runtime_clock_wake_listener(
        self,
        listener: Callable[[], None] | None,
    ) -> None:
        if listener is not None:
            self._require_root()
        bind_runtime_clock_wake_listener(self._product._core, listener)

    def bind_command_status_wake_listener(
        self,
        listener: Callable[[], None] | None,
    ) -> None:
        if listener is not None:
            self._require_root()
        bind_core_command_status_wake_listener(
            self._product._core,
            listener,
        )

    def bind_sample_wake_listener(
        self,
        listener: Callable[[], None] | None,
    ) -> None:
        if listener is not None:
            self._require_root()
        require_modem_sample_publication_fabric(
            self._product._core
        ).set_wake_listener(listener)

    def wake_sample_publication(self) -> None:
        self._require_root()
        require_modem_sample_publication_fabric(self._product._core).wake()

    def start_sample_publication(self) -> RuntimeParticipantStartReceipt:
        with self._participant_lock:
            with self._runtime_child_operation(starting=True) as root:
                if (
                    self._sample_publication is not None
                    or self._sample_publication_start is not None
                    or self._sample_publication_start_attempted
                ):
                    raise RuntimeError(
                        "product_ngui_sample_publication_overlap"
                    )
                if self._sample_publication_termination is not None:
                    raise RuntimeError(
                        "product_ngui_sample_publication_already_terminated"
                    )
                service = ModemSamplePublicationService(
                    core=self._product._core,
                    is_running=lambda: (
                        self._root is root
                        and self._product._admission.admits(root)
                    ),
                    report_error=lambda message: (
                        self._product._core.fault_supervisor.record(
                            boundary=FaultBoundaryClass.WORKER,
                            owner="ngui_modem_sample_publication",
                            operation="publish",
                            generation=self._product._lifecycle_generation,
                            severity=FaultSeverity.ERROR,
                            retryable=True,
                            degraded_behavior=(
                                "retain_last_accepted_generation_and_retry"
                            ),
                            error=RuntimeError(message),
                            suppression_budget=3,
                        )
                    ),
                )
                self._sample_publication = service
                self._sample_publication_start_attempted = True
                try:
                    service.start()
                except BaseException as exc:
                    self._sample_publication_start_error = (
                        f"{type(exc).__name__}:{exc}"
                    )
                    raise
                receipt = RuntimeParticipantStartReceipt(
                    participant=(
                        RuntimeTerminationParticipant
                        .MODEM_SAMPLE_PUBLICATION
                    ),
                    lifecycle_generation=root.lifecycle_generation,
                    started_at=time(),
                )
                self._sample_publication_start = receipt
                return receipt

    def stop_sample_publication(
        self,
        *,
        timeout_s: float,
    ) -> (
        ModemSamplePublicationTerminationReceipt
        | RuntimeParticipantAbsenceReceipt
    ):
        with self._participant_lock:
            with self._runtime_child_operation(starting=False):
                if self._sample_publication_termination is not None:
                    return self._sample_publication_termination
                service = self._sample_publication
                if service is None and not self._sample_publication_start_attempted:
                    receipt = RuntimeParticipantAbsenceReceipt(
                        participant=(
                            RuntimeTerminationParticipant
                            .MODEM_SAMPLE_PUBLICATION
                        ),
                        disposition=(
                            RuntimeParticipantAbsenceDisposition.NOT_CREATED
                        ),
                        blocker=RuntimeTerminationBlocker.NONE,
                        lifecycle_generation=(
                            self._require_root().lifecycle_generation
                        ),
                        observed_at=time(),
                        _issuer=self._termination_issuer,
                    )
                elif service is not None:
                    try:
                        # Accepted Foundation work can still read the Core root.
                        # Drain it before publication.stop retires that root.
                        self._product._foundation.quiesce(
                            lifecycle_generation=self._root.lifecycle_generation
                        )
                        receipt = service.stop(timeout_s=timeout_s)
                        if not isinstance(
                            receipt,
                            ModemSamplePublicationTerminationReceipt,
                        ):
                            raise TypeError(
                                "product_ngui_sample_termination_receipt_required"
                            )
                    except Exception as exc:
                        self._sample_publication_stop_error = f"{type(exc).__name__}:{exc}"
                        raise
                else:
                    raise RuntimeError(
                        "product_ngui_sample_start_attempt_has_no_owner"
                    )
                self._sample_publication_last_termination = receipt
                if receipt.terminated:
                    self._sample_publication_stop_error = ""
                    self._sample_publication_termination = receipt
                return receipt

    def start_runtime_clock(self) -> RuntimeParticipantStartReceipt:
        with self._participant_lock:
            with self._runtime_child_operation(starting=True) as root:
                if (
                    self._runtime_clock_start is not None
                    or self._runtime_clock_start_attempted
                ):
                    raise RuntimeError(
                        "product_ngui_runtime_clock_overlap"
                    )
                if self._runtime_clock_termination is not None:
                    raise RuntimeError(
                        "product_ngui_runtime_clock_already_terminated"
                    )
                self._runtime_clock_start_attempted = True
                try:
                    start_internal_runtime_clock(
                        self,
                        core=self._product._core,
                        is_running=lambda: (
                            self._root is root
                            and self._product._admission.admits(root)
                        ),
                        logger=log,
                        interval_s=self._internal_runtime_clock_interval_s,
                    )
                except BaseException as exc:
                    # Entering the three-worker activation transaction is
                    # lifecycle evidence even if a later Thread.start fails.
                    # Keep it set so shutdown proves stopped/survivor state
                    # through the clock owner instead of emitting NOT_CREATED.
                    self._runtime_clock_start_error = (
                        f"{type(exc).__name__}:{exc}"
                    )
                    raise
                receipt = RuntimeParticipantStartReceipt(
                    participant=RuntimeTerminationParticipant.RUNTIME_CLOCK,
                    lifecycle_generation=root.lifecycle_generation,
                    started_at=time(),
                )
                self._runtime_clock_start = receipt
                return receipt

    def stop_runtime_clock(
        self,
        *,
        timeout_s: float,
    ) -> (
        InternalRuntimeClockTerminationReceipt
        | RuntimeParticipantAbsenceReceipt
    ):
        with self._participant_lock:
            with self._runtime_child_operation(starting=False):
                if self._runtime_clock_termination is not None:
                    return self._runtime_clock_termination
                created = self._runtime_clock_start_attempted or any(
                    thread is not None
                    for thread in (
                        self._internal_runtime_clock_thread,
                        self._internal_runtime_clock_register_thread,
                        self._internal_core_clock_wake_thread,
                    )
                )
                if not created:
                    receipt = RuntimeParticipantAbsenceReceipt(
                        participant=RuntimeTerminationParticipant.RUNTIME_CLOCK,
                        disposition=(
                            RuntimeParticipantAbsenceDisposition.NOT_CREATED
                        ),
                        blocker=RuntimeTerminationBlocker.NONE,
                        lifecycle_generation=(
                            self._require_root().lifecycle_generation
                        ),
                        observed_at=time(),
                        _issuer=self._termination_issuer,
                    )
                else:
                    receipt = stop_internal_runtime_clock(
                        self,
                        core=self._product._core,
                        timeout_s=timeout_s,
                    )
                    if not isinstance(
                        receipt,
                        InternalRuntimeClockTerminationReceipt,
                    ):
                        raise TypeError(
                            "product_ngui_clock_termination_receipt_required"
                        )
                self._runtime_clock_last_termination = receipt
                if receipt.terminated:
                    self._runtime_clock_termination = receipt
                return receipt

    def issue_required_workers_termination(
        self,
        *,
        expected_workers: tuple[str, ...],
        observed_workers: tuple[str, ...],
        survivors: tuple[str, ...],
    ) -> RequiredRuntimeWorkerTerminationReceipt:
        with self._participant_lock:
            with self._runtime_child_operation(starting=False) as root:
                if expected_workers != self._REQUIRED_RUNTIME_WORKERS:
                    raise ValueError(
                        "product_ngui_required_worker_denominator_invalid"
                    )
                receipt = RequiredRuntimeWorkerTerminationReceipt(
                    lifecycle_generation=root.lifecycle_generation,
                    expected_workers=expected_workers,
                    observed_workers=observed_workers,
                    survivors=survivors,
                    _issuer=self._termination_issuer,
                )
                self._required_workers_last_termination = receipt
                return receipt

    def enter_runtime_frame_boundary(
        self,
    ) -> RuntimeParticipantStartReceipt:
        """Record Product admission before the frame owner binds live effects."""

        with self._participant_lock:
            with self._runtime_child_operation(starting=True) as root:
                if self._runtime_frame_entered:
                    raise RuntimeError(
                        "product_ngui_runtime_frame_boundary_overlap"
                    )
                if self._runtime_frame_last_termination is not None:
                    raise RuntimeError(
                        "product_ngui_runtime_frame_already_terminated"
                    )
                receipt = RuntimeParticipantStartReceipt(
                    participant=RuntimeTerminationParticipant.RUNTIME_FRAME,
                    lifecycle_generation=root.lifecycle_generation,
                    started_at=time(),
                )
                self._runtime_frame_entered = True
                self._runtime_frame_start = receipt
                return receipt

    def _issue_state_derived_absence(
        self,
        *,
        participant: RuntimeTerminationParticipant,
        entered: bool,
        entered_blocker: RuntimeTerminationBlocker,
        requested_blocker: RuntimeTerminationBlocker,
    ) -> RuntimeParticipantAbsenceReceipt:
        """Derive absence semantics from Product state, never caller claims."""

        with self._runtime_child_operation(starting=False) as root:
            if entered:
                if requested_blocker is not entered_blocker:
                    raise RuntimeError(
                        "product_ngui_entered_participant_requires_owner_receipt:"
                        + participant.value
                    )
                disposition = (
                    RuntimeParticipantAbsenceDisposition
                    .BLOCKED_BY_PREDECESSOR
                )
                blocker = entered_blocker
            else:
                disposition = RuntimeParticipantAbsenceDisposition.NOT_CREATED
                blocker = RuntimeTerminationBlocker.NONE
            return RuntimeParticipantAbsenceReceipt(
                participant=participant,
                disposition=disposition,
                blocker=blocker,
                lifecycle_generation=root.lifecycle_generation,
                observed_at=time(),
                _issuer=self._termination_issuer,
            )

    def derive_frame_boundary_absence(
        self,
        *,
        blocker: RuntimeTerminationBlocker,
    ) -> RuntimeParticipantAbsenceReceipt:
        with self._participant_lock:
            receipt = self._issue_state_derived_absence(
                participant=RuntimeTerminationParticipant.RUNTIME_FRAME,
                entered=self._runtime_frame_entered,
                entered_blocker=RuntimeTerminationBlocker.REQUIRED_WORKERS,
                requested_blocker=blocker,
            )
            self._runtime_frame_last_termination = receipt
            return receipt

    def derive_sample_publication_absence(
        self,
        *,
        blocker: RuntimeTerminationBlocker,
    ) -> RuntimeParticipantAbsenceReceipt:
        with self._participant_lock:
            entered = bool(
                self._sample_publication_start_attempted
                or self._sample_publication is not None
                or self._sample_publication_start is not None
            )
            receipt = self._issue_state_derived_absence(
                participant=(
                    RuntimeTerminationParticipant.MODEM_SAMPLE_PUBLICATION
                ),
                entered=entered,
                entered_blocker=RuntimeTerminationBlocker.RUNTIME_FRAME,
                requested_blocker=blocker,
            )
            self._sample_publication_last_termination = receipt
            return receipt

    def derive_runtime_clock_absence(
        self,
        *,
        blocker: RuntimeTerminationBlocker,
    ) -> RuntimeParticipantAbsenceReceipt:
        with self._participant_lock:
            entered = bool(
                self._runtime_clock_start_attempted
                or self._runtime_clock_start is not None
                or any(
                    thread is not None
                    for thread in (
                        self._internal_runtime_clock_thread,
                        self._internal_runtime_clock_register_thread,
                        self._internal_core_clock_wake_thread,
                    )
                )
            )
            receipt = self._issue_state_derived_absence(
                participant=RuntimeTerminationParticipant.RUNTIME_CLOCK,
                entered=entered,
                entered_blocker=(
                    RuntimeTerminationBlocker.MODEM_SAMPLE_PUBLICATION
                ),
                requested_blocker=blocker,
            )
            self._runtime_clock_last_termination = receipt
            return receipt

    def issue_frame_boundary_termination(
        self,
        *,
        projection_thread_alive: bool,
        pending_projection_cancelled: bool,
        inflight_projection_cancelled: bool,
        listener_detachment_errors: tuple[str, ...],
        publisher,
    ) -> RuntimeFrameBoundaryTerminationReceipt:
        with self._participant_lock:
            with self._runtime_child_operation(starting=False) as root:
                if not self._runtime_frame_entered:
                    raise RuntimeError(
                        "product_ngui_runtime_frame_not_entered"
                    )
                receipt = RuntimeFrameBoundaryTerminationReceipt(
                    lifecycle_generation=root.lifecycle_generation,
                    projection_thread_alive=projection_thread_alive,
                    pending_projection_cancelled=(
                        pending_projection_cancelled
                    ),
                    inflight_projection_cancelled=(
                        inflight_projection_cancelled
                    ),
                    listener_detachment_errors=(
                        listener_detachment_errors
                    ),
                    publisher=publisher,
                    _issuer=self._termination_issuer,
                )
                self._runtime_frame_last_termination = receipt
                return receipt

    def command_outcome_stream(self):
        self._require_root()
        return self._product._core.command_outcome_stream

    def submit_core_command(
        self,
        request: object,
        matrix_identity: MatrixGenerationIdentity,
    ):
        if not isinstance(request, CoreCommandRequest):
            raise TypeError("product_core_command_typed_request_required")
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("product_core_command_matrix_identity_required")
        self._require_namespace(matrix_identity.namespace)
        if request.matrix_identity != matrix_identity:
            raise ValueError(
                "product_core_command_matrix_identity_mismatch"
            )
        return require_core_command_gateway(self._product._core).submit(request)

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        self._require_root()
        return self._product.service_once(now=now)

    def transact_operational_request(
        self,
        typed: object,
        matrix_identity: MatrixGenerationIdentity,
    ) -> OperationalServiceReceipt:
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("product_operational_matrix_identity_required")
        self._require_namespace(matrix_identity.namespace)
        if operational_request_matrix_identity(typed) != matrix_identity:
            raise ValueError(
                "product_operational_request_matrix_identity_mismatch"
            )
        return self._product._operational_transactions.execute(
            typed,
            effect_owner=self._operational_effect_owner(typed),
            effect=lambda permit: self._execute_operational_effect(
                typed,
                matrix_identity,
                permit=permit,
            ),
            reconcile=lambda request, transaction, permit: (
                self._reconcile_operational_effect(
                    request,
                    transaction,
                    matrix_identity,
                    permit=permit,
                )
            ),
            on_effect_failure=lambda error: (
                self._product._core.fault_supervisor.record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner="product_operational_transaction",
                    operation=operational_request_kind(typed).value,
                    generation=matrix_identity.matrix_sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=False,
                    degraded_behavior=(
                        "retain_exact_failed_receipt_and_do_not_repeat_effect"
                    ),
                    error=error,
                    suppression_budget=0,
                )
            ),
        )

    def _reconcile_operational_effect(
        self,
        typed: object,
        transaction: object,
        matrix_identity: MatrixGenerationIdentity,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt | None:
        del transaction
        if operational_request_matrix_identity(typed) != matrix_identity:
            raise ValueError(
                "product_operational_reconcile_matrix_identity_mismatch"
            )
        require_product_operational_effect_permit(
            permit,
            typed,
            expected_kind=operational_request_kind(typed),
        )
        core = self._product._core
        foundation = self._product._foundation
        if isinstance(typed, WifiRadioControlRequest):
            return core.device_control.reconcile_wifi_radio(typed)
        if isinstance(typed, DownstreamHealthConfigRequest):
            return (
                core.core_diagnostics.reconcile_downstream_health(typed)
            )
        if isinstance(typed, SmsCommandControlConfigRequest):
            return (
                foundation.automation
                .reconcile_sms_command_control(typed)
            )
        if isinstance(typed, AntennaGuideControlRequest):
            return foundation.antenna_guide.reconcile_control(typed)
        if isinstance(typed, IntelligenceAutopilotControlRequest):
            return (
                self._product.intelligence_matrix()
                .reconcile_autopilot_request(typed, permit=permit)
            )
        if isinstance(typed, IntelligenceExploreControlRequest):
            return (
                self._product.intelligence_matrix()
                .reconcile_explore_request(typed, permit=permit)
            )
        if isinstance(
            typed,
            (
                BandSchedulePlanRequest,
                BandSequencePlanRequest,
                BandScheduleCancelRequest,
                BandSequenceCancelRequest,
            ),
        ):
            return (
                foundation.schedule
                .reconcile_operational_request(typed)
            )
        return None

    def _execute_operational_effect(
        self,
        typed: object,
        matrix_identity: MatrixGenerationIdentity,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        require_product_operational_effect_permit(
            permit,
            typed,
            expected_kind=operational_request_kind(typed),
        )
        core = self._product._core
        foundation = self._product._foundation
        if isinstance(typed, RouterReadDiagnosticRequest):
            receipt = core.device_control.read_diagnostics(typed, permit=permit)
        elif isinstance(typed, NetworkSettingsInspectionRequest):
            receipt = core.device_control.inspect_network_settings(typed, permit=permit)
        elif isinstance(typed, WifiRadioControlRequest):
            receipt = core.device_control.set_wifi_radio(
                typed,
                permit=permit,
            )
        elif isinstance(typed, DownstreamHealthConfigRequest):
            receipt = core.core_diagnostics.configure_downstream_health(
                typed,
                permit=permit,
            )
        elif isinstance(typed, SmsCommandControlConfigRequest):
            if typed.enabled:
                _require_sms_enable_supported(core)
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="sms_runtime",
                effect=lambda: foundation.automation.configure_sms_command_control(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, AntennaGuideControlRequest):
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="antenna_pipeline",
                effect=lambda: foundation.antenna_guide.control(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, BandSchedulePlanRequest):
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="schedule_pipeline",
                effect=lambda: foundation.schedule.plan_band_schedule(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, BandSequencePlanRequest):
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="schedule_pipeline",
                effect=lambda: foundation.schedule.plan_band_sequence(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, BandScheduleCancelRequest):
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="schedule_pipeline",
                effect=lambda: foundation.schedule.cancel_band_schedule(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, BandSequenceCancelRequest):
            receipt = foundation.run_bound_effect(
                matrix_identity=matrix_identity,
                dependency_group="schedule_pipeline",
                effect=lambda: foundation.schedule.cancel_band_sequence(
                    typed,
                    permit=permit,
                ),
            )
        elif isinstance(typed, IntelligenceAutopilotControlRequest):
            receipt = (
                self._product.intelligence_matrix()
                .set_autopilot_requested(typed, permit=permit)
            )
        elif isinstance(typed, IntelligenceExploreControlRequest):
            receipt = (
                self._product.intelligence_matrix()
                .set_explore_requested(typed, permit=permit)
            )
        else:
            raise TypeError("product_operational_request_type_invalid")
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("product_operational_receipt_required")
        if (
            receipt.runtime_namespace != matrix_identity.namespace
            or receipt.matrix_identity != matrix_identity
        ):
            raise ValueError(
                "product_operational_result_matrix_identity_mismatch"
            )
        return receipt

    @staticmethod
    def _operational_effect_owner(typed: object) -> str:
        kind = operational_request_kind(typed)
        return {
            OperationalKind.READ_DIAGNOSTICS: "54guc.core.device_control",
            OperationalKind.INSPECT_NETWORK_SETTINGS: "54guc.core.device_control",
            OperationalKind.WIFI_SET_RADIO: "54guc.core.device_control",
            OperationalKind.SET_DOWNSTREAM_HEALTH: (
                "54guc.core.diagnostics"
            ),
            OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL: (
                "54guc.foundation.automation"
            ),
            OperationalKind.ANTENNA_GUIDE: (
                "54guc.foundation.antenna_guide"
            ),
            OperationalKind.PLAN_BAND_SCHEDULE: (
                "54guc.foundation.schedule"
            ),
            OperationalKind.PLAN_BAND_SEQUENCE: (
                "54guc.foundation.schedule"
            ),
            OperationalKind.CANCEL_BAND_SCHEDULE: (
                "54guc.foundation.schedule"
            ),
            OperationalKind.CANCEL_BAND_SEQUENCE: (
                "54guc.foundation.schedule"
            ),
            OperationalKind.SET_INTELLIGENCE_AUTOPILOT: (
                INTELLIGENCE_MATRIX_OWNER
            ),
            OperationalKind.SET_INTELLIGENCE_EXPLORE: (
                INTELLIGENCE_MATRIX_OWNER
            ),
        }[kind]

    def deliver_operational_receipt(
        self,
        receipt: OperationalServiceReceipt,
        matrix_identity: MatrixGenerationIdentity,
    ) -> None:
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("product_operational_receipt_required")
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("product_operational_matrix_identity_required")
        self._require_namespace(matrix_identity.namespace)
        if (
            receipt.runtime_namespace != matrix_identity.namespace
            or receipt.matrix_identity != matrix_identity
        ):
            raise ValueError(
                "product_operational_publication_matrix_identity_mismatch"
            )
        self._product._operational_transactions.require_terminal(receipt)
        if receipt.kind in self._CORE_OPERATIONAL_KINDS:
            publish_operational_service_status(self._product._core, receipt)
        elif receipt.kind not in (
            self._FOUNDATION_OPERATIONAL_KINDS
            | self._PRODUCT_OPERATIONAL_KINDS
        ):
            raise RuntimeError(
                "product_operational_receipt_owner_unclassified:"
                + receipt.kind.value
            )

    def acknowledge_operational_receipt(
        self,
        receipt: OperationalServiceReceipt,
        matrix_identity: MatrixGenerationIdentity,
    ) -> None:
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("product_operational_matrix_identity_required")
        self._require_namespace(matrix_identity.namespace)
        if receipt.matrix_identity != matrix_identity:
            raise ValueError(
                "product_operational_ack_matrix_identity_mismatch"
            )
        self._product._operational_transactions.acknowledge_presentation(
            receipt
        )

    def record_runtime_termination(
        self,
        *,
        required_workers: RequiredRuntimeWorkerTerminationReceipt,
        publication: (
            ModemSamplePublicationTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
        clock: (
            InternalRuntimeClockTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
        frame: (
            RuntimeFrameBoundaryTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
    ) -> ProductChildTerminationReceipt:
        with self._participant_lock:
            with self._runtime_child_operation(starting=False):
                return self._record_runtime_termination(
                    required_workers=required_workers,
                    publication=publication,
                    clock=clock,
                    frame=frame,
                )

    def _record_runtime_termination(
        self,
        *,
        required_workers: RequiredRuntimeWorkerTerminationReceipt,
        publication: (
            ModemSamplePublicationTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
        clock: (
            InternalRuntimeClockTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
        frame: (
            RuntimeFrameBoundaryTerminationReceipt
            | RuntimeParticipantAbsenceReceipt
        ),
    ) -> ProductChildTerminationReceipt:
        if not isinstance(
            required_workers,
            RequiredRuntimeWorkerTerminationReceipt,
        ):
            raise TypeError(
                "product_ngui_required_workers_termination_receipt_required"
            )
        root = self._root
        if (
            root is None
            or required_workers.lifecycle_generation
            != root.lifecycle_generation
            or required_workers._issuer is not self._termination_issuer
            or required_workers.expected_workers
            != self._REQUIRED_RUNTIME_WORKERS
            or required_workers
            is not self._required_workers_last_termination
        ):
            raise RuntimeError(
                "product_ngui_required_workers_termination_not_product_issued"
            )
        participant_receipts = (
            (
                "publication",
                publication,
                ModemSamplePublicationTerminationReceipt,
                RuntimeTerminationParticipant.MODEM_SAMPLE_PUBLICATION,
            ),
            (
                "clock",
                clock,
                InternalRuntimeClockTerminationReceipt,
                RuntimeTerminationParticipant.RUNTIME_CLOCK,
            ),
            (
                "frame",
                frame,
                RuntimeFrameBoundaryTerminationReceipt,
                RuntimeTerminationParticipant.RUNTIME_FRAME,
            ),
        )
        for (
            field_name,
            receipt,
            concrete_type,
            expected_participant,
        ) in participant_receipts:
            if not isinstance(
                receipt,
                (concrete_type, RuntimeParticipantAbsenceReceipt),
            ):
                raise TypeError(
                    f"product_ngui_{field_name}_termination_receipt_required"
                )
            if (
                isinstance(receipt, RuntimeParticipantAbsenceReceipt)
                and (
                    receipt.participant is not expected_participant
                    or receipt.lifecycle_generation
                    != root.lifecycle_generation
                    or receipt._issuer is not self._termination_issuer
                )
            ):
                raise RuntimeError(
                    f"product_ngui_{field_name}_absence_not_product_issued"
                )
        if (
            isinstance(frame, RuntimeFrameBoundaryTerminationReceipt)
            and (
                frame.lifecycle_generation != root.lifecycle_generation
                or frame._issuer is not self._termination_issuer
            )
        ):
            raise RuntimeError(
                "product_ngui_frame_termination_not_product_issued"
            )
        if frame is not self._runtime_frame_last_termination:
            raise RuntimeError(
                "product_ngui_frame_termination_not_product_state_derived"
            )
        if publication is not self._sample_publication_last_termination:
            raise RuntimeError(
                "product_ngui_publication_termination_not_product_issued"
            )
        if clock is not self._runtime_clock_last_termination:
            raise RuntimeError(
                "product_ngui_clock_termination_not_product_issued"
            )
        source_receipts = (
            required_workers,
            publication,
            clock,
            frame,
        )
        if self._termination_sources is not None:
            if any(
                current is not retained
                for current, retained in zip(
                    source_receipts,
                    self._termination_sources,
                    strict=True,
                )
            ):
                raise RuntimeError(
                    "product_ngui_termination_receipt_conflict"
                )
            if self._termination is None:
                raise RuntimeError(
                    "product_ngui_termination_receipt_missing"
                )
            return self._termination

        survivors = list(required_workers.survivors)
        errors = [
            f"required_survivor:{item}"
            for item in required_workers.survivors
        ]
        if self._sample_publication_start_error:
            errors.append(
                "publication_start_failed:"
                + self._sample_publication_start_error
            )
        if self._runtime_clock_start_error:
            errors.append(
                "clock_start_failed:" + self._runtime_clock_start_error
            )
        if self._sample_publication_stop_error:
            errors.append("publication_stop_failed:" + self._sample_publication_stop_error)
        if not publication.terminated:
            survivors.append("ngui_modem_sample_publication")
            errors.append("participant_not_terminated:publication")
        if not clock.terminated:
            survivors.append("ngui_runtime_clock")
            errors.append("participant_not_terminated:clock")
        if not frame.terminated:
            survivors.append("ngui_runtime_frame_boundary")
            errors.append("participant_not_terminated:frame")
        normalized = tuple(sorted(set(survivors)))
        termination = ProductChildTerminationReceipt(
            child_id="ngui_runtime",
            lifecycle_generation=self._product._lifecycle_generation,
            disposition=(
                ProductTerminationDisposition.COMPLETE
                if not normalized and not errors
                else ProductTerminationDisposition.DEGRADED
            ),
            survivors=normalized,
            errors=tuple(errors),
            evidence={
                "required_workers": required_workers.as_dict(),
                "publication": publication.as_dict(),
                "clock": clock.as_dict(),
                "frame": frame.as_dict(),
            },
        )
        self._termination_sources = source_receipts
        self._termination = termination
        return termination

    def termination_receipt(self) -> ProductChildTerminationReceipt:
        with self._participant_lock:
            if self._termination is not None:
                return self._termination
            if self._root is None:
                return ProductChildTerminationReceipt(
                    child_id="ngui_runtime",
                    lifecycle_generation=self._product._lifecycle_generation,
                    disposition=ProductTerminationDisposition.COMPLETE,
                )
            retained = {
                "required_workers": (
                    self._required_workers_last_termination
                ),
                "frame": self._runtime_frame_last_termination,
                "publication": self._sample_publication_last_termination,
                "clock": self._runtime_clock_last_termination,
            }
            survivors: list[str] = []
            errors: list[str] = []
            evidence: dict[str, Any] = {}
            survivor_names = {
                "required_workers": (
                    "ngui_required_workers_termination_unproven"
                ),
                "frame": (
                    "ngui_runtime_frame_boundary_termination_unproven"
                ),
                "publication": (
                    "ngui_modem_sample_publication_termination_unproven"
                ),
                "clock": "ngui_runtime_clock_termination_unproven",
            }
            for name, receipt in retained.items():
                if receipt is None:
                    survivors.append(survivor_names[name])
                    errors.append(name + "_termination_unproven")
                    continue
                evidence[name] = receipt.as_dict()
                if not receipt.terminated:
                    survivors.append(survivor_names[name])
                    errors.append(name + "_participant_not_terminated")
            if self._sample_publication_start_error:
                errors.append(
                    "publication_start_failed:"
                    + self._sample_publication_start_error
                )
            if self._runtime_clock_start_error:
                errors.append(
                    "clock_start_failed:" + self._runtime_clock_start_error
                )
            if self._sample_publication_stop_error:
                errors.append("publication_stop_failed:" + self._sample_publication_stop_error)
            return ProductChildTerminationReceipt(
                child_id="ngui_runtime",
                lifecycle_generation=self._product._lifecycle_generation,
                disposition=ProductTerminationDisposition.RETAINED,
                survivors=tuple(survivors),
                errors=tuple(errors),
                evidence=evidence,
            )


class ProductSupervisor:
    """Own ordered CGF lifecycle and expose no mutable combined tree."""

    # A single operator Connect may recover from transient startup failures,
    # but must eventually return control. Established-session repair is owned
    # separately by ConnectivityModule and is unaffected by this limit.
    MAX_INITIAL_STARTUP_ATTEMPTS = 3

    def __init__(
        self,
        config: Arch4Config,
        *,
        settings_dir: str | Path,
        state_dir: str | Path,
        runtime_dir: str | Path,
        modem: ModemInterface | None = None,
        trace_selected_at: float | None = None,
        verify_only: bool = True,
        check_only: bool = False,
        observe_only: bool = False,
        probe_mode: bool = False,
    ) -> None:
        self._config = config
        self._settings_dir = Path(settings_dir).resolve()
        self._state_dir = Path(state_dir).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self._modem = modem
        self._verify_only = bool(verify_only)
        self._check_only = bool(check_only)
        self._observe_only = bool(observe_only)
        self._probe_mode = bool(probe_mode)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._startup_wire_trace = StartupModemTrace(selected_at=trace_selected_at)
        self._startup_identity_prompt: Callable[[], str] | None = None
        self._build_composition_roots()
        self._initialize_lifecycle()

    def _build_composition_roots(self) -> None:
        from arch4.product_intelligence_runtime import read_confirmed_device_associations
        diagnostics = CgfEvidenceStore(self.runtime_dir)
        self._admission = ProductRuntimeAdmissionAuthority()
        self._operational_transactions = (
            ProductOperationalTransactionAuthority(self._state_dir)
        )
        self._core = CoreCompositionRoot(
            self._config,
            settings_dir=self._settings_dir,
            state_dir=self._state_dir,
            runtime_dir=self.runtime_dir,
            event_log=diagnostics,
            startup_wire_trace=self._startup_wire_trace,
            wire_trace_sink=diagnostics.log_modem_wire,
            product_admission=self._admission,
            modem=self._modem,
            verify_only=self._verify_only,
            check_only=self._check_only,
            observe_only=self._observe_only,
            probe_mode=self._probe_mode,
            device_association_reader=lambda: read_confirmed_device_associations(self._state_dir),
        )
        if self._startup_identity_prompt is not None:
            self._core.bind_startup_identity_prompt(
                self._startup_identity_prompt
            )
        self._foundation = FoundationCompositionRoot(
            self._core,
            state_dir=self._state_dir,
            runtime_dir=self.runtime_dir,
            diagnostics=diagnostics,
            product_admission=self._admission,
        )
        self._core.bind_provider_pool_policy(
            self._foundation.provider_band_profiles.resolve_contract
        )

    def _rebuild_after_startup_cleanup(self) -> bool:
        """Create a fresh generation only after zero-survivor cleanup proof."""

        if self._modem is not None:
            return False
        self._build_composition_roots()
        return True

    def _initialize_lifecycle(self) -> None:
        """Initialize the one Product lifecycle transaction authority."""

        self._lifecycle = Condition(RLock())
        self._lifecycle_state = ProductLifecycleState.IDLE
        self._lifecycle_generation = 0
        self._startup_inflight = False
        self._service_inflight = 0
        self._stop_requested = False
        self._shutdown_coordinator = False
        self._last_startup_generation = 0
        self._last_startup_receipt: ProductStartupReceipt | None = None
        self._last_shutdown_receipt: ProductShutdownReceipt | None = None
        self._core_root_publication_receipt: (
            CoreAcceptedRootPublicationReceipt | None
        ) = None
        self._post_startup_evidence: dict[str, Any] | None = None
        self._active_runtime_kind = ""
        self._headless_port: ProductHeadlessRuntimePort | None = None
        self._ngui_port: ProductNGUIRuntimePort | None = None
        self._intelligence_matrix: ProductIntelligenceMatrix | None = None

    def headless_runtime_port(self) -> ProductHeadlessRuntimePort:
        with self._lifecycle:
            if self._headless_port is None:
                self._headless_port = ProductHeadlessRuntimePort(self)
            return self._headless_port

    def ngui_runtime_port(self) -> ProductNGUIRuntimePort:
        with self._lifecycle:
            if self._ngui_port is None:
                self._ngui_port = ProductNGUIRuntimePort(self)
            return self._ngui_port

    def bind_startup_identity_prompt(
        self,
        prompt: Callable[[], str] | None,
    ) -> None:
        with self._lifecycle:
            if self._lifecycle_state is not ProductLifecycleState.IDLE:
                raise RuntimeError(
                    "product_startup_identity_prompt_after_start_forbidden"
                )
            self._startup_identity_prompt = prompt
            self._core.bind_startup_identity_prompt(prompt)

    def intelligence_matrix(self) -> ProductIntelligenceMatrix:
        """Return the sole two-bank optional-customer boundary."""

        with self._lifecycle:
            if self._intelligence_matrix is None:
                self._intelligence_matrix = ProductIntelligenceMatrix(self)
            return self._intelligence_matrix

    @staticmethod
    def _require_exact_core_publication_receipt(
        root: ProductStartupReceipt,
        receipt: CoreAcceptedRootPublicationReceipt,
    ) -> None:
        if not isinstance(receipt, CoreAcceptedRootPublicationReceipt):
            raise TypeError(
                "product_core_root_publication_receipt_required"
            )
        candidate = root.core_candidate
        if candidate is None:
            raise RuntimeError(
                "product_core_root_publication_candidate_missing"
            )
        proof = candidate.private_root_proof
        if (
            receipt.matrix_identity != proof.matrix_identity
            or receipt.root_fingerprint
            != proof.accepted_root_fingerprint
        ):
            raise RuntimeError(
                "product_core_root_publication_proof_mismatch"
            )
        if (
            receipt.schema != "CoreAcceptedRootPublicationReceipt.v1"
            or receipt.owner != "54guc.core.accepted_root_outbox"
        ):
            raise RuntimeError(
                "product_core_root_publication_contract_mismatch"
            )

    def _commit_ready_root(
        self,
        receipt: ProductStartupReceipt,
    ) -> CoreAcceptedRootPublicationReceipt:
        """Run private cutovers before the one external admission assignment."""

        self._foundation.activate_prepared_runtime(receipt)
        core_candidate = receipt.core_candidate
        if core_candidate is None:
            raise RuntimeError(
                "product_ready_core_candidate_missing"
            )
        publication_receipt = (
            self._core.open_accepted_root_publication(core_candidate)
        )
        self._require_exact_core_publication_receipt(
            receipt,
            publication_receipt,
        )
        self._admission.install(receipt)
        return publication_receipt

    def _activate_runtime_child(
        self,
        root: ProductStartupReceipt,
        *,
        runtime_kind: str,
        activate: Callable[[], None],
    ) -> None:
        """Serialize the one selected runtime surface with Product shutdown."""

        kind = str(runtime_kind or "")
        if kind not in {"headless", "ngui"}:
            raise ValueError("product_runtime_kind_invalid")
        with self._lifecycle:
            if (
                self._lifecycle_state is not ProductLifecycleState.RUNNING
                or self._stop_requested
                or self._last_startup_receipt != root
                or not self._admission.admits(root)
                or self._core_root_publication_receipt is None
            ):
                raise RuntimeError(
                    "product_runtime_activation_admission_closed"
                )
            if self._active_runtime_kind:
                raise RuntimeError(
                    "product_runtime_surface_overlap_forbidden"
                )
            if self._service_inflight != 0:
                raise RuntimeError(
                    "product_runtime_activation_work_overlap_forbidden"
                )
            self._service_inflight += 1
            try:
                try:
                    activate()
                except BaseException:
                    port = (
                        self._headless_port
                        if kind == "headless"
                        else self._ngui_port
                    )
                    termination = (
                        getattr(port, "_termination", None)
                        if port is not None
                        else None
                    )
                    if (
                        isinstance(
                            termination,
                            ProductChildTerminationReceipt,
                        )
                        and not termination.terminated
                    ):
                        self._active_runtime_kind = kind
                    raise
                else:
                    self._active_runtime_kind = kind
            finally:
                self._service_inflight -= 1
                if self._service_inflight < 0:
                    raise RuntimeError(
                        "product_runtime_activation_inflight_underflow"
                    )
                self._lifecycle.notify_all()

    def startup(self) -> ProductStartupReceipt:
        with self._lifecycle:
            if self._lifecycle_state is ProductLifecycleState.STARTING:
                awaited_generation = self._lifecycle_generation
                while (
                    self._startup_inflight
                    and self._lifecycle_generation == awaited_generation
                ):
                    self._lifecycle.wait()
                if (
                    self._last_startup_generation == awaited_generation
                    and self._last_startup_receipt is not None
                ):
                    return self._last_startup_receipt
            state = self._lifecycle_state
            generation = self._lifecycle_generation
            if state is ProductLifecycleState.RUNNING:
                if self._last_startup_receipt is None:
                    raise RuntimeError(
                        "product_running_startup_receipt_missing"
                    )
                return self._last_startup_receipt
            if state is ProductLifecycleState.STOPPED:
                return ProductStartupReceipt(
                    ProductStartupDisposition.TERMINAL,
                    ProductStartupReason.PRODUCT_ALREADY_SHUTDOWN,
                    detail=(
                        "Product startup was requested after terminal shutdown"
                    ),
                    error_type="RuntimeError",
                    lifecycle_generation=generation,
                    lifecycle_state=state.value,
                )
            if state is not ProductLifecycleState.IDLE:
                return ProductStartupReceipt(
                    ProductStartupDisposition.TERMINAL,
                    ProductStartupReason.PRODUCT_LIFECYCLE_NOT_STARTABLE,
                    detail=f"Product lifecycle state is {state.value}",
                    error_type="RuntimeError",
                    lifecycle_generation=generation,
                    lifecycle_state=state.value,
                )
            self._lifecycle_generation += 1
            generation = self._lifecycle_generation
            self._lifecycle_state = ProductLifecycleState.STARTING
            self._startup_inflight = True
        receipt: ProductStartupReceipt
        try:
            core_candidate = self._core.prepare_startup_candidate(
                lifecycle_generation=generation,
            )
            matrix_readiness = core_candidate.matrix_readiness
            with self._lifecycle:
                cancelled_after_core = self._stop_requested
            if cancelled_after_core:
                cleanup = self._cleanup_failed_startup(generation)
                receipt = self._cancelled_startup_receipt(
                    generation,
                    cleanup=cleanup,
                )
            else:
                foundation = self._foundation.prepare_startup(core_candidate)
                receipt = ProductStartupReceipt(
                    ProductStartupDisposition.READY,
                    ProductStartupReason.STARTED,
                    lifecycle_generation=generation,
                    lifecycle_state=ProductLifecycleState.RUNNING.value,
                    matrix_readiness=matrix_readiness,
                    core_candidate=core_candidate,
                    foundation=foundation,
                    intelligence=self.intelligence_attachment_status(),
                )
                self._core.validate_product_ready_root(
                    candidate=core_candidate,
                    product_root=receipt,
                )
                self._foundation.validate_product_ready_root(receipt)
        except Exception as exc:
            diagnostic_error = ""
            try:
                self._foundation.diagnostics.log_event(
                    "product_startup_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except Exception as diagnostic_exc:
                diagnostic_error = (
                    "diagnostic_publication:"
                    f"{type(diagnostic_exc).__name__}:{diagnostic_exc}"
                )
            if isinstance(exc, AuthenticationError):
                disposition = ProductStartupDisposition.TERMINAL
                reason = ProductStartupReason.AUTHENTICATION_FAILED
            elif isinstance(exc, ModemIdentityRequiredError):
                disposition = ProductStartupDisposition.TERMINAL
                reason = ProductStartupReason.MODEM_IDENTITY_REQUIRED
            elif isinstance(exc, TargetGuardError):
                disposition = ProductStartupDisposition.TERMINAL
                reason = ProductStartupReason.WRONG_TARGET
            elif isinstance(exc, (ConnectionError, TimeoutError, OSError)):
                if generation >= self.MAX_INITIAL_STARTUP_ATTEMPTS:
                    disposition = ProductStartupDisposition.TERMINAL
                    reason = ProductStartupReason.CONNECTION_ATTEMPTS_EXHAUSTED
                else:
                    disposition = ProductStartupDisposition.RETRYABLE
                    reason = ProductStartupReason.MODEM_TEMPORARILY_UNAVAILABLE
            else:
                disposition = ProductStartupDisposition.TERMINAL
                reason = ProductStartupReason.PRODUCT_STARTUP_FAULT
            with self._lifecycle:
                cancelled = self._stop_requested
            classified_detail = str(exc)
            if reason is ProductStartupReason.CONNECTION_ATTEMPTS_EXHAUSTED:
                classified_detail = (
                    f"Could not connect after {generation} attempts. "
                    "Check the selected router profile, address and web login, then try Connect again. "
                    f"Last error: {classified_detail}"
                )
            if diagnostic_error:
                classified_detail += f"; {diagnostic_error}"
            cleanup = self._cleanup_failed_startup(generation)
            cleanup_stopped = cleanup.clean
            rebuilt = False
            rebuild_error = ""
            if (
                cleanup_stopped
                and not cancelled
                and disposition is ProductStartupDisposition.RETRYABLE
            ):
                try:
                    rebuilt = self._rebuild_after_startup_cleanup()
                except Exception as rebuild_exc:
                    rebuild_error = (
                        f"{type(rebuild_exc).__name__}: {rebuild_exc}"
                    )
            cleanup = ProductStartupCleanupReceipt(
                lifecycle_generation=cleanup.lifecycle_generation,
                children=cleanup.children,
                zero_survivor=cleanup.zero_survivor,
                composition_rebuilt=rebuilt,
                errors=(
                    cleanup.errors
                    + ((f"composition_rebuild:{rebuild_error}",) if rebuild_error else ())
                ),
            )
            if cancelled:
                receipt = self._cancelled_startup_receipt(
                    generation,
                    detail=(
                        f"{type(exc).__name__}: {exc}; "
                        f"{diagnostic_error}"
                    ).rstrip("; "),
                    cleanup=cleanup,
                )
            elif (
                cleanup_stopped
                and disposition is ProductStartupDisposition.TERMINAL
            ):
                receipt = ProductStartupReceipt(
                    disposition,
                    reason,
                    detail=classified_detail,
                    error_type=type(exc).__name__,
                    lifecycle_generation=generation,
                    lifecycle_state=ProductLifecycleState.STOPPED.value,
                    classified_disposition=disposition,
                    classified_reason=reason,
                    cleanup=cleanup,
                )
            elif cleanup_stopped and rebuilt:
                receipt = ProductStartupReceipt(
                    disposition,
                    reason,
                    detail=classified_detail,
                    error_type=type(exc).__name__,
                    lifecycle_generation=generation,
                    lifecycle_state=ProductLifecycleState.IDLE.value,
                    classified_disposition=disposition,
                    classified_reason=reason,
                    cleanup=cleanup,
                )
            else:
                receipt = ProductStartupReceipt(
                    ProductStartupDisposition.TERMINAL,
                    ProductStartupReason.PRODUCT_STARTUP_CLEANUP_REQUIRED,
                    detail=classified_detail,
                    error_type=type(exc).__name__,
                    lifecycle_generation=generation,
                    lifecycle_state=ProductLifecycleState.DEGRADED.value,
                    classified_disposition=disposition,
                    classified_reason=reason,
                    cleanup=cleanup,
                )
        if receipt.ready:
            commit_error: Exception | None = None
            commit_succeeded = False
            with self._lifecycle:
                cancelled_before_commit = self._stop_requested
                if not cancelled_before_commit:
                    try:
                        publication_receipt = self._commit_ready_root(receipt)
                    except Exception as exc:
                        commit_error = exc
                        if self._admission.admits(receipt):
                            self._admission.revoke(receipt)
                    else:
                        self._core_root_publication_receipt = (
                            publication_receipt
                        )
                        self._lifecycle_state = (
                            ProductLifecycleState.RUNNING
                        )
                        self._startup_inflight = False
                        self._last_startup_generation = generation
                        self._last_startup_receipt = receipt
                        commit_succeeded = True
                        self._lifecycle.notify_all()
            if commit_succeeded:
                core_candidate = receipt.core_candidate
                if core_candidate is None:
                    raise RuntimeError(
                        "product_committed_core_candidate_missing"
                    )
                try:
                    self._post_startup_evidence = (
                        self._core.publish_post_product_startup_evidence(
                            candidate=core_candidate,
                            product_root=receipt,
                        )
                    )
                except Exception as exc:
                    self._post_startup_evidence = {
                        "schema": "CorePostProductStartupEvidenceReceipt.v1",
                        "owner": "54guc.product_supervisor",
                        "product_root_digest": receipt.root_digest,
                        "published": False,
                        "failures": [
                            "post_product_startup_evidence:"
                            f"{type(exc).__name__}: {exc}"
                        ],
                    }
                return receipt
            if commit_error is not None:
                cleanup = self._cleanup_failed_startup(generation)
                if cleanup.clean:
                    receipt = ProductStartupReceipt(
                        ProductStartupDisposition.TERMINAL,
                        ProductStartupReason.PRODUCT_STARTUP_FAULT,
                        detail=(
                            "Product READY install transaction failed: "
                            f"{type(commit_error).__name__}: "
                            f"{commit_error}"
                        ),
                        error_type=type(commit_error).__name__,
                        lifecycle_generation=generation,
                        lifecycle_state=(
                            ProductLifecycleState.STOPPED.value
                        ),
                        classified_disposition=(
                            ProductStartupDisposition.TERMINAL
                        ),
                        classified_reason=(
                            ProductStartupReason.PRODUCT_STARTUP_FAULT
                        ),
                        cleanup=cleanup,
                    )
                else:
                    receipt = ProductStartupReceipt(
                        ProductStartupDisposition.TERMINAL,
                        (
                            ProductStartupReason
                            .PRODUCT_STARTUP_CLEANUP_REQUIRED
                        ),
                        detail=(
                            "Product READY install transaction failed: "
                            f"{type(commit_error).__name__}: "
                            f"{commit_error}"
                        ),
                        error_type=type(commit_error).__name__,
                        lifecycle_generation=generation,
                        lifecycle_state=(
                            ProductLifecycleState.DEGRADED.value
                        ),
                        classified_disposition=(
                            ProductStartupDisposition.TERMINAL
                        ),
                        classified_reason=(
                            ProductStartupReason.PRODUCT_STARTUP_FAULT
                        ),
                        cleanup=cleanup,
                    )
            else:
                cleanup = self._cleanup_failed_startup(generation)
                receipt = self._cancelled_startup_receipt(
                    generation,
                    cleanup=cleanup,
                )
        with self._lifecycle:
            if self._stop_requested:
                self._lifecycle_state = ProductLifecycleState.STOP_REQUESTED
            elif receipt.lifecycle_state == ProductLifecycleState.IDLE.value:
                self._lifecycle_state = ProductLifecycleState.IDLE
            elif (
                receipt.lifecycle_state
                == ProductLifecycleState.STOPPED.value
            ):
                self._lifecycle_state = ProductLifecycleState.STOPPED
            else:
                self._lifecycle_state = ProductLifecycleState.DEGRADED
            self._startup_inflight = False
            self._last_startup_generation = generation
            self._last_startup_receipt = receipt
            self._lifecycle.notify_all()
        return receipt

    @staticmethod
    def _cancelled_startup_receipt(
        generation: int,
        *,
        detail: str = "",
        cleanup: ProductStartupCleanupReceipt | None = None,
    ) -> ProductStartupReceipt:
        return ProductStartupReceipt(
            ProductStartupDisposition.TERMINAL,
            ProductStartupReason.STARTUP_CANCELLED_BY_SHUTDOWN,
            detail=detail or "Shutdown admission closed during Product startup",
            error_type="RuntimeError",
            lifecycle_generation=generation,
            lifecycle_state=ProductLifecycleState.STOP_REQUESTED.value,
            cleanup=cleanup,
        )

    def _cleanup_failed_startup(
        self,
        generation: int,
    ) -> ProductStartupCleanupReceipt:
        """Prove zero survivors before any startup generation can retry."""

        errors: list[str] = []
        children: list[ProductChildTerminationReceipt] = []
        try:
            foundation_quiesce = self._foundation.quiesce(
                lifecycle_generation=generation
            )
            children.append(foundation_quiesce)
        except Exception as exc:
            error = f"foundation_quiesce:{type(exc).__name__}:{exc}"
            errors.append(error)
            children.append(
                self._failed_child_receipt(
                    generation,
                    "foundation_jobs",
                    error,
                )
            )
        try:
            core_receipt = self._core.shutdown(
                lifecycle_generation=generation,
            )
            children.append(core_receipt)
        except Exception as exc:
            error = f"core:{type(exc).__name__}:{exc}"
            errors.append(error)
            core_receipt = self._failed_child_receipt(
                generation,
                "core",
                error,
            )
            children.append(core_receipt)
        try:
            foundation_receipt = self._foundation.finalize(
                    core_receipt=core_receipt,
                    shutdown_errors=tuple(errors),
                )
            children.append(foundation_receipt)
        except Exception as exc:
            error = f"foundation_finalize:{type(exc).__name__}:{exc}"
            errors.append(error)
            children.append(
                self._failed_child_receipt(
                    generation,
                    "foundation",
                    error,
                )
            )
        if core_receipt.terminated:
            try:
                self._foundation.diagnostics.close()
                children.append(
                    ProductChildTerminationReceipt(
                        child_id="diagnostics",
                        lifecycle_generation=generation,
                        disposition=ProductTerminationDisposition.COMPLETE,
                    )
                )
            except Exception as exc:
                error = f"diagnostics:{type(exc).__name__}:{exc}"
                errors.append(error)
                children.append(
                    self._failed_child_receipt(
                        generation,
                        "diagnostics",
                        error,
                    )
                )
        else:
            errors.append("core_cleanup_not_stopped")
            children.append(
                ProductChildTerminationReceipt(
                    child_id="diagnostics",
                    lifecycle_generation=generation,
                    disposition=ProductTerminationDisposition.RETAINED,
                    survivors=("diagnostics",),
                    errors=("retained_for_core_survivor",),
                )
            )
        survivors = tuple(
            survivor for child in children for survivor in child.survivors
        )
        return ProductStartupCleanupReceipt(
            lifecycle_generation=generation,
            children=tuple(children),
            zero_survivor=ProductZeroSurvivorReceipt(
                lifecycle_generation=generation,
                survivors=survivors,
                complete=not survivors,
            ),
            errors=tuple(errors),
        )

    @staticmethod
    def _failed_child_receipt(
        generation: int,
        child_id: str,
        error: str,
    ) -> ProductChildTerminationReceipt:
        return ProductChildTerminationReceipt(
            child_id=child_id,
            lifecycle_generation=generation,
            disposition=ProductTerminationDisposition.DEGRADED,
            survivors=(child_id,),
            errors=(error,),
        )

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lifecycle:
            if (
                not self._lifecycle_state.admits_service
                or self._stop_requested
            ):
                raise RuntimeError(
                    "product_supervisor_service_admission_closed:"
                    + self._lifecycle_state.value
                )
            if self._service_inflight != 0:
                raise RuntimeError(
                    "product_supervisor_service_cycle_overlap_forbidden"
                )
            self._service_inflight += 1
        try:
            return self._foundation.service_once(now=now)
        finally:
            with self._lifecycle:
                self._service_inflight -= 1
                if self._service_inflight < 0:
                    raise RuntimeError(
                        "product_supervisor_service_inflight_underflow"
                    )
                self._lifecycle.notify_all()

    def request_shutdown(self) -> ProductStopRequestReceipt:
        """Close Product admission without running teardown on the caller."""

        with self._lifecycle:
            if self._lifecycle_generation == 0:
                self._lifecycle_generation = 1
            self._stop_requested = True
            root = self._last_startup_receipt
            if root is not None and self._admission.admits(root):
                self._admission.revoke(root)
            if self._lifecycle_state not in {
                ProductLifecycleState.STOPPED,
                ProductLifecycleState.QUIESCING,
                ProductLifecycleState.STOPPING,
            }:
                self._lifecycle_state = ProductLifecycleState.STOP_REQUESTED
            self._lifecycle.notify_all()
            return ProductStopRequestReceipt(
                lifecycle_generation=self._lifecycle_generation,
                lifecycle_state=self._lifecycle_state,
                startup_inflight=self._startup_inflight,
                service_inflight=self._service_inflight,
                requested_at=time(),
            )

    def lifecycle_status(self) -> ProductLifecycleStatusReceipt:
        with self._lifecycle:
            root = self._last_startup_receipt
            return ProductLifecycleStatusReceipt(
                lifecycle_generation=self._lifecycle_generation,
                lifecycle_state=self._lifecycle_state,
                stop_requested=self._stop_requested,
                startup_inflight=self._startup_inflight,
                service_inflight=self._service_inflight,
                shutdown_coordinator=self._shutdown_coordinator,
                accepted_root_digest=(
                    root.root_digest
                    if (
                        self._lifecycle_state is ProductLifecycleState.RUNNING
                        and root is not None
                        and self._admission.admits(root)
                    )
                    else ""
                ),
            )

    def intelligence_attachment_status(self) -> IntelligenceStartupEvidence:
        status = self.intelligence_matrix().status().to_mapping()
        attached = bool(status["attached"])
        last_fault = str(status["last_error"] or "")
        state = "degraded" if attached and last_fault else (
            "attached" if attached else "unattached"
        )
        return IntelligenceStartupEvidence(
            boundary_id=INTELLIGENCE_MATRIX_OWNER,
            contract_version=INTELLIGENCE_MATRIX_CONTRACT_VERSION,
            state=state,
            customers=(INTELLIGENCE_CLIENT_ID,) if attached else (),
            attached=attached,
            last_fault=last_fault if state == "degraded" else "",
        )

    def shutdown(
        self,
        *,
        admission_timeout_s: float = 10.0,
    ) -> ProductShutdownReceipt:
        timeout_s = max(0.0, float(admission_timeout_s))
        deadline = monotonic() + timeout_s
        with self._lifecycle:
            if self._lifecycle_generation == 0:
                self._lifecycle_generation = 1
            if self._lifecycle_state is ProductLifecycleState.STOPPED:
                if self._last_shutdown_receipt is not None:
                    return self._last_shutdown_receipt
                cleanup = (
                    self._last_startup_receipt.cleanup
                    if self._last_startup_receipt is not None
                    else None
                )
                return ProductShutdownReceipt(
                    disposition=ProductShutdownDisposition.ALREADY_STOPPED,
                    lifecycle_generation=self._lifecycle_generation,
                    lifecycle_state=ProductLifecycleState.STOPPED,
                    children=cleanup.children if cleanup is not None else (),
                    zero_survivor=(
                        cleanup.zero_survivor
                        if cleanup is not None
                        else ProductZeroSurvivorReceipt(
                            lifecycle_generation=self._lifecycle_generation,
                            survivors=(),
                            complete=True,
                        )
                    ),
                    errors=(),
                    finished_at=time(),
                )
            if self._shutdown_coordinator:
                while self._shutdown_coordinator:
                    remaining = deadline - monotonic()
                    if remaining <= 0.0:
                        return self._shutdown_wait_timeout_receipt(
                            "shutdown_coordinator_wait_timeout"
                        )
                    self._lifecycle.wait(timeout=remaining)
                if self._last_shutdown_receipt is None:
                    raise RuntimeError(
                        "product_shutdown_coordinator_receipt_missing"
                    )
                return self._last_shutdown_receipt
            self._shutdown_coordinator = True
            self._stop_requested = True
            root = self._last_startup_receipt
            if root is not None and self._admission.admits(root):
                self._admission.revoke(root)
            self._lifecycle_state = ProductLifecycleState.STOP_REQUESTED
            while self._startup_inflight or self._service_inflight:
                remaining = deadline - monotonic()
                if remaining <= 0.0:
                    self._shutdown_coordinator = False
                    self._lifecycle_state = ProductLifecycleState.DEGRADED
                    receipt = self._shutdown_wait_timeout_receipt(
                        "product_admitted_work_drain_timeout"
                    )
                    self._last_shutdown_receipt = receipt
                    self._lifecycle.notify_all()
                    return receipt
                self._lifecycle.wait(timeout=remaining)
            self._lifecycle_state = ProductLifecycleState.QUIESCING
            generation = self._lifecycle_generation
        errors: list[str] = []
        children: list[ProductChildTerminationReceipt] = []
        headless_port = self._headless_port
        if headless_port is not None:
            try:
                headless_receipt = headless_port.terminate(timeout_s=5.0)
                children.append(headless_receipt)
                errors.extend(headless_receipt.errors)
            except Exception as exc:
                error = f"headless_runtime:{type(exc).__name__}:{exc}"
                errors.append(error)
                children.append(
                    self._failed_child_receipt(
                        generation,
                        "headless_runtime",
                        error,
                    )
                )
        ngui_port = self._ngui_port
        if ngui_port is not None:
            ngui_receipt = ngui_port.termination_receipt()
            children.append(ngui_receipt)
            errors.extend(ngui_receipt.errors)
        active_runtime_survivors = tuple(
            survivor for child in children for survivor in child.survivors
        )
        if active_runtime_survivors:
            receipt = ProductShutdownReceipt(
                disposition=ProductShutdownDisposition.DEGRADED,
                lifecycle_generation=generation,
                lifecycle_state=ProductLifecycleState.DEGRADED,
                children=tuple(children),
                zero_survivor=ProductZeroSurvivorReceipt(
                    lifecycle_generation=generation,
                    survivors=active_runtime_survivors,
                    complete=False,
                ),
                errors=tuple(errors),
                finished_at=time(),
            )
            with self._lifecycle:
                self._lifecycle_state = ProductLifecycleState.DEGRADED
                self._shutdown_coordinator = False
                self._last_shutdown_receipt = receipt
                self._lifecycle.notify_all()
            return receipt
        try:
            foundation_quiesce = self._foundation.quiesce(
                lifecycle_generation=generation
            )
            children.append(foundation_quiesce)
        except Exception as exc:
            error = f"foundation_quiesce:{type(exc).__name__}:{exc}"
            errors.append(error)
            children.append(
                self._failed_child_receipt(
                    generation,
                    "foundation_jobs",
                    error,
                )
            )
            try:
                self._foundation.diagnostics.log_event(
                    "foundation_quiesce_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except Exception as diagnostic_exc:
                errors.append(
                    "diagnostics_foundation_quiesce:"
                    f"{type(diagnostic_exc).__name__}:{diagnostic_exc}"
                )
        with self._lifecycle:
            self._lifecycle_state = ProductLifecycleState.STOPPING
        try:
            core_receipt = self._core.shutdown(
                lifecycle_generation=generation,
            )
            children.append(core_receipt)
        except Exception as exc:
            error = f"core:{type(exc).__name__}:{exc}"
            errors.append(error)
            core_receipt = self._failed_child_receipt(
                generation,
                "core",
                error,
            )
            children.append(core_receipt)
            try:
                self._foundation.diagnostics.log_event(
                    "core_shutdown_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except Exception as diagnostic_exc:
                errors.append(
                    "diagnostics_core_shutdown:"
                    f"{type(diagnostic_exc).__name__}:{diagnostic_exc}"
                )
        try:
            foundation_receipt = self._foundation.finalize(
                    core_receipt=core_receipt,
                    shutdown_errors=tuple(errors),
                )
            children.append(foundation_receipt)
        except Exception as exc:
            error = f"foundation_finalize:{type(exc).__name__}:{exc}"
            errors.append(error)
            children.append(
                self._failed_child_receipt(
                    generation,
                    "foundation",
                    error,
                )
            )
            try:
                self._foundation.diagnostics.log_event(
                    "foundation_finalize_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except Exception as diagnostic_exc:
                errors.append(
                    "diagnostics_foundation_finalize:"
                    f"{type(diagnostic_exc).__name__}:{diagnostic_exc}"
                )
        if not core_receipt.terminated:
            errors.append("diagnostics:retained_for_active_core_shutdown_survivor")
            children.append(
                ProductChildTerminationReceipt(
                    child_id="diagnostics",
                    lifecycle_generation=generation,
                    disposition=ProductTerminationDisposition.RETAINED,
                    survivors=("diagnostics",),
                    errors=("retained_for_active_core_shutdown_survivor",),
                )
            )
        else:
            try:
                self._foundation.diagnostics.close()
                children.append(
                    ProductChildTerminationReceipt(
                        child_id="diagnostics",
                        lifecycle_generation=generation,
                        disposition=ProductTerminationDisposition.COMPLETE,
                    )
                )
            except Exception as exc:
                error = f"diagnostics:{type(exc).__name__}:{exc}"
                errors.append(error)
                children.append(
                    self._failed_child_receipt(
                        generation,
                        "diagnostics",
                        error,
                    )
                )
        survivors = tuple(
            survivor for child in children for survivor in child.survivors
        )
        stopped = bool(
            not errors
            and not survivors
            and all(child.terminated for child in children)
        )
        receipt = ProductShutdownReceipt(
            disposition=(
                ProductShutdownDisposition.STOPPED
                if stopped
                else ProductShutdownDisposition.DEGRADED
            ),
            lifecycle_generation=generation,
            lifecycle_state=(
                ProductLifecycleState.STOPPED
                if stopped
                else ProductLifecycleState.DEGRADED
            ),
            children=tuple(children),
            zero_survivor=ProductZeroSurvivorReceipt(
                lifecycle_generation=generation,
                survivors=survivors,
                complete=not survivors,
            ),
            errors=tuple(errors),
            finished_at=time(),
        )
        with self._lifecycle:
            self._lifecycle_state = (
                ProductLifecycleState.STOPPED
                if stopped
                else ProductLifecycleState.DEGRADED
            )
            self._shutdown_coordinator = False
            self._last_shutdown_receipt = receipt
            self._lifecycle.notify_all()
        return receipt

    def _shutdown_wait_timeout_receipt(
        self,
        reason: str,
    ) -> ProductShutdownReceipt:
        survivors = []
        if self._startup_inflight:
            survivors.append("product_startup_transaction")
        survivors.extend(
            f"product_service_cycle:{index + 1}"
            for index in range(self._service_inflight)
        )
        child = ProductChildTerminationReceipt(
            child_id="product_admitted_work",
            lifecycle_generation=self._lifecycle_generation,
            disposition=ProductTerminationDisposition.RETAINED,
            survivors=tuple(survivors or ("product_shutdown_coordinator",)),
            errors=(
                str(reason),
                "teardown_not_started_while_admitted_work_survives",
            ),
        )
        return ProductShutdownReceipt(
            disposition=ProductShutdownDisposition.DEGRADED,
            lifecycle_generation=self._lifecycle_generation,
            lifecycle_state=ProductLifecycleState.DEGRADED,
            children=(child,),
            zero_survivor=ProductZeroSurvivorReceipt(
                lifecycle_generation=self._lifecycle_generation,
                survivors=child.survivors,
                complete=False,
            ),
            errors=child.errors,
            finished_at=time(),
        )


__all__ = (
    "PRODUCT_STARTUP_RECEIPT_SCHEMA",
    "PRODUCT_SUPERVISOR_OWNER",
    "PRODUCT_HEADLESS_LIFECYCLE_INTERNAL_ROOT_EXCEPTIONS",
    "PRODUCT_NGUI_LIFECYCLE_INTERNAL_ROOT_EXCEPTIONS",
    "ProductStartupDisposition",
    "ProductStartupReceipt",
    "ProductSupervisor",
)
