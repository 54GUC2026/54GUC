"""Parent-side IPC owner for the CGF worker used by production NGUI."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import logging
import math
from multiprocessing import get_context
from pathlib import Path
from queue import Empty, Full
from threading import RLock, Thread, current_thread
from time import monotonic, time
from typing import Any, Callable, Mapping

from arch4.core.command_request_builder import (
    build_core_command_request,
    build_core_command_target,
)
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.command_targets import CoreBandAutoTarget
from arch4.core_contracts.commands import (
    BAND_AUTO_COMMAND_KIND,
    CoreCommandIngressContext,
    CoreCommandOrigin,
    CoreCommandRequest,
)
from arch4.command_transactions import (
    CommandTerminalReceipt,
    DEFAULT_WRITE_RECEIPT_DEADLINE_S,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
    PRODUCT_SHUTDOWN_RECEIPT_SCHEMA,
    ProductShutdownReceipt,
    ProductStartupReceipt,
    ProductRuntimeWorkerReplacementReason,
)
from arch4.interfaces import Arch4Config, LeverAction, LeverType
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority
from arch4.operational_service_contracts import (
    AntennaGuideControlRequest,
    BandScheduleCancelRequest,
    BandSchedulePlanRequest,
    BandSequenceCancelRequest,
    BandSequencePlanRequest,
    CoreOperatorRequestContext,
    DownstreamHealthConfigRequest,
    FoundationOperatorRequestContext,
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
    OperationalServiceReceipt,
    ProductOperatorRequestContext,
    SmsCommandControlConfigRequest,
    WifiRadioControlRequest,
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
    CoreOperationalFreshness,
)

from .runtime_ipc import (
    RuntimeSupportHistoryFlushRequest,
    RuntimeEventKind,
    RuntimeEventScope,
    RuntimeFrameDeliveryReceipt,
    RuntimeFrameEnvelope,
    RuntimeInputFrame,
    RuntimeIPCEvent,
    RuntimeIPCRequest,
    RuntimeLifecycleApplicationReceipt,
    RuntimeLifecycleAcknowledgement,
    RuntimeLifecycleClaim,
    RuntimeLifecycleEnvelope,
    RuntimeManualCommandRequest,
    RuntimeManualResultReceipt,
    RuntimeNamespaceGenerationIdentity,
    RuntimeProductGenerationIdentity,
    RuntimeProductEventApplicationReceipt,
    RuntimeProductRootAdmission,
    RuntimeProductRootAdmissionAuthority,
    RuntimeWorkerGenerationIdentity,
    RuntimeWorkerReplacementRequiredReceipt,
    RuntimeWorkerStatusReceipt,
    RuntimeWorkerStatusState,
    RuntimeRequestKind,
    RuntimeStartupRouterSerial,
    build_runtime_frame_delivery_receipt,
    runtime_ipc_event_digest,
    runtime_frame_identity,
    validate_runtime_frame_delivery_receipt,
    validate_runtime_frame_envelope,
)
from .runtime_reset_manifest import (
    RuntimeResetApplicationPort,
    RuntimeResetCause,
    runtime_reset_manifest_digest,
)
from .operational_outbox import NGUIOperationalOutbox
log = logging.getLogger("arch4.ngui.runtime_service")


# Allow the already-started physical command to drain, then ordinary cleanup.
# This is a maximum wait, not a delay when no command is running.
RUNTIME_WORKER_SHUTDOWN_BUDGET_S = DEFAULT_WRITE_RECEIPT_DEADLINE_S + 16.0
CORE_COMMAND_CONTEXT_RETENTION = 128


class RuntimeServiceState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    IDENTITY_REQUIRED = "identity_required"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    LOGIN_REQUIRED = "login_required"


@dataclass(frozen=True, slots=True)
class RuntimeServiceStatus:
    state: RuntimeServiceState
    changed_at: float
    message: str = ""
    cycle_count: int = 0
    product_startup_receipt: ProductStartupReceipt | None = None

    def __post_init__(self) -> None:
        import math

        if not isinstance(self.state, RuntimeServiceState):
            raise TypeError("runtime_service_status_state_required")
        if type(self.changed_at) not in {int, float}:
            raise TypeError("runtime_service_status_timestamp_number_required")
        changed_at = float(self.changed_at)
        if not math.isfinite(changed_at) or changed_at <= 0.0:
            raise ValueError("runtime_service_status_timestamp_invalid")
        if type(self.message) is not str:
            raise TypeError("runtime_service_status_message_string_required")
        if type(self.cycle_count) is not int:
            raise TypeError("runtime_service_status_cycle_count_int_required")
        if self.cycle_count < 0:
            raise ValueError("runtime_service_status_cycle_count_invalid")
        receipt = self.product_startup_receipt
        if self.state is RuntimeServiceState.RUNNING:
            if not isinstance(receipt, ProductStartupReceipt):
                raise TypeError(
                    "runtime_service_running_product_startup_receipt_required"
                )
            if not receipt.ready:
                raise ValueError(
                    "runtime_service_running_product_startup_receipt_not_ready"
                )
        elif receipt is not None:
            raise ValueError(
                "runtime_service_nonrunning_product_startup_receipt_forbidden"
            )
        object.__setattr__(self, "changed_at", changed_at)


@dataclass(frozen=True, slots=True)
class OperationalRequestDelivery:
    sequence: int
    kind: RuntimeRequestKind
    request: object
    created_at: float


@dataclass(frozen=True, slots=True)
class _RuntimeResetPending:
    previous_worker_generation: int
    successor_worker_generation: int
    expected_manifest_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.previous_worker_generation) is not int
            or self.previous_worker_generation < 0
            or type(self.successor_worker_generation) is not int
            or self.successor_worker_generation
            != self.previous_worker_generation + 1
        ):
            raise ValueError("runtime_reset_pending_generation_invalid")
        if type(self.expected_manifest_digest) is not str:
            raise TypeError(
                "runtime_reset_pending_manifest_digest_string_required"
            )
        if (
            self.expected_manifest_digest
            != runtime_reset_manifest_digest()
        ):
            raise ValueError(
                "runtime_reset_pending_manifest_digest_mismatch"
            )


FrameCallback = Callable[[RuntimeInputFrame], RuntimeFrameDeliveryReceipt]
StatusCallback = Callable[[RuntimeServiceStatus], None]
WorkerTarget = Callable[..., None]


def _default_worker_entry(*args: Any) -> None:
    from .runtime_worker import run_runtime_worker

    run_runtime_worker(*args)


class NGUIRuntimeService:
    """Transport typed registers and commands across the CGF process boundary."""

    def __init__(
        self,
        *,
        on_frame: FrameCallback,
        on_status: StatusCallback,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
        application_lease: ApplicationLeaseIdentity,
        reset_application: RuntimeResetApplicationPort,
        operational_outbox_dir: str | Path,
        worker_target: WorkerTarget = _default_worker_entry,
        process_context: Any | None = None,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("runtime_service_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("runtime_service_requires_fault_supervisor")
        if not isinstance(application_lease, ApplicationLeaseIdentity):
            raise TypeError("runtime_service_application_lease_required")
        if not callable(
            getattr(reset_application, "apply_runtime_reset", None)
        ):
            raise TypeError("runtime_service_reset_application_port_required")
        self._on_frame = on_frame
        self._on_status = on_status
        self._worker_target = worker_target
        self._context = process_context or get_context("spawn")
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._application_lease = application_lease
        self._reset_application = reset_application
        self._operational_outbox = NGUIOperationalOutbox(
            operational_outbox_dir
        )
        self._worker_health.register(
            "ngui_runtime_status_receiver",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self._worker_health.register(
            "ngui_runtime_lifecycle_receiver",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self._worker_health.register(
            "ngui_runtime_frame_receiver",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self._lock = RLock()
        self._process: Any | None = None
        self._lifecycle_lock = RLock()
        self._status_receiver_thread: Thread | None = None
        self._lifecycle_receiver_thread: Thread | None = None
        self._frame_receiver_thread: Thread | None = None
        self._worker_replacement_thread: Thread | None = None
        self._reset_pending: _RuntimeResetPending | None = None
        self._reset_pending_cancelled = False
        self._receiver_stop: Any | None = None
        self._process_stop: Any | None = None
        self._wake: Any | None = None
        self._parent_lifetime_writer: Any | None = None
        self._request_queue: Any | None = None
        self._manual_nmi_queue: Any | None = None
        self._status_queue: Any | None = None
        self._lifecycle_queue: Any | None = None
        self._lifecycle_acknowledgement_queue: Any | None = None
        self._frame_queue: Any | None = None
        self._frame_acknowledgement_queue: Any | None = None
        self._runtime_generation = 0
        self._observe_only = False
        self._active_runtime_generation = 0
        self._active_worker_identity: (
            RuntimeWorkerGenerationIdentity | None
        ) = None
        self._active_product_identity: (
            RuntimeProductGenerationIdentity | None
        ) = None
        self._product_root_admission: (
            RuntimeProductRootAdmissionAuthority | None
        ) = None
        self._active_namespace_identity: (
            RuntimeNamespaceGenerationIdentity | None
        ) = None
        self._request_admission_open = False
        self._request_admission_closed_by_shutdown = False
        self._frame_admission_open = False
        self._request_admission_generation = 0
        self._active_matrix_identity: MatrixGenerationIdentity | None = None
        self._config: Arch4Config | None = None
        self._request_sequence = 0
        self._startup_router_serial_submitted = False
        self._operational_outbox_enqueued: set[str] = set()
        self._operational_outbox_foreign_reported: set[str] = set()
        self._status = RuntimeServiceStatus(RuntimeServiceState.IDLE, monotonic())
        self._child_status = self._status
        self._status_revision = 0
        self._last_late_child_status: dict[str, Any] = {}
        self._frame_consumer_degraded = False
        self._terminal_failure_active = False
        self._last_frame_delivery: dict[str, Any] = {}
        self._pending_lifecycle_envelope: RuntimeLifecycleEnvelope | None = None
        self._pending_lifecycle_generation = 0
        self._pending_lifecycle_claimed = False
        self._pending_lifecycle_acknowledgement_queue: Any | None = None
        self._last_lifecycle_application: (
            RuntimeLifecycleApplicationReceipt | None
        ) = None
        self._last_lifecycle_acknowledgement: (
            RuntimeLifecycleAcknowledgement | None
        ) = None
        self._core_command_contexts: OrderedDict[
            MatrixGenerationIdentity,
            CoreCommandIngressContext,
        ] = OrderedDict()
        self._last_product_shutdown_receipt: ProductShutdownReceipt | None = None
        self._pending_worker_replacement: (
            RuntimeWorkerReplacementRequiredReceipt | None
        ) = None
        self._last_stop_receipt: dict[str, Any] = {}
        self._last_lifecycle_sequence = 0
        self._last_lifecycle_digest = ""
        self._last_lifecycle_kind: RuntimeEventKind | None = None
        self._last_committed_frame_identity: (
            tuple[
                int,
                str,
                str,
                str,
                int,
                str,
                str,
                tuple[tuple[str, int, str], ...],
            ]
            | None
        ) = None
        self._last_committed_frame_receipt: (
            RuntimeFrameDeliveryReceipt | None
        ) = None

    def _rollback_failed_worker_start_unlocked(
        self,
        *,
        process: Any,
        parent_lifetime_reader: Any,
        parent_lifetime_writer: Any,
        receivers: tuple[Thread, ...],
    ) -> None:
        """Clean up all child and IPC state after a partial start."""

        if self._process_stop is not None:
            self._process_stop.set()
        if self._wake is not None:
            self._wake.set()
        parent_lifetime_writer.close()
        parent_lifetime_reader.close()
        if self._receiver_stop is not None:
            self._receiver_stop.set()
        process_started = getattr(process, "pid", None) is not None
        if process_started:
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
        for receiver in receivers:
            if receiver.ident is not None and receiver.is_alive():
                receiver.join(timeout=0.5)
        self._close_queues()
        close_process = getattr(process, "close", None)
        if callable(close_process) and (
            not process_started or not process.is_alive()
        ):
            try:
                close_process()
            except (OSError, ValueError) as exc:
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_runtime_service",
                    operation="rollback_process_close",
                    generation=max(0, int(self._active_runtime_generation)),
                    severity=FaultSeverity.WARNING,
                    retryable=False,
                    degraded_behavior="process_resources_left_for_os_cleanup",
                    error=exc,
                    suppression_budget=0,
                )
        self._parent_lifetime_writer = None
        self._active_runtime_generation = 0
        self._active_worker_identity = None
        self._active_product_identity = None
        self._product_root_admission = None
        self._active_namespace_identity = None
        self._process = None
        self._status_receiver_thread = None
        self._lifecycle_receiver_thread = None
        self._frame_receiver_thread = None
        self._process_stop = None
        self._wake = None
        self._receiver_stop = None
        self._request_queue = None
        self._manual_nmi_queue = None
        self._status_queue = None
        self._lifecycle_queue = None
        self._lifecycle_acknowledgement_queue = None
        self._frame_queue = None
        self._frame_acknowledgement_queue = None
        self._config = None
        self._request_admission_open = False
        self._frame_admission_open = False

    def _start_unlocked(
        self,
        config: Arch4Config,
        *,
        observe_only: bool,
        worker_generation_identity: RuntimeWorkerGenerationIdentity,
        trace_selected_at: float | None = None,
    ) -> None:
        if not isinstance(
            worker_generation_identity,
            RuntimeWorkerGenerationIdentity,
        ):
            raise TypeError("runtime worker generation identity required")
        runtime_generation = worker_generation_identity.worker_generation
        with self._lock:
            if self._process is not None:
                raise RuntimeError(
                    "NGUI CGF runtime boundary is not terminal"
                )
            if (
                worker_generation_identity.application_lease
                != self._application_lease
                or self._runtime_generation + 1 != runtime_generation
            ):
                raise RuntimeError(
                    "NGUI runtime generation identity is not the reserved successor"
                )
            self._config = config
            self._observe_only = bool(observe_only)
            self._process_stop = self._context.Event()
            self._wake = self._context.Event()
            (
                parent_lifetime_reader,
                parent_lifetime_writer,
            ) = self._context.Pipe(duplex=False)
            self._parent_lifetime_writer = parent_lifetime_writer
            self._receiver_stop = self._context.Event()
            self._request_queue = self._context.Queue(maxsize=64)
            self._manual_nmi_queue = self._context.Queue(maxsize=16)
            self._status_queue = self._context.Queue(maxsize=1)
            self._lifecycle_queue = self._context.Queue(maxsize=1)
            self._lifecycle_acknowledgement_queue = self._context.Queue(maxsize=1)
            self._frame_queue = self._context.Queue(maxsize=1)
            self._frame_acknowledgement_queue = self._context.Queue(maxsize=1)
            self._runtime_generation = runtime_generation
            self._active_runtime_generation = runtime_generation
            self._active_worker_identity = worker_generation_identity
            self._active_product_identity = None
            self._product_root_admission = (
                RuntimeProductRootAdmissionAuthority(
                    worker_generation_identity
                )
            )
            self._active_namespace_identity = None
            # A live process is not necessarily ready. Keep commands blocked
            # until NGUI accepts a Matrix generation.
            self._request_admission_open = False
            self._request_admission_closed_by_shutdown = False
            self._frame_admission_open = True
            self._request_admission_generation = runtime_generation
            self._active_matrix_identity = None
            self._last_product_shutdown_receipt = None
            self._pending_worker_replacement = None
            self._last_stop_receipt = {}
            self._core_command_contexts.clear()
            self._pending_lifecycle_envelope = None
            self._pending_lifecycle_generation = 0
            self._pending_lifecycle_claimed = False
            self._pending_lifecycle_acknowledgement_queue = None
            self._last_lifecycle_application = None
            self._last_lifecycle_acknowledgement = None
            self._frame_consumer_degraded = False
            self._terminal_failure_active = False
            self._last_lifecycle_sequence = 0
            self._last_lifecycle_digest = ""
            self._last_lifecycle_kind = None
            self._last_committed_frame_identity = None
            self._last_committed_frame_receipt = None
            self._last_late_child_status = {}
            self._startup_router_serial_submitted = False
            self._operational_outbox_enqueued = set()
            self._operational_outbox_foreign_reported = set()
            process = self._context.Process(
                target=self._worker_target,
                args=(
                    config,
                    worker_generation_identity,
                    bool(observe_only),
                    self._request_queue,
                    self._manual_nmi_queue,
                    self._status_queue,
                    self._lifecycle_queue,
                    self._lifecycle_acknowledgement_queue,
                    self._frame_queue,
                    self._frame_acknowledgement_queue,
                    self._process_stop,
                    self._wake,
                    parent_lifetime_reader,
                    trace_selected_at,
                ),
                name="54guc-cgf-runtime",
            )
            status_receiver = Thread(
                target=self._supervise_receiver,
                args=(
                    "ngui_runtime_status_receiver",
                    runtime_generation,
                    self._status_receiver_loop,
                    runtime_generation,
                    process,
                    self._status_queue,
                    self._receiver_stop,
                ),
                name="ngui-runtime-status-ipc",
                daemon=True,
            )
            lifecycle_receiver = Thread(
                target=self._supervise_receiver,
                args=(
                    "ngui_runtime_lifecycle_receiver",
                    runtime_generation,
                    self._lifecycle_receiver_loop,
                    runtime_generation,
                    process,
                    self._lifecycle_queue,
                    self._lifecycle_acknowledgement_queue,
                    self._receiver_stop,
                ),
                name="ngui-runtime-lifecycle-ipc",
                daemon=True,
            )
            frame_receiver = Thread(
                target=self._supervise_receiver,
                args=(
                    "ngui_runtime_frame_receiver",
                    runtime_generation,
                    self._frame_receiver_loop,
                    runtime_generation,
                    process,
                    self._frame_queue,
                    self._frame_acknowledgement_queue,
                    self._process_stop,
                    self._receiver_stop,
                ),
                name="ngui-runtime-frame-ipc",
                daemon=True,
            )
            self._process = process
            self._status_receiver_thread = status_receiver
            self._lifecycle_receiver_thread = lifecycle_receiver
            self._frame_receiver_thread = frame_receiver
            status = self._replace_status_unlocked(RuntimeServiceState.STARTING)
            self._child_status = status
            try:
                process.start()
            except BaseException:
                self._rollback_failed_worker_start_unlocked(
                    process=process,
                    parent_lifetime_reader=parent_lifetime_reader,
                    parent_lifetime_writer=parent_lifetime_writer,
                    receivers=(
                        status_receiver,
                        lifecycle_receiver,
                        frame_receiver,
                    ),
                )
                raise
            parent_lifetime_reader.close()
            try:
                status_receiver.start()
                lifecycle_receiver.start()
                frame_receiver.start()
            except BaseException:
                self._process_stop.set()
                self._wake.set()
                parent_lifetime_writer.close()
                self._parent_lifetime_writer = None
                self._receiver_stop.set()
                raise
            self._worker_health.mark_started("ngui_runtime_status_receiver")
            self._worker_health.mark_started("ngui_runtime_lifecycle_receiver")
            self._worker_health.mark_started("ngui_runtime_frame_receiver")
        self._on_status(status)

    def restart(
        self,
        config: Arch4Config,
        *,
        observe_only: bool,
        timeout_s: float = RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
        expected_reset_manifest_digest: str | None = None,
        trace_selected_at: float | None = None,
    ) -> None:
        """Replace the CGF child in one serialized lifecycle transaction."""

        self._replace_runtime(
            config, observe_only=observe_only, timeout_s=timeout_s,
            expected_reset_manifest_digest=expected_reset_manifest_digest,
            trace_selected_at=trace_selected_at,
        )

    def _replace_runtime(
        self,
        config: Arch4Config | None,
        *,
        observe_only: bool,
        timeout_s: float,
        expected_reset_manifest_digest: str | None,
        login_required_message: str = "Different router detected. Select its profile and sign in.",
        trace_selected_at: float | None = None,
    ) -> None:
        """One reset owner for restart and physical replacement to Sign In."""

        if expected_reset_manifest_digest is None:
            expected_manifest = runtime_reset_manifest_digest()
        else:
            if type(expected_reset_manifest_digest) is not str:
                raise TypeError(
                    "runtime_reset_manifest_digest_string_required"
                )
            expected_manifest = expected_reset_manifest_digest
            if expected_manifest != runtime_reset_manifest_digest():
                raise ValueError(
                    "runtime_reset_manifest_digest_mismatch"
                )
        with self._lifecycle_lock:
            with self._lock:
                if self._reset_pending is not None:
                    raise RuntimeError(
                        "runtime reset application already in flight"
                    )
                existing_process = self._process
                previous_worker_generation = self._runtime_generation
            if existing_process is not None:
                stop_receipt = self._stop_unlocked(timeout_s=timeout_s)
            else:
                stop_receipt = {"terminated": True}
            if stop_receipt.get("terminated") is not True:
                raise RuntimeError(
                    "active runtime exceeded the worker shutdown budget; "
                    "replacement runtime was not started"
                )
            with self._lock:
                product_shutdown = self._last_product_shutdown_receipt
            if existing_process is not None and (
                not isinstance(product_shutdown, ProductShutdownReceipt)
                or not product_shutdown.terminated
                or not product_shutdown.zero_survivor.complete
            ):
                raise RuntimeError(
                    "active Product zero-survivor proof missing; "
                    "replacement runtime was not started"
                )
            reset_cause = (
                RuntimeResetCause.INITIAL_WORKER
                if previous_worker_generation == 0
                else RuntimeResetCause.WORKER_REPLACEMENT
            )
            reset_pending = _RuntimeResetPending(
                previous_worker_generation=previous_worker_generation,
                successor_worker_generation=previous_worker_generation + 1,
                expected_manifest_digest=expected_manifest,
            )
            with self._lock:
                if (
                    self._process is not None
                    or self._runtime_generation
                    != previous_worker_generation
                ):
                    raise RuntimeError(
                        "runtime boundary changed before reset reservation"
                    )
                self._reset_pending = reset_pending
                self._reset_pending_cancelled = False
        runtime_generation = reset_pending.successor_worker_generation
        try:
            reset_receipt = self._reset_application.apply_runtime_reset(
                application_lease=self._application_lease,
                worker_generation=runtime_generation,
                previous_worker_generation=(
                    0
                    if reset_cause is RuntimeResetCause.INITIAL_WORKER
                    else previous_worker_generation
                ),
                cause=reset_cause,
                expected_manifest_digest=reset_pending.expected_manifest_digest,
            )
            worker_generation_identity = (
                RuntimeWorkerGenerationIdentity.from_reset_application(
                    application_lease=self._application_lease,
                    worker_generation=runtime_generation,
                    reset_receipt=reset_receipt,
                )
            )
            with self._lifecycle_lock:
                with self._lock:
                    if (
                        self._reset_pending is not reset_pending
                        or self._reset_pending_cancelled
                        or self._process is not None
                        or self._runtime_generation
                        != reset_pending.previous_worker_generation
                        or self._request_admission_open
                        or self._frame_admission_open
                    ):
                        raise RuntimeError(
                            "runtime reset reservation superseded before start"
                        )
                if config is None:
                    with self._lock:
                        self._runtime_generation = runtime_generation
                        self._config = None
                else:
                    self._start_unlocked(
                        config,
                        observe_only=observe_only,
                        worker_generation_identity=worker_generation_identity,
                        trace_selected_at=trace_selected_at,
                    )
                with self._lock:
                    if self._reset_pending is reset_pending:
                        self._reset_pending = None
                        self._reset_pending_cancelled = False
            if config is None:
                self._set_status(
                    RuntimeServiceState.LOGIN_REQUIRED,
                    login_required_message,
                )
        except Exception:
            with self._lock:
                if self._reset_pending is reset_pending:
                    self._reset_pending = None
                    self._reset_pending_cancelled = False
            raise

    def stop(
        self,
        *,
        timeout_s: float = RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
    ) -> Mapping[str, Any]:
        with self._lifecycle_lock:
            receipt = self._stop_unlocked(timeout_s=timeout_s)
            with self._lock:
                if self._reset_pending is not None:
                    self._reset_pending_cancelled = True
            return receipt

    def _stop_unlocked(self, *, timeout_s: float) -> dict[str, Any]:
        started_at = time()
        started_mono = monotonic()
        operational_pending_at_stop = (
            self._operational_outbox.pending_count()
        )
        with self._lock:
            process = self._process
            status_receiver = self._status_receiver_thread
            lifecycle_receiver = self._lifecycle_receiver_thread
            frame_receiver = self._frame_receiver_thread
            process_stop = self._process_stop
            wake = self._wake
            parent_lifetime_writer = self._parent_lifetime_writer
            self._parent_lifetime_writer = None
            receiver_stop = self._receiver_stop
            self._request_admission_open = False
            self._request_admission_closed_by_shutdown = True
            self._frame_admission_open = False
            if process is None:
                status = self._replace_status_unlocked(RuntimeServiceState.STOPPED)
                self._config = None
                no_process = True
                prior_receipt = dict(self._last_stop_receipt)
            else:
                status = self._replace_status_unlocked(RuntimeServiceState.STOPPING)
                no_process = False
                prior_receipt = {}
        self._on_status(status)
        if no_process:
            if parent_lifetime_writer is not None:
                parent_lifetime_writer.close()
            if (
                prior_receipt.get("finished_at")
                and int(
                    prior_receipt.get(
                        "operational_pending_after_stop",
                        -1,
                    )
                )
                == self._operational_outbox.pending_count()
            ):
                return prior_receipt
            pending_after_stop = self._operational_outbox.pending_count()
            receipt = {
                "schema": "RuntimeProcessTerminationReceipt.v1",
                "owner": "arch4.ngui.runtime_service",
                "started_at": started_at,
                "finished_at": time(),
                "duration_s": round(max(0.0, monotonic() - started_mono), 3),
                "status": (
                    "complete"
                    if pending_after_stop == 0
                    else "degraded"
                ),
                "terminal_disposition": (
                    "not_running"
                    if pending_after_stop == 0
                    else "not_running_with_pending_operational_outbox"
                ),
                "graceful": True,
                "termination_requested": False,
                "terminated": True,
                "pid": 0,
                "exit_code": None,
                "product_shutdown_receipt": {},
                "operational_pending_at_stop": operational_pending_at_stop,
                "operational_pending_after_stop": pending_after_stop,
            }
            with self._lock:
                self._last_stop_receipt = dict(receipt)
            return receipt
        assert process is not None
        pid = int(getattr(process, "pid", 0) or 0)
        pending_rejection: tuple[
            Any,
            RuntimeLifecycleEnvelope,
        ] | None = None
        with self._lock:
            if (
                self._pending_lifecycle_envelope is not None
                and self._pending_lifecycle_acknowledgement_queue is not None
            ):
                pending_rejection = (
                    self._pending_lifecycle_acknowledgement_queue,
                    self._pending_lifecycle_envelope,
                )
                self._clear_pending_lifecycle_unlocked()
        if pending_rejection is not None:
            acknowledgement_queue, pending_envelope = pending_rejection
            self._publish_lifecycle_acknowledgement(
                acknowledgement_queue,
                pending_envelope,
                accepted=False,
                reason="runtime_lifecycle_parent_stopping",
            )
        if process_stop is not None:
            process_stop.set()
        if wake is not None:
            wake.set()
        if parent_lifetime_writer is not None:
            parent_lifetime_writer.close()
        process.join(timeout=max(0.0, timeout_s))
        graceful = not process.is_alive()
        termination_requested = not graceful
        if not graceful:
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
        terminated = not process.is_alive()
        # Let both receivers consume final child events before closing IPC. If
        # receiver_stop is set first, a queued Product shutdown receipt can be
        # lost.
        for receiver in (
            status_receiver,
            lifecycle_receiver,
            frame_receiver,
        ):
            if receiver is not None and receiver is not current_thread():
                receiver.join(timeout=1.0)
        if receiver_stop is not None:
            receiver_stop.set()
        for receiver in (
            status_receiver,
            lifecycle_receiver,
            frame_receiver,
        ):
            if (
                receiver is not None
                and receiver is not current_thread()
                and receiver.is_alive()
            ):
                receiver.join(timeout=0.2)
        receiver_survivors = tuple(
            receiver.name
            for receiver in (
                status_receiver,
                lifecycle_receiver,
                frame_receiver,
            )
            if receiver is not None
            and receiver is not current_thread()
            and receiver.is_alive()
        )
        receivers_terminated = not receiver_survivors
        boundary_terminated = bool(terminated and receivers_terminated)
        with self._lock:
            product_receipt = self._last_product_shutdown_receipt
            child_status = self._child_status
        if receivers_terminated:
            self._close_queues()
        if graceful and receivers_terminated:
            terminal_disposition = "graceful_exit"
        elif boundary_terminated:
            terminal_disposition = "forced_termination"
        elif terminated and receiver_survivors:
            terminal_disposition = "receiver_survivor"
        else:
            terminal_disposition = "termination_failed"
        exit_code = getattr(process, "exitcode", None)
        product_receipt_received = isinstance(
            product_receipt,
            ProductShutdownReceipt,
        )
        product_shutdown_clean = bool(
            product_receipt is not None and product_receipt.terminated
        )
        child_exit_clean = exit_code == 0
        operational_pending_after_stop = (
            self._operational_outbox.pending_count()
        )
        receipt_status = (
            "complete"
            if terminal_disposition == "graceful_exit"
            and product_shutdown_clean
            and child_exit_clean
            and boundary_terminated
            and operational_pending_after_stop == 0
            else "degraded"
        )
        stop_receipt = {
            "schema": "RuntimeProcessTerminationReceipt.v1",
            "owner": "arch4.ngui.runtime_service",
            "started_at": started_at,
            "finished_at": time(),
            "duration_s": round(max(0.0, monotonic() - started_mono), 3),
            "status": receipt_status,
            "terminal_disposition": terminal_disposition,
            "graceful": graceful,
            "termination_requested": termination_requested,
            "terminated": boundary_terminated,
            "process_terminated": terminated,
            "receivers_terminated": receivers_terminated,
            "receiver_survivors": receiver_survivors,
            "pid": pid,
            "exit_code": exit_code,
            "product_shutdown_receipt": (
                product_receipt.as_dict()
                if product_receipt is not None
                else {}
            ),
            "product_shutdown_clean": product_shutdown_clean,
            "child_exit_clean": child_exit_clean,
            "last_child_status": {
                "state": child_status.state.value,
                "message": child_status.message,
                "cycle_count": child_status.cycle_count,
                "changed_at": child_status.changed_at,
            },
            "operational_pending_at_stop": operational_pending_at_stop,
            "operational_pending_after_stop": (
                operational_pending_after_stop
            ),
        }
        with self._lock:
            if boundary_terminated:
                self._active_runtime_generation = 0
                self._active_worker_identity = None
                self._active_product_identity = None
                self._product_root_admission = None
                self._active_namespace_identity = None
                self._process = None
                self._status_receiver_thread = None
                self._lifecycle_receiver_thread = None
                self._frame_receiver_thread = None
                self._process_stop = None
                self._wake = None
                self._parent_lifetime_writer = None
                self._receiver_stop = None
                self._request_queue = None
                self._manual_nmi_queue = None
                self._status_queue = None
                self._lifecycle_queue = None
                self._lifecycle_acknowledgement_queue = None
                self._frame_queue = None
                self._frame_acknowledgement_queue = None
                self._config = None
                self._core_command_contexts.clear()
                self._active_matrix_identity = None
                self._operational_outbox_enqueued = set()
                self._operational_outbox_foreign_reported = set()
            self._last_stop_receipt = dict(stop_receipt)
            if not boundary_terminated:
                final_status = self._replace_status_unlocked(
                    RuntimeServiceState.FAILED,
                    (
                        "CGF runtime boundary remained active after stop: "
                        + ",".join(receiver_survivors)
                        if receiver_survivors
                        else "CGF process remained alive after termination"
                    ),
                    self._status.cycle_count,
                )
            elif (
                receipt_status == "complete"
                and self._status.state is not RuntimeServiceState.FAILED
            ):
                final_status = self._replace_status_unlocked(
                    RuntimeServiceState.STOPPED,
                    (
                        ""
                        if graceful
                        else "CGF process required forced termination"
                    ),
                    self._status.cycle_count,
                )
            else:
                final_status = self._replace_status_unlocked(
                    RuntimeServiceState.FAILED,
                    (
                        "CGF runtime teardown was not clean: "
                        f"disposition={terminal_disposition};"
                        f"exit_code={exit_code};"
                        "product_disposition="
                        + (
                            product_receipt.disposition.value
                            if product_receipt is not None
                            else "missing"
                        )
                    ),
                    self._status.cycle_count,
                )
        self._on_status(final_status)
        return dict(stop_receipt)

    def status(self) -> RuntimeServiceStatus:
        with self._lock:
            return self._status

    def prepare_support_history(self, cutoff: float, history_path: Path, *, timeout_s: float = 60.) -> None:
        """Bound the existing child's History drain; never open another writer."""
        if not math.isfinite(cutoff) or cutoff <= 0 or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("support_history_request_bounds_invalid")
        selected_path = str(history_path.resolve())
        with self._lock:
            if self._process is None and (
                self._last_stop_receipt.get("product_shutdown_clean") is True
                or (self._active_runtime_generation == 0 and self._status.state is RuntimeServiceState.IDLE)
            ):
                return  # Explicit stopped/not-started owner: no pending live writes.
            identity = self._required_namespace_identity_unlocked()
            reader, writer = self._context.Pipe(duplex=False)
            try:
                request = RuntimeSupportHistoryFlushRequest(identity, selected_path,
                    cutoff, monotonic() + timeout_s, writer)
                self._put_unlocked(self._request_queue, request)
            except Exception:
                reader.close()
                writer.close()
                raise
        try:
            if not reader.poll(timeout_s):
                raise TimeoutError("support_history_worker_timeout")
            reply = reader.recv()
            with self._lock:
                if self._required_namespace_identity_unlocked().product != identity.product:
                    raise RuntimeError("support_history_worker_replaced")
            if not isinstance(reply, dict) or reply.get("identity") != identity:
                raise RuntimeError("support_history_reply_identity_mismatch")
            if reply.get("error"):
                raise RuntimeError("support_history_preparation_failed:" + reply["error"])
            if reply.get("complete") is not True:
                raise RuntimeError("support_history_preparation_incomplete")
        finally:
            reader.close()
            writer.close()

    def submit_startup_router_serial(self, serial: str) -> None:
        submission = RuntimeStartupRouterSerial(serial)
        with self._lock:
            if self._status.state is not RuntimeServiceState.IDENTITY_REQUIRED:
                raise RuntimeError("router serial is not currently required")
            if self._startup_router_serial_submitted:
                raise RuntimeError("router serial has already been submitted")
            if self._process is None or not self._process.is_alive():
                raise RuntimeError("CGF runtime is unavailable")
            self._put_unlocked(self._request_queue, submission)
            self._startup_router_serial_submitted = True
            if self._wake is not None:
                self._wake.set()

    def close_operational_admission(self) -> int:
        with self._lock:
            self._request_admission_closed_by_shutdown = True
            self._request_admission_open = False
            return self._operational_outbox.pending_count()

    def operational_pending_count(self) -> int:
        return self._operational_outbox.pending_count()

    def active_runtime_generation(self) -> int:
        with self._lock:
            return int(self._active_runtime_generation)

    def frame_delivery_status(self) -> Mapping[str, Any]:
        with self._lock:
            return dict(self._last_frame_delivery)

    def claim_lifecycle_event(self) -> RuntimeLifecycleClaim | None:
        """Claim the event waiting for NGUI without consuming it."""

        with self._lock:
            envelope = self._pending_lifecycle_envelope
            generation = self._pending_lifecycle_generation
            if envelope is None or self._pending_lifecycle_claimed:
                return None
            if generation != self._active_runtime_generation:
                raise RuntimeError(
                    "runtime lifecycle claim belongs to retired generation"
                )
            self._pending_lifecycle_claimed = True
            return RuntimeLifecycleClaim(
                generation_identity=envelope.event.generation_identity,
                sequence=envelope.sequence,
                event_kind=envelope.event.kind,
                event_digest=envelope.event_digest,
                event=envelope.event,
            )

    def commit_lifecycle_event(
        self,
        claim: RuntimeLifecycleClaim,
        receipt: RuntimeLifecycleApplicationReceipt,
    ) -> None:
        """Acknowledge the child only after the event is visible."""

        if not isinstance(claim, RuntimeLifecycleClaim):
            raise TypeError("runtime lifecycle commit requires typed claim")
        if not isinstance(receipt, RuntimeLifecycleApplicationReceipt):
            raise TypeError(
                "runtime lifecycle commit requires application receipt"
            )
        with self._lock:
            envelope = self._require_pending_lifecycle_claim_unlocked(claim)
            if (
                receipt.generation_identity != claim.generation_identity
                or receipt.sequence != claim.sequence
                or receipt.event_kind is not claim.event_kind
                or receipt.event_digest != claim.event_digest
            ):
                raise RuntimeError(
                    "runtime lifecycle application receipt identity mismatch"
                )
            acknowledgement_queue = (
                self._pending_lifecycle_acknowledgement_queue
            )
            if envelope.event.kind in {
                RuntimeEventKind.OPERATIONAL_RECEIPT,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
            }:
                payload = envelope.event.payload
                if not isinstance(payload, OperationalServiceReceipt):
                    raise TypeError(
                        "typed operational receipt payload required"
                    )
                if (
                    envelope.event.kind
                    is RuntimeEventKind.OPERATIONAL_RECEIPT
                ):
                    self._operational_outbox.acknowledge(payload)
                else:
                    self._operational_outbox.confirm_product_acknowledgement(
                        payload
                    )
                    self._operational_outbox_enqueued.discard(
                        payload.request_id
                    )
            self._last_lifecycle_sequence = envelope.sequence
            self._last_lifecycle_digest = envelope.event_digest
            self._last_lifecycle_kind = envelope.event.kind
            self._last_lifecycle_application = receipt
            acknowledgement = RuntimeLifecycleAcknowledgement(
                sequence=envelope.sequence,
                event_kind=envelope.event.kind,
                generation_identity=envelope.event.generation_identity,
                event_digest=envelope.event_digest,
                accepted=True,
                application_receipt=receipt,
            )
            self._last_lifecycle_acknowledgement = acknowledgement
            self._clear_pending_lifecycle_unlocked()
        if acknowledgement_queue is None:
            raise RuntimeError(
                "runtime lifecycle acknowledgement queue unavailable"
            )
        self._worker_health.mark_success(
            "ngui_runtime_lifecycle_receiver"
        )
        self._put_lifecycle_acknowledgement(
            acknowledgement_queue,
            acknowledgement,
        )

    def reject_lifecycle_event(
        self,
        claim: RuntimeLifecycleClaim,
        *,
        reason: str,
    ) -> None:
        """Return an invalid application to its source owner."""

        rejection = str(reason or "").strip()
        if not rejection:
            raise ValueError("runtime lifecycle rejection requires reason")
        with self._lock:
            envelope = self._require_pending_lifecycle_claim_unlocked(claim)
            acknowledgement_queue = (
                self._pending_lifecycle_acknowledgement_queue
            )
            acknowledgement = RuntimeLifecycleAcknowledgement(
                sequence=envelope.sequence,
                event_kind=envelope.event.kind,
                generation_identity=envelope.event.generation_identity,
                event_digest=envelope.event_digest,
                accepted=False,
                reason=rejection,
            )
            self._last_lifecycle_sequence = envelope.sequence
            self._last_lifecycle_digest = envelope.event_digest
            self._last_lifecycle_kind = envelope.event.kind
            self._last_lifecycle_acknowledgement = acknowledgement
            if envelope.event.kind in {
                RuntimeEventKind.OPERATIONAL_RECEIPT,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
            }:
                payload = envelope.event.payload
                if isinstance(payload, OperationalServiceReceipt):
                    self._operational_outbox_enqueued.discard(
                        payload.request_id
                    )
            self._clear_pending_lifecycle_unlocked()
        if acknowledgement_queue is None:
            raise RuntimeError(
                "runtime lifecycle acknowledgement queue unavailable"
            )
        self._put_lifecycle_acknowledgement(
            acknowledgement_queue,
            acknowledgement,
        )

    def _require_pending_lifecycle_claim_unlocked(
        self,
        claim: RuntimeLifecycleClaim,
    ) -> RuntimeLifecycleEnvelope:
        envelope = self._pending_lifecycle_envelope
        payload_identity_valid = bool(
            envelope is not None
            and runtime_ipc_event_digest(envelope.event)
            == envelope.event_digest
            and runtime_ipc_event_digest(claim.event)
            == claim.event_digest
        )
        if (
            envelope is None
            or not payload_identity_valid
            or not self._pending_lifecycle_claimed
            or self._pending_lifecycle_generation
            != claim.runtime_generation
            or envelope.sequence != claim.sequence
            or envelope.event.kind is not claim.event_kind
            or envelope.event.generation_identity
            != claim.generation_identity
            or envelope.event_digest != claim.event_digest
        ):
            raise RuntimeError("runtime lifecycle claim is not active")
        return envelope

    def _clear_pending_lifecycle_unlocked(self) -> None:
        self._pending_lifecycle_envelope = None
        self._pending_lifecycle_generation = 0
        self._pending_lifecycle_claimed = False
        self._pending_lifecycle_acknowledgement_queue = None

    def _core_operation_context_unlocked(
        self,
        *,
        sequence: int,
        requested_at_monotonic: float,
        source: str,
        freshness: CoreOperationalFreshness,
    ) -> CoreOperatorRequestContext:
        self._require_running_config_unlocked()
        ingress = self._latest_core_command_context_unlocked()
        received_at = monotonic()
        created_at = time() - max(
            0.0,
            received_at - float(requested_at_monotonic),
        )
        return CoreOperatorRequestContext(
            request_id=f"ngui-core-operation-{sequence}-{int(created_at * 1000)}",
            created_at=created_at,
            expires_at=created_at + 300.0,
            ingress=ingress,
            freshness=freshness,
            source=source,
        )

    def _latest_core_command_context_unlocked(self) -> CoreCommandIngressContext:
        if not self._core_command_contexts:
            raise RuntimeError("core operational context is not available")
        identity = next(reversed(self._core_command_contexts))
        return self._core_command_contexts[identity]

    def _exact_core_command_context_unlocked(
        self,
        matrix_identity: MatrixGenerationIdentity,
    ) -> CoreCommandIngressContext:
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("manual command Matrix identity required")
        context = self._core_command_contexts.get(matrix_identity)
        if context is None:
            raise RuntimeError("manual command Matrix identity is not retained")
        return context

    def _retain_core_command_context_unlocked(
        self,
        context: CoreCommandIngressContext,
    ) -> None:
        if not isinstance(context, CoreCommandIngressContext):
            raise TypeError("core command context required")
        identity = context.matrix_identity
        existing = self._core_command_contexts.get(identity)
        if existing is not None and existing != context:
            raise RuntimeError("core command context identity collision")
        self._core_command_contexts[identity] = context
        self._core_command_contexts.move_to_end(identity)
        while len(self._core_command_contexts) > CORE_COMMAND_CONTEXT_RETENTION:
            self._core_command_contexts.popitem(last=False)

    def _foundation_operation_context_unlocked(
        self,
        *,
        sequence: int,
        requested_at_monotonic: float,
        source: str,
    ) -> FoundationOperatorRequestContext:
        self._require_running_config_unlocked()
        received_at = monotonic()
        created_at = time() - max(
            0.0,
            received_at - float(requested_at_monotonic),
        )
        generation_identity = self._required_namespace_identity_unlocked()
        return FoundationOperatorRequestContext(
            request_id=f"ngui-foundation-operation-{sequence}-{int(created_at * 1000)}",
            created_at=created_at,
            runtime_namespace=generation_identity.namespace,
            matrix_identity=generation_identity.matrix_identity,
            source=source,
        )

    def _product_operation_context_unlocked(
        self,
        *,
        sequence: int,
        requested_at_monotonic: float,
        source: str,
    ) -> ProductOperatorRequestContext:
        self._require_running_config_unlocked()
        received_at = monotonic()
        created_at = time() - max(
            0.0,
            received_at - float(requested_at_monotonic),
        )
        generation_identity = self._required_namespace_identity_unlocked()
        return ProductOperatorRequestContext(
            request_id=(
                f"ngui-product-operation-{sequence}-{int(created_at * 1000)}"
            ),
            created_at=created_at,
            runtime_namespace=generation_identity.namespace,
            matrix_identity=generation_identity.matrix_identity,
            source=source,
        )

    def _submit_operational_unlocked(
        self,
        *,
        kind: RuntimeRequestKind,
        sequence: int,
        requested_at_monotonic: float,
        request: object,
    ) -> OperationalRequestDelivery:
        self._require_running_config_unlocked()
        ipc = RuntimeIPCRequest(
            kind=kind,
            generation_identity=self._required_namespace_identity_unlocked(),
            sequence=sequence,
            created_at=float(requested_at_monotonic),
            payload=request,
        )
        entry = self._operational_outbox.admit(ipc)
        self._put_unlocked(self._request_queue, ipc)
        self._operational_outbox_enqueued.add(entry.request_id)
        self._wake.set()
        return OperationalRequestDelivery(
            sequence=sequence,
            kind=kind,
            request=request,
            created_at=float(requested_at_monotonic),
        )

    def _redeliver_operational_outbox_unlocked(self) -> None:
        identity = self._required_namespace_identity_unlocked()
        enqueued = False
        for ipc in self._operational_outbox.pending_for(identity):
            request_id = ipc.payload.context.request_id
            if request_id in self._operational_outbox_enqueued:
                continue
            self._put_unlocked(self._request_queue, ipc)
            self._operational_outbox_enqueued.add(request_id)
            enqueued = True
        foreign = set(
            self._operational_outbox.foreign_request_ids(identity)
        )
        unreported = foreign.difference(
            self._operational_outbox_foreign_reported
        )
        if unreported:
            error = RuntimeError(
                "ngui_operational_outbox_foreign_generation:"
                + ",".join(sorted(unreported))
            )
            self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner="ngui_operational_outbox",
                operation="generation_reconciliation",
                generation=identity.matrix_identity.matrix_sequence,
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior=(
                    "retain_unresolved_request_and_refuse_foreign_execution"
                ),
                error=error,
                suppression_budget=0,
            )
            self._operational_outbox_foreign_reported.update(unreported)
        if enqueued:
            self._wake.set()

    def submit_read_diagnostics(self, *, action: str, created_at: float) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._core_operation_context_unlocked(
                sequence=sequence, requested_at_monotonic=created_at,
                source="ngui_read_diagnostics",
                freshness=CoreOperationalFreshness.CURRENT_OBSERVATION_DIAGNOSTIC)
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.CORE_READ_DIAGNOSTICS,
                sequence=sequence, requested_at_monotonic=created_at,
                request=RouterReadDiagnosticRequest(context, action))

    def submit_network_settings_inspection(self, *, created_at: float) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._core_operation_context_unlocked(
                sequence=sequence, requested_at_monotonic=created_at,
                source="ngui_network_inspection",
                freshness=CoreOperationalFreshness.CURRENT_OBSERVATION_DIAGNOSTIC,
            )
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.CORE_NETWORK_INSPECTION,
                sequence=sequence, requested_at_monotonic=created_at,
                request=NetworkSettingsInspectionRequest(context),
            )

    def submit_wifi_radio(
        self,
        *,
        radio: str,
        enabled: bool,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._core_operation_context_unlocked(
                sequence=sequence,
                requested_at_monotonic=created_at,
                source="ngui_device_wifi",
                freshness=CoreOperationalFreshness.PERSISTENT_CONFIGURATION,
            )
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.CORE_WIFI_CONTROL,
                sequence=sequence,
                requested_at_monotonic=created_at,
                request=WifiRadioControlRequest(context, radio, enabled),
            )

    def submit_downstream_health(
        self,
        *,
        enabled: bool,
        target: str,
        ping_count: int,
        interval_s: float,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._core_operation_context_unlocked(sequence=sequence, requested_at_monotonic=created_at, source="ngui_downstream_diagnostics", freshness=CoreOperationalFreshness.PERSISTENT_CONFIGURATION)
            request = DownstreamHealthConfigRequest(context, enabled, target=target, ping_count=ping_count, interval_s=interval_s)
            return self._submit_operational_unlocked(kind=RuntimeRequestKind.CORE_DOWNSTREAM_DIAGNOSTIC, sequence=sequence, requested_at_monotonic=created_at, request=request)

    def submit_sms_command_control(
        self,
        *,
        enabled: bool,
        approved_senders: tuple[str, ...],
        code_word: str,
        poll_interval_s: float,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(sequence=sequence, requested_at_monotonic=created_at, source="ngui_sms_command_control")
            request = SmsCommandControlConfigRequest(context, enabled, code_word, approved_senders, poll_interval_s)
            return self._submit_operational_unlocked(kind=RuntimeRequestKind.FOUNDATION_SMS_CONTROL, sequence=sequence, requested_at_monotonic=created_at, request=request)

    def submit_intelligence_autopilot(
        self,
        *,
        enabled: bool,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._product_operation_context_unlocked(
                sequence=sequence,
                requested_at_monotonic=created_at,
                source="ngui_developer_autopilot",
            )
            request = IntelligenceAutopilotControlRequest(context, enabled)
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.PRODUCT_INTELLIGENCE_AUTOPILOT,
                sequence=sequence,
                requested_at_monotonic=created_at,
                request=request,
            )

    def submit_intelligence_explore(
        self,
        *,
        enabled: bool,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._product_operation_context_unlocked(
                sequence=sequence,
                requested_at_monotonic=created_at,
                source="ngui_developer_explore",
            )
            request = IntelligenceExploreControlRequest(context, enabled)
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.PRODUCT_INTELLIGENCE_EXPLORE,
                sequence=sequence,
                requested_at_monotonic=created_at,
                request=request,
            )

    def submit_band_schedule_plan(
        self,
        *,
        bands: tuple[int, ...],
        operation: str,
        activate_at: str,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(
                sequence=sequence,
                requested_at_monotonic=created_at,
                source="ngui_band_schedule",
            )
            request = BandSchedulePlanRequest(
                context,
                bands=bands,
                operation=operation,
                activate_at=activate_at,
            )
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_PLAN,
                sequence=sequence,
                requested_at_monotonic=created_at,
                request=request,
            )

    def submit_band_sequence_plan(self, *, bands: tuple[int, ...], interval_s: int, created_at: float) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(sequence=sequence, requested_at_monotonic=created_at, source="ngui_band_sequence")
            request = BandSequencePlanRequest(context, bands=bands, interval_s=interval_s)
            return self._submit_operational_unlocked(kind=RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_PLAN, sequence=sequence, requested_at_monotonic=created_at, request=request)

    def submit_band_schedule_cancel(self, *, created_at: float) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(sequence=sequence, requested_at_monotonic=created_at, source="ngui_band_schedule")
            return self._submit_operational_unlocked(kind=RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_CANCEL, sequence=sequence, requested_at_monotonic=created_at, request=BandScheduleCancelRequest(context))

    def submit_band_sequence_cancel(self, *, created_at: float) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(sequence=sequence, requested_at_monotonic=created_at, source="ngui_band_sequence")
            return self._submit_operational_unlocked(kind=RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_CANCEL, sequence=sequence, requested_at_monotonic=created_at, request=BandSequenceCancelRequest(context))

    def submit_antenna_guide(
        self,
        *,
        action: str,
        created_at: float,
    ) -> OperationalRequestDelivery:
        with self._lock:
            sequence = self._next_sequence_unlocked()
            context = self._foundation_operation_context_unlocked(
                sequence=sequence,
                requested_at_monotonic=created_at,
                source="ngui_antenna_guide",
            )
            return self._submit_operational_unlocked(
                kind=RuntimeRequestKind.FOUNDATION_ANTENNA_GUIDE,
                sequence=sequence,
                requested_at_monotonic=created_at,
                request=AntennaGuideControlRequest(context, action),
            )

    def submit_manual_action(
        self,
        action: LeverAction,
        config: Arch4Config,
        *,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> Mapping[str, Any]:
        """Publish a complete Core command request to the child NMI lane."""
        del config
        if created_at <= 0:
            raise ValueError("manual action timestamp must be positive")
        parent_received_at = monotonic()
        with self._lock:
            self._require_running_config_unlocked()
            context = self._exact_core_command_context_unlocked(matrix_identity)
            sequence = self._next_sequence_unlocked()
            requested_at = time() - max(0.0, parent_received_at - created_at)
            correlation_id = str(getattr(action, "_gui_action_id", "") or "").strip()
            if not correlation_id:
                raise RuntimeError("ngui_manual_action_correlation_required")
            target = build_core_command_target(
                action.lever.name,
                dict(action.params or {}),
            )
            target_keys = set(target.to_mapping()) - {"lever"}
            extra_params = {
                str(key): value
                for key, value in dict(action.params or {}).items()
                if key not in target_keys
            }
            command_request = build_core_command_request(
                context,
                request_id=f"ngui-command-{sequence}-{int(requested_at * 1000)}",
                correlation_id=correlation_id,
                client_id="ngui.operator",
                client_role=CoreClientRole.OPERATOR,
                origin=CoreCommandOrigin.OPERATOR,
                command_kind=action.lever.name,
                target=target,
                parameters={
                    "reason": str(action.reason or "manual command"),
                    "intent": str(action.intent or "manual"),
                    "objective_id": str(action.objective_id or correlation_id),
                    "action_parameters": extra_params,
                },
                capability_reference=lever_action_authority(
                    action.lever
                ).capability_key,
                created_at=requested_at,
                expires_at=requested_at + 300.0,
                confirmed=True,
            )
            self._submit_manual_command_unlocked(
                command_request=command_request,
                sequence=sequence,
                created_at=created_at,
                parent_received_at=parent_received_at,
            )
            parent_enqueued_at = monotonic()
        return {
            "submitted": True,
            "reason": "submitted_to_core_process_nmi",
            "sequence": sequence,
            "confirm_to_app_enqueue_ms": round(
                max(0.0, parent_enqueued_at - created_at) * 1000.0,
                3,
            ),
        }

    def submit_band_auto(
        self,
        *,
        domain: str,
        correlation_id: str,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> Mapping[str, Any]:
        """Publish semantic AUTO. Core resolves the physical band target."""

        domain_value = str(domain or "").strip().lower()
        correlation_value = str(correlation_id or "").strip()
        if domain_value not in {"lte_allocated_auto", "nr_approved_auto"}:
            raise ValueError("band auto domain is invalid")
        if not correlation_value:
            raise ValueError("band auto correlation is required")
        if created_at <= 0:
            raise ValueError("band auto timestamp must be positive")
        parent_received_at = monotonic()
        with self._lock:
            self._require_running_config_unlocked()
            context = self._exact_core_command_context_unlocked(matrix_identity)
            sequence = self._next_sequence_unlocked()
            requested_at = time() - max(0.0, parent_received_at - created_at)
            command_request = build_core_command_request(
                context,
                request_id=f"ngui-command-{sequence}-{int(requested_at * 1000)}",
                correlation_id=correlation_value,
                client_id="ngui.operator",
                client_role=CoreClientRole.OPERATOR,
                origin=CoreCommandOrigin.OPERATOR,
                command_kind=BAND_AUTO_COMMAND_KIND,
                target=CoreBandAutoTarget(domain=domain_value),
                parameters={
                    "reason": "Manual band AUTO restore",
                    "intent": "manual",
                    "objective_id": correlation_value,
                    "action_parameters": {},
                },
                capability_reference=lever_action_authority(
                    LeverType.NR_BAND_LOCK if domain_value == "nr_approved_auto" else LeverType.BAND_SELECTION
                ).capability_key,
                created_at=requested_at,
                expires_at=requested_at + 300.0,
                confirmed=True,
            )
            self._submit_manual_command_unlocked(
                command_request=command_request,
                sequence=sequence,
                created_at=created_at,
                parent_received_at=parent_received_at,
            )
            parent_enqueued_at = monotonic()
        return {
            "submitted": True,
            "reason": "submitted_to_core_process_nmi",
            "sequence": sequence,
            "confirm_to_app_enqueue_ms": round(
                max(0.0, parent_enqueued_at - created_at) * 1000.0,
                3,
            ),
        }

    def _submit_manual_command_unlocked(
        self,
        *,
        command_request: CoreCommandRequest,
        sequence: int,
        created_at: float,
        parent_received_at: float,
    ) -> None:
        # A fresh sample must not change what the user confirmed. Keep the
        # current worker authority, but carry the click's exact Matrix in both
        # envelope and payload. The identity constructor rejects a new session.
        active = self._required_namespace_identity_unlocked()
        generation = RuntimeNamespaceGenerationIdentity(
            product=active.product,
            matrix_identity=command_request.matrix_identity,
        )
        request = RuntimeIPCRequest(
            kind=RuntimeRequestKind.MANUAL_NMI,
            generation_identity=generation,
            sequence=sequence,
            created_at=created_at,
            payload=RuntimeManualCommandRequest(
                command_request=command_request,
                parent_received_at=parent_received_at,
            ),
        )
        self._put_unlocked(self._manual_nmi_queue, request)

    def _status_receiver_loop(
        self,
        runtime_generation: int,
        process: Any,
        status_queue: Any,
        stop_event: Any,
    ) -> None:
        while not stop_event.is_set():
            if not self._receiver_generation_is_active(runtime_generation):
                return
            try:
                event = status_queue.get(timeout=0.1)
            except Empty:
                if not process.is_alive():
                    state = self.status().state
                    if state not in {
                        RuntimeServiceState.FAILED,
                        RuntimeServiceState.STOPPED,
                        RuntimeServiceState.STOPPING,
                    }:
                        self._set_terminal_failure(
                            RuntimeServiceState.FAILED,
                            f"CGF process exited with code {process.exitcode}",
                        )
                    return
                continue
            if (
                not isinstance(event, RuntimeIPCEvent)
                or event.kind is not RuntimeEventKind.STATUS
                or event.scope is not RuntimeEventScope.APPLICATION_GLOBAL
                or event.matrix_identity is not None
                or not isinstance(event.payload, RuntimeWorkerStatusReceipt)
            ):
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "invalid CGF status IPC event",
                )
                continue
            self._worker_health.mark_success("ngui_runtime_status_receiver")
            with self._lock:
                active_worker = self._active_worker_identity
            identity = event.generation_identity
            identity_matches = bool(
                active_worker is not None
                and (
                    identity == active_worker
                    or (
                        isinstance(
                            identity,
                            RuntimeProductGenerationIdentity,
                        )
                        and identity.worker == active_worker
                    )
                )
            )
            if not identity_matches:
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "CGF status generation identity mismatch",
                )
                continue
            payload = event.payload
            try:
                state = RuntimeServiceState(payload.state.value)
                product_root = None
                if isinstance(identity, RuntimeProductGenerationIdentity):
                    with self._lock:
                        admission_authority = self._product_root_admission
                    if admission_authority is None:
                        raise RuntimeError(
                            "CGF Product root admission authority unavailable"
                        )
                    product_root = admission_authority.require_product(
                        identity
                    ).product_root
                child_status = RuntimeServiceStatus(
                    state=state,
                    changed_at=payload.changed_at,
                    message=payload.message,
                    cycle_count=payload.cycle_count,
                    product_startup_receipt=(
                        product_root
                        if (
                            state is RuntimeServiceState.RUNNING
                            and isinstance(
                                identity,
                                RuntimeProductGenerationIdentity,
                            )
                        )
                        else None
                    ),
                )
            except Exception as exc:
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    f"invalid CGF status contract: {type(exc).__name__}: {exc}",
                )
                continue
            if isinstance(identity, RuntimeProductGenerationIdentity):
                with self._lock:
                    if self._active_worker_identity != identity.worker:
                        return
                    current_product = self._active_product_identity
                    if (
                        current_product is not None
                        and current_product != identity
                    ):
                        self._set_terminal_failure(
                            RuntimeServiceState.FAILED,
                            "CGF Product generation identity changed",
                        )
                        continue
                    self._active_product_identity = identity
            self._apply_child_status(runtime_generation, child_status)

    def _lifecycle_receiver_loop(
        self,
        runtime_generation: int,
        process: Any,
        lifecycle_queue: Any,
        acknowledgement_queue: Any,
        stop_event: Any,
    ) -> None:
        while not stop_event.is_set():
            if not self._receiver_generation_is_active(runtime_generation):
                return
            try:
                envelope = lifecycle_queue.get(timeout=0.1)
            except Empty:
                if not process.is_alive():
                    return
                continue
            if not isinstance(envelope, RuntimeLifecycleEnvelope):
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "invalid CGF lifecycle envelope",
                )
                continue
            if runtime_ipc_event_digest(envelope.event) != envelope.event_digest:
                self._publish_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    envelope,
                    accepted=False,
                    reason="runtime_lifecycle_event_digest_mismatch",
                )
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "runtime lifecycle event digest mismatch",
                )
                continue
            with self._lock:
                last_sequence = self._last_lifecycle_sequence
                last_digest = self._last_lifecycle_digest
                last_kind = self._last_lifecycle_kind
                last_acknowledgement = (
                    self._last_lifecycle_acknowledgement
                )
            if envelope.sequence == last_sequence:
                if (
                    envelope.event_digest == last_digest
                    and envelope.event.kind is last_kind
                    and last_acknowledgement is not None
                ):
                    self._put_lifecycle_acknowledgement(
                        acknowledgement_queue,
                        last_acknowledgement,
                    )
                    continue
                self._publish_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    envelope,
                    accepted=False,
                    reason="runtime_lifecycle_sequence_identity_mismatch",
                )
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "runtime lifecycle sequence identity mismatch",
                )
                continue
            with self._lock:
                pending = self._pending_lifecycle_envelope
            if pending is not None:
                if (
                    envelope.sequence == pending.sequence
                    and envelope.event_digest == pending.event_digest
                    and envelope.event.kind is pending.event.kind
                ):
                    # The child resends the same unacknowledged envelope. Keep
                    # one staged claim. Acknowledge only after NGUI commits the
                    # visible result.
                    continue
                self._publish_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    envelope,
                    accepted=False,
                    reason="runtime_lifecycle_inflight_identity_mismatch",
                )
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "runtime lifecycle inflight identity mismatch",
                )
                continue
            if envelope.sequence < last_sequence:
                self._publish_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    envelope,
                    accepted=False,
                    reason="runtime_lifecycle_sequence_regressed",
                )
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "runtime lifecycle sequence regressed",
                )
                continue
            if envelope.sequence != last_sequence + 1:
                self._publish_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    envelope,
                    accepted=False,
                    reason="runtime_lifecycle_sequence_gap",
                )
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "runtime lifecycle sequence gap",
                )
                continue
            try:
                self._validate_lifecycle_event(envelope.event)
            except Exception as exc:
                if (
                    envelope.event.kind
                    is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED
                ):
                    with self._lock:
                        self._request_admission_open = False
                        self._frame_admission_open = False
                        process_stop = self._process_stop
                    self._set_terminal_failure(
                        RuntimeServiceState.FAILED,
                        "runtime worker replacement quarantined: "
                        f"{type(exc).__name__}: {exc}",
                    )
                    if process_stop is not None:
                        process_stop.set()
                self._worker_health.mark_failure(
                    "ngui_runtime_lifecycle_receiver",
                    exc,
                    next_retry_at=time() + 0.1,
                )
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_runtime_lifecycle_receiver",
                    operation="apply_lifecycle_event",
                    generation=envelope.sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=False,
                    degraded_behavior=(
                        "invalid_lifecycle_event_rejected_status_and_frames_continue"
                    ),
                    error=exc,
                    suppression_budget=3,
                )
                rejection = RuntimeLifecycleAcknowledgement(
                    sequence=envelope.sequence,
                    event_kind=envelope.event.kind,
                    generation_identity=(
                        envelope.event.generation_identity
                    ),
                    event_digest=envelope.event_digest,
                    accepted=False,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                with self._lock:
                    self._last_lifecycle_sequence = envelope.sequence
                    self._last_lifecycle_digest = envelope.event_digest
                    self._last_lifecycle_kind = envelope.event.kind
                    self._last_lifecycle_acknowledgement = rejection
                    if envelope.event.kind in {
                        RuntimeEventKind.OPERATIONAL_RECEIPT,
                        RuntimeEventKind
                        .OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
                    }:
                        payload = envelope.event.payload
                        if isinstance(
                            payload,
                            OperationalServiceReceipt,
                        ):
                            self._operational_outbox_enqueued.discard(
                                payload.request_id
                            )
                self._put_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    rejection,
                )
                continue
            if envelope.event.kind in {
                RuntimeEventKind.MANUAL_NMI_RESULT,
                RuntimeEventKind.CORE_COMMAND_OUTCOME,
                RuntimeEventKind.OPERATIONAL_RECEIPT,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
            }:
                with self._lock:
                    if self._active_runtime_generation != runtime_generation:
                        return
                    if self._pending_lifecycle_envelope is not None:
                        raise RuntimeError(
                            "runtime lifecycle staged owner overlap"
                        )
                    self._pending_lifecycle_envelope = envelope
                    self._pending_lifecycle_generation = runtime_generation
                    self._pending_lifecycle_claimed = False
                    self._pending_lifecycle_acknowledgement_queue = (
                        acknowledgement_queue
                    )
                continue
            product_application = self._apply_service_lifecycle_event(
                envelope,
            )
            with self._lock:
                if self._active_runtime_generation != runtime_generation:
                    return
                self._last_lifecycle_sequence = envelope.sequence
                self._last_lifecycle_digest = envelope.event_digest
                self._last_lifecycle_kind = envelope.event.kind
            self._worker_health.mark_success(
                "ngui_runtime_lifecycle_receiver"
            )
            acknowledgement = RuntimeLifecycleAcknowledgement(
                sequence=envelope.sequence,
                event_kind=envelope.event.kind,
                generation_identity=envelope.event.generation_identity,
                event_digest=envelope.event_digest,
                accepted=True,
                application_receipt=product_application,
            )
            with self._lock:
                self._last_lifecycle_acknowledgement = acknowledgement
            if envelope.event.kind is not RuntimeEventKind.SHUTDOWN_RECEIPT:
                self._put_lifecycle_acknowledgement(
                    acknowledgement_queue,
                    acknowledgement,
                )
            if (
                envelope.event.kind
                is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED
            ):
                payload = envelope.event.payload
                if not isinstance(
                    payload,
                    RuntimeWorkerReplacementRequiredReceipt,
                ):
                    raise TypeError(
                        "runtime worker replacement typed receipt required"
                    )
                self._schedule_worker_replacement(
                    runtime_generation,
                    payload,
                )

    def _schedule_worker_replacement(
        self,
        runtime_generation: int,
        receipt: RuntimeWorkerReplacementRequiredReceipt,
    ) -> None:
        if not isinstance(receipt, RuntimeWorkerReplacementRequiredReceipt):
            raise TypeError("runtime worker replacement receipt required")
        with self._lock:
            if self._active_runtime_generation != runtime_generation:
                return
            if self._pending_worker_replacement != receipt:
                raise RuntimeError(
                    "runtime worker replacement receipt not admitted"
                )
            existing = self._worker_replacement_thread
            if existing is not None:
                return
            config = self._config
            if not isinstance(config, Arch4Config):
                raise RuntimeError(
                    "runtime worker replacement config unavailable"
                )
            observe_only = self._observe_only
            replacement = Thread(
                target=self._replace_worker_after_ack,
                args=(
                    runtime_generation,
                    receipt,
                    config,
                    observe_only,
                ),
                name="ngui-runtime-worker-replacement",
                daemon=True,
            )
            self._worker_replacement_thread = replacement
        replacement.start()

    def _replace_worker_after_ack(
        self,
        runtime_generation: int,
        receipt: RuntimeWorkerReplacementRequiredReceipt,
        config: Arch4Config,
        observe_only: bool,
    ) -> None:
        try:
            with self._lock:
                if (
                    self._active_runtime_generation != runtime_generation
                    or self._pending_worker_replacement != receipt
                ):
                    return
            self._replace_runtime(
                None if receipt.reason in {
                    ProductRuntimeWorkerReplacementReason.PHYSICAL_DEVICE_CHANGED,
                    ProductRuntimeWorkerReplacementReason.AUTOMATIC_LOGIN_FAILED,
                } else config,
                observe_only=observe_only,
                timeout_s=RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
                expected_reset_manifest_digest=(
                    receipt.expected_reset_manifest_digest
                ),
                login_required_message=(
                    "Automatic sign-in failed. Please sign in again."
                    if receipt.reason is ProductRuntimeWorkerReplacementReason.AUTOMATIC_LOGIN_FAILED
                    else "Different router detected. Select its profile and sign in."
                ),
            )
        except Exception as exc:
            self._set_terminal_failure(
                RuntimeServiceState.FAILED,
                "runtime worker replacement failed: "
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            with self._lock:
                if self._worker_replacement_thread is current_thread():
                    self._worker_replacement_thread = None

    @staticmethod
    def _publish_lifecycle_acknowledgement(
        acknowledgement_queue: Any,
        envelope: RuntimeLifecycleEnvelope,
        *,
        accepted: bool,
        reason: str = "",
    ) -> None:
        acknowledgement = RuntimeLifecycleAcknowledgement(
            sequence=envelope.sequence,
            event_kind=envelope.event.kind,
            generation_identity=envelope.event.generation_identity,
            event_digest=envelope.event_digest,
            accepted=accepted,
            reason=reason,
        )
        NGUIRuntimeService._put_lifecycle_acknowledgement(
            acknowledgement_queue,
            acknowledgement,
        )

    @staticmethod
    def _put_lifecycle_acknowledgement(
        acknowledgement_queue: Any,
        acknowledgement: RuntimeLifecycleAcknowledgement,
    ) -> None:
        try:
            acknowledgement_queue.put(acknowledgement, timeout=0.25)
        except Full:
            # Only one event is in flight, so a full acknowledgement slot may
            # contain only the matching replay acknowledgement.
            return

    def _validate_lifecycle_event(self, event: RuntimeIPCEvent) -> None:
        with self._lock:
            active_worker = self._active_worker_identity
            active_product = self._active_product_identity
            admission_authority = self._product_root_admission
        if event.kind is RuntimeEventKind.PRODUCT_ROOT_ADMISSION:
            if (
                not isinstance(
                    event.generation_identity,
                    RuntimeProductGenerationIdentity,
                )
                or event.generation_identity.worker != active_worker
                or not isinstance(event.payload, RuntimeProductRootAdmission)
                or admission_authority is None
            ):
                raise RuntimeError(
                    "runtime Product root admission identity mismatch"
                )
        elif event.kind is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED:
            if (
                event.generation_identity != active_product
                or admission_authority is None
            ):
                raise RuntimeError(
                    "runtime worker replacement Product identity mismatch"
                )
            admission_authority.require_product(event.generation_identity)
        elif event.kind is RuntimeEventKind.SHUTDOWN_RECEIPT:
            if isinstance(event.generation_identity, RuntimeWorkerGenerationIdentity):
                # Failed startup has no admitted Product root. Product still
                # owns its cleanup proof, bound to this exact worker lifetime.
                if active_product is not None or event.generation_identity != active_worker:
                    raise RuntimeError("runtime pre-admission shutdown worker identity mismatch")
            else:
                if event.generation_identity != active_product or admission_authority is None:
                    raise RuntimeError("runtime lifecycle Product generation identity mismatch")
                admission_authority.require_product(event.generation_identity)
        elif event.kind in {
            RuntimeEventKind.OPERATIONAL_RECEIPT,
            RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
        }:
            if (
                not isinstance(
                    event.generation_identity,
                    RuntimeNamespaceGenerationIdentity,
                )
                or event.generation_identity.product != active_product
                or admission_authority is None
                or not isinstance(
                    event.payload,
                    OperationalServiceReceipt,
                )
            ):
                raise RuntimeError(
                    "runtime operational receipt Product identity mismatch"
                )
            admission_authority.require_namespace(
                event.generation_identity
            )
            self._operational_outbox.require_receipt(event.payload)
        elif event.kind is not RuntimeEventKind.STATUS:
            if (
                not isinstance(
                    event.generation_identity,
                    RuntimeNamespaceGenerationIdentity,
                )
                or event.generation_identity.product != active_product
                or admission_authority is None
            ):
                raise RuntimeError(
                    "runtime lifecycle Product identity mismatch"
                )
            admission_authority.require_namespace(event.generation_identity)
        if event.kind is RuntimeEventKind.STATUS:
            raise RuntimeError("runtime status is illegal on lifecycle lane")
        if event.kind is RuntimeEventKind.MANUAL_NMI_RESULT:
            if not isinstance(event.payload, RuntimeManualResultReceipt):
                raise TypeError("typed manual NMI result payload required")
            return
        if event.kind is RuntimeEventKind.CORE_COMMAND_OUTCOME:
            if not isinstance(event.payload, CommandTerminalReceipt):
                raise TypeError(
                    "typed CommandAuthority terminal receipt required"
                )
            return
        if event.kind in {
            RuntimeEventKind.OPERATIONAL_RECEIPT,
            RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
        }:
            if not isinstance(event.payload, OperationalServiceReceipt):
                raise TypeError("typed operational receipt payload required")
            return
        if event.kind is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED:
            if not isinstance(
                event.payload,
                RuntimeWorkerReplacementRequiredReceipt,
            ):
                raise TypeError(
                    "typed worker replacement receipt payload required"
                )
            if event.payload.generation_identity != event.generation_identity:
                raise ValueError(
                    "worker replacement receipt Product identity mismatch"
                )
            with self._lock:
                pending_replacement = self._pending_worker_replacement
                replacement_conflict = bool(
                    pending_replacement is not None
                    and pending_replacement != event.payload
                )
                if replacement_conflict:
                    self._request_admission_open = False
                    self._frame_admission_open = False
                    process_stop = self._process_stop
                else:
                    process_stop = None
            if replacement_conflict:
                if process_stop is not None:
                    process_stop.set()
                raise RuntimeError(
                    "runtime worker replacement receipt conflict"
                )
            return
        if event.kind is RuntimeEventKind.PRODUCT_ROOT_ADMISSION:
            if not isinstance(event.payload, RuntimeProductRootAdmission):
                raise TypeError("typed Product root admission required")
            return
        if event.kind is RuntimeEventKind.SHUTDOWN_RECEIPT:
            if not isinstance(event.payload, ProductShutdownReceipt):
                raise TypeError("typed Product shutdown receipt required")
            return
        raise RuntimeError(
            "unclassified runtime lifecycle event: " + event.kind.value
        )

    def _apply_service_lifecycle_event(
        self,
        envelope: RuntimeLifecycleEnvelope,
    ) -> RuntimeProductEventApplicationReceipt:
        """Apply lifecycle evidence owned by the service, never GUI outcomes."""

        event = envelope.event
        if event.kind is RuntimeEventKind.PRODUCT_ROOT_ADMISSION:
            with self._lock:
                authority = self._product_root_admission
                if authority is None:
                    raise RuntimeError(
                        "runtime Product root admission authority unavailable"
                    )
                identity = authority.admit(event)
                current = self._active_product_identity
                if current is not None and current != identity:
                    raise RuntimeError(
                        "runtime Product root admission overlap"
                    )
                self._active_product_identity = identity
            return RuntimeProductEventApplicationReceipt(
                generation_identity=identity,
                sequence=envelope.sequence,
                event_kind=event.kind,
                event_digest=envelope.event_digest,
                applied_at=time(),
                application_key=(
                    "runtime-product-root-admission:"
                    f"{identity.identity_digest}"
                ),
            )
        if event.kind is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED:
            payload = event.payload
            if not isinstance(
                payload,
                RuntimeWorkerReplacementRequiredReceipt,
            ):
                raise TypeError(
                    "runtime worker replacement typed receipt required"
                )
            with self._lock:
                current = self._pending_worker_replacement
                if current is not None and current != payload:
                    raise RuntimeError(
                        "runtime worker replacement receipt overlap"
                    )
                self._request_admission_open = False
                self._frame_admission_open = False
                self._pending_worker_replacement = payload
            return RuntimeProductEventApplicationReceipt(
                generation_identity=event.generation_identity,
                sequence=envelope.sequence,
                event_kind=event.kind,
                event_digest=envelope.event_digest,
                applied_at=time(),
                application_key=(
                    "runtime-worker-replacement:"
                    f"{event.generation_identity.identity_digest}:"
                    f"{envelope.event_digest}"
                ),
            )
        if event.kind is RuntimeEventKind.SHUTDOWN_RECEIPT:
            with self._lock:
                self._last_product_shutdown_receipt = event.payload
            return RuntimeProductEventApplicationReceipt(
                generation_identity=event.generation_identity,
                sequence=envelope.sequence,
                event_kind=event.kind,
                event_digest=envelope.event_digest,
                applied_at=time(),
                application_key=(
                    "runtime-product-shutdown:"
                    f"{event.generation_identity.identity_digest}:"
                    f"{event.payload.disposition.value}"
                ),
            )
        raise RuntimeError(
            "GUI lifecycle outcome requires visible application claim"
        )

    def _frame_receiver_loop(
        self,
        runtime_generation: int,
        process: Any,
        frame_queue: Any,
        acknowledgement_queue: Any,
        process_stop: Any,
        stop_event: Any,
    ) -> None:
        with self._lock:
            if self._active_runtime_generation != runtime_generation:
                return
            last_identity = self._last_committed_frame_identity
        last_sequence = int(last_identity[0]) if last_identity is not None else 0
        if acknowledgement_queue is None:
            raise RuntimeError(
                "runtime frame acknowledgement queue unavailable before receive"
            )
        while not stop_event.is_set():
            if not self._receiver_generation_is_active(runtime_generation):
                return
            try:
                envelope = frame_queue.get(timeout=0.1)
            except Empty:
                if not process.is_alive():
                    return
                continue
            if not isinstance(envelope, RuntimeFrameEnvelope):
                self._set_terminal_failure(
                    RuntimeServiceState.FAILED,
                    "invalid CGF frame envelope",
                )
                if process_stop is not None:
                    process_stop.set()
                raise RuntimeError(
                    "invalid CGF frame envelope requires boundary replacement"
                )
            validate_runtime_frame_envelope(envelope)
            frame = envelope.frame
            frame_identity = runtime_frame_identity(frame)
            with self._lock:
                if self._active_runtime_generation != runtime_generation:
                    return
                committed_identity = self._last_committed_frame_identity
                committed_receipt = self._last_committed_frame_receipt
            if (
                committed_identity == frame_identity
                and committed_receipt is not None
            ):
                validate_runtime_frame_delivery_receipt(
                    frame,
                    committed_receipt,
                )
                self._put_runtime_frame_acknowledgement(
                    acknowledgement_queue,
                    committed_receipt,
                    stop_event,
                )
                last_sequence = frame.sequence
                continue
            if frame.sequence <= last_sequence:
                error = RuntimeError(
                    f"stale CGF frame sequence {frame.sequence}; "
                    f"current is {last_sequence}"
                )
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_runtime_frame_receiver",
                    operation="quarantine_stale_frame",
                    generation=frame.sequence,
                    severity=FaultSeverity.WARNING,
                    retryable=False,
                    degraded_behavior="retain_current_front_and_continue",
                    error=error,
                    suppression_budget=3,
                )
                self._put_runtime_frame_acknowledgement(
                    acknowledgement_queue,
                    self._rejected_runtime_frame_receipt(frame),
                    stop_event,
                )
                continue
            received_at = monotonic()
            command_context = None
            delivery = {
                "frame_sequence": frame.sequence,
                "received_at": received_at,
                "ipc_enqueued_at": envelope.enqueued_at,
                "ipc_latency_ms": round(
                    max(0.0, received_at - envelope.enqueued_at) * 1000.0,
                    3,
                ),
                "payload_bytes": envelope.payload_bytes,
                "root_fingerprint": (
                    frame.accepted_root_fingerprint
                ),
                "matrix_identity": frame.matrix_identity.as_dict(),
            }
            application_failure_recorded = False
            with self._lock:
                if self._active_runtime_generation != runtime_generation:
                    return
                if not self._frame_admission_open:
                    receipt = self._rejected_runtime_frame_receipt(
                        frame,
                    )
                    self._put_runtime_frame_acknowledgement(
                        acknowledgement_queue,
                        receipt,
                        stop_event,
                    )
                    continue
            try:
                command_context = (
                    frame.command_ingress_context
                )
                if command_context.matrix_identity != frame.matrix_identity:
                    raise RuntimeError(
                        "runtime accepted root command context identity mismatch"
                    )
                if not self._receiver_generation_is_active(
                    runtime_generation
                ):
                    return
                receipt = self._on_frame(frame)
                if not self._receiver_generation_is_active(
                    runtime_generation
                ):
                    return
                if not isinstance(receipt, RuntimeFrameDeliveryReceipt):
                    raise TypeError("runtime frame callback receipt required")
                validate_runtime_frame_delivery_receipt(frame, receipt)
            except Exception as exc:
                application_failure_recorded = True
                record = self._worker_health.mark_failure(
                    "ngui_runtime_frame_receiver",
                    exc,
                    next_retry_at=time(),
                )
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_runtime_frame_receiver",
                    operation="deliver_frame_to_orchestrator",
                    generation=record.success_count + record.failure_count,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="frame_rejected_receiver_continues",
                    error=exc,
                    suppression_budget=3,
                )
                log.exception("NGUI runtime frame consumer failed")
                with self._lock:
                    self._frame_consumer_degraded = True
                receipt = self._rejected_runtime_frame_receipt(frame)
            with self._lock:
                if self._active_runtime_generation != runtime_generation:
                    return
                if command_context is not None and receipt.frame_applied:
                    self._retain_core_command_context_unlocked(command_context)
                self._last_frame_delivery = {
                    **delivery,
                    "disposition": (
                        "accepted"
                        if receipt.frame_applied
                        else "frame_rejected_truth_held"
                    ),
                    "accepted_commit_id": receipt.accepted_commit_id,
                    "session_scope_key": receipt.session_scope_key,
                    "source_epoch": receipt.source_epoch,
                    "source_sequence": receipt.source_sequence,
                    "front_register_applied": receipt.front_register_applied,
                    "chart_history_accepted": receipt.chart_history_accepted,
                }
            if receipt.frame_applied:
                with self._lock:
                    if self._active_runtime_generation != runtime_generation:
                        return
                    self._last_committed_frame_identity = frame_identity
                    self._last_committed_frame_receipt = receipt
                    self._active_matrix_identity = frame.matrix_identity
                    product_identity = self._active_product_identity
                    if product_identity is None:
                        raise RuntimeError(
                            "CGF Product generation is not ready"
                        )
                    self._active_namespace_identity = (
                        RuntimeNamespaceGenerationIdentity(
                            product=product_identity,
                            matrix_identity=frame.matrix_identity,
                        )
                    )
                    admission_authority = self._product_root_admission
                    if admission_authority is None:
                        raise RuntimeError(
                            "CGF Product root admission authority unavailable"
                        )
                    admission_authority.require_namespace(
                        self._active_namespace_identity
                    )
                    if (
                        self._frame_admission_open
                        and not self._request_admission_closed_by_shutdown
                    ):
                        self._request_admission_generation = runtime_generation
                        self._request_admission_open = True
                        self._redeliver_operational_outbox_unlocked()
                self._worker_health.mark_success(
                    "ngui_runtime_frame_receiver"
                )
            else:
                if not application_failure_recorded:
                    rejection_error = RuntimeError(
                        "runtime_frame_application_rejected:"
                        f"frame={frame.sequence}"
                    )
                    self._worker_health.mark_failure(
                        "ngui_runtime_frame_receiver",
                        rejection_error,
                        next_retry_at=time(),
                    )
            self._put_runtime_frame_acknowledgement(
                acknowledgement_queue,
                receipt,
                stop_event,
            )
            # A positive or negative receipt ends this transport sequence. Only
            # an accepted Front changes GUI truth, but either result advances
            # transport ordering.
            last_sequence = max(last_sequence, frame.sequence)
            if receipt.frame_applied:
                self._recover_frame_consumer(frame.sequence)

    @staticmethod
    def _rejected_runtime_frame_receipt(
        frame: RuntimeInputFrame,
    ) -> RuntimeFrameDeliveryReceipt:
        return build_runtime_frame_delivery_receipt(
            frame,
            front_register_applied=False,
            chart_history_accepted=False,
        )

    @staticmethod
    def _put_runtime_frame_acknowledgement(
        acknowledgement_queue: Any,
        receipt: RuntimeFrameDeliveryReceipt,
        stop_event: Any,
    ) -> None:
        """Do not consume a frame without a final transport receipt."""

        while not stop_event.is_set():
            try:
                acknowledgement_queue.put(receipt, timeout=0.1)
                return
            except Full:
                continue
        raise RuntimeError(
            "runtime frame acknowledgement interrupted by runtime stop"
        )

    def _supervise_receiver(
        self,
        worker_id: str,
        runtime_generation: int,
        target: Callable[..., None],
        *target_args: Any,
    ) -> None:
        if not target_args:
            raise ValueError("runtime receiver requires target arguments")
        stop_event = target_args[-1]
        try:
            while (
                not stop_event.is_set()
                and self._receiver_generation_is_active(runtime_generation)
            ):
                try:
                    target(*target_args)
                    return
                except Exception as exc:
                    current = self._worker_health.record(worker_id)
                    retry_s = min(
                        1.0,
                        0.05 * (2 ** min(current.failure_streak + 1, 5)),
                    )
                    record = self._worker_health.mark_failure(
                        worker_id,
                        exc,
                        next_retry_at=time() + retry_s,
                    )
                    self._fault_supervisor.record(
                        boundary=FaultBoundaryClass.GUI,
                        owner=worker_id,
                        operation="receive_ipc",
                        generation=record.success_count + record.failure_count,
                        severity=FaultSeverity.ERROR,
                        retryable=True,
                        degraded_behavior="ipc_receiver_restarts_with_bounded_backoff",
                        error=exc,
                        suppression_budget=3,
                    )
                    if stop_event.wait(retry_s):
                        return
                    if self._receiver_generation_is_active(
                        runtime_generation
                    ):
                        self._worker_health.mark_started(worker_id)
        finally:
            if self._receiver_generation_is_active(runtime_generation):
                self._worker_health.mark_stopped(
                    worker_id,
                    reason="runtime_receiver_stop_requested",
                )

    def _receiver_generation_is_active(
        self,
        runtime_generation: int,
    ) -> bool:
        with self._lock:
            return (
                int(runtime_generation) > 0
                and self._active_runtime_generation
                == int(runtime_generation)
            )

    def _require_running_config_unlocked(self) -> Arch4Config:
        if (
            not self._request_admission_open
            or self._request_admission_generation
            != self._active_runtime_generation
            or self._status.state
            in {RuntimeServiceState.STOPPING, RuntimeServiceState.STOPPED}
            or self._config is None
            or self._process is None
            or not self._process.is_alive()
        ):
            raise RuntimeError("CGF process is not ready")
        return self._config

    def _next_sequence_unlocked(self) -> int:
        self._require_running_config_unlocked()
        self._request_sequence += 1
        return self._request_sequence

    def _required_matrix_identity_unlocked(self) -> MatrixGenerationIdentity:
        self._require_running_config_unlocked()
        identity = self._active_matrix_identity
        if not isinstance(identity, MatrixGenerationIdentity):
            raise RuntimeError("CGF Matrix generation is not ready")
        return identity

    def _required_namespace_identity_unlocked(
        self,
    ) -> RuntimeNamespaceGenerationIdentity:
        self._require_running_config_unlocked()
        identity = self._active_namespace_identity
        if not isinstance(identity, RuntimeNamespaceGenerationIdentity):
            raise RuntimeError("CGF runtime namespace generation is not ready")
        authority = self._product_root_admission
        if authority is None:
            raise RuntimeError(
                "CGF Product root admission authority is not ready"
            )
        authority.require_namespace(identity)
        return identity

    @staticmethod
    def _put_unlocked(queue: Any, request: RuntimeIPCRequest | RuntimeSupportHistoryFlushRequest) -> None:
        if queue is None:
            raise RuntimeError("CGF IPC queue is unavailable")
        try:
            queue.put_nowait(request)
        except Full as exc:
            raise RuntimeError("CGF IPC request queue is full") from exc

    def _set_status(
        self,
        state: RuntimeServiceState,
        message: str = "",
        cycle_count: int = 0,
        *,
        changed_at: float | None = None,
    ) -> None:
        with self._lock:
            status = self._replace_status_unlocked(
                state,
                message,
                cycle_count,
                changed_at=changed_at,
            )
        self._on_status(status)

    def _apply_child_status(
        self,
        runtime_generation: int,
        child_status: RuntimeServiceStatus,
    ) -> None:
        """Apply child status through the parent lifecycle state machine."""

        if not isinstance(child_status, RuntimeServiceStatus):
            raise TypeError("runtime_child_status_required")
        status: RuntimeServiceStatus | None = None
        with self._lock:
            if self._active_runtime_generation != int(runtime_generation):
                return
            self._child_status = child_status
            current = self._status.state
            if current in {
                RuntimeServiceState.STOPPING,
                RuntimeServiceState.STOPPED,
            }:
                self._last_late_child_status = {
                    "runtime_generation": int(runtime_generation),
                    "state": child_status.state.value,
                    "message": child_status.message,
                    "cycle_count": child_status.cycle_count,
                    "changed_at": child_status.changed_at,
                    "rejected_by": current.value,
                }
                return
            if self._terminal_failure_active:
                return
            if (
                self._frame_consumer_degraded
                and child_status.state is not RuntimeServiceState.FAILED
            ):
                return
            allowed = {
                RuntimeServiceState.IDLE: {
                    RuntimeServiceState.STARTING,
                },
                RuntimeServiceState.STARTING: {
                    RuntimeServiceState.STARTING,
                    RuntimeServiceState.IDENTITY_REQUIRED,
                    RuntimeServiceState.RUNNING,
                    RuntimeServiceState.RECONNECTING,
                    RuntimeServiceState.FAILED,
                },
                RuntimeServiceState.IDENTITY_REQUIRED: {
                    RuntimeServiceState.IDENTITY_REQUIRED,
                    RuntimeServiceState.RUNNING,
                    RuntimeServiceState.FAILED,
                },
                RuntimeServiceState.RUNNING: {
                    RuntimeServiceState.RUNNING,
                    RuntimeServiceState.RECONNECTING,
                    RuntimeServiceState.FAILED,
                },
                RuntimeServiceState.RECONNECTING: {
                    RuntimeServiceState.RECONNECTING,
                    RuntimeServiceState.RUNNING,
                    RuntimeServiceState.FAILED,
                },
                RuntimeServiceState.FAILED: set(),
            }
            if child_status.state not in allowed.get(current, set()):
                self._terminal_failure_active = True
                status = self._replace_status_unlocked(
                    RuntimeServiceState.FAILED,
                    (
                        "illegal child runtime status transition:"
                        f"{current.value}->{child_status.state.value}"
                    ),
                    child_status.cycle_count,
                    changed_at=child_status.changed_at,
                )
            else:
                if child_status.state is RuntimeServiceState.FAILED:
                    self._terminal_failure_active = True
                status = self._replace_status_unlocked(
                    child_status.state,
                    child_status.message,
                    child_status.cycle_count,
                    changed_at=child_status.changed_at,
                    product_startup_receipt=(
                        child_status.product_startup_receipt
                    ),
                )
        if status is not None:
            self._on_status(status)

    def _set_terminal_failure(
        self,
        state: RuntimeServiceState,
        message: str = "",
        cycle_count: int = 0,
        *,
        changed_at: float | None = None,
    ) -> None:
        if state is not RuntimeServiceState.FAILED:
            raise ValueError("terminal runtime failure requires FAILED state")
        with self._lock:
            self._terminal_failure_active = True
            status = self._replace_status_unlocked(
                state,
                message,
                cycle_count,
                changed_at=changed_at,
            )
        self._on_status(status)

    def _recover_frame_consumer(self, frame_sequence: int) -> None:
        status: RuntimeServiceStatus | None = None
        with self._lock:
            if (
                not self._frame_consumer_degraded
                or self._terminal_failure_active
                or self._status.state
                in {RuntimeServiceState.STOPPING, RuntimeServiceState.STOPPED}
            ):
                return
            self._frame_consumer_degraded = False
            child = self._child_status
            status = self._replace_status_unlocked(
                child.state,
                child.message,
                child.cycle_count,
                product_startup_receipt=child.product_startup_receipt,
            )
        self._fault_supervisor.record_recovery(
            boundary=FaultBoundaryClass.GUI,
            owner="ngui_runtime_frame_receiver",
            operation="deliver_frame_to_orchestrator",
            generation=frame_sequence,
            detail="later valid owner-batch delivery restored the NGUI boundary",
        )
        self._on_status(status)

    def _replace_status_unlocked(
        self,
        state: RuntimeServiceState,
        message: str = "",
        cycle_count: int = 0,
        *,
        changed_at: float | None = None,
        product_startup_receipt: ProductStartupReceipt | None = None,
    ) -> RuntimeServiceStatus:
        status = RuntimeServiceStatus(
            state,
            float(changed_at if changed_at is not None else monotonic()),
            str(message or ""),
            int(cycle_count),
            product_startup_receipt,
        )
        self._status_revision += 1
        self._status = status
        return status

    def _close_queues(self) -> None:
        for queue in (
            self._request_queue,
            self._manual_nmi_queue,
            self._status_queue,
            self._lifecycle_queue,
            self._lifecycle_acknowledgement_queue,
            self._frame_queue,
            self._frame_acknowledgement_queue,
        ):
            if queue is None:
                continue
            close = getattr(queue, "close", None)
            if callable(close):
                close()
            join_thread = getattr(queue, "join_thread", None)
            if callable(join_thread):
                join_thread()


__all__ = [
    "NGUIRuntimeService",
    "OperationalRequestDelivery",
    "RUNTIME_WORKER_SHUTDOWN_BUDGET_S",
    "RuntimeServiceState",
    "RuntimeServiceStatus",
]
