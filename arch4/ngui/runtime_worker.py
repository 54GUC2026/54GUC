"""Own the CGF product inside the NGUI runtime worker process."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full
import logging
from threading import Condition, Event as ThreadEvent, RLock, Thread, current_thread
from time import monotonic, time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    ModemLiveReadyReceipt,
    RuntimeGenerationNamespace,
)
from arch4.architecture_contracts.application_lifecycle import (
    ProductRuntimeWorkerReplacementReason,
    ProductRuntimeWorkerReplacementRequired,
    ProductShutdownReceipt,
    ProductStartupReceipt,
)
from arch4.architecture_contracts.runtime_termination import (
    RequiredRuntimeWorkerTerminationReceipt,
    RuntimeFrameBoundaryTerminationReceipt,
    RuntimeFramePublisherTerminationReceipt,
    RuntimeParticipantAbsenceReceipt,
    RuntimeParticipantStartReceipt,
    RuntimeTerminationBlocker,
    RuntimeTerminationParticipant,
)
from arch4.core.command_outcome_stream import CORE_COMMAND_OUTCOME_HOOK_ID
from arch4.core.accepted_root_outbox import ACCEPTED_ROOT_HOOK_ID
from arch4.command_transactions import (
    COMMAND_TERMINAL_RECEIPT_SCHEMA,
    CommandTerminalReceipt,
)
from arch4.core_contracts.clients import CoreClientManifest, CoreClientRole
from arch4.core_contracts.commands import CoreCommandRequest
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.fault_supervisor import FaultBoundaryClass, FaultSeverity
from arch4.foundation.service_admission import FoundationServiceAdmission
from arch4.interfaces import Arch4Config
from arch4.operational_service_contracts import (
    AntennaGuideControlRequest,
    BandScheduleCancelRequest,
    BandSchedulePlanRequest,
    BandSequenceCancelRequest,
    BandSequencePlanRequest,
    DownstreamHealthConfigRequest,
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    SmsCommandControlConfigRequest,
    WifiRadioControlRequest,
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.accepted_root_outbox import (
    CoreAcceptedRootOutboxRecord,
    CoreAcceptedRootResyncRequired,
)
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
)
from arch4.runtime_clock_worker_fabric import ClockJobWorkerFabric
from arch4.worker_health import WorkerExecutionKind
from arch4.product_supervisor import (
    ProductNGUIRuntimePort,
    ProductStartupDisposition,
    ProductSupervisor,
)
from arch4.product_intelligence_runtime import (
    build_intelligence_runtime,
    resolve_intelligence_history_path,
)
from arch4.runtime_paths import resolve_cgf_product_storage

from .runtime_ipc import (
    RuntimeSupportHistoryFlushRequest,
    RuntimeEventKind,
    RuntimeFrameDeliveryReceipt,
    RuntimeIPCEvent,
    RuntimeIPCRequest,
    RuntimeManualResultReceipt,
    RuntimeManualTimingReceipt,
    RuntimeNamespaceGenerationIdentity,
    RuntimeProductGenerationIdentity,
    RuntimeProductRootAdmission,
    RuntimeRequestKind,
    RuntimeStartupRouterSerial,
    RuntimeWorkerGenerationIdentity,
    RuntimeWorkerReplacementRequiredReceipt,
    RuntimeWorkerStatusReceipt,
    RuntimeWorkerStatusState,
    validate_runtime_frame_delivery_receipt,
)
from .runtime_lifecycle_transport import (
    RuntimeLifecycleDelivery,
    RuntimeLifecycleDeliveryDisposition,
    RuntimeLifecyclePublisher,
    RuntimeLifecycleReservation,
)
from .runtime_frame_transport import (
    LatestRuntimeFramePublisher,
    RuntimeFramePublisherStopping,
)
from .runtime_projection import (
    NGUICompleteFrameProjector,
)


log = logging.getLogger("arch4.ngui.runtime_worker")

TYPED_REQUEST_DRAIN_MAX_ITEMS = 8
TYPED_REQUEST_DRAIN_BUDGET_S = 0.025
RUNTIME_FRAME_CONTEXT_INTERVAL_S = 1.0
RUNTIME_CLOCK_STARTUP_READY_TIMEOUT_S = 0.25
STARTUP_MATRIX_READY_STATUS_INTERVAL_S = 1.0
NGUI_ACCEPTED_ROOT_CLIENT_ID = "ngui.accepted_root_projection"
NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID = "ngui.command_outcome"


def _stop_intelligence_runtime(runtime: Any, *, timeout_s: float) -> None:
    """Stop the optional Intelligence process without leaving a survivor."""

    if not runtime.stop(timeout_s=timeout_s):
        raise RuntimeError("new_horizon_runtime_termination_unproved")


def _start_parent_lifetime_monitor(
    reader: Any,
    stop_event: Any,
    wake_event: Any,
    fault_event: ThreadEvent,
) -> Thread:
    """Stop this worker when its NGUI parent lifetime ends."""

    if reader is None or not callable(getattr(reader, "recv_bytes", None)):
        raise TypeError("runtime_parent_lifetime_reader_required")
    if getattr(reader, "writable", None) is not False:
        raise TypeError("runtime_parent_lifetime_receive_only_required")
    if getattr(reader, "readable", None) is not True:
        raise TypeError("runtime_parent_lifetime_readable_channel_required")
    if not isinstance(fault_event, ThreadEvent):
        raise TypeError("runtime_parent_lifetime_fault_event_required")

    def monitor() -> None:
        try:
            reader.recv_bytes()
        except EOFError:
            if not stop_event.is_set():
                fault_event.set()
                log.error(
                    "NGUI parent lifetime ended; stopping the runtime worker"
                )
        except (OSError, ValueError) as exc:
            if not stop_event.is_set():
                fault_event.set()
                log.error(
                    "NGUI parent lifetime channel failed; stopping the "
                    "runtime worker: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        else:
            fault_event.set()
            log.critical(
                "NGUI parent lifetime channel carried forbidden payload; "
                "stopping the runtime worker"
            )
        finally:
            stop_event.set()
            wake_event.set()

    thread = Thread(
        target=monitor,
        name="ngui-parent-lifetime",
        daemon=True,
    )
    thread.start()
    return thread


def _record_runtime_fault_fail_soft(
    runtime: ProductNGUIRuntimePort,
    *,
    boundary: FaultBoundaryClass,
    owner: str,
    operation: str,
    generation: int,
    severity: FaultSeverity,
    retryable: bool,
    degraded_behavior: str,
    error: BaseException,
    suppression_budget: int,
) -> bool:
    """Record secondary fault evidence without changing primary control flow."""

    try:
        runtime.fault_supervisor_authority().record(
            boundary=boundary,
            owner=owner,
            operation=operation,
            generation=generation,
            severity=severity,
            retryable=retryable,
            degraded_behavior=degraded_behavior,
            error=error,
            suppression_budget=suppression_budget,
        )
    except Exception as report_error:
        log.error(
            "Fault evidence publication failed for %s/%s: %s: %s",
            owner,
            operation,
            type(report_error).__name__,
            report_error,
        )
        return False
    return True


def _publish_foundation_runtime_health(
    runtime: ProductNGUIRuntimePort,
    *,
    phase: str,
    cycle_count: int,
    error: BaseException | str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    try:
        return runtime.publish_runtime_health(
            phase=phase,
            cycle_count=max(0, int(cycle_count)),
            error=error,
            detail=dict(detail or {}),
        )
    except Exception as exc:
        log.error("Foundation runtime-health boundary unavailable: %s", exc)
        _record_runtime_fault_fail_soft(
            runtime,
            boundary=FaultBoundaryClass.STORAGE,
            owner="ngui_runtime_worker",
            operation="publish_foundation_runtime_health",
            generation=max(0, int(cycle_count)),
            severity=FaultSeverity.ERROR,
            retryable=True,
            degraded_behavior="retain_live_ngui_and_retry_runtime_health",
            error=exc,
            suppression_budget=3,
        )
        return False


def _await_runtime_clock_startup_ready(
    runtime: ProductNGUIRuntimePort,
    status_queue: Any,
    stop_event: Any,
    product_identity: RuntimeProductGenerationIdentity,
) -> Mapping[str, Any] | None:
    workers = runtime.clock_job_workers()
    if not isinstance(workers, ClockJobWorkerFabric):
        raise RuntimeError("runtime_clock_startup_worker_fabric_unavailable")
    while not stop_event.is_set():
        receipt = workers.wait_until_all_declared_ready(
            timeout_s=RUNTIME_CLOCK_STARTUP_READY_TIMEOUT_S
        )
        payload = receipt.to_payload()
        if receipt.ready:
            return payload
        pending = ",".join(receipt.pending_jobs) or "<none>"
        errors = ",".join(
            f"{job_key}={error}" for job_key, error in receipt.error_jobs
        ) or "<none>"
        message = (
            "runtime_clock_startup_not_ready:"
            f"pending={pending};errors={errors};"
            f"waited_s={receipt.waited_s:.3f}"
        )
        _publish_foundation_runtime_health(
            runtime,
            phase="reconnecting",
            cycle_count=0,
            detail={"runtime_clock_startup": payload},
        )
        _publish_status(
            status_queue,
            product_identity,
            "reconnecting",
            message,
            0,
        )
    return None


@dataclass(frozen=True, slots=True)
class TypedRequestDrainReport:
    consumed: int
    completed: int
    dead_lettered: int
    item_budget_exhausted: bool
    time_budget_exhausted: bool
    elapsed_ms: float

    @property
    def handled(self) -> bool:
        return self.consumed > 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "TypedRequestDrainReport.v1",
            "owner": "ngui_typed_request_worker",
            "consumed": int(self.consumed),
            "completed": int(self.completed),
            "dead_lettered": int(self.dead_lettered),
            "item_budget_exhausted": bool(self.item_budget_exhausted),
            "time_budget_exhausted": bool(self.time_budget_exhausted),
            "elapsed_ms": float(self.elapsed_ms),
        }


@dataclass(slots=True)
class _PendingOperationalDelivery:
    request: RuntimeIPCRequest
    receipt: OperationalServiceReceipt
    reservation: RuntimeLifecycleReservation | None
    failed_effect: bool
    owner_published: bool = False
    presentation_acknowledged: bool = False
    delivery: RuntimeLifecycleDelivery | None = None


class _OperationalRequestDeliveryOwner:
    """Keep an executed request until the GUI accepts its result.

    Dequeue and execution happen once. Owner projection and lifecycle delivery
    may retry, but the physical effect must never run twice.
    """

    def __init__(
        self,
        runtime: ProductNGUIRuntimePort,
        lifecycle_publisher: RuntimeLifecyclePublisher,
    ) -> None:
        self._runtime = runtime
        self._publisher = lifecycle_publisher
        self._active: _PendingOperationalDelivery | None = None
        self._support_history: RuntimeSupportHistoryFlushRequest | None = None
        self._closed = False

    @property
    def active(self) -> bool:
        return self._active is not None

    def try_reserve(self) -> RuntimeLifecycleReservation | None:
        if self._closed or self._active is not None:
            return None
        return self._publisher.try_reserve()

    def cancel(self, reservation: RuntimeLifecycleReservation) -> None:
        self._publisher.cancel(reservation)

    def admit_support_history(self, request: RuntimeSupportHistoryFlushRequest) -> None:
        if self._closed or self._support_history is not None:
            try:
                request.reply.send({"identity": request.generation_identity, "complete": False,
                                    "error": "support_history_owner_closed" if self._closed else "support_history_busy"})
            except (OSError, EOFError) as exc:
                log.info("Support History reply delivery failed (%s): %s", type(exc).__name__, exc)
            finally:
                request.reply.close()
            return
        self._support_history = request

    def progress_support_history(self, prepare: Callable[[RuntimeSupportHistoryFlushRequest], bool]) -> int:
        request = self._support_history
        if request is not None and prepare(request):
            self._support_history = None
            return 1
        return 0

    def admit(
        self,
        *,
        request: RuntimeIPCRequest,
        receipt: OperationalServiceReceipt,
        reservation: RuntimeLifecycleReservation,
        failed_effect: bool,
    ) -> None:
        if self._closed:
            raise RuntimeError("operational_delivery_admission_closed")
        if self._active is not None:
            raise RuntimeError("operational_delivery_overlap")
        if (
            receipt.runtime_namespace
            != request.generation_identity.namespace
            or receipt.matrix_identity != request.matrix_identity
        ):
            raise ValueError("operational_delivery_identity_mismatch")
        self._active = _PendingOperationalDelivery(
            request=request,
            receipt=receipt,
            reservation=reservation,
            failed_effect=bool(failed_effect),
        )

    def progress(self) -> tuple[int, int, bool]:
        """Advance retained delivery without blocking the Product service loop."""

        active = self._active
        if active is None:
            return 0, 0, False
        try:
            if not active.owner_published:
                _publish_owned_operational_receipt(
                    self._runtime,
                    active.receipt,
                    active.request.matrix_identity,
                )
                active.owner_published = True
            if active.delivery is None:
                if active.reservation is None:
                    active.reservation = self._publisher.try_reserve()
                    if active.reservation is None:
                        return 0, 0, True
                try:
                    active.delivery = self._publisher.commit(
                        active.reservation,
                        RuntimeIPCEvent(
                            kind=(
                                RuntimeEventKind
                                .OPERATIONAL_PRESENTATION_ACKNOWLEDGED
                                if active.presentation_acknowledged
                                else RuntimeEventKind.OPERATIONAL_RECEIPT
                            ),
                            generation_identity=(
                                active.request.generation_identity
                            ),
                            payload=active.receipt,
                        ),
                    )
                except Exception:
                    active.reservation = None
                    raise
                active.reservation = None
            observation = active.delivery.observation(timeout_s=0.0)
        except Exception as exc:
            self._record_delivery_fault(active, exc, retryable=True)
            return 0, 0, True
        if observation.disposition is (
            RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
        ):
            return 0, 0, True
        if observation.disposition is RuntimeLifecycleDeliveryDisposition.ACCEPTED:
            if not active.presentation_acknowledged:
                try:
                    self._runtime.acknowledge_operational_receipt(
                        active.receipt,
                        active.request.matrix_identity,
                    )
                except Exception as exc:
                    self._record_delivery_fault(
                        active,
                        exc,
                        retryable=True,
                    )
                    return 0, 0, True
                active.presentation_acknowledged = True
                active.delivery = None
                return 0, 0, True
            self._active = None
            return (
                0 if active.failed_effect else 1,
                1 if active.failed_effect else 0,
                False,
            )
        self._active = None
        error = RuntimeError(
            "operational_owner_transition_"
            + observation.disposition.value
            + (":" + observation.reason if observation.reason else "")
        )
        self._record_delivery_fault(active, error, retryable=False)
        return 0, 1, False

    def close(self, *, timeout_s: float) -> None:
        self._closed = True
        request, self._support_history = self._support_history, None
        if request is not None:
            # Cancel diagnostics before stopping the sole History owner.
            self.admit_support_history(request)
        deadline = monotonic() + max(0.0, float(timeout_s))
        while self._active is not None and monotonic() < deadline:
            _completed, _dead_lettered, pending = self.progress()
            if not pending:
                break
            ThreadEvent().wait(min(0.05, max(0.0, deadline - monotonic())))
        if self._active is not None:
            active = self._active
            if active.reservation is not None:
                try:
                    self._publisher.cancel(active.reservation)
                except RuntimeError:
                    pass
                active.reservation = None
            error = RuntimeError(
                "operational_delivery_shutdown_unresolved:"
                + active.receipt.request_id
                + ":"
                + active.receipt.matrix_identity.matrix_revision
            )
            self._record_delivery_fault(
                active,
                error,
                retryable=False,
            )
            self._active = None
            raise error

    def _record_delivery_fault(
        self,
        active: _PendingOperationalDelivery,
        error: Exception,
        *,
        retryable: bool,
    ) -> None:
        _record_runtime_fault_fail_soft(
            self._runtime,
            boundary=FaultBoundaryClass.WORKER,
            owner="ngui_operational_delivery",
            operation=active.request.kind.value,
            generation=max(0, int(active.request.sequence)),
            severity=FaultSeverity.ERROR,
            retryable=bool(retryable),
            degraded_behavior=(
                "retain_exact_operational_receipt_and_retry_delivery"
                if retryable
                else "operational_effect_not_reexecuted_delivery_failed_loudly"
            ),
            error=error,
            suppression_budget=3,
        )


def _attach_ngui_accepted_root_client(
    runtime: ProductNGUIRuntimePort,
) -> None:
    manifest = CoreClientManifest(
        client_id=NGUI_ACCEPTED_ROOT_CLIENT_ID,
        role=CoreClientRole.OBSERVER,
        contract_versions=(
            "CoreAcceptedGenerationRoot.v1",
            CORE_RUNTIME_SNAPSHOT_SCHEMA,
        ),
        queue_budget=8,
        required=True,
    )
    runtime.attach_core_client(
        manifest,
        hook_id=ACCEPTED_ROOT_HOOK_ID,
        mode=CoreHookAttachmentMode.SUBSCRIBER,
    )
    runtime.attach_core_client(
        manifest,
        hook_id=CORE_RUNTIME_SNAPSHOT_HOOK_ID,
        mode=CoreHookAttachmentMode.SUBSCRIBER,
    )


def _detach_ngui_accepted_root_client(
    runtime: ProductNGUIRuntimePort,
) -> tuple[str, ...]:
    errors: tuple[str, ...] = ()
    for hook_id in (
        ACCEPTED_ROOT_HOOK_ID,
        CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    ):
        try:
            runtime.detach_core_client(
                NGUI_ACCEPTED_ROOT_CLIENT_ID,
                hook_id=hook_id,
            )
        except Exception as exc:
            errors = (
                *errors,
                f"core_hook_detach:{hook_id}:"
                f"{type(exc).__name__}:{exc}",
            )
    return errors


def _attach_ngui_core_command_outcome_client(
    runtime: ProductNGUIRuntimePort,
) -> None:
    runtime.attach_core_client(
        CoreClientManifest(
            client_id=NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
            role=CoreClientRole.OBSERVER,
            contract_versions=(COMMAND_TERMINAL_RECEIPT_SCHEMA,),
            queue_budget=16,
            required=True,
        ),
        hook_id=CORE_COMMAND_OUTCOME_HOOK_ID,
        mode=CoreHookAttachmentMode.SUBSCRIBER,
    )


class _RuntimeFrameBoundary:
    """Project register values into a bounded stream of complete NGUI frames."""

    def __init__(
        self,
        runtime: ProductNGUIRuntimePort,
        frame_queue: Any,
        frame_acknowledgement_queue: Any,
        stop_event: Any,
        on_worker_replacement_required: Callable[
            [ProductRuntimeWorkerReplacementRequired],
            None,
        ],
        intelligence_presentation_supplier: Callable[
            [RuntimeGenerationNamespace],
            Any,
        ],
    ) -> None:
        self._runtime = runtime
        self._accepted_root_port = (
            runtime.accepted_root_customer_port()
        )
        self._stop_event = stop_event
        self._on_worker_replacement_required = (
            on_worker_replacement_required
        )
        if not callable(intelligence_presentation_supplier):
            raise TypeError("runtime intelligence presentation supplier required")
        self._intelligence_presentation_supplier = (
            intelligence_presentation_supplier
        )
        self._runtime_namespace: RuntimeGenerationNamespace | None = None
        self._modem_live_ready: ModemLiveReadyReceipt | None = None
        self._projection: NGUICompleteFrameProjector | None = None
        self._worker_health = runtime.worker_health_authority()
        self._fault_supervisor = runtime.fault_supervisor_authority()
        self._publisher = LatestRuntimeFramePublisher(
            frame_queue,
            frame_acknowledgement_queue,
            stop_event,
            worker_health=self._worker_health,
            fault_supervisor=self._fault_supervisor,
            on_receipt=self._on_frame_delivery_receipt,
        )
        self._changed = Condition()
        self._local_stop = ThreadEvent()
        self._wake_pending = True
        self._owner_wake_listener_labels: tuple[str, ...] = ()
        self._inflight_projection: (
            tuple[CoreAcceptedRootOutboxRecord | None, Any, tuple[Any, ...]]
            | None
        ) = None
        self._last_terminal_source_vector: tuple[Any, ...] | None = None
        self._projection_thread: Thread | None = None
        self._projection_worker_id = "ngui_runtime_frame_projection"
        self._projected_count = 0
        self._worker_health.register(
            self._projection_worker_id,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=True,
            retryable=True,
        )

    def start(self) -> None:
        with self._changed:
            if self._projection is None or self._runtime_namespace is None:
                raise RuntimeError("runtime frame boundary namespace is unbound")
            if (
                self._projection_thread is not None
                and self._projection_thread.is_alive()
            ):
                raise RuntimeError("runtime frame boundary is already running")
            self._local_stop.clear()
        publisher_started = False
        try:
            self._bind_owner_wake_listeners()
            self._publisher.start()
            publisher_started = True
            self._worker_health.mark_started(self._projection_worker_id)
            projection_thread = Thread(
                target=self._projection_loop,
                name="ngui-runtime-frame-projection",
                daemon=True,
            )
            with self._changed:
                self._projection_thread = projection_thread
            projection_thread.start()
        except Exception:
            with self._changed:
                self._local_stop.set()
                self._projection_thread = None
                self._changed.notify_all()
            if publisher_started:
                self._publisher.stop(timeout_s=2.0)
            self._detach_owner_wake_listeners()
            self._worker_health.mark_stopped(
                self._projection_worker_id,
                reason="frame_boundary_partial_start_rolled_back",
            )
            raise

    def wake(self) -> None:
        """Coalesce a payload-free notification from an owner."""

        with self._changed:
            if self._local_stop.is_set():
                return
            self._wake_pending = True
            self._changed.notify_all()

    def _bind_owner_wake_listeners(self) -> None:
        if self._owner_wake_listener_labels:
            raise RuntimeError("runtime frame owner wake listeners already bound")
        operations = (
            ("sample", self._runtime.bind_sample_wake_listener),
            ("runtime_clock", self._runtime.bind_runtime_clock_wake_listener),
            ("command_status", self._runtime.bind_command_status_wake_listener),
        )
        try:
            for label, operation in operations:
                operation(self.wake)
                self._owner_wake_listener_labels = (
                    *self._owner_wake_listener_labels,
                    label,
                )
        except Exception:
            self._detach_owner_wake_listeners()
            raise

    def _detach_owner_wake_listeners(self) -> tuple[str, ...]:
        if not self._owner_wake_listener_labels:
            return ()
        errors: tuple[str, ...] = ()
        failed: tuple[str, ...] = ()
        for label, operation in (
            ("sample", self._runtime.bind_sample_wake_listener),
            ("runtime_clock", self._runtime.bind_runtime_clock_wake_listener),
            ("command_status", self._runtime.bind_command_status_wake_listener),
        ):
            if label not in self._owner_wake_listener_labels:
                continue
            try:
                operation(None)
            except Exception as exc:
                errors = (*errors, f"{label}:{type(exc).__name__}:{exc}")
                failed = (*failed, label)
        self._owner_wake_listener_labels = failed
        return errors

    def bind_runtime_namespace(
        self,
        modem_live_ready: ModemLiveReadyReceipt,
    ) -> None:
        """Bind Modem Live READY proof before admitting the worker."""

        if not isinstance(
            modem_live_ready,
            ModemLiveReadyReceipt,
        ):
            raise TypeError("runtime frame boundary Modem Live ready required")
        namespace = modem_live_ready.matrix_identity.namespace
        with self._changed:
            if self._projection_thread is not None:
                raise RuntimeError(
                    "runtime frame boundary namespace bind after start"
                )
            if self._runtime_namespace is not None:
                raise RuntimeError(
                    "runtime frame boundary namespace already bound"
                )
            self._runtime_namespace = namespace
            self._modem_live_ready = modem_live_ready
            self._projection = NGUICompleteFrameProjector(
                modem_live_ready
            )

    def _record_worker_failure(
        self,
        worker_id: str,
        operation: str,
        exc: Exception,
    ) -> float:
        current = self._worker_health.record(worker_id)
        retry_s = min(
            5.0,
            0.1 * (2 ** min(current.failure_streak + 1, 6)),
        )
        record = self._worker_health.mark_failure(
            worker_id,
            exc,
            next_retry_at=time() + retry_s,
        )
        self._fault_supervisor.record(
            boundary=FaultBoundaryClass.GUI,
            owner=worker_id,
            operation=str(operation),
            generation=int(record.success_count + record.failure_count),
            severity=FaultSeverity.ERROR,
            retryable=True,
            degraded_behavior=(
                "retain_last_complete_frame_and_retry"
            ),
            error=exc,
            suppression_budget=3,
        )
        return retry_s

    def _project_pending(self) -> None:
        root_port = self._accepted_root_port
        try:
            record = root_port.claim_next(NGUI_ACCEPTED_ROOT_CLIENT_ID)
        except CoreAcceptedRootResyncRequired as exc:
            loss = exc.state.loss_range
            assert loss is not None
            root_port.acknowledge_resync(
                NGUI_ACCEPTED_ROOT_CLIENT_ID,
                through_source_sequence=loss.last_source_sequence,
            )
            log.warning(
                "NGUI accepted-root gap %d-%d quarantined; resuming latest truth",
                loss.first_source_sequence,
                loss.last_source_sequence,
            )
            return
        root = record.root if record is not None else root_port.current()
        if root is None:
            return
        projection = self._projection
        if projection is None:
            raise RuntimeError("runtime frame boundary namespace is unbound")
        try:
            extension = self._runtime.foundation_extension_latest(
                self._required_runtime_namespace()
            )
            intelligence_extension = self._intelligence_presentation_supplier(
                self._required_runtime_namespace()
            )
            extensions = tuple(
                item
                for item in (extension, intelligence_extension)
                if item is not None
            )
            source_vector = (
                root.root_fingerprint,
                self._runtime.runtime_register_sequence(
                    self._required_runtime_namespace()
                ),
                tuple(
                    (item.provider_id, item.sequence, item.values.sha256)
                    for item in extensions
                ),
            )
            if source_vector == self._last_terminal_source_vector:
                if record is not None:
                    root_port.acknowledge(
                        NGUI_ACCEPTED_ROOT_CLIENT_ID,
                        key=record.key,
                    )
                return
            snapshot = self._runtime.runtime_snapshot_for(
                NGUI_ACCEPTED_ROOT_CLIENT_ID,
                accepted_root=root,
                health={},
                metadata={},
                published_at=time(),
            )
            frame = projection.project(snapshot, extensions)
            if frame is None:
                self._last_terminal_source_vector = source_vector
                if record is not None:
                    root_port.acknowledge(
                        NGUI_ACCEPTED_ROOT_CLIENT_ID,
                        key=record.key,
                    )
                return
        except Exception as exc:
            if (
                root.matrix_identity.namespace
                != self._required_runtime_namespace()
            ):
                self._on_worker_replacement_required(
                    ProductRuntimeWorkerReplacementRequired(
                        current_namespace=self._required_runtime_namespace(),
                        requested_namespace=root.matrix_identity.namespace,
                    )
                )
            if record is not None:
                root_port.release(
                    NGUI_ACCEPTED_ROOT_CLIENT_ID,
                    key=record.key,
                    error=exc,
                )
            raise
        with self._changed:
            if self._inflight_projection is not None:
                raise RuntimeError(
                    "runtime frame projection already awaits application"
                )
            self._inflight_projection = (record, frame, source_vector)
        try:
            self._publisher.submit(frame)
        except Exception:
            with self._changed:
                if self._inflight_projection == (
                    record,
                    frame,
                    source_vector,
                ):
                    self._inflight_projection = None
                    self._changed.notify_all()
            if record is not None:
                root_port.release(
                    NGUI_ACCEPTED_ROOT_CLIENT_ID,
                    key=record.key,
                    error=RuntimeError(
                        "ngui complete runtime frame transport rejected"
                    ),
                )
            raise

    def _on_frame_delivery_receipt(
        self,
        receipt: RuntimeFrameDeliveryReceipt,
    ) -> None:
        with self._changed:
            transaction = self._inflight_projection
            if transaction is None:
                raise RuntimeError(
                    "runtime frame receipt has no projection transaction"
                )
            record, frame, source_vector = transaction
            projection = self._projection
            if projection is None:
                raise RuntimeError("runtime frame boundary namespace is unbound")
        root_port = self._accepted_root_port
        try:
            validate_runtime_frame_delivery_receipt(frame, receipt)
        except Exception as exc:
            if record is not None:
                root_port.release(
                    NGUI_ACCEPTED_ROOT_CLIENT_ID,
                    key=record.key,
                    error=exc,
                )
            with self._changed:
                if self._inflight_projection != transaction:
                    raise RuntimeError(
                        "runtime frame invalid receipt transaction changed"
                    ) from exc
                self._inflight_projection = None
                self._changed.notify_all()
            raise
        if not receipt.frame_applied:
            projection.discard(frame)
            if record is not None:
                root_port.acknowledge(
                    NGUI_ACCEPTED_ROOT_CLIENT_ID,
                    key=record.key,
                )
            with self._changed:
                if self._inflight_projection != transaction:
                    raise RuntimeError(
                        "runtime frame rejection transaction changed"
                    )
                self._inflight_projection = None
                self._last_terminal_source_vector = source_vector
                self._changed.notify_all()
            return
        projection.commit(frame)
        if record is not None:
            root_port.acknowledge(
                NGUI_ACCEPTED_ROOT_CLIENT_ID,
                key=record.key,
            )
        with self._changed:
            if self._inflight_projection != transaction:
                raise RuntimeError(
                    "runtime frame acknowledgement transaction changed"
                )
            self._inflight_projection = None
            self._last_terminal_source_vector = source_vector
            self._projected_count += 1
            self._changed.notify_all()

    def _required_runtime_namespace(self) -> RuntimeGenerationNamespace:
        namespace = self._runtime_namespace
        if namespace is None:
            raise RuntimeError("runtime frame boundary namespace is unbound")
        return namespace

    def _projection_loop(self) -> None:
        next_recovery_at = monotonic()
        try:
            while not self._stop_event.is_set() and not self._local_stop.is_set():
                with self._changed:
                    while True:
                        if self._stop_event.is_set() or self._local_stop.is_set():
                            return
                        now = monotonic()
                        if self._inflight_projection is None and (
                            self._wake_pending or now >= next_recovery_at
                        ):
                            self._wake_pending = False
                            next_recovery_at = (
                                now + RUNTIME_FRAME_CONTEXT_INTERVAL_S
                            )
                            break
                        self._changed.wait(
                            timeout=max(0.001, next_recovery_at - now)
                        )
                try:
                    self._project_pending()
                    self._worker_health.mark_success(self._projection_worker_id)
                except RuntimeFramePublisherStopping:
                    # Publisher and projection worker share the stop signal. A
                    # projection already between its stop check and submit is
                    # cancelled work, not a Product fault.
                    return
                except Exception as exc:
                    retry_s = self._record_worker_failure(
                        self._projection_worker_id,
                        "project_complete_runtime_frame",
                        exc,
                    )
                    self._local_stop.wait(retry_s)
        finally:
            self._worker_health.mark_stopped(
                self._projection_worker_id,
                reason="frame_boundary_stop_requested",
            )

    def stop(
        self,
        *,
        timeout_s: float = 2.0,
    ) -> RuntimeFrameBoundaryTerminationReceipt:
        with self._changed:
            self._local_stop.set()
            self._changed.notify_all()
            projection_thread = self._projection_thread
        boundary_errors = self._detach_owner_wake_listeners()
        if projection_thread is not None:
            projection_thread.join(timeout=max(0.0, float(timeout_s)))
        with self._changed:
            projection_thread_alive = bool(
                projection_thread is not None and projection_thread.is_alive()
            )
            pending_projection_cancelled = False
            inflight_projection_cancelled = (
                self._inflight_projection is not None
            )
        if projection_thread_alive:
            pending_projection_cancelled = False
            inflight_projection_cancelled = False
            publisher = self._publisher.termination_observation()
        else:
            publisher = self._publisher.stop(timeout_s=timeout_s)
        with self._changed:
            if (
                not projection_thread_alive
                and publisher.terminated
                and self._inflight_projection is not None
            ):
                record, _frame, _source_vector = self._inflight_projection
                if record is not None:
                    try:
                        self._accepted_root_port.release(
                            NGUI_ACCEPTED_ROOT_CLIENT_ID,
                            key=record.key,
                            error=RuntimeError(
                                "runtime frame boundary stopped before application"
                            ),
                        )
                    except Exception as exc:
                        boundary_errors = (
                            *boundary_errors,
                            "accepted_root_release:"
                            f"{type(exc).__name__}:{exc}",
                        )
            if not projection_thread_alive and publisher.terminated:
                if not any(
                    error.startswith("accepted_root_release:")
                    for error in boundary_errors
                ):
                    self._inflight_projection = None
        if not projection_thread_alive and publisher.terminated:
            boundary_errors = (
                *boundary_errors,
                *_detach_ngui_accepted_root_client(self._runtime),
            )
        return self._runtime.issue_frame_boundary_termination(
            projection_thread_alive=projection_thread_alive,
            pending_projection_cancelled=pending_projection_cancelled,
            inflight_projection_cancelled=inflight_projection_cancelled,
            listener_detachment_errors=boundary_errors,
            publisher=publisher,
        )


def _terminate_required_runtime_workers(
    runtime: ProductNGUIRuntimePort | None,
    workers: tuple[Thread | None, ...],
    *,
    timeout_s: float,
) -> RequiredRuntimeWorkerTerminationReceipt:
    deadline = monotonic() + max(0.0, float(timeout_s))
    for worker in workers:
        if (
            worker is not None
            and worker.is_alive()
            and worker is not current_thread()
        ):
            worker.join(timeout=max(0.0, deadline - monotonic()))
    survivors = tuple(
        worker.name
        for worker in workers
        if (
            worker is not None
            and worker.is_alive()
            and worker is not current_thread()
        )
    )
    expected_workers = (
        "ngui-core-manual-nmi",
        "ngui-core-command-outcome",
    )
    observed_workers = tuple(
        worker.name for worker in workers if worker is not None
    )
    if runtime is not None:
        return runtime.issue_required_workers_termination(
            expected_workers=expected_workers,
            observed_workers=observed_workers,
            survivors=survivors,
        )
    return RequiredRuntimeWorkerTerminationReceipt(
        lifecycle_generation=1,
        expected_workers=expected_workers,
        observed_workers=observed_workers,
        survivors=survivors,
        _issuer=object(),
    )


def _require_command_outcome_readiness(
    ready: ThreadEvent,
    *,
    timeout_s: float,
) -> None:
    """Do not report RUNNING until command outcomes can be delivered."""

    if not isinstance(ready, ThreadEvent):
        raise TypeError("ngui_command_outcome_readiness_event_required")
    if not ready.wait(timeout=max(0.0, float(timeout_s))):
        raise RuntimeError(
            "ngui_required_command_outcome_worker_not_ready"
        )


def _wait_for_startup_router_serial(
    request_queue: Any,
    status_queue: Any,
    stop_event: Any,
    worker_identity: RuntimeWorkerGenerationIdentity,
) -> str:
    """Wait for one serial without discarding the authenticated router session."""

    _publish_status(
        status_queue,
        worker_identity,
        "identity_required",
        "Router login succeeded; enter the router serial number to continue.",
        0,
    )
    while not stop_event.is_set():
        try:
            submission = request_queue.get(timeout=0.1)
        except Empty:
            continue
        if not isinstance(submission, RuntimeStartupRouterSerial):
            raise RuntimeError("invalid startup router serial submission")
        return submission.serial
    return ""


def run_runtime_worker(
    config: Arch4Config,
    worker_generation_identity: RuntimeWorkerGenerationIdentity,
    observe_only: bool,
    request_queue: Any,
    manual_nmi_queue: Any,
    status_queue: Any,
    lifecycle_queue: Any,
    lifecycle_acknowledgement_queue: Any,
    frame_queue: Any,
    frame_acknowledgement_queue: Any,
    stop_event: Any,
    wake_event: Any,
    parent_lifetime_reader: Any,
    trace_selected_at: float | None = None,
) -> None:
    """Run one CGF Product outside the NGUI and GUI process."""

    if not isinstance(
        worker_generation_identity,
        RuntimeWorkerGenerationIdentity,
    ):
        raise TypeError("runtime_worker_generation_identity_required")
    product: ProductSupervisor | None = None
    product_identity: RuntimeProductGenerationIdentity | None = None
    runtime: ProductNGUIRuntimePort | None = None
    runtime_activated = False
    manual_stop = ThreadEvent()
    manual_thread: Thread | None = None
    command_outcome_stop = ThreadEvent()
    command_outcome_ready = ThreadEvent()
    command_outcome_thread: Thread | None = None
    advisory_runtime: Any | None = None
    frame_boundary: _RuntimeFrameBoundary | None = None
    operational_delivery_owner: _OperationalRequestDeliveryOwner | None = None
    lifecycle_publisher = RuntimeLifecyclePublisher(
        lifecycle_queue,
        lifecycle_acknowledgement_queue,
        logger=log,
    )
    worker_replacement_lock = RLock()
    worker_replacement_delivery = None
    worker_replacement_namespace: RuntimeGenerationNamespace | None = None
    cycle_count = 0
    runtime_faulted = False
    parent_lifetime_fault = ThreadEvent()
    parent_lifetime_monitor: Thread | None = None
    try:
        parent_lifetime_monitor = _start_parent_lifetime_monitor(
            parent_lifetime_reader,
            stop_event,
            wake_event,
            parent_lifetime_fault,
        )
        lifecycle_publisher.start()
        _publish_status(
            status_queue,
            worker_generation_identity,
            "starting",
            "",
            cycle_count,
        )
        runtime_dir, state_dir = resolve_cgf_product_storage(config.modem_backend)
        product = ProductSupervisor(
            config,
            settings_dir=runtime_dir,
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            trace_selected_at=trace_selected_at,
            verify_only=True,
            check_only=False,
            observe_only=bool(observe_only),
        )
        product.bind_startup_identity_prompt(
            lambda: _wait_for_startup_router_serial(
                request_queue,
                status_queue,
                stop_event,
                worker_generation_identity,
            )
        )
        runtime = product.ngui_runtime_port()
        startup_receipt = _startup_until_ready(
            product,
            status_queue,
            stop_event,
            worker_generation_identity,
        )
        if startup_receipt is None or stop_event.is_set():
            runtime_faulted = bool(
                startup_receipt is None and not stop_event.is_set()
            )
            return
        runtime.activate(startup_receipt)
        runtime_activated = True
        operational_delivery_owner = _OperationalRequestDeliveryOwner(
            runtime,
            lifecycle_publisher,
        )
        product_identity = RuntimeProductGenerationIdentity.from_admitted_root(
            worker=worker_generation_identity,
            product_root=startup_receipt,
        )
        product_admission_delivery = lifecycle_publisher.publish_and_wait(
            RuntimeIPCEvent(
                kind=RuntimeEventKind.PRODUCT_ROOT_ADMISSION,
                generation_identity=product_identity,
                payload=RuntimeProductRootAdmission(
                    product_root=startup_receipt,
                ),
            ),
            timeout_s=2.0,
        )
        product_admission_observation = (
            product_admission_delivery.observation(timeout_s=0.0)
        )
        if product_admission_observation.disposition is not (
            RuntimeLifecycleDeliveryDisposition.ACCEPTED
        ):
            raise RuntimeError(
                "runtime_product_root_admission_"
                + product_admission_observation.disposition.value
                + (
                    ":" + product_admission_observation.reason
                    if product_admission_observation.reason
                    else ""
                )
            )

        def request_worker_replacement(
            request: ProductRuntimeWorkerReplacementRequired,
        ) -> None:
            nonlocal worker_replacement_delivery
            nonlocal worker_replacement_namespace
            if not isinstance(request, ProductRuntimeWorkerReplacementRequired):
                raise TypeError(
                    "runtime_worker_replacement_typed_request_required"
                )
            with worker_replacement_lock:
                if (
                    worker_replacement_namespace is not None
                    and worker_replacement_namespace != request.requested_namespace
                ):
                    raise RuntimeError(
                        "runtime_worker_replacement_observation_changed"
                    )
                worker_replacement_namespace = request.requested_namespace
                delivery = worker_replacement_delivery
                if delivery is None:
                    delivery = lifecycle_publisher.publish_and_wait(
                        RuntimeIPCEvent(
                            kind=(
                                RuntimeEventKind
                                .WORKER_REPLACEMENT_REQUIRED
                            ),
                            generation_identity=product_identity,
                            payload=(
                                RuntimeWorkerReplacementRequiredReceipt(
                                    generation_identity=product_identity,
                                    active_namespace=(
                                        product_identity.runtime_namespace
                                    ),
                                    requested_namespace=request.requested_namespace,
                                    reason=request.reason,
                                    observed_at=time(),
                                )
                            ),
                        ),
                        timeout_s=2.0,
                    )
                    worker_replacement_delivery = delivery
                observation = delivery.observation(timeout_s=2.0)
                if observation.disposition is (
                    RuntimeLifecycleDeliveryDisposition.ACCEPTED
                ):
                    stop_event.set()
                    wake_event.set()
                    return
                if observation.disposition is (
                    RuntimeLifecycleDeliveryDisposition
                    .PENDING_INDETERMINATE
                ):
                    raise TimeoutError(
                        "runtime_worker_replacement_ack_pending"
                    )
                raise RuntimeError(
                    "runtime_worker_replacement_"
                    + observation.disposition.value
                    + (
                        ":" + observation.reason
                        if observation.reason
                        else ""
                    )
                )

        _attach_ngui_accepted_root_client(runtime)
        _attach_ngui_core_command_outcome_client(runtime)
        _publish_foundation_runtime_health(
            runtime,
            phase="startup_begin",
            cycle_count=cycle_count,
        )
        frame_boundary = _RuntimeFrameBoundary(
            runtime,
            frame_queue,
            frame_acknowledgement_queue,
            stop_event,
            request_worker_replacement,
            product.intelligence_matrix().latest_presentation,
        )
        frame_start = runtime.enter_runtime_frame_boundary()
        if (
            not isinstance(frame_start, RuntimeParticipantStartReceipt)
            or frame_start.participant
            is not RuntimeTerminationParticipant.RUNTIME_FRAME
        ):
            raise RuntimeError(
                "product_runtime_frame_boundary_start_receipt_invalid"
            )
        publication_start = runtime.start_sample_publication()
        if (
            not isinstance(
                publication_start,
                RuntimeParticipantStartReceipt,
            )
            or publication_start.participant
            is not (
                RuntimeTerminationParticipant
                .MODEM_SAMPLE_PUBLICATION
            )
        ):
            raise RuntimeError(
                "product_sample_publication_start_receipt_invalid"
            )
        modem_live_ready = startup_receipt.matrix_readiness
        if not isinstance(
            modem_live_ready,
            ModemLiveReadyReceipt,
        ):
            raise RuntimeError("Product Modem Live ready receipt unavailable")
        frame_boundary.bind_runtime_namespace(
            modem_live_ready,
        )
        if stop_event.is_set():
            return
        command_outcome_thread = Thread(
            target=_command_outcome_loop,
            args=(
                runtime,
                lifecycle_publisher,
                stop_event,
                command_outcome_stop,
                command_outcome_ready,
                product_identity,
            ),
            name="ngui-core-command-outcome",
            daemon=True,
        )
        command_outcome_thread.start()
        manual_thread = Thread(
            target=_manual_nmi_loop,
            args=(
                runtime,
                manual_nmi_queue,
                lifecycle_publisher,
                stop_event,
                manual_stop,
                command_outcome_ready,
            ),
            name="ngui-core-manual-nmi",
            daemon=True,
        )
        manual_thread.start()
        _require_command_outcome_readiness(
            command_outcome_ready,
            timeout_s=0.5,
        )
        clock_start = runtime.start_runtime_clock()
        if (
            not isinstance(clock_start, RuntimeParticipantStartReceipt)
            or clock_start.participant
            is not RuntimeTerminationParticipant.RUNTIME_CLOCK
        ):
            raise RuntimeError(
                "product_runtime_clock_start_receipt_invalid"
            )
        clock_startup_readiness = _await_runtime_clock_startup_ready(
            runtime,
            status_queue,
            stop_event,
            product_identity,
        )
        if clock_startup_readiness is None:
            return
        _publish_foundation_runtime_health(
            runtime,
            phase="running",
            cycle_count=cycle_count,
            detail={"runtime_clock_startup": clock_startup_readiness},
        )
        frame_boundary.start()
        _publish_status(
            status_queue,
            product_identity,
            "running",
            "startup_ready",
            cycle_count,
        )
        # This ordering is common to every modem: login, live-frame delivery
        # and command workers are ready before Intelligence opens History.
        try:
            advisory_runtime = build_intelligence_runtime(
                product,
                history_path=resolve_intelligence_history_path(state_dir),
                diagnostic_path=runtime_dir / "new_horizon_diagnostic_snapshot.json",
            )
            advisory_runtime.start(startup_receipt)
        except Exception as exc:
            if advisory_runtime is not None:
                _stop_intelligence_runtime(advisory_runtime, timeout_s=1.0)
            advisory_runtime = None
            try:
                product.intelligence_matrix().report_customer_start_failure(
                    startup_receipt, exc,
                )
            except Exception as report_exc:
                log.critical(
                    "New Horizon start failure could not reach PIM: %s", report_exc,
                )
            log.error("New Horizon runtime unavailable: %s", exc)
        foundation_service_admission = FoundationServiceAdmission()
        force_foundation_service = False
        while not stop_event.is_set():
            try:
                replacement = runtime.pending_device_replacement()
                if replacement is not None:
                    request_worker_replacement(replacement)
                    continue
                request_report = _drain_typed_requests(
                    runtime,
                    request_queue,
                    delivery_owner=operational_delivery_owner,
                    support_history_preparer=lambda request: _prepare_support_history_request(
                        request, advisory_runtime=advisory_runtime, product_identity=product_identity),
                )
                observed_at = time()
                if foundation_service_admission.admit(
                    register_sequence=runtime.runtime_register_sequence(
                        product_identity.runtime_namespace
                    ),
                    observed_at=monotonic(),
                    force=(
                        request_report.handled or force_foundation_service
                    ),
                ):
                    runtime.service_once(now=observed_at)
                    cycle_count += 1
                    force_foundation_service = False
                wake_event.wait(0.1)
                wake_event.clear()
            except Exception as exc:
                force_foundation_service = True
                retry_s = min(
                    5.0,
                    0.1 * (2 ** min(cycle_count + 1, 6)),
                )
                _record_runtime_fault_fail_soft(
                    runtime,
                    boundary=FaultBoundaryClass.WORKER,
                    owner="foundation_service_loop",
                    operation="foundation_service_cycle",
                    generation=cycle_count,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="foundation_service_retries_without_false_frame",
                    error=exc,
                    suppression_budget=3,
                )
                _publish_foundation_runtime_health(
                    runtime,
                    phase="reconnecting",
                    cycle_count=cycle_count,
                    error=exc,
                    detail={"retry_s": retry_s},
                )
                log.exception(
                    "NGUI Foundation service cycle failed; retrying in %.3fs",
                    retry_s,
                )
                _publish_status(
                    status_queue,
                    product_identity,
                    "reconnecting",
                    (
                        f"Foundation service failure after cycle {cycle_count}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    cycle_count,
                )
                if stop_event.wait(retry_s):
                    break
                wake_event.clear()
    except Exception as exc:
        runtime_faulted = True
        if runtime is not None:
            _publish_foundation_runtime_health(
                runtime,
                phase="fatal_runner",
                cycle_count=cycle_count,
                error=exc,
            )
        log.exception("NGUI runtime worker failed")
        _publish_status(
            status_queue,
            product_identity or worker_generation_identity,
            "failed",
            f"{type(exc).__name__}: {exc}",
            cycle_count,
        )
    finally:
        if parent_lifetime_fault.is_set():
            runtime_faulted = True
        shutdown_phase_errors: list[str] = []

        def _shutdown_phase(name: str, operation: Callable[[], Any]) -> Any:
            nonlocal runtime_faulted
            try:
                return operation()
            except Exception as exc:
                runtime_faulted = True
                shutdown_phase_errors.append(
                    f"{name}:{type(exc).__name__}:{exc}"
                )
                log.exception("NGUI runtime shutdown phase failed: %s", name)
                return None

        if runtime_activated and runtime is not None:
            _shutdown_phase(
                "publish_shutdown_begin",
                lambda: _publish_foundation_runtime_health(
                    runtime,
                    phase="shutdown_begin",
                    cycle_count=cycle_count,
                ),
            )
        if operational_delivery_owner is not None:
            _shutdown_phase(
                "terminate_operational_delivery",
                lambda: operational_delivery_owner.close(timeout_s=2.0),
            )
        if advisory_runtime is not None:
            _shutdown_phase(
                "terminate_intelligence",
                lambda: _stop_intelligence_runtime(
                    advisory_runtime,
                    timeout_s=3.0,
                ),
            )
        if product is not None:
            _shutdown_phase("request_product_shutdown", product.request_shutdown)
        manual_stop.set()
        command_outcome_stop.set()
        required_workers = None
        frame_receipt = None
        publication_receipt = None
        clock_receipt = None
        if runtime_activated and runtime is not None:
            required_workers = _shutdown_phase(
                "terminate_required_workers",
                lambda: _terminate_required_runtime_workers(
                    runtime,
                    (manual_thread, command_outcome_thread),
                    timeout_s=6.0,
                ),
            )
            if (
                isinstance(
                    required_workers,
                    RequiredRuntimeWorkerTerminationReceipt,
                )
                and required_workers.terminated
            ):
                if frame_boundary is not None:
                    frame_receipt = _shutdown_phase(
                        "terminate_runtime_frame",
                        lambda: frame_boundary.stop(timeout_s=2.0),
                    )
                else:
                    frame_receipt = runtime.derive_frame_boundary_absence(
                        blocker=RuntimeTerminationBlocker.NONE,
                    )
            else:
                frame_receipt = runtime.derive_frame_boundary_absence(
                    blocker=RuntimeTerminationBlocker.REQUIRED_WORKERS,
                )
            frame_terminated = bool(
                frame_receipt is not None and frame_receipt.terminated
            )
            if frame_terminated:
                publication_receipt = _shutdown_phase(
                    "terminate_sample_publication",
                    lambda: runtime.stop_sample_publication(timeout_s=6.0),
                )
            else:
                publication_receipt = runtime.derive_sample_publication_absence(
                    blocker=RuntimeTerminationBlocker.RUNTIME_FRAME,
                )
            publication_terminated = bool(
                publication_receipt is not None
                and publication_receipt.terminated
            )
            if publication_terminated:
                clock_receipt = _shutdown_phase(
                    "terminate_runtime_clock",
                    lambda: runtime.stop_runtime_clock(timeout_s=2.0),
                )
            else:
                clock_receipt = runtime.derive_runtime_clock_absence(
                    blocker=RuntimeTerminationBlocker.MODEM_SAMPLE_PUBLICATION,
                )
        shutdown_receipt: ProductShutdownReceipt | None = None
        if (
            runtime_activated
            and runtime is not None
            and isinstance(
                required_workers,
                RequiredRuntimeWorkerTerminationReceipt,
            )
            and frame_receipt is not None
            and publication_receipt is not None
            and clock_receipt is not None
        ):
            _shutdown_phase(
                "record_runtime_termination",
                lambda: runtime.record_runtime_termination(
                    required_workers=required_workers,
                    publication=publication_receipt,
                    clock=clock_receipt,
                    frame=frame_receipt,
                ),
            )
        if product is not None:
            shutdown_receipt = _shutdown_phase(
                "shutdown_product",
                product.shutdown,
            )
        shutdown_envelope = None
        if shutdown_receipt is not None:
            shutdown_envelope = _shutdown_phase(
                "publish_product_shutdown_receipt",
                lambda: _publish_shutdown_receipt(
                    lifecycle_publisher,
                    shutdown_receipt,
                    product_identity=product_identity or worker_generation_identity,
                    timeout_s=2.0,
                ),
            )
        lifecycle_termination = _shutdown_phase(
            "terminate_lifecycle_transport",
            lambda: lifecycle_publisher.stop(timeout_s=2.0),
        )
        lifecycle_transport_terminated = bool(
            lifecycle_termination is not None
            and lifecycle_termination.terminated
        )
        try:
            parent_lifetime_reader.close()
        except (OSError, ValueError) as exc:
            log.warning(
                "NGUI parent lifetime channel close failed during shutdown: "
                "%s: %s",
                type(exc).__name__,
                exc,
            )
        if (
            parent_lifetime_monitor is not None
            and parent_lifetime_monitor.is_alive()
            and parent_lifetime_monitor is not current_thread()
        ):
            parent_lifetime_monitor.join(timeout=0.2)
        if not lifecycle_transport_terminated:
            log.error("NGUI runtime lifecycle transport did not drain at shutdown")
        clean_shutdown = bool(
            shutdown_receipt is not None
            and shutdown_receipt.terminated
            and shutdown_envelope is not None
            and lifecycle_transport_terminated
            and not runtime_faulted
            and not shutdown_phase_errors
        )
        if clean_shutdown:
            _publish_status(
                status_queue,
                product_identity or worker_generation_identity,
                "stopped",
                "",
                cycle_count,
            )
        else:
            _publish_status(
                status_queue,
                product_identity or worker_generation_identity,
                "failed",
                (
                    "product_shutdown_not_clean"
                    if lifecycle_transport_terminated
                    and shutdown_envelope is not None
                    else "product_shutdown_receipt_not_published"
                    if lifecycle_transport_terminated
                    else "runtime_lifecycle_transport_termination_unproven"
                ) + (
                    f"; runtime_faulted={runtime_faulted}"
                    f"; parent_lifetime_fault={parent_lifetime_fault.is_set()}"
                    f"; shutdown_phase_errors={shutdown_phase_errors!r}"
                ),
                cycle_count,
            )
            raise SystemExit(1)


def _startup_until_ready(
    product: ProductSupervisor,
    status_queue: Any,
    stop_event: Any,
    worker_identity: RuntimeWorkerGenerationIdentity,
) -> ProductStartupReceipt | None:
    delay_s = 4.0
    while not stop_event.is_set():
        receipt = product.startup()
        if receipt.disposition is ProductStartupDisposition.READY:
            return receipt
        error = RuntimeError(
            f"{receipt.error_type or 'StartupError'}: "
            f"{receipt.detail or receipt.reason}"
        )
        if receipt.disposition is ProductStartupDisposition.TERMINAL:
            _publish_status(
                status_queue,
                worker_identity,
                "failed",
                receipt.detail or receipt.reason,
                0,
            )
            return None
        _publish_status(
            status_queue,
            worker_identity,
            "reconnecting",
            receipt.detail or receipt.reason,
            0,
        )
        stop_event.wait(delay_s)
        delay_s = min(60.0, delay_s * 2.0)
    return None


def _command_outcome_loop(
    runtime: ProductNGUIRuntimePort,
    lifecycle_publisher: RuntimeLifecyclePublisher,
    process_stop: Any,
    local_stop: ThreadEvent,
    ready: ThreadEvent,
    product_identity: RuntimeProductGenerationIdentity,
) -> None:
    """Deliver final CommandAuthority receipts to NGUI."""

    worker_health = runtime.worker_health_authority()
    worker_id = "ngui_command_outcome"
    worker_health.register(
        worker_id,
        execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        required=True,
        retryable=True,
    )
    worker_health.mark_started(worker_id)
    stream = runtime.command_outcome_stream()
    if bool(stream.status().get("persistence_available", False)):
        ready.set()
    last_idle_health = 0.0
    try:
        while not process_stop.is_set() and not local_stop.is_set():
            if not ready.is_set():
                if bool(stream.status().get("persistence_available", False)):
                    ready.set()
                else:
                    local_stop.wait(0.1)
                    continue
            reservation = lifecycle_publisher.try_reserve()
            if reservation is None:
                local_stop.wait(0.05)
                continue
            reservation_committed = False
            record = None
            try:
                record = stream.claim_next(
                    NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
                    runtime_namespace=product_identity.runtime_namespace,
                )
                if record is None:
                    lifecycle_publisher.cancel(reservation)
                    now_monotonic = monotonic()
                    if now_monotonic - last_idle_health >= 1.0:
                        worker_health.mark_success(worker_id)
                        last_idle_health = now_monotonic
                    local_stop.wait(0.25)
                    continue
                receipt = record.receipt
                if not isinstance(receipt, CommandTerminalReceipt):
                    raise TypeError(
                        "ngui_command_outcome_typed_terminal_receipt_required"
                    )
                matrix_identity = receipt.transaction.matrix_identity
                if not isinstance(
                    matrix_identity,
                    MatrixGenerationIdentity,
                ):
                    raise TypeError(
                        "command_outcome_transaction_matrix_identity_required"
                    )
                delivery = lifecycle_publisher.commit(
                    reservation,
                    RuntimeIPCEvent(
                        kind=RuntimeEventKind.CORE_COMMAND_OUTCOME,
                        generation_identity=RuntimeNamespaceGenerationIdentity(
                            product=product_identity,
                            matrix_identity=matrix_identity,
                        ),
                        payload=receipt,
                    ),
                )
                reservation_committed = True
                observation = delivery.observation(timeout_s=0.2)
                while observation.disposition is (
                    RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
                ) and not process_stop.is_set() and not local_stop.is_set():
                    observation = delivery.observation(timeout_s=0.2)
                if observation.disposition is (
                    RuntimeLifecycleDeliveryDisposition.PENDING_INDETERMINATE
                ):
                    stream.release(
                        NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
                        runtime_namespace=product_identity.runtime_namespace,
                        key=record.key,
                        error=RuntimeError(
                            "ngui_command_outcome_delivery_interrupted"
                        ),
                    )
                    break
                if observation.disposition is (
                    RuntimeLifecycleDeliveryDisposition.ACCEPTED
                ):
                    stream.acknowledge(
                        NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
                        runtime_namespace=product_identity.runtime_namespace,
                        key=record.key,
                    )
                    worker_health.mark_success(worker_id)
                else:
                    rejection = RuntimeError(
                        "ngui_lifecycle_command_outcome_"
                        + observation.disposition.value
                        + (
                            ":" + observation.reason
                            if observation.reason
                            else ""
                        )
                    )
                    stream.release(
                        NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
                        runtime_namespace=product_identity.runtime_namespace,
                        key=record.key,
                        error=rejection,
                    )
                    worker_health.mark_failure(
                        worker_id,
                        rejection,
                        next_retry_at=time() + 0.1,
                    )
            except Exception as exc:
                ready.clear()
                if not reservation_committed:
                    try:
                        lifecycle_publisher.cancel(reservation)
                    except RuntimeError:
                        pass
                if record is not None:
                    try:
                        stream.release(
                            NGUI_CORE_COMMAND_OUTCOME_CLIENT_ID,
                            runtime_namespace=(
                                product_identity.runtime_namespace
                            ),
                            key=record.key,
                            error=exc,
                        )
                    except Exception as recovery_error:
                        log.error(
                            "NGUI command-outcome release failed: %s",
                            recovery_error,
                        )
                if (
                    bool(stream.status().get("persistence_available", False))
                ):
                    ready.set()
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time() + 0.1,
                )
                _record_runtime_fault_fail_soft(
                    runtime,
                    boundary=FaultBoundaryClass.GUI,
                    owner=worker_id,
                    operation="deliver_command_outcome",
                    generation=int(
                        getattr(
                            getattr(record, "receipt", None),
                            "receipt_sequence",
                            0,
                        )
                        or 0
                    ),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=(
                        "core_monitoring_continues_and_outcome_delivery_retries"
                    ),
                    error=exc,
                    suppression_budget=3,
                )
                local_stop.wait(0.1)
    finally:
        worker_health.mark_stopped(
            worker_id,
            reason="command_outcome_stop_requested",
        )


def _manual_nmi_loop(
    runtime: ProductNGUIRuntimePort,
    manual_nmi_queue: Any,
    lifecycle_publisher: RuntimeLifecyclePublisher,
    process_stop: Any,
    local_stop: ThreadEvent,
    command_outcome_ready: ThreadEvent,
) -> None:
    worker_health = runtime.worker_health_authority()
    worker_id = "ngui_manual_nmi"
    worker_health.register(
        worker_id,
        execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        required=True,
        retryable=True,
    )
    worker_health.mark_started(worker_id)
    try:
        while not process_stop.is_set() and not local_stop.is_set():
            reservation = lifecycle_publisher.try_reserve()
            if reservation is None:
                local_stop.wait(0.05)
                continue
            try:
                request = manual_nmi_queue.get(timeout=0.05)
            except Empty:
                lifecycle_publisher.cancel(reservation)
                continue
            except Exception as exc:
                lifecycle_publisher.cancel(reservation)
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time() + 0.05,
                )
                _record_runtime_fault_fail_soft(
                    runtime,
                    boundary=FaultBoundaryClass.WORKER,
                    owner=worker_id,
                    operation="receive_request",
                    generation=0,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="manual_nmi_receiver_retries",
                    error=exc,
                    suppression_budget=3,
                )
                local_stop.wait(0.05)
                continue
            if not command_outcome_ready.is_set():
                timing = _manual_nmi_timing(
                    request,
                    child_received_at=float(monotonic()),
                )
                try:
                    _publish_manual_result(
                        lifecycle_publisher,
                        reservation,
                        request,
                        False,
                        "core_command_outcome_boundary_unavailable",
                        timing=timing,
                        gateway_outcome_expected=False,
                    )
                except Exception as exc:
                    _record_manual_delivery_failure(
                        runtime,
                        worker_health,
                        request,
                        exc,
                    )
                continue
            try:
                _process_core_command_nmi_request(
                    runtime,
                    request,
                    lifecycle_publisher,
                    reservation,
                    worker_health=worker_health,
                )
            except Exception as exc:
                _record_manual_delivery_failure(
                    runtime,
                    worker_health,
                    request,
                    exc,
                )
    finally:
        worker_health.mark_stopped(worker_id, reason="manual_nmi_stop_requested")


def _record_manual_delivery_failure(
    runtime: ProductNGUIRuntimePort,
    worker_health: Any,
    request: Any,
    error: Exception,
) -> None:
    now = time()
    worker_health.mark_failure(
        "ngui_manual_nmi",
        error,
        next_retry_at=now + 0.05,
        now=now,
    )
    _record_runtime_fault_fail_soft(
        runtime,
        boundary=FaultBoundaryClass.WORKER,
        owner="ngui_manual_nmi",
        operation="publish_manual_result",
        generation=max(0, int(getattr(request, "sequence", 0) or 0)),
        severity=FaultSeverity.ERROR,
        retryable=True,
        degraded_behavior=(
            "manual_receiver_continues_and_command_outcome_remains_authoritative"
        ),
        error=error,
        suppression_budget=3,
    )


def _process_core_command_nmi_request(
    runtime: ProductNGUIRuntimePort,
    request: Any,
    lifecycle_publisher: RuntimeLifecyclePublisher,
    reservation: RuntimeLifecycleReservation,
    *,
    worker_health: Any,
    monotonic_clock: Callable[[], float] = monotonic,
    wall_clock: Callable[[], float] = time,
) -> dict[str, Any]:
    """Process one child NMI request through the live worker contract."""

    worker_id = "ngui_manual_nmi"
    child_received_at = float(monotonic_clock())
    timing = _manual_nmi_timing(
        request,
        child_received_at=child_received_at,
    )
    if (
        not isinstance(request, RuntimeIPCRequest)
        or request.kind is not RuntimeRequestKind.MANUAL_NMI
    ):
        reason = "invalid manual NMI request"
        lifecycle_publisher.cancel(reservation)
        worker_health.mark_success(worker_id, now=float(wall_clock()))
        return {"submitted": False, "reason": reason, "timing": timing}
    command_request = getattr(request.payload, "command_request", None)
    if not isinstance(command_request, CoreCommandRequest):
        reason = "manual NMI Core command request is invalid"
        _publish_manual_result(
            lifecycle_publisher,
            reservation,
            request,
            False,
            reason,
            timing=timing,
            gateway_outcome_expected=False,
        )
        worker_health.mark_success(worker_id, now=float(wall_clock()))
        return {"submitted": False, "reason": reason, "timing": timing}
    try:
        core_submit_started_at = float(monotonic_clock())
        result = dict(
            runtime.submit_core_command(
                command_request,
                request.matrix_identity,
            )
        )
        core_submit_completed_at = float(monotonic_clock())
        timing.update(
            {
                "core_submit_ms": round(
                    max(0.0, core_submit_completed_at - core_submit_started_at)
                    * 1000.0,
                    3,
                ),
                "confirm_to_core_ms": round(
                    max(0.0, core_submit_completed_at - request.created_at)
                    * 1000.0,
                    3,
                ),
            }
        )
        if type(result.get("submitted")) is not bool:
            raise RuntimeError("core_command_gateway_submitted_receipt_required")
        submitted = result["submitted"]
        reason = str(result.get("reason") or "").strip()
        if not reason:
            raise RuntimeError("core_command_gateway_reason_required")
        # An accepted request publishes its lifecycle through the command
        # outcome stream. NGUI transport must not inspect gateway-private event
        # history to infer the result.
        gateway_outcome_expected = submitted
        worker_health.mark_success(worker_id, now=float(wall_clock()))
        response = {
            "submitted": submitted,
            "reason": reason,
            "timing": timing,
            "engine_result": result,
        }
    except Exception as exc:
        failed_at = float(wall_clock())
        worker_health.mark_failure(
            worker_id,
            exc,
            next_retry_at=failed_at,
            now=failed_at,
        )
        _record_runtime_fault_fail_soft(
            runtime,
            boundary=FaultBoundaryClass.WORKER,
            owner=worker_id,
            operation="submit_manual_action",
            generation=max(0, int(request.sequence)),
            severity=FaultSeverity.ERROR,
            retryable=True,
            degraded_behavior="manual_action_failed_receiver_continues",
            error=exc,
            suppression_budget=3,
        )
        reason = f"{type(exc).__name__}: {exc}"
        submitted = False
        gateway_outcome_expected = False
        response = {"submitted": False, "reason": reason, "timing": timing}
    _publish_manual_result(
        lifecycle_publisher,
        reservation,
        request,
        submitted,
        reason,
        timing=timing,
        gateway_outcome_expected=gateway_outcome_expected,
    )
    return response


def _request_label(request: RuntimeIPCRequest) -> str:
    return request.kind.value


_OPERATION_BY_REQUEST_KIND = {
    RuntimeRequestKind.CORE_READ_DIAGNOSTICS: OperationalKind.READ_DIAGNOSTICS,
    RuntimeRequestKind.CORE_NETWORK_INSPECTION: OperationalKind.INSPECT_NETWORK_SETTINGS,
    RuntimeRequestKind.CORE_WIFI_CONTROL: OperationalKind.WIFI_SET_RADIO,
    RuntimeRequestKind.CORE_DOWNSTREAM_DIAGNOSTIC: OperationalKind.SET_DOWNSTREAM_HEALTH,
    RuntimeRequestKind.FOUNDATION_SMS_CONTROL: OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL,
    RuntimeRequestKind.FOUNDATION_ANTENNA_GUIDE: OperationalKind.ANTENNA_GUIDE,
    RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_PLAN: OperationalKind.PLAN_BAND_SCHEDULE,
    RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_PLAN: OperationalKind.PLAN_BAND_SEQUENCE,
    RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_CANCEL: OperationalKind.CANCEL_BAND_SCHEDULE,
    RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_CANCEL: OperationalKind.CANCEL_BAND_SEQUENCE,
    RuntimeRequestKind.PRODUCT_INTELLIGENCE_AUTOPILOT: OperationalKind.SET_INTELLIGENCE_AUTOPILOT,
    RuntimeRequestKind.PRODUCT_INTELLIGENCE_EXPLORE: OperationalKind.SET_INTELLIGENCE_EXPLORE,
}

_CORE_OPERATIONAL_KINDS = frozenset(
    {
        OperationalKind.READ_DIAGNOSTICS,
        OperationalKind.INSPECT_NETWORK_SETTINGS,
        OperationalKind.WIFI_SET_RADIO,
        OperationalKind.SET_DOWNSTREAM_HEALTH,
    }
)


def _publish_owned_operational_receipt(
    runtime: ProductNGUIRuntimePort,
    receipt: OperationalServiceReceipt,
    matrix_identity: MatrixGenerationIdentity,
) -> None:
    """Route a typed receipt to its declared presentation owner."""

    runtime.deliver_operational_receipt(receipt, matrix_identity)


def _execute_runtime_request(
    runtime: ProductNGUIRuntimePort,
    request: RuntimeIPCRequest,
) -> OperationalServiceReceipt:
    typed = request.payload
    expected_type = {
        RuntimeRequestKind.CORE_READ_DIAGNOSTICS: RouterReadDiagnosticRequest,
        RuntimeRequestKind.CORE_NETWORK_INSPECTION: NetworkSettingsInspectionRequest,
        RuntimeRequestKind.CORE_WIFI_CONTROL: WifiRadioControlRequest,
        RuntimeRequestKind.CORE_DOWNSTREAM_DIAGNOSTIC: DownstreamHealthConfigRequest,
        RuntimeRequestKind.FOUNDATION_SMS_CONTROL: SmsCommandControlConfigRequest,
        RuntimeRequestKind.FOUNDATION_ANTENNA_GUIDE: AntennaGuideControlRequest,
        RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_PLAN: BandSchedulePlanRequest,
        RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_PLAN: BandSequencePlanRequest,
        RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_CANCEL: BandScheduleCancelRequest,
        RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_CANCEL: BandSequenceCancelRequest,
        RuntimeRequestKind.PRODUCT_INTELLIGENCE_AUTOPILOT: IntelligenceAutopilotControlRequest,
        RuntimeRequestKind.PRODUCT_INTELLIGENCE_EXPLORE: IntelligenceExploreControlRequest,
    }.get(request.kind)
    if expected_type is None:
        raise RuntimeError(f"runtime worker received illegal request kind: {request.kind}")
    if not isinstance(typed, expected_type):
        raise TypeError(
            "runtime_operational_request_type_mismatch:" + request.kind.value
        )
    return runtime.transact_operational_request(
        typed,
        request.matrix_identity,
    )


def _prepare_support_history_request(request: RuntimeSupportHistoryFlushRequest, *, advisory_runtime, product_identity) -> bool:
    """Reply to a bounded diagnostic barrier without entering radio authority."""
    reply = {"identity": request.generation_identity, "complete": False, "error": ""}
    try:
        if request.generation_identity.product != product_identity:
            raise RuntimeError("support_history_worker_identity_mismatch")
        remaining = request.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("support_history_request_expired")
        if advisory_runtime is None:
            raise RuntimeError("support_history_owner_unavailable")
        advisory_runtime.prepare_support_history(history_path=request.history_path,
            cutoff=request.cutoff, timeout_s=min(TYPED_REQUEST_DRAIN_BUDGET_S / 2., remaining))
        reply["complete"] = True
    except TimeoutError as exc:
        if str(exc) in {"support_history_admission_timeout", "support_history_flush_timeout"} and monotonic() < request.deadline:
            return False  # Same request/deadline, next normal Foundation cycle.
        reply["error"] = type(exc).__name__ + ":" + str(exc)
    except Exception as exc:
        reply["error"] = type(exc).__name__ + ":" + str(exc)
    try:
        request.reply.send(reply)
    except (OSError, EOFError) as exc:
        log.info("Support History reply delivery failed (%s): %s", type(exc).__name__, exc)
        # A closed requesting export needs no retry or radio effect.
    finally:
        request.reply.close()
    return True


def _drain_typed_requests(
    runtime: ProductNGUIRuntimePort,
    request_queue: Any,
    *,
    delivery_owner: _OperationalRequestDeliveryOwner,
    support_history_preparer: Callable[[RuntimeSupportHistoryFlushRequest], bool] | None = None,
    max_items: int = TYPED_REQUEST_DRAIN_MAX_ITEMS,
    budget_s: float = TYPED_REQUEST_DRAIN_BUDGET_S,
) -> TypedRequestDrainReport:
    item_limit = max(1, int(max_items))
    elapsed_limit_s = max(0.001, float(budget_s))
    started = monotonic()
    consumed = 0
    completed = 0
    dead_lettered = 0
    time_budget_exhausted = False
    queue_empty = False
    if support_history_preparer is not None:
        completed += delivery_owner.progress_support_history(support_history_preparer)
    for _ in range(item_limit):
        if consumed > 0 and monotonic() - started >= elapsed_limit_s:
            time_budget_exhausted = True
            break
        progressed, failed, pending = delivery_owner.progress()
        completed += progressed
        dead_lettered += failed
        if pending:
            time_budget_exhausted = True
            break
        reservation = delivery_owner.try_reserve()
        if reservation is None:
            time_budget_exhausted = True
            break
        try:
            request = request_queue.get_nowait()
        except Empty:
            delivery_owner.cancel(reservation)
            queue_empty = True
            break
        consumed += 1
        if isinstance(request, RuntimeSupportHistoryFlushRequest):
            delivery_owner.cancel(reservation)
            if support_history_preparer is None:
                _prepare_support_history_request(request, advisory_runtime=None, product_identity=None)
            else:
                delivery_owner.admit_support_history(request)
                continue
            completed += 1
            continue
        if not isinstance(request, RuntimeIPCRequest):
            delivery_owner.cancel(reservation)
            exc = RuntimeError("runtime worker received an invalid command request")
            _record_runtime_fault_fail_soft(
                runtime,
                boundary=FaultBoundaryClass.WORKER,
                owner="ngui_typed_request_worker",
                operation="invalid_request",
                generation=0,
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior="invalid_request_dead_lettered_next_command_continues",
                error=exc,
                suppression_budget=0,
            )
            dead_lettered += 1
            continue
        try:
            receipt = _execute_runtime_request(
                runtime,
                request,
            )
        except Exception as exc:
            delivery_owner.cancel(reservation)
            _record_runtime_fault_fail_soft(
                runtime,
                boundary=FaultBoundaryClass.WORKER,
                owner="ngui_typed_request_worker",
                operation=_request_label(request),
                generation=max(0, int(request.sequence)),
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior=(
                    "operational_contract_failure_stops_request_lane"
                ),
                error=exc,
                suppression_budget=0,
            )
            raise
        try:
            delivery_owner.admit(
                request=request,
                receipt=receipt,
                reservation=reservation,
                failed_effect=(
                    receipt.disposition is OperationalDisposition.FAILED
                ),
            )
        except Exception:
            delivery_owner.cancel(reservation)
            raise
        progressed, failed, pending = delivery_owner.progress()
        completed += progressed
        dead_lettered += failed
        if pending:
            time_budget_exhausted = True
            break
    elapsed_ms = max(0.0, monotonic() - started) * 1000.0
    return TypedRequestDrainReport(
        consumed=consumed,
        completed=completed,
        dead_lettered=dead_lettered,
        item_budget_exhausted=bool(consumed >= item_limit and not queue_empty),
        time_budget_exhausted=time_budget_exhausted,
        elapsed_ms=round(elapsed_ms, 3),
    )


def _publish_status(
    status_queue: Any,
    generation_identity: (
        RuntimeWorkerGenerationIdentity
        | RuntimeProductGenerationIdentity
    ),
    state: str,
    message: str,
    cycle_count: int,
) -> bool:
    event = RuntimeIPCEvent(
        kind=RuntimeEventKind.STATUS,
        generation_identity=generation_identity,
        payload=RuntimeWorkerStatusReceipt(
            state=RuntimeWorkerStatusState(str(state)),
            changed_at=monotonic(),
            message=str(message or ""),
            cycle_count=int(cycle_count),
        ),
    )
    for _ in range(3):
        try:
            status_queue.put_nowait(event)
            return True
        except Full:
            try:
                status_queue.get_nowait()
            except Empty:
                continue
        except (EOFError, OSError, ValueError):
            return False
    return False


def _publish_manual_result(
    lifecycle_publisher: RuntimeLifecyclePublisher,
    reservation: RuntimeLifecycleReservation,
    request: Any,
    submitted: bool,
    reason: str,
    *,
    timing: Mapping[str, Any] | None = None,
    gateway_outcome_expected: bool,
) -> None:
    sequence = int(getattr(request, "sequence", 0) or 0)
    payload = getattr(request, "payload", None)
    command_request = getattr(payload, "command_request", None)
    correlation_id = (
        command_request.correlation_id
        if isinstance(command_request, CoreCommandRequest)
        else ""
    )
    lifecycle_publisher.commit(
        reservation,
        RuntimeIPCEvent(
            kind=RuntimeEventKind.MANUAL_NMI_RESULT,
            generation_identity=request.generation_identity,
            payload=RuntimeManualResultReceipt(
                sequence=sequence,
                submitted=bool(submitted),
                reason=str(reason or ""),
                correlation_id=correlation_id,
                gateway_outcome_expected=bool(gateway_outcome_expected),
                timing=RuntimeManualTimingReceipt(**dict(timing or {})),
            ),
        ),
    )


def _publish_shutdown_receipt(
    lifecycle_publisher: RuntimeLifecyclePublisher,
    receipt: ProductShutdownReceipt,
    *,
    product_identity: RuntimeWorkerGenerationIdentity | RuntimeProductGenerationIdentity,
    timeout_s: float,
) -> Any:
    if not isinstance(receipt, ProductShutdownReceipt):
        raise TypeError("runtime_shutdown_product_receipt_required")
    return lifecycle_publisher.publish_terminal(
        RuntimeIPCEvent(
            kind=RuntimeEventKind.SHUTDOWN_RECEIPT,
            generation_identity=product_identity,
            payload=receipt,
        ),
        timeout_s=timeout_s,
    )


def _manual_nmi_timing(
    request: Any,
    *,
    child_received_at: float,
) -> dict[str, float]:
    created_at = float(getattr(request, "created_at", 0.0) or 0.0)
    payload = getattr(request, "payload", {})
    parent_received_at = float(
        getattr(payload, "parent_received_at", 0.0) or 0.0
    )
    return {
        "intent_created_at": created_at,
        "parent_received_at": parent_received_at,
        "child_received_at": child_received_at,
        "confirm_to_app_ms": round(
            max(0.0, parent_received_at - created_at) * 1000.0,
            3,
        ) if created_at > 0.0 and parent_received_at > 0.0 else 0.0,
        "app_to_core_ms": round(
            max(0.0, child_received_at - parent_received_at) * 1000.0,
            3,
        ) if parent_received_at > 0.0 else 0.0,
        "confirm_to_core_process_ms": round(
            max(0.0, child_received_at - created_at) * 1000.0,
            3,
        ) if created_at > 0.0 else 0.0,
    }


__all__ = ["run_runtime_worker"]
