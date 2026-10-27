"""NGUI lifecycle, intent routing, and presentation clocks without a GUI toolkit."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import localtime, monotonic, strftime, time
from typing import Any, Callable, Mapping, Protocol


log = logging.getLogger("arch4.ngui.orchestrator")

from arch4.application_lease import mark_current_application_product_ready
from arch4.intelligence_presentation_contract import action_actor_label
from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.foundation.operational_service_contracts import (
    LicenseServicePort,
    LicenseStatusRequest,
    OperatorConfigServicePort,
    OperatorScopeRequest,
    TrafficPlanSaveResult,
)
from arch4.foundation.runtime_artifacts import (
    SHUTDOWN_RECEIPT_OWNER,
    SHUTDOWN_RECEIPT_SCHEMA,
)
from arch4.interfaces import LeverAction, LeverType
from arch4.foundation.settings_handoff import (
    import_confirmed_next_start_handoff_settings,
)
from arch4.command_transactions import (
    CommandTerminalReceipt,
    CommandTerminalStatus,
)
from arch4.core_contracts.command_targets import (
    CoreWifiRadioTarget,
    core_lte_band_signature,
)
from arch4.core_contracts.action_reachability import (
    ReachabilityOutageInterval,
)
from arch4.operational_service_contracts import (
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority

from .control_availability import (
    PHYSICAL_CONTROLS,
    build_control_availability,
)
from .adapters.owner_register import adapt_owner_register
from .chart_model import (
    DEFAULT_CHART_WINDOW_SECONDS,
    NGUIChartProcessor,
    SUPPORTED_CHART_WINDOW_SECONDS,
    chart_front_cursor,
    compare_chart_front_cursors,
)
from .debug_surface import (
    PageTruthGenerationRotationReceipt,
    front_register_payload,
)
from .edit_register import (
    NGUIEditRegister,
    SMS_SENDER_GROUP,
    TRAFFIC_PLAN_GROUP,
)
from .external_actions import (
    ExternalActionKind,
    ExternalActionResult,
    ExternalActionState,
    NGUIExternalActionProcessor,
)
from .front_register import FrontRegisterSnapshot, PresentationState
from .heavy_work import HeavyWorkKind, HeavyWorkState, NGUIHeavyWorkProcessor
from .input_registers import (
    InputRegisterSnapshot,
    InputValue,
    NGUIInputRegisterBank,
    OwnerInputBatch,
    owner_input_batch_fingerprint,
    thaw_ngui_input_value,
)
from .interactions import (
    INTENT_REGISTRY,
    IntentKind,
    NGUIInteractionRegister,
    UserIntent,
)
from .manual_action_lifecycle import (
    NGUIActionLifecycleProjection,
    NGUIActionLifecycleProjectionCandidate,
)
from .manual_actions import (
    build_manual_band_apply_action,
    build_manual_nr_apply_action,
    parse_manual_band_pool,
)
from .operator_event_feed import NGUIOperatorEventFeed
from .presentation import NGUIPresentationAuthority
from .presentation.processor import NGUIPresentationProcessor
from .profile_authority import ExactProfileAuthority, ResolvedProfileLogin
from .registry import input_source_spec
from .runtime_ipc import (
    RuntimeEventKind,
    RuntimeFrameDeliveryReceipt,
    RuntimeInputFrame,
    RuntimeLifecycleApplicationReceipt,
    RuntimeLifecycleClaim,
    RuntimeManualResultReceipt,
    build_runtime_frame_delivery_receipt,
)
from .runtime_service import (
    NGUIRuntimeService,
    RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
    RuntimeServiceState,
    RuntimeServiceStatus,
)
from .runtime_generation import (
    NGUIFrozenValue,
    NGUIFrontRegisterView,
    NGUIOutboxKind,
    NGUIOutboxTerminalDisposition,
    NGUIRuntimeGenerationCoordinator,
    NGUIRuntimeGenerationPreparedComponents,
)
from .runtime_ipc import RuntimeCanonicalMapping
from .runtime_reset_manifest import (
    RUNTIME_RESET_MANIFEST,
    RuntimeResetApplicationReceipt,
    RuntimeResetCause,
    RuntimeResetRowApplicationReceipt,
    runtime_reset_manifest_digest,
    runtime_reset_transaction_digest,
)
from .types import FreshnessState, PageId, SourceOwner
from .utility_lifecycle import (
    EXTERNAL_UTILITY_KINDS,
    HEAVY_UTILITY_KINDS,
    NGUIUtilityTerminal,
    UtilityKind,
    UtilityTerminalState,
    project_utility_lifecycle,
    terminal_from_external,
    terminal_from_heavy,
)
from .voice_lifecycle import (
    NGUISINRVoiceLifecycle,
    NGUISINRVoiceRequest,
    SINRVoiceState,
)


class UserIntentInputError(ValueError):
    """A rejected operator draft, not an NGUI worker-health failure."""


from arch4.foundation.network_settings import NETWORK_INPUT_MESSAGES as _NETWORK_INPUT_MESSAGES


def _network_draft_input(operation, *args, **kwargs):
    """Translate known input rejections; the Foundation validator stays authoritative."""
    try:
        return operation(*args, **kwargs)
    except ValueError as exc:
        message = _NETWORK_INPUT_MESSAGES.get(str(exc))
        if message is None:
            raise
        if args and isinstance(args[0], Mapping) and "intention" in args[0]:
            from arch4.foundation.network_settings import connect_network_input_message
            message = connect_network_input_message(str(exc), args[0])
        raise UserIntentInputError(message) from exc


class RuntimeServicePort(Protocol):
    def restart(
        self,
        config,
        *,
        observe_only: bool,
        timeout_s: float = RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
        trace_selected_at: float | None = None,
    ) -> None: ...
    def stop(
        self,
        *,
        timeout_s: float = RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
    ) -> bool: ...
    def status(self) -> RuntimeServiceStatus: ...
    def submit_startup_router_serial(self, serial: str) -> None: ...
    def close_operational_admission(self) -> int: ...
    def operational_pending_count(self) -> int: ...
    def active_runtime_generation(self) -> int: ...
    def claim_lifecycle_event(self) -> RuntimeLifecycleClaim | None: ...
    def commit_lifecycle_event(
        self,
        claim: RuntimeLifecycleClaim,
        receipt: RuntimeLifecycleApplicationReceipt,
    ) -> None: ...
    def reject_lifecycle_event(
        self,
        claim: RuntimeLifecycleClaim,
        *,
        reason: str,
    ) -> None: ...
    def submit_read_diagnostics(self, *, action: str, created_at: float): ...

    def submit_network_settings_inspection(self, *, created_at: float): ...

    def submit_wifi_radio(self, *, radio: str, enabled: bool, created_at: float): ...
    def submit_downstream_health(self, *, enabled: bool, target: str, ping_count: int, interval_s: float, created_at: float): ...
    def submit_sms_command_control(self, *, enabled: bool, approved_senders: tuple[str, ...], code_word: str, poll_interval_s: float, created_at: float): ...
    def submit_band_schedule_plan(
        self,
        *,
        bands: tuple[int, ...],
        operation: str,
        activate_at: str,
        created_at: float,
    ): ...
    def submit_band_sequence_plan(self, *, bands: tuple[int, ...], interval_s: int, created_at: float): ...
    def submit_band_schedule_cancel(self, *, created_at: float): ...
    def submit_band_sequence_cancel(self, *, created_at: float): ...
    def submit_antenna_guide(self, *, action: str, created_at: float): ...
    def submit_manual_action(
        self,
        action: LeverAction,
        config,
        *,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> Mapping[str, Any]: ...
    def submit_band_auto(
        self,
        *,
        domain: str,
        correlation_id: str,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class NGUIOutputPorts:
    publish_page_truth: Callable[[FrontRegisterSnapshot, int], None]
    rotate_page_truth_generation: Callable[
        [int, str, float],
        Mapping[str, Any],
    ]
    close_page_truth: Callable[[float], Mapping[str, Any]]
    publish_utility_terminal: Callable[[NGUIUtilityTerminal], None]
    request_sinr_voice: Callable[[NGUISINRVoiceRequest], None]
    request_router_serial: Callable[[], None]
    request_application_exit: Callable[[], None]
    report_error: Callable[[str], None]


@dataclass(slots=True)
class _PendingLifecycleApplication:
    claim: RuntimeLifecycleClaim
    applied: bool = False
    application_key: str = ""
    target_input_generation: int = 0


RuntimeServiceFactory = Callable[
    [
        Callable,
        Callable,
        WorkerHealthAuthority,
        FaultSupervisor,
        ApplicationLeaseIdentity,
        object,
        str | Path,
    ],
    RuntimeServicePort,
]


def _runtime_service_factory(
    on_frame: Callable,
    on_status: Callable,
    worker_health: WorkerHealthAuthority,
    fault_supervisor: FaultSupervisor,
    application_lease: ApplicationLeaseIdentity,
    reset_application: object,
    operational_outbox_dir: str | Path,
) -> RuntimeServicePort:
    return NGUIRuntimeService(
        on_frame=on_frame,
        on_status=on_status,
        worker_health=worker_health,
        fault_supervisor=fault_supervisor,
        application_lease=application_lease,
        reset_application=reset_application,
        operational_outbox_dir=operational_outbox_dir,
    )


_INTENT_EVENT_LABELS: Mapping[IntentKind, str] = {
    IntentKind.APPLY_PBAND_CA_POOL: "Manual radio configuration",
    IntentKind.APPLY_NR_BAND: "Manual 5G band configuration",
    IntentKind.TOGGLE_BAND_LOCK: "Band lock change",
    IntentKind.TOGGLE_CELL_LOCK: "Cell lock change",
    IntentKind.RESTART_MODEM: "Modem restart",
    IntentKind.SET_BAND_AUTO: "Automatic band selection",
    IntentKind.START_SPEED_TEST: "Speed test",
    IntentKind.SET_TRAFFIC_PLAN: "Traffic plan update",
    IntentKind.CREATE_SITE_REPORT: "Site report",
    IntentKind.CREATE_SUPPORT_BUNDLE: "Support bundle",
    IntentKind.SCAN_NETWORK: "Network scan",
    IntentKind.OPEN_BUY: "License purchase information",
    IntentKind.OPEN_INFO: "Application information",
    IntentKind.CHECK_UPDATES: "Update check",
    IntentKind.UPDATE_LICENSE: "License update",
    IntentKind.EXIT_APPLICATION: "Application shutdown",
}

_MANUAL_INTENT_EVENT_LABELS: Mapping[IntentKind, str] = {
    IntentKind.APPLY_PBAND_CA_POOL: "RADIO CONFIGURATION",
    IntentKind.APPLY_NR_BAND: "5G BAND CONFIGURATION",
    IntentKind.TOGGLE_BAND_LOCK: "BAND LOCK",
    IntentKind.TOGGLE_CELL_LOCK: "CELL LOCK",
    IntentKind.RESTART_MODEM: "MODEM RESTART",
    IntentKind.SET_BAND_AUTO: "BAND AUTO",
    IntentKind.SET_MOBILE_DATA: "MOBILE NETWORK",
    IntentKind.APPLY_NETWORK_SETTINGS: "SIM & NETWORK",
}

_RECEIPT_BACKED_INTENTS = frozenset(
    {
        IntentKind.TOGGLE_WIFI_RADIO,
        IntentKind.ANTENNA_GUIDE_ACTION,
        IntentKind.PLAN_BAND_SEQUENCE,
        IntentKind.CANCEL_BAND_SEQUENCE,
        IntentKind.PLAN_BAND_SCHEDULE,
        IntentKind.CANCEL_BAND_SCHEDULE,
        IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,
        IntentKind.SET_PING_MONITOR,
    }
)

_RECEIPT_BACKED_EVENT_LABELS: Mapping[IntentKind, str] = {
    IntentKind.TOGGLE_WIFI_RADIO: "Wi-Fi radio change",
    IntentKind.ANTENNA_GUIDE_ACTION: "Antenna guide action",
    IntentKind.PLAN_BAND_SEQUENCE: "Band sequence",
    IntentKind.CANCEL_BAND_SEQUENCE: "Band sequence cancellation",
    IntentKind.PLAN_BAND_SCHEDULE: "Band schedule",
    IntentKind.CANCEL_BAND_SCHEDULE: "Band schedule cancellation",
    IntentKind.CONFIGURE_SMS_COMMAND_CONTROL: "SMS command control",
    IntentKind.SET_PING_MONITOR: "Ping monitor setting",
}

_MANUAL_EVENT_TERMINAL_STATUSES = frozenset(
    {
        "accepted",
        "confirmed",
        "settled",
        "failed",
        "rejected",
        "transport_failed",
        "presentation_failed",
        "timed_out",
        "cancelled",
    }
)

_MANUAL_EVENT_FAILED_STATUSES = frozenset(
    {
        "failed",
        "rejected",
        "transport_failed",
        "presentation_failed",
        "timed_out",
        "cancelled",
    }
)

_RADIO_EVENT_LEVERS = frozenset(
    {
        "BAND_SELECTION",
        "BAND_LOCK",
        "PBAND",
    }
)


def _manual_event_message(manual: Mapping[str, Any]) -> str:
    status = str(manual.get("status") or "").strip().lower()
    if status == "settled":
        return "Completed; requested change verified."
    if status == "confirmed":
        return "Completed; requested change verified."
    if status == "accepted":
        return "Completed; modem accepted the requested command."
    message = str(manual.get("message") or "").strip()
    marker = "): "
    if message.startswith("Last manual action at ") and marker in message:
        message = message.split(marker, 1)[1].strip()
    return message or status or "Manual action finished."


def _manual_terminal_belongs_in_recent_events(manual: Mapping[str, Any]) -> bool:
    """Keep one success owner and retain each final failure."""

    status = str(manual.get("status") or "").strip().lower()
    lever = str(manual.get("lever") or "").strip().upper()
    if status in {"confirmed", "settled"} and lever in {
        "PBAND",
        "BAND_SELECTION",
        "BAND_LOCK",
    }:
        return False
    return True


def _operator_ca_pool_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        if not value:
            return "unknown pool"
        return core_lte_band_signature(list(value))
    return str(value or "").strip().upper()


def _operator_pband_text(value: object) -> str:
    text = str(value or "").strip().upper()
    if text and not text.startswith("B"):
        text = "B" + text
    return text or "unknown band"


def _operator_radio_actor(value: object) -> str:
    return action_actor_label(value)


def _operator_radio_target_text(target: Mapping[str, object]) -> str:
    lever = str(target.get("lever") or "").strip().upper()
    if lever in {"BAND_SELECTION", "BAND_LOCK"}:
        return "CA pool " + _operator_ca_pool_text(target.get("bands"))
    if lever == "PBAND":
        text = _operator_pband_text(target.get("pband"))
        if target.get("ca_pool") is not None:
            text += " (" + _operator_ca_pool_text(target.get("ca_pool")) + ")"
        return text
    raise ValueError("ngui_radio_event_target_invalid")


def _failed_radio_attempt_message(
    *,
    actor: str,
    target: Mapping[str, object],
    observed_pband_path: tuple[str, ...],
    final_pband: object,
    final_ca_pool: object,
) -> str:
    """Describe one failed request without assigning modem choices to its actor."""

    final_text = (
        f"{_operator_pband_text(final_pband)} "
        f"({_operator_ca_pool_text(final_ca_pool)})"
    )
    target_text = _operator_radio_target_text(target)
    route = "→".join(observed_pband_path)
    if len(observed_pband_path) > 1:
        return (
            f"{actor} tried {target_text}; modem moved {route} "
            f"and settled on {final_text}."
        )
    return f"{actor} tried {target_text}; modem kept {final_text}."


def _radio_event_target_dimensions(
    target: Mapping[str, object],
    *,
    pband: object,
    ca_pool: object,
) -> tuple[bool, bool]:
    """Match requested PBAND and CA pool independently."""

    lever = str(target.get("lever") or "").strip().upper()
    pool_text = _operator_ca_pool_text(ca_pool)
    if lever in {"BAND_SELECTION", "BAND_LOCK"}:
        return False, pool_text == _operator_ca_pool_text(target.get("bands"))
    if lever == "PBAND":
        try:
            expected_pband = int(target.get("pband"))
            observed_pband = int(str(pband).strip().upper().removeprefix("B"))
        except (TypeError, ValueError):
            return False, False
        expected_pool = target.get("ca_pool")
        return (
            expected_pband == observed_pband,
            bool(
                expected_pool is not None
                and pool_text == _operator_ca_pool_text(expected_pool)
            ),
        )
    return False, False

class NGUIOrchestrator:
    """Coordinate all typed NGUI boundaries."""

    PRESENT_INTERVAL_S = 0.1
    CHART_INTERVAL_S = 0.5

    def __init__(
        self,
        *,
        app_root: str | Path,
        application_lease: ApplicationLeaseIdentity,
        storage_root: str | Path,
        heavy_work: NGUIHeavyWorkProcessor,
        external_actions: NGUIExternalActionProcessor,
        profile_authority: ExactProfileAuthority,
        license_service: LicenseServicePort,
        operator_config_service: OperatorConfigServicePort,
        ports: NGUIOutputPorts,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
        runtime_service_factory: RuntimeServiceFactory = _runtime_service_factory,
        memory_dir: str | Path | None = None,
        chart_processor: NGUIChartProcessor | None = None,
        dev_full_access: bool = False,
    ) -> None:
        self._app_root = Path(app_root).resolve()
        if not isinstance(application_lease, ApplicationLeaseIdentity):
            raise TypeError("ngui_orchestrator_application_lease_required")
        self._application_lease = application_lease
        self._application_ready_acknowledged = False
        self._storage_root = Path(storage_root).resolve()
        self._memory_dir = Path(
            memory_dir
            if memory_dir is not None
            else self._app_root / "portable_runtime" / "guc_memory"
        ).resolve()
        self.inputs = NGUIInputRegisterBank()
        self.interactions = NGUIInteractionRegister()
        self._presentation_transaction_lock = RLock()
        self.edits = NGUIEditRegister(
            lock=self._presentation_transaction_lock,
        )
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("ngui_orchestrator_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("ngui_orchestrator_requires_fault_supervisor")
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self.charts = chart_processor or NGUIChartProcessor(
            worker_health=worker_health,
            fault_supervisor=fault_supervisor,
        )
        self.presentation = NGUIPresentationAuthority(
            chart_register=self.charts.output,
            chart_status_provider=self.charts.status,
        )
        if not isinstance(profile_authority, ExactProfileAuthority):
            raise TypeError("ngui_orchestrator_requires_profile_authority")
        if license_service is None:
            raise TypeError("ngui_orchestrator_requires_license_service")
        if operator_config_service is None:
            raise TypeError("ngui_orchestrator_requires_operator_config_service")
        if Path(license_service.storage_root).resolve() != self._storage_root:
            raise RuntimeError("orchestrator LicenseService root mismatch")
        if Path(operator_config_service.storage_root).resolve() != self._storage_root:
            raise RuntimeError("orchestrator OperatorConfigService root mismatch")
        self.profile = profile_authority
        self._license_service = license_service
        self._dev_full_access = bool(dev_full_access)
        self._operator_config_service = operator_config_service
        self.heavy_work = heavy_work
        self.external_actions = external_actions
        self.ports = ports
        self._lock = RLock()
        self._parent_async_generation = 0
        # Generation zero owns profile selection before the worker starts. The
        # first runtime reset closes this admission under the transaction lock,
        # then advances the fence to worker generation one.
        self._parent_async_admission_open = True
        self._operator_connection_draft: dict[str, str] = {}
        self._network_choice_sequence = 0
        self._router_check_pending = None
        self._apn_library_requested = False
        from arch4.foundation.apn_profiles import public_network_choices
        self._apn_library = {"schema": "ApnProfileLibrary.v1", "state": "loading", "error": "", "profiles": [], "networks": public_network_choices()}
        self._pending_connect_network = None
        self._pending_network_inspected_at = 0.0
        self._network_settings_monitoring = True
        self._network_inspection_namespace = None
        self._network_recovery_read_token = None
        self._worker_health.register(
            "ngui_interactions",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self._worker_health.register(
            "ngui_presentation",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self._worker_health.register(
            "ngui_profile_lifecycle",
            required=True,
            retryable=True,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        )
        self.runtime = runtime_service_factory(
            self._on_runtime_frame,
            self._on_service_status,
            self._worker_health,
            self._fault_supervisor,
            self._application_lease,
            self,
            self._storage_root,
        )
        self._stop = Event()
        self._interaction_thread: Thread | None = None
        self._presentation_thread: Thread | None = None
        self._profile_lifecycle_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ngui-profile"
        )
        self._profile_lifecycle_futures: set[Future[Any]] = set()
        self._profile_lifecycle_closed = False
        self._login: ResolvedProfileLogin | None = None
        self._login_pending = False
        self._connection_stop_pending = False
        self._router_serial_prompt_requested = False
        self._login_identity_confirmed = False
        self._confirmed_serial = ""
        self._confirmed_profile_id = ""
        self._login_completion_key: tuple[str, str, str, str, str, str] | None = None
        self._login_completion_state = "idle"
        self._login_completion_payload: tuple[
            ResolvedProfileLogin,
            dict[str, object],
        ] | None = None
        self._login_completion_retry_not_before = 0.0
        self._chart_window_s = DEFAULT_CHART_WINDOW_SECONDS
        self._site_report_window = "1d"
        self._manual_lifecycle = NGUIActionLifecycleProjection()
        self._sinr_voice = NGUISINRVoiceLifecycle()
        self._wifi_action_sequence = 0
        self._operator_events = NGUIOperatorEventFeed()
        self._operator_connectivity_transition_sequence = 0
        self._operator_internet_outage_key: tuple[float, float] | None = None
        self._operator_runtime_fact_state: dict[str, object] = {}
        self._operator_pending_radio_command_root = ""
        self._operator_pending_radio_actor = ""
        self._operator_pending_radio_target: dict[str, object] = {}
        self._operator_pending_radio_pband_path: list[str] = []
        self._operator_severe_congestion_started_at = 0.0
        self._operator_severe_congestion_announced = False
        self._operator_severe_congestion_recovery_started_at = 0.0
        self._operator_service_state = ""
        self._operator_manual_signature: tuple[str, ...] | None = None
        self._manual_action_sequence = 0
        self._pending_lifecycle_application: (
            _PendingLifecycleApplication | None
        ) = None
        self._manual_projection_rejected_revision: tuple[Any, ...] | None = None
        self._runtime_worker_health: dict[str, Any] = {}
        self._runtime_worker_health_signature: tuple[str, ...] = ()
        self._runtime_worker_health_fault_generation = 0
        self._pending_runtime_worker_health_transition: (
            tuple[dict[str, Any], tuple[str, ...], int] | None
        ) = None
        self._settings_sequence = 0
        self._license_sequence = 0
        self._last_settings_values: dict[str, Any] = {}
        self._applied_runtime_namespace: tuple[str, str] = ("", "")
        self._last_chart_source_sequence = 0
        self._last_chart_source_cursor = (0, 0)
        self._pending_chart_front: FrontRegisterSnapshot | None = None
        self._pending_page_truth_front: FrontRegisterSnapshot | None = None
        self._last_page_truth_admitted_sequence = 0
        self._last_consumed_input_generation = -1
        self._last_presented_input_generation = -1
        self._last_presented_chart_sequence = 0
        self._shutdown_receipt: dict[str, Any] | None = None
        self._runtime_generation_coordinator: (
            NGUIRuntimeGenerationCoordinator | None
        ) = None
        self._runtime_front_view: NGUIFrontRegisterView | None = None

        self._handlers: dict[IntentKind, Callable[[UserIntent], None]] = {}
        for kind, spec in INTENT_REGISTRY.items():
            handler = getattr(self, spec.handler_name, None)
            if not callable(handler):
                raise RuntimeError(
                    f"invalid NGUI intent route {kind.value}: "
                    f"handler={spec.handler_name}"
                )
            self._handlers[kind] = handler

    @property
    def front_register(self):
        view = self._runtime_front_view
        return view if view is not None else self.presentation.front_register

    def apply_runtime_reset(
        self,
        *,
        application_lease: ApplicationLeaseIdentity,
        worker_generation: int,
        previous_worker_generation: int,
        cause: RuntimeResetCause,
        expected_manifest_digest: str,
    ) -> RuntimeResetApplicationReceipt:
        """Reset all parent owners before the next worker starts."""

        if application_lease != self._application_lease:
            raise ValueError("ngui_reset_application_lease_mismatch")
        if type(expected_manifest_digest) is not str:
            raise TypeError("ngui_reset_manifest_digest_string_required")
        if expected_manifest_digest != runtime_reset_manifest_digest():
            raise ValueError("ngui_reset_manifest_digest_mismatch")
        manifest_digest = expected_manifest_digest
        transaction_digest = runtime_reset_transaction_digest(
            application_lease=application_lease,
            worker_generation=worker_generation,
            previous_worker_generation=previous_worker_generation,
            cause=cause,
            manifest_digest=manifest_digest,
        )

        def neutral_row(
            state_id: str,
            prior_value: Any,
            result_value: Any,
        ) -> RuntimeResetRowApplicationReceipt:
            self._require_runtime_reset_owner_neutral(
                state_id,
                result_value,
            )
            return RuntimeResetRowApplicationReceipt.from_observed(
                state_id=state_id,
                prior_value=prior_value,
                result_value=RUNTIME_RESET_MANIFEST[
                    state_id
                ].neutral_seed,
            )

        with self._presentation_transaction_lock:
            with self._lock:
                if (
                    self._parent_async_generation
                    != previous_worker_generation
                    or worker_generation != previous_worker_generation + 1
                ):
                    raise RuntimeError(
                        "ngui_reset_parent_generation_not_successor"
                    )
                self._parent_async_admission_open = False
                retained_connection_draft = dict(
                    self._operator_connection_draft
                )

            rows: dict[str, RuntimeResetRowApplicationReceipt] = {}
            rows["ngui.owner.operator_connection_draft"] = (
                RuntimeResetRowApplicationReceipt.from_observed(
                    state_id=(
                        "ngui.owner.operator_connection_draft"
                    ),
                    prior_value=retained_connection_draft,
                    result_value=dict(self._operator_connection_draft),
                )
            )

            input_prior, input_result = self.inputs.reset_runtime_state()
            rows["ngui.owner.input_register_bank"] = neutral_row(
                "ngui.owner.input_register_bank",
                input_prior,
                input_result,
            )
            interaction_prior, interaction_result = (
                self.interactions.reset_runtime_state()
            )
            rows["ngui.owner.interaction_register"] = neutral_row(
                "ngui.owner.interaction_register",
                interaction_prior,
                interaction_result,
            )
            profile_prior, profile_result, _neutral_batches = (
                self.profile.reset_runtime_state(now=time())
            )
            rows["ngui.owner.profile_authority"] = neutral_row(
                "ngui.owner.profile_authority",
                profile_prior,
                profile_result,
            )
            presentation_prior, presentation_result = (
                self.presentation.reset_runtime_state()
            )
            rows["ngui.owner.presentation_authority"] = neutral_row(
                "ngui.owner.presentation_authority",
                presentation_prior,
                presentation_result,
            )
            edit_prior, edit_result = self.edits.reset_runtime_state()
            rows["ngui.owner.edit_register"] = neutral_row(
                "ngui.owner.edit_register",
                edit_prior,
                edit_result,
            )
            manual_prior, manual_result = (
                self._manual_lifecycle.reset_runtime_state()
            )
            rows["ngui.owner.manual_action_lifecycle"] = neutral_row(
                "ngui.owner.manual_action_lifecycle",
                manual_prior,
                manual_result,
            )
            event_prior, event_result = (
                self._operator_events.reset_runtime_state()
            )
            rows["ngui.owner.operator_event_feed"] = neutral_row(
                "ngui.owner.operator_event_feed",
                event_prior,
                event_result,
            )
            voice_prior, voice_result = (
                self._sinr_voice.reset_runtime_state()
            )
            rows["ngui.owner.voice_lifecycle"] = neutral_row(
                "ngui.owner.voice_lifecycle",
                voice_prior,
                voice_result,
            )
            chart_prior, chart_result = self.charts.reset_runtime_state()
            rows["ngui.owner.chart_processor"] = neutral_row(
                "ngui.owner.chart_processor",
                chart_prior,
                chart_result,
            )
            heavy_prior, heavy_result = self.heavy_work.reset_runtime_state(
                worker_generation=worker_generation,
            )
            external_prior, external_result = (
                self.external_actions.reset_runtime_state(
                    worker_generation=worker_generation,
                )
            )
            if heavy_result != {
                "runtime_generation": worker_generation,
                "sequence": 0,
                "inflight": (),
                "register": {
                    "progress": {},
                    "results": {},
                    "terminals": {},
                    "terminal_receipts": {},
                },
            }:
                raise RuntimeError(
                    "ngui_reset_heavy_work_not_neutral"
                )
            if external_result != {
                "runtime_generation": worker_generation,
                "sequence": 0,
                "inflight": (),
                "register": {
                    "results": {},
                    "terminal_receipts": {},
                },
            }:
                raise RuntimeError(
                    "ngui_reset_external_action_not_neutral"
                )
            rows["ngui.owner.utility_processors"] = neutral_row(
                "ngui.owner.utility_processors",
                {
                    "heavy_work": heavy_prior,
                    "external_actions": external_prior,
                },
                {"state": "neutral"},
            )

            with self._lock:
                orchestrator_prior = self._runtime_reset_observation_unlocked()
                self._login = None
                self._pending_connect_network = None
                self._network_settings_monitoring = False
                self._network_recovery_read_token = None
                self._network_choice_sequence += 1
                self._login_pending = False
                self._connection_stop_pending = False
                self._login_identity_confirmed = False
                self._confirmed_serial = ""
                self._confirmed_profile_id = ""
                self._reset_login_completion_unlocked()
                self._wifi_action_sequence = 0
                self._operator_connectivity_transition_sequence = 0
                self._operator_internet_outage_key = None
                self._operator_runtime_fact_state = {}
                self._operator_pending_radio_command_root = ""
                self._operator_pending_radio_actor = ""
                self._operator_pending_radio_target = {}
                self._operator_pending_radio_pband_path = []
                self._operator_severe_congestion_started_at = 0.0
                self._operator_severe_congestion_announced = False
                self._operator_severe_congestion_recovery_started_at = 0.0
                self._operator_service_state = ""
                self._operator_manual_signature = None
                self._manual_action_sequence = 0
                self._pending_lifecycle_application = None
                self._manual_projection_rejected_revision = None
                self._runtime_worker_health = {}
                self._runtime_worker_health_signature = ()
                self._runtime_worker_health_fault_generation = 0
                self._pending_runtime_worker_health_transition = None
                self._settings_sequence = 0
                self._license_sequence = 0
                self._last_settings_values = {}
                self._applied_runtime_namespace = ("", "")
                self._last_chart_source_sequence = 0
                self._last_chart_source_cursor = (0, 0)
                self._pending_chart_front = None
                self._pending_page_truth_front = None
                self._last_page_truth_admitted_sequence = 0
                self._last_consumed_input_generation = -1
                self._last_presented_input_generation = -1
                self._last_presented_chart_sequence = 0
                self._runtime_generation_coordinator = None
                self._runtime_front_view = None
                orchestrator_result = (
                    self._runtime_reset_observation_unlocked()
                )
            if not self._runtime_reset_observation_is_neutral(
                orchestrator_result
            ):
                raise RuntimeError(
                    "ngui_reset_orchestrator_state_not_neutral"
                )
            rows["ngui.owner.orchestrator_runtime_state"] = neutral_row(
                "ngui.owner.orchestrator_runtime_state",
                orchestrator_prior,
                {"state": "neutral"},
            )

            page_truth_rotation = (
                PageTruthGenerationRotationReceipt.from_payload(
                    self.ports.rotate_page_truth_generation(
                        worker_generation,
                        transaction_digest,
                        2.0,
                    )
                )
            )
            rows["ngui.owner.page_truth_generation"] = neutral_row(
                "ngui.owner.page_truth_generation",
                page_truth_rotation.prior_termination.to_payload(),
                {"state": "fresh_generation"},
            )

            with self._lock:
                self._parent_async_generation = worker_generation
                self._parent_async_admission_open = True
            rows["ngui.owner.parent_async_fence"] = neutral_row(
                "ngui.owner.parent_async_fence",
                {
                    "previous_generation": previous_worker_generation,
                    "admission_open": False,
                },
                {"state": "active_successor"},
            )

            return RuntimeResetApplicationReceipt(
                application_lease=application_lease,
                worker_generation=worker_generation,
                previous_worker_generation=previous_worker_generation,
                cause=cause,
                manifest_digest=manifest_digest,
                transaction_digest=transaction_digest,
                page_truth_rotation=page_truth_rotation,
                row_results=tuple(
                    rows[state_id]
                    for state_id in RUNTIME_RESET_MANIFEST
                ),
                applied_at=time(),
            )

    def _runtime_reset_observation_unlocked(self) -> Mapping[str, Any]:
        return {
            "login": {
                "active": self._login is not None,
                "profile_id": (
                    self._login.contract.profile_id
                    if self._login is not None
                    else ""
                ),
                "observe_only": (
                    self._login.observe_only
                    if self._login is not None
                    else False
                ),
            },
            "pending_connect_network": self._pending_connect_network is not None,
            "login_pending": self._login_pending,
            "connection_stop_pending": self._connection_stop_pending,
            "login_identity_confirmed": self._login_identity_confirmed,
            "confirmed_serial": self._confirmed_serial,
            "confirmed_profile_id": self._confirmed_profile_id,
            "login_completion_key": self._login_completion_key,
            "login_completion_state": self._login_completion_state,
            "wifi_action_sequence": self._wifi_action_sequence,
            "operator_connectivity_transition_sequence": (
                self._operator_connectivity_transition_sequence
            ),
            "operator_internet_outage_key": (
                self._operator_internet_outage_key
            ),
            "operator_runtime_fact_state": dict(
                self._operator_runtime_fact_state
            ),
            "operator_pending_radio_command_root": (
                self._operator_pending_radio_command_root
            ),
            "operator_pending_radio_actor": self._operator_pending_radio_actor,
            "operator_pending_radio_target": dict(
                self._operator_pending_radio_target
            ),
            "operator_pending_radio_pband_path": tuple(
                self._operator_pending_radio_pband_path
            ),
            "operator_severe_congestion_started_at": (
                self._operator_severe_congestion_started_at
            ),
            "operator_severe_congestion_announced": (
                self._operator_severe_congestion_announced
            ),
            "operator_severe_congestion_recovery_started_at": (
                self._operator_severe_congestion_recovery_started_at
            ),
            "operator_service_state": self._operator_service_state,
            "operator_manual_signature": self._operator_manual_signature,
            "manual_action_sequence": self._manual_action_sequence,
            "pending_lifecycle_application": (
                self._pending_lifecycle_application is not None
            ),
            "manual_projection_rejected_revision": (
                self._manual_projection_rejected_revision
            ),
            "runtime_worker_health": dict(self._runtime_worker_health),
            "runtime_worker_health_signature": (
                self._runtime_worker_health_signature
            ),
            "runtime_worker_health_fault_generation": (
                self._runtime_worker_health_fault_generation
            ),
            "settings_sequence": self._settings_sequence,
            "license_sequence": self._license_sequence,
            "last_settings_values": dict(self._last_settings_values),
            "applied_runtime_namespace": self._applied_runtime_namespace,
            "last_chart_source_sequence": self._last_chart_source_sequence,
            "last_chart_source_cursor": self._last_chart_source_cursor,
            "pending_chart_front_sequence": int(
                getattr(self._pending_chart_front, "sequence", 0) or 0
            ),
            "pending_page_truth_front_sequence": int(
                getattr(self._pending_page_truth_front, "sequence", 0)
                or 0
            ),
            "last_page_truth_admitted_sequence": (
                self._last_page_truth_admitted_sequence
            ),
            "last_consumed_input_generation": (
                self._last_consumed_input_generation
            ),
            "last_presented_input_generation": (
                self._last_presented_input_generation
            ),
            "last_presented_chart_sequence": (
                self._last_presented_chart_sequence
            ),
        }

    @staticmethod
    def _require_runtime_reset_owner_neutral(
        state_id: str,
        value: Any,
    ) -> None:
        if state_id == "ngui.owner.input_register_bank":
            expected = {
                "generation": 0,
                "published_at": 0.0,
                "values": {},
                "owner_sequences": {},
                "owner_fingerprints": {},
                "source_owner_sequences": {},
                "runtime_session_scope_key": "",
                "runtime_source_epoch": "",
                "matrix_identity": {},
                "accepted_source_sampled_at": 0.0,
            }
            if value != expected:
                raise RuntimeError(
                    "ngui_reset_input_register_bank_not_neutral"
                )
            return
        if state_id == "ngui.owner.interaction_register":
            expected_lanes = {
                "MANUAL_NMI": (),
                "NORMAL": (),
                "BACKGROUND": (),
            }
            expected_counts = {
                "MANUAL_NMI": 0,
                "NORMAL": 0,
                "BACKGROUND": 0,
            }
            if value != {
                "sequence": 0,
                "pending": expected_lanes,
                "pending_bytes": expected_counts,
                "high_water_count": expected_counts,
                "high_water_bytes": expected_counts,
                "rejected_count": expected_counts,
            }:
                raise RuntimeError(
                    "ngui_reset_interaction_register_not_neutral"
                )
            return
        if state_id == "ngui.owner.profile_authority":
            lifecycle = value.get("login_lifecycle", {})
            if not (
                set(value.get("sequences", {}).values()) == {1}
                and value.get("selected_profile") == ""
                and value.get("runtime_mode") == "Not connected"
                and value.get("discovery_suggestion") == {}
                and value.get("login_status") == "Enter router details"
                and lifecycle.get("state") == "draft"
                and lifecycle.get("connected") is False
                and lifecycle.get("message") == "Enter router details"
            ):
                raise RuntimeError(
                    "ngui_reset_profile_authority_not_neutral"
                )
            return
        if state_id == "ngui.owner.presentation_authority":
            front = value.get("front")
            if not (
                value.get("processor")
                == NGUIPresentationProcessor().runtime_reset_observation()
                and isinstance(front, Mapping)
                and front == {
                    "sequence": 0,
                    "input_generation": 0,
                    "runtime_session_scope_key": "",
                    "runtime_source_epoch": "",
                    "accepted_modem_source_sequence": 0,
                    "accepted_modem_sampled_at": 0.0,
                    "published_at": 0.0,
                    "fields": {},
                    "pages": {},
                    "chart_snapshot": None,
                    "chart_status": {},
                    "matrix_identity": None,
                }
            ):
                raise RuntimeError(
                    "ngui_reset_presentation_authority_not_neutral"
                )
            return
        if state_id == "ngui.owner.edit_register":
            if value != {
                "generation": 0,
                "accepted": {},
                "accepted_sequences": {},
                "drafts": {},
                "invalid_drafts": {},
                "pending": {},
                "external_pending": {},
                "scopes": {},
                "terminal_receipts": {},
                "request_sequence": 0,
            }:
                raise RuntimeError(
                    "ngui_reset_edit_register_not_neutral"
                )
            return
        if state_id == "ngui.owner.manual_action_lifecycle":
            _prior, expected = (
                NGUIActionLifecycleProjection().reset_runtime_state()
            )
            if value != expected:
                raise RuntimeError(
                    "ngui_reset_manual_action_lifecycle_not_idle"
                )
            return
        if state_id == "ngui.owner.operator_event_feed":
            _prior, expected = NGUIOperatorEventFeed().reset_runtime_state()
            if value != expected:
                raise RuntimeError(
                    "ngui_reset_operator_event_feed_not_neutral"
                )
            return
        if state_id == "ngui.owner.voice_lifecycle":
            _prior, expected = NGUISINRVoiceLifecycle().reset_runtime_state()
            if value != expected:
                raise RuntimeError(
                    "ngui_reset_voice_lifecycle_not_inactive"
                )
            return
        if state_id == "ngui.owner.chart_processor":
            if value != {
                "request_sequence": 0,
                "history_source_sequence": 0,
                "history_source_cursor": (0, 0),
                "history_append_ts": 0.0,
                "session_scope_key": "",
                "source_epoch": "",
                "render_active": False,
                "history": {
                    "timestamps": (),
                    "samples": (),
                    "markers": (),
                    "outages": (),
                    "measurement_outages": (),
                    "mobile_off_intervals": (),
                    "mobile_off_started": None,
                    "identity": {},
                    "session_scope_key": "",
                    "source_epoch": "",
                    "seed_next_chunk": {},
                    "historical_seed_count": 0,
                    "last_seed_transaction_id": "",
                },
                "output": {
                    "expected_sequence": 0,
                    "session_scope_key": "",
                    "source_epoch": "",
                    "snapshot": None,
                },
                "worker": {
                    "active_request": None,
                    "pending_request": None,
                    "coalesced": 0,
                    "retry_streak": 0,
                    "closed": False,
                },
            }:
                raise RuntimeError(
                    "ngui_reset_chart_processor_not_neutral"
                )
            return
        if value != RUNTIME_RESET_MANIFEST[state_id].neutral_seed:
            raise RuntimeError(
                "ngui_reset_owner_not_neutral:" + state_id
            )

    @staticmethod
    def _runtime_reset_observation_is_neutral(
        value: Mapping[str, Any],
    ) -> bool:
        return bool(
            value.get("login") == {
                "active": False,
                "profile_id": "",
                "observe_only": False,
            }
            and value.get("pending_connect_network") is False
            and value.get("login_pending") is False
            and value.get("connection_stop_pending") is False
            and value.get("login_identity_confirmed") is False
            and value.get("confirmed_serial") == ""
            and value.get("confirmed_profile_id") == ""
            and value.get("login_completion_key") is None
            and value.get("login_completion_state") == "idle"
            and value.get("wifi_action_sequence") == 0
            and value.get("operator_connectivity_transition_sequence") == 0
            and value.get("operator_internet_outage_key") is None
            and value.get("operator_runtime_fact_state") == {}
            and value.get("operator_pending_radio_command_root") == ""
            and value.get("operator_pending_radio_actor") == ""
            and value.get("operator_pending_radio_target") == {}
            and value.get("operator_pending_radio_pband_path") == ()
            and value.get("operator_service_state") == ""
            and value.get("operator_manual_signature") is None
            and value.get("manual_action_sequence") == 0
            and value.get("pending_lifecycle_application") is False
            and value.get("manual_projection_rejected_revision") is None
            and value.get("runtime_worker_health") == {}
            and value.get("runtime_worker_health_signature") == ()
            and value.get("runtime_worker_health_fault_generation") == 0
            and value.get("settings_sequence") == 0
            and value.get("license_sequence") == 0
            and value.get("last_settings_values") == {}
            and value.get("applied_runtime_namespace") == ("", "")
            and value.get("last_chart_source_sequence") == 0
            and value.get("pending_chart_front_sequence") == 0
            and value.get("pending_page_truth_front_sequence") == 0
            and value.get("last_page_truth_admitted_sequence") == 0
            and value.get("last_consumed_input_generation") == -1
            and value.get("last_presented_input_generation") == -1
            and value.get("last_presented_chart_sequence") == 0
        )

    def start(self) -> None:
        with self._lock:
            if self._interaction_thread is not None:
                raise RuntimeError("NGUI orchestrator already started")
            self._stop.clear()
            self._shutdown_receipt = None
            published_at = time()
            self._publish_local_batches(
                *self.profile.neutral_publications(now=published_at)
            )
            self._publish_license_state(now=published_at)
            self._publish_gui_settings(published_at, force=True)
            initial_inputs = self.inputs.snapshot()
            initial_front = self.presentation.publish(
                initial_inputs,
                now=published_at,
            )
            self._last_consumed_input_generation = initial_inputs.generation
            self._last_presented_input_generation = initial_inputs.generation
            self._last_presented_chart_sequence = int(
                getattr(initial_front.chart_snapshot, "request_sequence", 0)
                or 0
            )
            self._submit_page_truth(
                initial_front,
                operation="startup_page_truth",
            )
            self._interaction_thread = Thread(
                target=self._interaction_loop,
                name="ngui-interactions",
                daemon=True,
            )
            self._presentation_thread = Thread(
                target=self._presentation_loop,
                name="ngui-presentation",
                daemon=True,
            )
            self._worker_health.mark_started("ngui_interactions")
            self._worker_health.mark_started("ngui_presentation")
            self._worker_health.mark_started("ngui_profile_lifecycle")
            self._interaction_thread.start()
            self._presentation_thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> Mapping[str, Any]:
        existing = self._shutdown_receipt
        if (
            isinstance(existing, dict)
            and existing.get("finished_at")
            and existing.get("status") == "complete"
        ):
            return dict(existing)
        started = monotonic()
        deadline = started + max(0.05, float(timeout_s))
        receipt: dict[str, Any] = {
            "schema": SHUTDOWN_RECEIPT_SCHEMA,
            "owner": SHUTDOWN_RECEIPT_OWNER,
            "started_at": time(),
            "finished_at": 0.0,
            "budget_s": max(0.05, float(timeout_s)),
            "status": "running",
            "phases": [],
        }
        self._shutdown_receipt = receipt

        def phase(
            name: str,
            fn: Callable[[], Any],
            *,
            must_attempt: bool = False,
        ) -> Any:
            phase_started = monotonic()
            try:
                remaining = max(0.0, deadline - monotonic())
                if remaining <= 0.0 and not must_attempt:
                    raise TimeoutError(
                        f"{name} skipped: NGUI shutdown budget exhausted"
                    )
                detail = fn()
                status = "ok"
                error = ""
            except Exception as exc:
                detail = None
                status = "fault"
                error = f"{type(exc).__name__}: {exc}"
                # The shutdown receipt already carries the failure. Calling the
                # UI error port here could make teardown unbounded after this
                # phase has exhausted its time budget.
            receipt["phases"].append(
                {
                    "name": name,
                    "status": status,
                    "duration_ms": round((monotonic() - phase_started) * 1000.0, 3),
                    "detail": detail,
                    "error": error,
                }
            )
            return detail

        phase(
            "close_operational_admission",
            self.runtime.close_operational_admission,
        )
        runtime_reserve_s = min(
            2.0,
            max(0.25, float(timeout_s) * 0.40),
        )
        drain_deadline = max(
            monotonic(),
            deadline - runtime_reserve_s,
        )
        while (
            self.runtime.operational_pending_count() > 0
            and monotonic() < drain_deadline
        ):
            Event().wait(
                min(
                    0.02,
                    max(0.0, drain_deadline - monotonic()),
                )
            )
        runtime_receipt = phase(
            "runtime",
            lambda: self.runtime.stop(
                timeout_s=max(0.05, deadline - monotonic())
            ),
            must_attempt=True,
        )
        self._stop.set()
        self.inputs.wake_waiters()
        for thread in (self._interaction_thread, self._presentation_thread):
            if thread is not None and thread is not current_thread():
                phase(
                    f"join_{thread.name}",
                    lambda thread=thread: thread.join(
                        timeout=max(0.0, deadline - monotonic())
                    ),
                )
        live_threads = [
            thread.name
            for thread in (self._interaction_thread, self._presentation_thread)
            if thread is not None and thread.is_alive()
        ]
        phase("charts", self.charts.close)
        if "ngui-presentation" in live_threads:
            receipt["phases"].append(
                {
                    "name": "page_truth",
                    "status": "fault",
                    "duration_ms": 0.0,
                    "detail": {
                        "schema": "NGUIPageTruthWriterTerminationReceipt.v1",
                        "owner": "ngui_page_truth_writer",
                        "admission_closed": False,
                        "terminated": False,
                        "status": "degraded",
                        "pending_sequence": 0,
                        "last_written_sequence": 0,
                        "deadline_exceeded": True,
                    },
                    "error": (
                        "page-truth admission remains open because its "
                        "presentation producer survived"
                    ),
                }
            )
        else:
            pending_page_truth = self._pending_page_truth_front
            if pending_page_truth is not None:
                phase(
                    "page_truth_final_admission",
                    self._retry_pending_page_truth,
                )
            phase(
                "page_truth",
                lambda: self.ports.close_page_truth(
                    max(0.0, deadline - monotonic())
                ),
            )
        phase("heavy_work", self.heavy_work.close)
        phase("external_actions", self.external_actions.close)
        phase(
            "profile_lifecycle",
            self._close_profile_lifecycle,
        )
        with self._lock:
            if not live_threads:
                self._interaction_thread = None
                self._presentation_thread = None
        receipt["finished_at"] = time()
        receipt["duration_s"] = round(monotonic() - started, 3)
        receipt["budget_exceeded"] = monotonic() > deadline
        receipt["live_threads"] = live_threads
        unfinished_owners = [
            str(row["detail"].get("owner") or row["name"])
            for row in receipt["phases"]
            if isinstance(row.get("detail"), Mapping)
            and row["detail"].get("terminated") is False
        ]
        receipt["unfinished_owners"] = unfinished_owners
        degraded_owners = [
            str(row["detail"].get("owner") or row["name"])
            for row in receipt["phases"]
            if isinstance(row.get("detail"), Mapping)
            and str(row["detail"].get("status") or "complete") != "complete"
        ]
        if self._pending_page_truth_front is not None:
            degraded_owners.append("ngui_page_truth_admission_outbox")
        receipt["degraded_owners"] = degraded_owners
        runtime_terminated = bool(
            isinstance(runtime_receipt, Mapping)
            and runtime_receipt.get("terminated") is True
        )
        runtime_clean = bool(
            runtime_terminated
            and runtime_receipt.get("status") == "complete"
        )
        receipt["status"] = (
            "complete"
            if runtime_clean
            and not live_threads
            and not unfinished_owners
            and not degraded_owners
            and all(row["status"] == "ok" for row in receipt["phases"])
            else "degraded"
        )
        self._shutdown_receipt = dict(receipt)
        return dict(receipt)

    def _submit_profile_task(
        self, fn: Callable[..., Any], *args: Any,
        presentation_locked: bool = True,
    ) -> Future[Any]:
        with self._lock:
            if self._profile_lifecycle_closed:
                raise RuntimeError("profile-lifecycle admission is closed")
            if not self._parent_async_admission_open:
                raise RuntimeError(
                    "profile-lifecycle runtime-generation admission is closed"
                )
            generation = self._parent_async_generation

            def generation_fenced_call() -> Any:
                with self._presentation_transaction_lock:
                    with self._lock:
                        if (
                            not self._parent_async_admission_open
                            or self._parent_async_generation != generation
                        ):
                            raise CancelledError(
                                "profile-lifecycle completion belongs to "
                                "a retired runtime generation"
                            )
                    if presentation_locked:
                        return fn(*args)
                # Bounded runtime termination must let receiver threads finish
                # their presentation callbacks. The cancellation task locks its
                # own publications, never the physical stop/join interval.
                return fn(*args)

            future = self._profile_lifecycle_executor.submit(
                generation_fenced_call
            )
            self._profile_lifecycle_futures.add(future)
            future.add_done_callback(self._observe_profile_future)
            return future

    def _reset_login_completion_unlocked(self) -> None:
        self._login_completion_key = None
        self._login_completion_state = "idle"
        self._login_completion_payload = None
        self._login_completion_retry_not_before = 0.0

    def _schedule_pending_login_completion(self) -> bool:
        with self._lock:
            key = self._login_completion_key
            payload = self._login_completion_payload
            if (
                key is None
                or payload is None
                or self._login_completion_state != "pending"
                or self._profile_lifecycle_closed
                or self._stop.is_set()
                or monotonic() < self._login_completion_retry_not_before
            ):
                return False
            self._login_completion_state = "inflight"
            login, canonical_identity = payload
        try:
            future = self._submit_profile_task(
                self._complete_identity_login,
                key,
                login,
                canonical_identity,
            )
        except Exception:
            with self._lock:
                if self._login_completion_key == key:
                    self._login_completion_state = "pending"
                    self._login_completion_retry_not_before = monotonic() + 0.25
            raise
        future.add_done_callback(
            lambda completed, completion_key=key: (
                self._observe_identity_login_completion(
                    completion_key,
                    completed,
                )
            )
        )
        return True

    def _observe_identity_login_completion(
        self,
        key: tuple[str, str, str, str, str, str],
        future: Future[Any],
    ) -> None:
        succeeded = not future.cancelled() and future.exception() is None
        with self._lock:
            if self._login_completion_key != key:
                return
            if succeeded:
                self._login_completion_state = "complete"
                self._login_completion_retry_not_before = 0.0
            else:
                self._login_completion_state = "pending"
                self._login_completion_retry_not_before = monotonic() + 0.25
        self.inputs.wake_waiters()

    def _require_login_completion_key(
        self,
        key: tuple[str, str, str, str, str, str],
    ) -> None:
        with self._lock:
            if (
                self._login_completion_key != key
                or self._login_completion_state != "inflight"
                or self._applied_runtime_namespace != key[:2]
                or not self._login_identity_confirmed
                or self._confirmed_serial != key[2]
                or self._login is None
                or self._login.contract.profile_id != key[3]
            ):
                raise RuntimeError(
                    "ngui_login_completion_generation_retired"
                )

    def _login_action_identity_ready_unlocked(self) -> bool:
        key = self._login_completion_key
        return bool(
            key is not None
            and self._login_completion_state == "complete"
            and self._applied_runtime_namespace == key[:2]
            and self._login_identity_confirmed
            and self._confirmed_serial == key[2]
            and self._confirmed_profile_id == key[3]
            and self._login is not None
            and self._login.contract.profile_id == key[3]
        )

    def _observe_profile_future(self, future: Future[Any]) -> None:
        if future.cancelled():
            self._worker_health.mark_stopped(
                "ngui_profile_lifecycle",
                reason="profile_task_cancelled_during_shutdown",
            )
        else:
            try:
                future.result()
            except CancelledError:
                # The generation fence refused obsolete work before execution.
                # It must not change the health of the replacement generation.
                log.debug("NGUI profile task cancelled at the runtime-generation fence")
            except Exception as exc:
                self._record_worker_failure(
                    "ngui_profile_lifecycle",
                    "profile_task",
                    exc,
                    retry_s=0.1,
                )
            else:
                self._worker_health.mark_success("ngui_profile_lifecycle")
        with self._lock:
            self._profile_lifecycle_futures.discard(future)

    def _close_profile_lifecycle(self) -> Mapping[str, Any]:
        with self._lock:
            self._profile_lifecycle_closed = True
        self._profile_lifecycle_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        with self._lock:
            inflight = sum(
                not future.done() for future in self._profile_lifecycle_futures
            )
        self._worker_health.mark_stopped(
            "ngui_profile_lifecycle",
            reason="profile_lifecycle_closed",
        )
        return {
            "owner": "ngui_profile_lifecycle",
            "admission_closed": True,
            "inflight": inflight,
            "terminated": inflight == 0,
        }

    def _record_worker_failure(
        self,
        worker_id: str,
        operation: str,
        exc: Exception,
        *,
        retry_s: float,
    ) -> None:
        record = self._worker_health.mark_failure(
            worker_id,
            exc,
            next_retry_at=time() + max(0.0, float(retry_s)),
        )
        fault = self._fault_supervisor.record(
            boundary=FaultBoundaryClass.GUI,
            owner=worker_id,
            operation=operation,
            generation=record.success_count + record.failure_count,
            severity=FaultSeverity.ERROR,
            retryable=True,
            degraded_behavior="ngui_worker_retries_without_false_page_truth",
            error=exc,
            suppression_budget=3,
        )
        if not fault.budget_exhausted:
            self.ports.report_error(
                f"{operation}: {fault.error_type}: {fault.detail}"
            )

    def _interaction_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    intents = self.interactions.wait_and_drain(timeout_s=0.05)
                    for intent in intents:
                        try:
                            with self._presentation_transaction_lock:
                                declared = INTENT_REGISTRY[intent.kind]
                                if intent.destination is not declared.destination:
                                    raise RuntimeError(
                                        "NGUI intent destination mismatch: "
                                        f"{intent.kind.value}"
                                    )
                                self._handlers[intent.kind](intent)
                                if self._record_intent_event(intent):
                                    self._publish_gui_settings(
                                        time(),
                                        force=True,
                                    )
                        except UserIntentInputError as exc:
                            with self._presentation_transaction_lock:
                                if self._record_intent_failure(intent, exc):
                                    self._publish_gui_settings(
                                        time(),
                                        force=True,
                                    )
                        except Exception as exc:
                            self._record_worker_failure(
                                "ngui_interactions",
                                intent.kind.value,
                                exc,
                                retry_s=0.0,
                            )
                            with self._presentation_transaction_lock:
                                if self._record_intent_failure(intent, exc):
                                    self._publish_gui_settings(
                                        time(),
                                        force=True,
                                    )
                    self._worker_health.mark_success("ngui_interactions")
                except Exception as exc:
                    self._record_worker_failure(
                        "ngui_interactions",
                        "interaction_loop",
                        exc,
                        retry_s=0.05,
                    )
                    self._stop.wait(0.05)
        finally:
            self._worker_health.mark_stopped(
                "ngui_interactions",
                reason="orchestrator_stop_requested",
            )

    def _presentation_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._presentation_iteration()
                    self._worker_health.mark_success("ngui_presentation")
                except Exception as exc:
                    self._record_worker_failure(
                        "ngui_presentation",
                        "presentation_iteration",
                        exc,
                        retry_s=self.PRESENT_INTERVAL_S,
                    )
                    self._stop.wait(self.PRESENT_INTERVAL_S)
        finally:
            self._worker_health.mark_stopped(
                "ngui_presentation",
                reason="orchestrator_stop_requested",
            )

    def _presentation_iteration(self) -> None:
        with self._presentation_transaction_lock:
            after_generation = self._last_consumed_input_generation
        self.inputs.wait_for_generation(
            after_generation,
            timeout_s=self.PRESENT_INTERVAL_S,
        )
        with self._presentation_transaction_lock:
            self._presentation_iteration_locked()

    def _presentation_iteration_locked(self) -> None:
        if self._stop.is_set():
            return
        self._retry_pending_chart_front()
        self._retry_pending_page_truth()
        self._schedule_pending_login_completion()
        self._schedule_pending_network_settings()
        self._publish_gui_settings(time())
        inputs = self.inputs.snapshot()
        published_at = time()
        chart_snapshot = self.charts.output.snapshot()
        chart_sequence = int(
            getattr(chart_snapshot, "request_sequence", 0) or 0
        )
        coordinator = self._runtime_generation_coordinator
        if coordinator is not None:
            accepted = coordinator.snapshot()
            if inputs != accepted.input_snapshot:
                raise RuntimeError(
                    "ngui_presentation_uncoordinated_input_bypass"
                )
            if inputs.generation > self._last_consumed_input_generation:
                raise RuntimeError(
                    "ngui_presentation_direct_input_route_retired"
                )
            if chart_sequence <= self._last_presented_chart_sequence:
                return
            self._commit_chart_result_transition(
                coordinator,
                inputs=inputs,
                chart_sequence=chart_sequence,
                accepted_at=published_at,
            )
            return
        if inputs.matrix_identity is not None:
            raise RuntimeError(
                "ngui_presentation_runtime_root_requires_coordinator"
            )
        if (
            inputs.generation <= self._last_consumed_input_generation
            and chart_sequence <= self._last_presented_chart_sequence
        ):
            return
        input_changed = (
            inputs.generation > self._last_consumed_input_generation
        )
        if input_changed:
            frame = self.presentation.publish(inputs, now=published_at)
        else:
            frame = self.presentation.publish_chart_update(
                inputs,
                accepted_front=self.presentation.front_register.snapshot(),
                now=published_at,
            )
        self._commit_visible_lifecycle_application(frame)
        self._last_consumed_input_generation = inputs.generation
        self._last_presented_input_generation = inputs.generation
        self._last_presented_chart_sequence = chart_sequence
        developer_control = frame.pages.get(PageId.DEVELOPER_CONTROL)
        if developer_control is not None:
            self.edits.reconcile(developer_control.fields)
        self._admit_chart_front(frame, requested_at=published_at)
        self._submit_page_truth(
            frame,
            operation="presentation_page_truth",
        )

    def _commit_chart_result_transition(
        self,
        coordinator: NGUIRuntimeGenerationCoordinator,
        *,
        inputs: InputRegisterSnapshot,
        chart_sequence: int,
        accepted_at: float,
    ) -> None:
        """Commit chart evidence, Edit readback, and PageTruth together."""

        accepted = coordinator.snapshot()
        if (
            inputs != accepted.input_snapshot
            or self.presentation.front_register.snapshot()
            != accepted.front
            or self.edits.snapshot() != accepted.edits
        ):
            raise RuntimeError(
                "ngui_chart_result_replica_root_mismatch"
            )
        presentation_candidate = self.presentation.prepare_chart_candidate(
            inputs,
            accepted_front=accepted.front,
            now=accepted_at,
        )
        chart_cause = NGUIFrozenValue.from_value(
            front_register_payload(presentation_candidate.front)
        )
        candidate = coordinator.prepare_chart_result(
            cause_identity=(
                "chart_result:"
                f"{inputs.runtime_session_scope_key}:"
                f"{inputs.runtime_source_epoch}:"
                f"{chart_sequence}"
            ),
            cause_digest=chart_cause.digest,
            prepared=NGUIRuntimeGenerationPreparedComponents(
                input_snapshot=accepted.input_snapshot,
                presentation_policy_state=(
                    accepted.presentation_policy_state
                ),
                front=presentation_candidate.front,
                edits=accepted.edits,
                lifecycle_state=accepted.lifecycle_state,
                command_context=accepted.command_context,
            ),
            accepted_at=accepted_at,
        )
        coordinator.commit_atomic_reentry(candidate)
        self.presentation.commit_chart_candidate(
            presentation_candidate
        )
        self._last_presented_chart_sequence = chart_sequence
        self._drain_runtime_generation_outbox(
            coordinator,
            NGUIOutboxKind.PAGE_TRUTH,
            requested_at=accepted_at,
        )

    def _admit_chart_front(
        self,
        frame: FrontRegisterSnapshot,
        *,
        requested_at: float,
    ) -> bool:
        """Reconcile an accepted Front with the bounded chart input owner."""

        chart_source_cursor = chart_front_cursor(frame)
        if chart_source_cursor == (0, 0):
            return True
        if compare_chart_front_cursors(
            chart_source_cursor,
            self._last_chart_source_cursor,
        ) <= 0:
            pending = self._pending_chart_front
            if (
                pending is not None
                and compare_chart_front_cursors(
                    chart_front_cursor(pending),
                    self._last_chart_source_cursor,
                ) <= 0
            ):
                self._pending_chart_front = None
            return True
        chart_source_sequence = int(frame.sequence)
        try:
            self.charts.bind_runtime_namespace(
                frame.runtime_session_scope_key,
                frame.runtime_source_epoch,
            )
            self.charts.append(frame)
            self.charts.refresh_if_active(
                window_seconds=self._chart_window_s,
                requested_at=requested_at,
            )
        except Exception as exc:
            pending = self._pending_chart_front
            first_failure_for_generation = (
                pending is None
                or chart_front_cursor(pending) != chart_source_cursor
            )
            if (
                pending is None
                or compare_chart_front_cursors(
                    chart_source_cursor,
                    chart_front_cursor(pending),
                ) >= 0
            ):
                self._pending_chart_front = frame
            self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner="ngui_chart_delivery",
                operation="reconcile_front_generation",
                generation=chart_source_sequence,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior=(
                    "front_truth_remains_live_chart_intake_is_retained"
                ),
                error=exc,
                suppression_budget=3,
            )
            if first_failure_for_generation:
                self.ports.report_error(
                    "NGUI chart intake is retrying; live fields remain active: "
                    f"{type(exc).__name__}: {exc}"
                )
            return False
        self._last_chart_source_sequence = chart_source_sequence
        self._last_chart_source_cursor = chart_source_cursor
        pending = self._pending_chart_front
        if (
            pending is not None
            and compare_chart_front_cursors(
                chart_front_cursor(pending),
                chart_source_cursor,
            ) <= 0
        ):
            self._pending_chart_front = None
        return True

    def _retry_pending_chart_front(self) -> bool:
        pending = self._pending_chart_front
        if pending is None:
            return True
        return self._admit_chart_front(pending, requested_at=time())

    def _on_runtime_frame(
        self,
        frame: RuntimeInputFrame,
    ) -> RuntimeFrameDeliveryReceipt:
        with self._presentation_transaction_lock:
            try:
                return self._deliver_runtime_frame(frame)
            except Exception as exc:
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_runtime_frame_application",
                    operation="complete_runtime_frame",
                    generation=frame.sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=(
                        "last_valid_truth_held_bad_frame_quarantined"
                    ),
                    error=exc,
                    suppression_budget=3,
                )
                try:
                    self.ports.report_error(
                        "NGUI quarantined one invalid runtime frame: "
                        f"{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass
                return self._runtime_frame_failure_receipt(frame)

    def _runtime_frame_failure_receipt(
        self,
        frame: RuntimeInputFrame,
    ) -> RuntimeFrameDeliveryReceipt:
        return build_runtime_frame_delivery_receipt(
            frame,
            front_register_applied=False,
            chart_history_accepted=False,
        )

    def _deliver_runtime_frame(
        self,
        frame: RuntimeInputFrame,
    ) -> RuntimeFrameDeliveryReceipt:
        status = self.runtime.status()
        product_root = status.product_startup_receipt
        if (
            status.state is not RuntimeServiceState.RUNNING
            or product_root is None
            or not product_root.ready
            or product_root.matrix_readiness is None
        ):
            raise RuntimeError(
                "ngui_runtime_frame_product_root_not_admitted"
            )
        modem_live_ready = product_root.matrix_readiness
        if frame.namespace != modem_live_ready.matrix_identity.namespace:
            raise RuntimeError(
                "ngui_runtime_frame_foreign_product_namespace"
            )
        coordinator = self._runtime_generation_coordinator
        if coordinator is None:
            coordinator = NGUIRuntimeGenerationCoordinator(
                modem_live_ready
            )
            self._runtime_generation_coordinator = coordinator
            self._runtime_front_view = NGUIFrontRegisterView(coordinator)

        input_previous = self.inputs.snapshot()
        runtime_input_candidate = (
            NGUIInputRegisterBank.prepare_runtime_frame_candidate(
                frame,
                input_previous,
            )
        )
        accepted_at = time()
        transaction_source = runtime_input_candidate.values.get(
            "command.manual_transaction"
        )
        manual_transaction = _manual_transaction_from_input_value(
            transaction_source.value
            if transaction_source is not None
            else None
        )
        manual_candidate = self._prepare_manual_lifecycle_candidate(
            transaction=manual_transaction,
            chart_status=self.charts.status(),
            now=accepted_at,
        )
        control_availability = self._build_control_availability(
            snapshot=runtime_input_candidate,
            manual_pending=bool(
                manual_candidate.surface.get("action_pending", False)
            ),
        )
        with self._lock:
            visible_manual_sequence = self._manual_action_sequence
        action_values = self._action_surface_values(
            manual_candidate.surface,
            control_availability=control_availability,
            visible_manual_sequence=visible_manual_sequence,
        )
        action_changed = any(
            runtime_input_candidate.values.get(source_id) is None
            or runtime_input_candidate.values[source_id].value != value
            for source_id, value in action_values.items()
        )
        action_sequence = int(
            runtime_input_candidate.owner_sequences.get(
                SourceOwner.GUI_ACTION,
                0,
            )
        )
        if action_changed:
            action_sequence += 1
            action_batch = adapt_owner_register(
                owner=SourceOwner.GUI_ACTION,
                sequence=action_sequence,
                published_at=accepted_at,
                values=action_values,
                sampled_at={
                    source_id: accepted_at for source_id in action_values
                },
                sample_sequences={
                    source_id: action_sequence for source_id in action_values
                },
            )
            action_input_candidate = (
                NGUIInputRegisterBank.prepare_local_batch_candidate(
                    action_batch,
                    runtime_input_candidate,
                )
            )
        else:
            action_input_candidate = runtime_input_candidate
        provisional_presentation_candidate = (
            self.presentation.prepare_runtime_candidate(
                action_input_candidate,
                now=accepted_at,
            )
        )
        developer_control = (
            provisional_presentation_candidate.front.pages.get(
                PageId.DEVELOPER_CONTROL
            )
        )
        edit_candidate = (
            self.edits.prepare_reconcile(developer_control.fields)
            if developer_control is not None
            else None
        )
        edit_snapshot = (
            edit_candidate.snapshot
            if edit_candidate is not None
            else self.edits.snapshot()
        )
        edit_values = {
            "network.apn_profiles": self._apn_library,
            "edit.lifecycle": (
                NGUIEditRegister.lifecycle_payload_from_snapshot(
                    edit_snapshot
                )
            )
        }
        edit_changed = any(
            runtime_input_candidate.values.get(source_id) is None
            or runtime_input_candidate.values[source_id].value != value
            for source_id, value in edit_values.items()
        )
        edit_sequence = int(
            runtime_input_candidate.owner_sequences.get(
                SourceOwner.GUI_EDIT,
                0,
            )
        )
        if edit_changed:
            edit_sequence += 1
            edit_batch = adapt_owner_register(
                owner=SourceOwner.GUI_EDIT,
                sequence=edit_sequence,
                published_at=accepted_at,
                values=edit_values,
                sampled_at={source_id: accepted_at for source_id in edit_values},
                sample_sequences={source_id: edit_sequence for source_id in edit_values},
            )
            input_candidate = (
                NGUIInputRegisterBank.prepare_local_batch_candidate(
                    edit_batch,
                    action_input_candidate,
                )
            )
            presentation_candidate = (
                self.presentation.prepare_runtime_candidate(
                    input_candidate,
                    now=accepted_at,
                )
            )
        else:
            input_candidate = action_input_candidate
            # The candidate already used this immutable input and timestamp.
            # Rebuilding it would duplicate PageTruth without changing a field.
            presentation_candidate = provisional_presentation_candidate
        prepared = NGUIRuntimeGenerationPreparedComponents(
            input_snapshot=input_candidate,
            presentation_policy_state=NGUIFrozenValue.from_value(
                presentation_candidate.policy.policy_state_payload()
            ),
            front=presentation_candidate.front,
            edits=edit_snapshot,
            lifecycle_state=RuntimeCanonicalMapping.from_mapping(
                {
                    "schema": "NGUIRuntimeLifecycleState.v1",
                    "runtime_service_state": status.state.value,
                    "product_root_digest": product_root.root_digest,
                    "runtime_namespace": frame.namespace.as_dict(),
                }
            ),
            command_context=RuntimeCanonicalMapping.from_mapping(
                {
                    "accepted_root_fingerprint": (
                        frame.accepted_root_fingerprint
                    ),
                    "runtime_view": (
                        frame.command_context.to_mapping()
                    ),
                }
            ),
        )
        candidate = coordinator.prepare_runtime_frame(
            frame,
            prepared,
            accepted_at=accepted_at,
        )
        committed_receipt = coordinator.commit_runtime_frame(candidate)
        accepted = coordinator.snapshot()

        if self.inputs.snapshot() != accepted.input_snapshot:
            if accepted.input_snapshot != input_candidate:
                raise RuntimeError(
                    "ngui_runtime_input_replica_replay_mismatch"
                )
            self.inputs.install_prepared_runtime_snapshot(
                input_previous,
                input_candidate,
            )
        if (
            self.presentation.front_register.snapshot()
            != accepted.front
        ):
            if accepted.front != presentation_candidate.front:
                raise RuntimeError(
                    "ngui_runtime_front_replica_replay_mismatch"
                )
            self.presentation.commit_runtime_candidate(
                presentation_candidate
            )
        self._manual_lifecycle.commit_observe(manual_candidate)
        if edit_candidate is not None:
            self.edits.commit_reconcile(edit_candidate)
        with self._lock:
            self._settings_sequence = max(
                self._settings_sequence,
                action_sequence,
                edit_sequence,
            )
            self._last_settings_values = {
                **self._last_settings_values,
                **action_values,
                **edit_values,
            }

        runtime_namespace = (
            frame.session_scope_key,
            frame.source_epoch,
        )
        if self._applied_runtime_namespace not in {
            ("", ""),
            runtime_namespace,
        }:
            raise RuntimeError(
                "ngui_runtime_namespace_requires_worker_replacement"
            )
        self._applied_runtime_namespace = runtime_namespace
        self._observe_committed_login_identity(
            accepted.input_snapshot,
            runtime_namespace=runtime_namespace,
        )
        self._record_runtime_input_events(accepted.input_snapshot)
        self._open_completed_router_check(accepted.input_snapshot)
        self._last_consumed_input_generation = max(
            self._last_consumed_input_generation,
            accepted.input_snapshot.generation,
        )
        self._last_presented_input_generation = max(
            self._last_presented_input_generation,
            accepted.front.input_generation,
        )
        attached_chart_sequence = int(
            getattr(
                accepted.front.chart_snapshot,
                "request_sequence",
                0,
            )
            or 0
        )
        self._last_presented_chart_sequence = max(
            self._last_presented_chart_sequence,
            attached_chart_sequence,
        )
        self._drain_runtime_generation_outbox(
            coordinator,
            NGUIOutboxKind.CHART_WORK,
            requested_at=accepted_at,
        )
        self._drain_runtime_generation_outbox(
            coordinator,
            NGUIOutboxKind.PAGE_TRUTH,
            requested_at=accepted_at,
        )
        try:
            return coordinator.terminal_delivery_receipt(
                accepted.identity
            )
        except RuntimeError:
            return committed_receipt

    def _observe_committed_login_identity(
        self,
        snapshot: InputRegisterSnapshot,
        *,
        runtime_namespace: tuple[str, str],
    ) -> bool:
        """Arm login completion only from committed Matrix identity."""

        with self._lock:
            if self._applied_runtime_namespace != runtime_namespace:
                raise RuntimeError(
                    "ngui_login_identity_namespace_not_committed"
                )
            login = self._login
            identity = _confirmed_identity_payload_from_input_snapshot(
                snapshot,
                login,
            )
            token = str(
                identity.get("serial") or identity.get("imei") or ""
            )
            if login is None or not token:
                return False
            if str(identity.get('profile_id') or '') != login.contract.profile_id:
                raise RuntimeError('ngui_selected_device_profile_admission_mismatch')
            if (
                self._login_identity_confirmed
                and self._confirmed_serial
                and token != self._confirmed_serial
            ):
                raise RuntimeError(
                    "ngui_login_identity_changed_inside_runtime_namespace"
                )
            completion_key = (
                runtime_namespace[0],
                runtime_namespace[1],
                token,
                str(identity['profile_id']),
                str(identity.get("imsi") or ""),
                str(identity.get("provider_plmn") or ""),
            )
            self._login_identity_confirmed = True
            self._confirmed_serial = token
            if self._login_completion_key != completion_key:
                self._login_completion_key = completion_key
                self._login_completion_state = "pending"
                self._login_completion_payload = (
                    login,
                    dict(identity),
                )
                self._login_completion_retry_not_before = 0.0
            elif self._login_completion_state == "pending":
                self._login_completion_payload = (
                    login,
                    dict(identity),
                )
            return True

    def _drain_runtime_generation_outbox(
        self,
        coordinator: NGUIRuntimeGenerationCoordinator,
        kind: NGUIOutboxKind,
        *,
        requested_at: float,
    ) -> bool:
        while True:
            claim = coordinator.claim_outbox(
                kind,
                claimed_at=time(),
            )
            if claim is None:
                return True
            front = claim.intent.front
            if kind is NGUIOutboxKind.CHART_WORK:
                applied = self._admit_chart_front(
                    front,
                    requested_at=requested_at,
                )
            else:
                applied = self._submit_page_truth(
                    front,
                    operation="runtime_page_truth",
                )
            if not applied:
                coordinator.release_outbox_claim(claim)
                return False
            coordinator.complete_outbox(
                claim,
                disposition=NGUIOutboxTerminalDisposition.APPLIED,
                completed_at=time(),
            )

    def _submit_page_truth(
        self,
        frame: FrontRegisterSnapshot,
        *,
        operation: str,
    ) -> bool:
        """Keep each latest Front until the evidence owner accepts it."""

        with self._lock:
            worker_generation = self._parent_async_generation
            admission_open = self._parent_async_admission_open
        if worker_generation <= 0:
            # Before the first runtime reset, no PageTruth writer generation
            # exists. The bootstrap Front is visible GUI state, not failed
            # durable evidence. The first active generation publishes its full
            # current Front through the normal route.
            return True
        if frame.sequence <= self._last_page_truth_admitted_sequence:
            pending = self._pending_page_truth_front
            if pending is not None and pending.sequence <= frame.sequence:
                self._pending_page_truth_front = None
            return True
        pending = self._pending_page_truth_front
        if pending is None or frame.sequence >= pending.sequence:
            self._pending_page_truth_front = frame
        candidate = self._pending_page_truth_front
        if candidate is None:
            return True
        if not admission_open:
            return False
        try:
            self.ports.publish_page_truth(
                candidate,
                worker_generation,
            )
        except Exception as exc:
            self._record_worker_failure(
                "ngui_presentation",
                operation,
                exc,
                retry_s=self.CHART_INTERVAL_S,
            )
            return False
        self._last_page_truth_admitted_sequence = candidate.sequence
        if (
            self._pending_page_truth_front is not None
            and self._pending_page_truth_front.sequence
            <= self._last_page_truth_admitted_sequence
        ):
            self._pending_page_truth_front = None
        return True

    def _retry_pending_page_truth(self) -> bool:
        pending = self._pending_page_truth_front
        if pending is None:
            return True
        return self._submit_page_truth(
            pending,
            operation="retained_page_truth",
        )

    def _record_runtime_worker_health(
        self,
        child_worker_health: Mapping[str, Any],
    ) -> bool:
        candidate_health = dict(child_worker_health)
        degraded_workers = tuple(
            str(item)
            for item in child_worker_health.get("required_unhealthy", ())
            if str(item or "").strip()
        )
        if degraded_workers == self._runtime_worker_health_signature:
            self._runtime_worker_health = candidate_health
            return False
        pending = self._pending_runtime_worker_health_transition
        if pending is not None:
            if pending[0] == candidate_health and pending[1] == degraded_workers:
                return True
            raise RuntimeError(
                "runtime worker-health transition replaced before GUI admission"
            )
        previous = self._runtime_worker_health_signature
        generation = self._runtime_worker_health_fault_generation + 1
        if degraded_workers:
            detail = ", ".join(degraded_workers)
            self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner="ngui_runtime_child_worker_health",
                operation="required_worker_health",
                generation=generation,
                severity=FaultSeverity.WARNING,
                retryable=True,
                degraded_behavior=(
                    "child_runtime_health_truth_retained_ngui_holds_last_accepted_truth"
                ),
                error=RuntimeError(f"required runtime worker unhealthy: {detail}"),
                suppression_budget=3,
            )
        elif previous:
            self._fault_supervisor.record_recovery(
                boundary=FaultBoundaryClass.GUI,
                owner="ngui_runtime_child_worker_health",
                operation="required_worker_health",
                generation=generation,
                detail="required runtime workers recovered: " + ", ".join(previous),
            )
        self._pending_runtime_worker_health_transition = (
            candidate_health,
            degraded_workers,
            generation,
        )
        return True

    def _on_service_status(self, status: RuntimeServiceStatus) -> None:
        if status.state is RuntimeServiceState.RUNNING:
            with self._lock:
                # The process lease records its first Product startup. Worker
                # replacements keep their own typed readiness in RuntimeService.
                if not self._application_ready_acknowledged:
                    self._application_ready_acknowledged = (
                        mark_current_application_product_ready(
                            receipt=status.product_startup_receipt,
                        )
                    )
        if status.state in {
            RuntimeServiceState.STARTING,
            RuntimeServiceState.STOPPING,
            RuntimeServiceState.STOPPED,
        }:
            with self._presentation_transaction_lock:
                self._pending_lifecycle_application = None
        with self._lock:
            identity_confirmed = self._login_identity_confirmed
            login_active = self._login is not None
        if status.state is RuntimeServiceState.LOGIN_REQUIRED or login_active and (
            not identity_confirmed
            or status.state
            in {
                RuntimeServiceState.RECONNECTING,
                RuntimeServiceState.FAILED,
                RuntimeServiceState.STOPPED,
            }
        ):
            self._submit_profile_task(
                self._publish_service_progress,
                status,
            )
        if status.state is RuntimeServiceState.FAILED:
            self.ports.report_error(status.message or "runtime failed")
        if status.state is RuntimeServiceState.IDENTITY_REQUIRED:
            self._request_router_serial_dialog()
        state = str(
            status.state.value if hasattr(status.state, "value") else status.state
        ).strip().lower()
        if state != self._operator_service_state:
            self._operator_service_state = state

    def _request_router_serial_dialog(self) -> None:
        with self._lock:
            if (
                self._router_serial_prompt_requested
                or self._login is None
                or self._connection_stop_pending
                or self.runtime.status().state
                is not RuntimeServiceState.IDENTITY_REQUIRED
            ):
                return
            self._router_serial_prompt_requested = True
        self.ports.request_router_serial()

    def submit_startup_router_serial(self, serial: str) -> None:
        from .runtime_ipc import RuntimeStartupRouterSerial

        submission = RuntimeStartupRouterSerial(serial)
        with self._lock:
            if (
                self._login is None
                or self._connection_stop_pending
                or self.runtime.status().state
                is not RuntimeServiceState.IDENTITY_REQUIRED
            ):
                raise UserIntentInputError(
                    "The connection is no longer waiting for a router serial number."
                )
            previous_serial = self._login.config.target_guard_serial
            self._login.config.target_guard_serial = submission.serial
            self._operator_connection_draft = {
                **self._operator_connection_draft,
                "router_serial": submission.serial,
            }
        try:
            self.runtime.submit_startup_router_serial(submission.serial)
        except Exception:
            with self._lock:
                if self._login is not None:
                    self._login.config.target_guard_serial = previous_serial
            raise

    def cancel_startup_router_serial(self) -> None:
        self._request_connection_stop(disconnect=False)

    def _publish_service_progress(self, status: RuntimeServiceStatus) -> None:
        with self._lock:
            if self._connection_stop_pending or (
                self._login is None
                and status.state is not RuntimeServiceState.LOGIN_REQUIRED
            ):
                return
        if status != self.runtime.status():
            return  # A queued progress receipt cannot revive a retired attempt.
        self._publish_local_batches(
            self.profile.service_progress_publication(
                state=str(
                    status.state.value
                    if hasattr(status.state, "value")
                    else status.state
                ),
                message=status.message,
                cycle_count=status.cycle_count,
                now=time(),
            )
        )

    def _complete_identity_login(
        self,
        key: tuple[str, str, str, str, str, str],
        login: ResolvedProfileLogin,
        canonical_identity: Mapping[str, object],
    ) -> None:
        with self._presentation_transaction_lock:
            self._require_login_completion_key(key)
            if str(canonical_identity.get('profile_id') or '') != login.contract.profile_id:
                raise RuntimeError('ngui_selected_device_profile_admission_mismatch')
            try:
                import_confirmed_next_start_handoff_settings(
                    self._storage_root,
                    license_service=self._license_service,
                    confirmed_profile_id=str(canonical_identity['profile_id']),
                    confirmed_identity=canonical_identity,
                    runtime_memory_dir=self._memory_dir,
                )
            except Exception as exc:
                self.ports.report_error(
                    "confirmed handoff settings: "
                    f"{type(exc).__name__}: {exc}"
                )
            self._require_login_completion_key(key)
            with self._lock:
                self._confirmed_profile_id = str(canonical_identity['profile_id'])
            self._publish_local_batches(
                *self.profile.confirm_connected(login, now=time())
            )
            self._require_login_completion_key(key)
            self._publish_license_state(now=time())
            if _identity_has_operator_scope(canonical_identity):
                self._require_login_completion_key(key)
                self._sync_traffic_plan_scope(canonical_identity)
            self._require_login_completion_key(key)
            self._publish_gui_settings(time(), force=True)

    def _manual(
        self,
        action: LeverAction,
        *,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> None:
        login = self._require_login()
        requested_ts = time() - max(0.0, monotonic() - float(created_at))
        self._manual_lifecycle.request(
            action,
            requested_ts=requested_ts,
            requested_monotonic=created_at,
        )
        action_id = str(getattr(action, "_gui_action_id", "") or "")
        try:
            result = self.runtime.submit_manual_action(
                action,
                login.config,
                matrix_identity=matrix_identity,
                created_at=created_at,
            )
        except Exception as exc:
            self._manual_lifecycle.submission_failed(
                action_id,
                f"{type(exc).__name__}: {exc}",
            )
            self._publish_gui_settings(time(), force=True)
            raise
        if not bool(result.get("submitted", False)):
            self._manual_lifecycle.submission_failed(
                action_id,
                str(result.get("reason") or "Core rejected manual action"),
            )
        else:
            self._manual_lifecycle.submitted(action_id)
        self._publish_gui_settings(time(), force=True)

    def _band_auto(
        self,
        *,
        domain: str,
        matrix_identity: MatrixGenerationIdentity,
        created_at: float,
    ) -> None:
        if domain not in {"lte_allocated_auto", "nr_approved_auto"}:
            raise ValueError("invalid band AUTO domain")
        self._require_login()
        requested_ts = time() - max(0.0, monotonic() - float(created_at))
        surface = self._manual_lifecycle.request_semantic(
            lever="NR_BAND_LOCK" if domain == "nr_approved_auto" else "BAND_SELECTION",
            source="ngui_band_auto",
            requested_ts=requested_ts,
            requested_monotonic=created_at,
        )
        action_id = str(surface.get("action_id") or "")
        try:
            result = self.runtime.submit_band_auto(
                domain=domain,
                correlation_id=action_id,
                matrix_identity=matrix_identity,
                created_at=created_at,
            )
        except Exception as exc:
            self._manual_lifecycle.submission_failed(
                action_id,
                f"{type(exc).__name__}: {exc}",
            )
            self._publish_gui_settings(time(), force=True)
            raise
        if not bool(result.get("submitted", False)):
            self._manual_lifecycle.submission_failed(
                action_id,
                str(result.get("reason") or "Core rejected band AUTO"),
            )
        else:
            self._manual_lifecycle.submitted(action_id)
        self._publish_gui_settings(time(), force=True)

    def _require_login(self) -> ResolvedProfileLogin:
        with self._lock:
            if self._login is None:
                raise RuntimeError("connect to a modem first")
            return self._login

    def _front(self) -> FrontRegisterSnapshot:
        return self.front_register.snapshot()

    def _field_value(self, field_id: str) -> Any:
        field = self._front().fields.get(field_id)
        return field.value if field is not None else None

    def _route_navigation(self, intent: UserIntent) -> None:
        page = PageId(str(intent.payload["page"]))
        self.charts.set_render_active(
            active=page is PageId.GRAPHS,
            window_seconds=self._chart_window_s,
            requested_at=time(),
        )

    def _route_chart_window(self, intent: UserIntent) -> None:
        requested = int(intent.payload["seconds"])
        if requested not in SUPPORTED_CHART_WINDOW_SECONDS:
            raise ValueError("unsupported chart window")
        self._chart_window_s = requested
        chart_status = self.charts.status()
        if (
            str(chart_status.get("session_scope_key") or "").strip()
            and str(chart_status.get("source_epoch") or "").strip()
        ):
            self.charts.refresh_if_active(
                window_seconds=self._chart_window_s,
                requested_at=time(),
            )

    def _route_pband(self, intent: UserIntent) -> None:
        matrix_identity = MatrixGenerationIdentity.from_mapping(
            intent.payload["matrix_identity"]
        )
        pool = parse_manual_band_pool(intent.payload["ca_pool"])
        if not pool:
            raise ValueError("at least one band is required")
        pband_text = str(intent.payload["pband"] or "").strip()
        pband = _parse_band(pband_text) if pband_text else 0
        self._manual(
            build_manual_band_apply_action(
                pband=pband,
                bands=pool,
            ),
            matrix_identity=matrix_identity,
            created_at=intent.created_at,
        )

    def _route_nr_band(self, intent: UserIntent) -> None:
        identity = MatrixGenerationIdentity.from_mapping(intent.payload["matrix_identity"])
        text = str(intent.payload["nr"]).strip().upper().removeprefix("N")
        if not text.isdecimal():
            raise ValueError("Enter one NR band number, for example n78.")
        self._manual(build_manual_nr_apply_action(band=int(text)),
                     matrix_identity=identity, created_at=intent.created_at)

    def _route_band_lock(self, intent: UserIntent) -> None:
        matrix_identity = MatrixGenerationIdentity.from_mapping(
            intent.payload["matrix_identity"]
        )
        if not bool(intent.payload["desired_locked"]):
            self._band_auto(
                domain="lte_allocated_auto",
                matrix_identity=matrix_identity,
                created_at=intent.created_at,
            )
            return
        band = _parse_band(intent.payload["band"])
        self._manual(
            LeverAction(
                LeverType.BAND_LOCK, {"bands": [band]}, f"Manual band lock to B{band}"
            ),
            matrix_identity=matrix_identity,
            created_at=intent.created_at,
        )

    def _route_cell_lock(self, intent: UserIntent) -> None:
        matrix_identity = MatrixGenerationIdentity.from_mapping(
            intent.payload["matrix_identity"]
        )
        if not bool(intent.payload["desired_locked"]):
            self._manual(
                LeverAction(LeverType.CELL_LOCK, {"unlock": True}, "Manual cell unlock"),
                matrix_identity=matrix_identity,
                created_at=intent.created_at,
            )
            return
        pci = _parse_lte_pci(intent.payload["pci"])
        earfcn = _parse_positive_int(
            intent.payload["earfcn"],
            field="earfcn",
        )
        if earfcn <= 0:
            raise UserIntentInputError("Enter an EARFCN greater than zero for cell lock.")
        params: dict[str, Any] = {"pci": pci, "earfcn": earfcn}
        if intent.payload.get("band") not in (None, ""):
            params["band"] = _parse_band(intent.payload["band"])
        duration_s = _parse_duration_s(intent.payload.get("lock_duration"))
        if duration_s is not None:
            params["lock_duration_s"] = duration_s
        self._manual(
            LeverAction(LeverType.CELL_LOCK, params, "Manual cell lock"),
            matrix_identity=matrix_identity,
            created_at=intent.created_at,
        )

    def _apply_connect_network_settings(self, draft, profile, identity, created_at, *, require_mobile_off=False):
        from arch4.foundation.network_settings import network_choice_from_draft, network_target_from_draft
        validate = network_choice_from_draft if profile is not None else network_target_from_draft
        target = _network_draft_input(validate, draft, self._field_value("network.settings"))
        if target.apn is not None and profile is None:
            raise UserIntentInputError("Choose an APN profile name and network before connecting.")
        with self._lock:
            self._network_settings_monitoring = False
            self._network_choice_sequence += 1
            choice_sequence = self._network_choice_sequence
        if profile is None:
            self._remember_network_preset(target)
            self._dispatch_network_target(target, identity, created_at)
            return
        if (not isinstance(profile, Mapping) or set(profile) != {"subscription_key", "network_plmn", "saved_profile_id", "apn"}
                or profile["subscription_key"] != target.subscription_key
                or profile["apn"] != draft["apn"]
                or (target.selection == "manual" and profile["network_plmn"] != target.target_plmn)):
            raise UserIntentInputError("The APN profile does not match the selected SIM and network.")
        # Resolve saved secrets through Foundation before considering no-change.
        if not profile['saved_profile_id'] and self._complete_matching_network_target(target, identity, created_at):
            return
        generation = self._parent_async_generation
        future = self.external_actions.submit(
            ExternalActionKind.APN_PROFILES, created_at=created_at,
            payload={"operation": "save", "choice": dict(profile)})
        future.add_done_callback(lambda done: self._apn_profiles_completed(
            done, generation=generation, pending=(created_at, target, identity, choice_sequence, require_mobile_off)))

    def _complete_matching_network_target(self, target, identity, created_at):
        from arch4.core_contracts.network_settings import subscription_key
        from arch4.foundation.network_settings import network_target_already_current
        front = self._front()
        settings = front.fields.get('network.settings')
        mobile = front.fields.get('device.mobile_data_enabled')
        imsi = front.fields.get('device.imsi')
        if (not self._login_identity_confirmed or self._connection_stop_pending
                or front.matrix_identity is None or front.matrix_identity.namespace != identity.namespace
                or settings is None or settings.state is not PresentationState.VALID
                or mobile is None or mobile.state is not PresentationState.VALID or mobile.value is not True
                or imsi is None or imsi.state is not PresentationState.VALID
                or subscription_key(str(imsi.value or '')) != target.subscription_key):
            return False
        if not network_target_already_current(target, settings.value):
            return False
        surface = self._manual_lifecycle.request_semantic(
            lever='NETWORK_SETTINGS', source='ngui_network_settings',
            requested_ts=time() - max(0.0, monotonic() - created_at),
            requested_monotonic=created_at)
        self._manual_lifecycle.network_settings_unchanged(surface['action_id'])
        self._publish_gui_settings(time(), force=True)
        return True

    def _dispatch_network_target(self, target, identity, created_at):
        from arch4.foundation.network_settings import require_network_target_binding
        if self._complete_matching_network_target(target, identity, created_at):
            return
        require_network_target_binding(target, self._field_value('network.settings'),
            allow_profile_creation=bool(target.apn and target.apn.creates_profile
                and self._login and self._login.contract.apn_profile_creation))
        params = target.to_mapping()
        params.pop("lever")
        self._manual(LeverAction(LeverType.NETWORK_SETTINGS, params, "Manual SIM & Network change", intent="manual"),
                     matrix_identity=identity, created_at=created_at)

    def _load_apn_profiles(self):
        if self._apn_library_requested:
            return
        self._apn_library_requested = True
        generation = self._parent_async_generation
        future = self.external_actions.submit(ExternalActionKind.APN_PROFILES, created_at=monotonic(),
                                              payload={"operation": "load", "choice": {}})
        future.add_done_callback(lambda done: self._apn_profiles_completed(done, generation=generation))

    def _apn_profiles_completed(self, future, *, generation, pending=None):
        from dataclasses import replace
        from arch4.core_contracts.network_settings import CoreApnSettings, subscription_key
        with self._presentation_transaction_lock:
            if self._stop.is_set() or not self._parent_async_admission_open or self._parent_async_generation != generation:
                return
            if pending is not None and pending[3] != self._network_choice_sequence:
                return
            profile_saved = False
            try:
                result = future.result()
                if result.state is not ExternalActionState.COMPLETED:
                    raise ValueError(result.message or "APN profiles could not be saved.")
                library = result.result["library"]
                if library.get("schema") != "ApnProfileLibrary.v1" or library.get("state") != "ready":
                    raise ValueError("APN profile receipt is invalid.")
                self._apn_library = dict(library)
                profile_saved = pending is not None
                if pending is not None:
                    created_at, target, identity, choice_sequence, require_mobile_off = pending
                    if choice_sequence != self._network_choice_sequence:
                        return
                    current = self._field_value("network.settings")
                    front = self._front()
                    imsi = front.fields.get("device.imsi")
                    current_sim = subscription_key(str(imsi.value or "")) if imsi and imsi.state is PresentationState.VALID else ""
                    if (not self._login_identity_confirmed or self._connection_stop_pending
                            or front.matrix_identity is None or front.matrix_identity.namespace != identity.namespace
                            or front.fields['network.settings'].state is not PresentationState.VALID
                            or current_sim != target.subscription_key
                            or not isinstance(current, Mapping) or current.get("subscription_key") != target.subscription_key):
                        raise ValueError("The active SIM or connection changed. Disconnect to edit and reconnect.")
                    apn = CoreApnSettings.from_mapping(result.result["apn"])
                    # Saving the profile never bypasses current APN binding or the
                    # normal Core preflight/readback path.
                    choice = replace(target, apn=apn)
                    if require_mobile_off:
                        self._require_connected_network_editor(identity)
                    self._remember_network_preset(choice)
                    self._dispatch_network_target(choice, identity, created_at)
            except Exception as exc:
                reason = (str(exc) if isinstance(exc, UserIntentInputError) else
                    _NETWORK_INPUT_MESSAGES.get(str(exc).removeprefix("ValueError: "), "APN profiles could not be loaded or saved. Please retry."))
                if pending is not None:
                    if profile_saved:
                        reason = "APN profile saved, but settings were not applied. " + reason
                    self._report_network_settings_rejection(reason)
                else:
                    from arch4.foundation.apn_profiles import public_network_choices
                    self._apn_library = {"schema": "ApnProfileLibrary.v1", "state": "failed", "error": reason, "profiles": [], "networks": public_network_choices()}
                    self.ports.report_error(reason)
            self._publish_gui_settings(time(), force=True)

    def _schedule_pending_network_settings(self) -> None:
        with self._lock:
            confirmed_profile_id = self._confirmed_profile_id
            ready = (self._login_identity_confirmed and bool(confirmed_profile_id)
                     and not self._connection_stop_pending)
            monitoring = self._network_settings_monitoring
        if not ready:
            return
        self._load_apn_profiles()
        front = self._front()
        if front.matrix_identity is None:
            return
        namespace = front.matrix_identity.namespace
        if namespace != self._network_inspection_namespace:
            self._network_inspection_namespace = namespace
            self._pending_network_inspected_at = 0.0
        field = front.fields.get("network.settings")
        if not monitoring:
            if field is None or field.reason != "modem_measurement_interrupted":
                return
            health = front.fields.get("runtime.state")
            if health is None or health.state is not PresentationState.VALID:
                return
            if health.value != "ok":
                # A later recovery can retry a previously failed read, but
                # inaccessible-device frames never enqueue fresh inspections.
                self._network_recovery_read_token = None
                return
            token = (namespace, field.sampled_at)
            with self._lock:
                if (self._connection_stop_pending or not self._login_identity_confirmed
                        or self._confirmed_profile_id != confirmed_profile_id
                        or token == self._network_recovery_read_token):
                    return
                self._network_recovery_read_token = token
                self._network_settings_monitoring = True
                self._pending_network_inspected_at = 0.0
        sim = front.fields.get("sim.network")
        current_sim = (sim.value.get("subscription_key", "") if sim and sim.state is PresentationState.VALID
                       and isinstance(sim.value, Mapping) else "")
        completed = bool(field and field.state is PresentationState.VALID and isinstance(field.value, Mapping)
                         and current_sim and field.value.get("subscription_key") == current_sim)
        if completed:
            with self._lock:
                pending = self._pending_connect_network
                self._pending_connect_network = None
                self._network_settings_monitoring = False
            if pending is not None:
                from arch4.foundation.network_settings import bind_connect_network_draft
                try:
                    draft, profile = _network_draft_input(bind_connect_network_draft, pending, field.value)
                    self._apply_connect_network_settings(draft, profile, front.matrix_identity, monotonic())
                except (ValueError, TypeError) as exc:
                    self._report_network_settings_rejection(str(exc))
            else:
                self._remember_network_preset(None)
            return
        if (field and field.state is PresentationState.VALID
                and isinstance(field.value, Mapping)
                and self._pending_network_inspected_at
                and field.sampled_at >= self._pending_network_inspected_at):
            # A completed inspection without matching identity is a result,
            # not permission to wait indefinitely or apply another SIM's form.
            with self._lock:
                pending = self._pending_connect_network
                self._pending_connect_network = None
                self._network_settings_monitoring = False
            if pending is not None:
                self._report_network_settings_rejection(
                    "The router's settings could not be matched to the active SIM. " +
                    "Your choices are kept; no settings were sent. "
                    "Monitoring continues. Disconnect to review and retry.")
            return
        # A forbidden mutation must not forbid reading the router's settings.
        # Exact-profile read qualification belongs to the Modem Live catalog.
        from arch4.modem_live.profile_catalog import modem_profile_network_settings_readable
        firmware = front.fields.get("device.firmware")
        if firmware is None or firmware.state is not PresentationState.VALID:
            return
        if not modem_profile_network_settings_readable(
                confirmed_profile_id, firmware=firmware.value):
            if self._pending_connect_network is not None:
                self._pending_connect_network = None
                self._report_network_settings_rejection("Reading network/APN settings is not supported for this router profile and firmware. Current monitoring continues.")
            self._network_settings_monitoring = False
            return
        if not self._pending_network_inspected_at:
            with self._lock:
                if (not self._login_identity_confirmed or self._connection_stop_pending
                        or not self._network_settings_monitoring
                        or self._confirmed_profile_id != confirmed_profile_id):
                    return
                self._pending_network_inspected_at = time()
                self.runtime.submit_network_settings_inspection(created_at=monotonic())

    def _remember_network_preset(self, target) -> None:
        """Keep only an identity-bound, non-secret form after a fresh SIM read."""
        login = getattr(self, "_login", None)
        if login is None:
            return
        front = self._front()
        sim_field = front.fields.get("sim.network")
        network_field = front.fields.get("network.settings")
        if (sim_field is None or sim_field.state is not PresentationState.VALID
                or network_field is None or network_field.state is not PresentationState.VALID
                or not isinstance(sim_field.value, Mapping)
                or not isinstance(network_field.value, Mapping)):
            return
        sim, observed = sim_field.value, network_field.value
        subscription = str(sim.get("subscription_key") or "")
        if not subscription or observed.get("subscription_key") != subscription:
            return
        from arch4.foundation.network_country_reference import MCC_NAMES, country_verdict
        from arch4.customer_contracts.apn_profiles import apn_profile_id
        identity = _confirmed_identity_payload_from_input_snapshot(self.inputs.snapshot(), login)
        if not identity:
            return
        selection = (
            target.selection if target is not None and target.selection != "keep"
            else str(observed.get("selection") or "")
        )
        roaming = (
            target.roaming_enabled if target is not None and target.roaming_enabled is not None
            else observed.get("roaming_enabled")
        )
        network_plmn = (
            target.target_plmn if target is not None and target.selection == "manual"
            else str(observed.get("target_plmn") or "") if selection == "manual"
            else str(sim.get("home_plmn") or "")
        )
        apn = target.apn if target is not None and target.apn is not None else observed.get("apn")
        apn_values = {
            key: str((getattr(apn, key, "") if target is not None and target.apn is not None
                      else (apn or {}).get(key)) or "")
            for key in ("name", "apn", "username", "authentication", "ip_protocol")
        }
        saved_id = ""
        password_known = False
        if apn_values["name"] and network_plmn:
            try:
                expected_id = apn_profile_id(subscription, network_plmn, apn_values["name"])
            except ValueError:
                expected_id = ""
            for row in self._apn_library.get("profiles", ()):
                if (isinstance(row, Mapping) and row.get("profile_id") == expected_id
                        and row.get("apn") == apn_values["apn"]):
                    saved_id = expected_id
                    password_known = bool(row.get("password_known"))
                    break
        _, network_country = country_verdict("", network_plmn)
        row = {
            "profile_id": login.contract.profile_id,
            "router_serial": str(identity.get("serial") or identity.get("imei") or ""),
            "firmware": str(identity.get("firmware") or ""),
            "subscription_key": subscription,
            "sim_country": MCC_NAMES.get(str(sim.get("mcc") or ""), ("",))[0],
            "roaming_enabled": roaming,
            "selection": selection,
            "network_plmn": network_plmn,
            "network_country": network_country,
            "apn": apn_values,
            "saved_profile_id": saved_id,
            "password_known": password_known,
        }
        try:
            self.profile.remember_network_preset(row)
        except Exception as exc:
            self.ports.report_error(f"SIM & Network choices could not be remembered: {type(exc).__name__}")

    def _report_network_settings_rejection(self, message: str) -> None:
        self._project_network_settings_rejection(message)
        self.ports.report_error(message)
        if self._operator_events.record(message, timestamp=time(), tone="warning"):
            self._publish_gui_settings(time(), force=True)

    def _project_network_settings_rejection(self, message: str, *, created_at: float | None = None) -> None:
        requested = monotonic() if created_at is None else created_at
        surface = self._manual_lifecycle.request_semantic(
            lever="NETWORK_SETTINGS", source="ngui_network_settings",
            requested_ts=time() - max(0.0, monotonic() - requested),
            requested_monotonic=requested)
        self._manual_lifecycle.submission_failed(surface["action_id"], message, user_message=message)

    def _route_read_diagnostics(self, intent: UserIntent) -> None:
        self._require_login()
        delivery = self.runtime.submit_read_diagnostics(action=intent.payload["action"], created_at=intent.created_at)
        if intent.payload["action"] == "start":
            self._router_check_pending = (self._parent_async_generation, delivery.request.context.request_id)

    def _route_wifi_radio(self, intent: UserIntent) -> None:
        radio = str(intent.payload["radio"]).strip()
        if radio not in {"2.4", "5", "6"}:
            raise ValueError("Wi-Fi radio must be 2.4, 5, or 6")
        enabled = bool(intent.payload["enabled"])
        self._require_login()
        self.runtime.submit_wifi_radio(
            radio=radio,
            enabled=enabled,
            created_at=intent.created_at,
        )

    def _open_completed_router_check(self, snapshot):
        pending = self._router_check_pending
        if pending is None:
            return
        if pending[0] != self._parent_async_generation:
            self._router_check_pending = None
            return
        fact = snapshot.values.get("read.diagnostic")
        envelope = fact.value if fact is not None else None
        if not isinstance(envelope, Mapping):
            return
        data = envelope.get("data", {})
        from arch4.foundation.read_diagnostic_report import TERMINAL_READ_STATES
        if data.get("state") not in TERMINAL_READ_STATES or data.get("run_id") != pending[1]:
            return
        self._router_check_pending = None
        future = self.external_actions.submit(
            ExternalActionKind.ROUTER_CHECK_REPORT, created_at=monotonic(),
            payload={"envelope": envelope})
        future.add_done_callback(lambda done: self._router_check_report_completed(done, generation=pending[0]))

    def _router_check_report_completed(self, future, *, generation):
        with self._presentation_transaction_lock:
            if self._stop.is_set() or self._parent_async_generation != generation:
                return
            try:
                result = future.result()
                if result.state is not ExternalActionState.COMPLETED:
                    self.ports.publish_utility_terminal(
                        terminal_from_external(result, kind=UtilityKind.ROUTER_CHECK))
                    raise RuntimeError(result.message)
            except Exception as exc:
                self.ports.report_error("Could not save or open the router check report: " + str(exc))

    def _route_mobile_data(self, intent: UserIntent) -> None:
        self._manual(
            LeverAction(LeverType.MOBILE_DATA, {"enabled": intent.payload["enabled"]}, "Mobile network"),
            matrix_identity=MatrixGenerationIdentity.from_mapping(intent.payload["matrix_identity"]),
            created_at=intent.created_at,
        )

    def _require_connected_network_editor(self, identity):
        front = self._front()
        mobile = front.fields.get("device.mobile_data_enabled")
        if (not self._login_identity_confirmed or self._connection_stop_pending
                or not self._login or not self._login.contract.network_settings_writable
                or front.matrix_identity is None or front.matrix_identity.namespace != identity.namespace
                or mobile is None or mobile.state is not PresentationState.VALID or mobile.value is not False):
            raise UserIntentInputError("Keep the device connected and disconnect mobile data before applying settings.")

    def _route_network_settings(self, intent: UserIntent) -> None:
        from arch4.foundation.network_settings import bind_connect_network_draft
        identity = MatrixGenerationIdentity.from_mapping(intent.payload["matrix_identity"])
        self._require_connected_network_editor(identity)
        draft, profile = _network_draft_input(bind_connect_network_draft,
            intent.payload["network_draft"], self._field_value("network.settings"))
        self._apply_connect_network_settings(draft, profile, identity, intent.created_at,
                                             require_mobile_off=True)

    def _route_restart(self, intent: UserIntent) -> None:
        self._manual(
            LeverAction(LeverType.RESTART, {}, "Manual restart"),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                intent.payload["matrix_identity"]
            ),
            created_at=intent.created_at,
        )

    def _route_band_auto(self, intent: UserIntent) -> None:
        self._band_auto(
            domain=intent.payload["domain"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                intent.payload["matrix_identity"]
            ),
            created_at=intent.created_at,
        )

    def _route_speed_test(self, _intent: UserIntent) -> None:
        self._submit_external_action(
            ExternalActionKind.OPEN_SPEED_TEST,
            _intent,
            label="Speed test",
        )

    def _route_sinr_voice(self, intent: UserIntent) -> None:
        requested = self._sinr_voice.request(
            enabled=bool(intent.payload["enabled"]),
            now=monotonic(),
        )
        self._publish_gui_settings(time(), force=True)
        try:
            self.ports.request_sinr_voice(requested)
        except Exception as exc:
            self._sinr_voice.fail_request(
                sequence=requested.sequence,
                now=monotonic(),
                error=f"{type(exc).__name__}: {exc}",
            )
            self._publish_gui_settings(time(), force=True)
            raise

    def acknowledge_sinr_voice(
        self,
        *,
        sequence: int,
        active: bool,
        error: str = "",
    ) -> bool:
        """Accept a GUI speech acknowledgement into PageTruth."""

        accepted = self._sinr_voice.acknowledge(
            sequence=sequence,
            active=active,
            now=monotonic(),
            error=error,
        )
        if not accepted:
            return False
        snapshot = self._sinr_voice.snapshot()
        if snapshot.state is SINRVoiceState.FAILED:
            self._operator_events.record(
                f"SINR voice failed: {snapshot.message}",
                timestamp=time(),
                tone="warning",
            )
            self.ports.report_error(f"SINR voice: {snapshot.message}")
        else:
            self._operator_events.record(
                snapshot.message,
                timestamp=time(),
                tone="ok",
            )
        self._publish_gui_settings(time(), force=True)
        return True

    def report_sinr_voice_failure(
        self,
        *,
        sequence: int,
        error: str,
    ) -> bool:
        """Accept a GUI speech failure after start into PageTruth."""

        accepted = self._sinr_voice.fail_active(
            sequence=sequence,
            now=monotonic(),
            error=error,
        )
        if not accepted:
            return False
        snapshot = self._sinr_voice.snapshot()
        self._operator_events.record(
            f"SINR voice failed: {snapshot.message}",
            timestamp=time(),
            tone="warning",
        )
        self.ports.report_error(f"SINR voice: {snapshot.message}")
        self._publish_gui_settings(time(), force=True)
        return True

    def _route_antenna_guide(self, intent: UserIntent) -> None:
        self._require_login()
        self.runtime.submit_antenna_guide(
            action=str(intent.payload["action"]),
            created_at=intent.created_at,
        )

    def _route_band_sequence(self, intent: UserIntent) -> None:
        self.runtime.submit_band_sequence_plan(
            bands=tuple(_parse_bands(intent.payload["bands"])),
            interval_s=int(intent.payload["delay_s"]),
            created_at=intent.created_at,
        )

    def _route_cancel_band_sequence(self, intent: UserIntent) -> None:
        self.runtime.submit_band_sequence_cancel(created_at=intent.created_at)

    def _route_band_schedule(self, intent: UserIntent) -> None:
        self.runtime.submit_band_schedule_plan(
            bands=tuple(_parse_bands(intent.payload["bands"])),
            operation=str(intent.payload["operation"]),
            activate_at=str(intent.payload["start_time"]),
            created_at=intent.created_at,
        )

    def _route_cancel_band_schedule(self, intent: UserIntent) -> None:
        self.runtime.submit_band_schedule_cancel(created_at=intent.created_at)

    def _route_sms(self, intent: UserIntent) -> None:
        approved_senders = [
            item.strip()
            for item in str(intent.payload["sender"] or "")
            .replace(";", ",")
            .split(",")
            if item.strip()
        ]
        self._require_login()
        self.runtime.submit_sms_command_control(
            enabled=bool(intent.payload["enabled"]),
            approved_senders=tuple(approved_senders),
            code_word=str(intent.payload["keyword"] or "reboot").lower(),
            poll_interval_s=float(intent.payload["every_s"]),
            created_at=intent.created_at,
        )

    def _route_ping(self, intent: UserIntent) -> None:
        self._require_login()
        self.runtime.submit_downstream_health(
            enabled=bool(intent.payload["enabled"]),
            target=str(intent.payload["host"]),
            ping_count=int(intent.payload["count"]),
            interval_s=float(intent.payload["every_s"]),
            created_at=intent.created_at,
        )

    def _route_traffic_plan(self, intent: UserIntent) -> None:
        login = self._require_login()
        identity = _confirmed_identity_payload_from_input_snapshot(
            self.inputs.snapshot(), login
        )
        context = _traffic_plan_scope_context(
            identity,
            operator_config_service=self._operator_config_service,
        )
        self._sync_traffic_plan_scope(identity)
        if self.edits.scope(TRAFFIC_PLAN_GROUP) != context["scope_id"]:
            raise RuntimeError("traffic plan draft is not bound to active exact scope")
        draft = dict(self.edits.payload(TRAFFIC_PLAN_GROUP))
        if (
            float(intent.payload["limit_gb"]) != float(draft["limit_gb"])
            or int(intent.payload["cycle_day"]) != int(draft["cycle_day"])
        ):
            raise RuntimeError("traffic plan intent does not match canonical draft")
        pending = dict(
            self.edits.request_external_apply(
                TRAFFIC_PLAN_GROUP,
            )
        )
        if pending != draft:
            self.edits.reject_external(
                TRAFFIC_PLAN_GROUP,
                completed_at=monotonic(),
                reason="pending_draft_changed_during_submission",
            )
            raise RuntimeError("traffic plan pending draft changed during submission")
        try:
            future = self.external_actions.submit(
                ExternalActionKind.SAVE_TRAFFIC_PLAN,
                created_at=intent.created_at,
                payload={
                    "limit_gb": float(draft["limit_gb"]),
                    "cycle_day": int(draft["cycle_day"]),
                    **context,
                },
            )
        except Exception:
            self.edits.reject_external(
                TRAFFIC_PLAN_GROUP,
                completed_at=monotonic(),
                reason="foundation_submission_failed",
            )
            raise
        future.add_done_callback(self._traffic_plan_action_completed)
        self._publish_gui_settings(time(), force=True)

    def _sync_traffic_plan_scope(
        self,
        identity: Mapping[str, object],
    ) -> bool:
        try:
            context = _traffic_plan_scope_context(
                identity,
                operator_config_service=self._operator_config_service,
            )
        except RuntimeError as exc:
            if str(exc) in {"traffic plan requires confirmed provider name or PLMN",
                            "traffic plan requires confirmed active SIM"}:
                return False
            raise
        scope_id = context["scope_id"]
        if self.edits.scope(TRAFFIC_PLAN_GROUP) == scope_id:
            return False
        settings = self._operator_config_service.load_traffic_plan(
            OperatorScopeRequest(
                subscription_key=context["subscription_key"],
                backend=context["backend"],
                provider_name=context["provider_name"],
                provider_plmn=context["provider_plmn"],
                profile_id=context["profile_id"],
            )
        )
        loaded_scope = str(settings.get("scope_id") or "")
        if loaded_scope != scope_id:
            raise RuntimeError(
                "traffic plan settings owner returned a different exact scope"
            )
        self.edits.bind_scope(
            TRAFFIC_PLAN_GROUP,
            scope_id=scope_id,
            accepted={
                "traffic.plan_limit_gb": settings["monthly_allowance_gb"],
                "traffic.plan_cycle_day": settings["cycle_start_day"],
            },
        )
        self.edits.bind_scope(
            SMS_SENDER_GROUP,
            scope_id=scope_id,
            accepted={"command.sms_sender": ""},
        )
        return True

    def _route_site_report_window(self, intent: UserIntent) -> None:
        self._site_report_window = str(intent.payload["window"])
        self._publish_gui_settings(time(), force=True)

    def _route_site_report(self, intent: UserIntent) -> None:
        window = str(intent.payload.get("window") or self._site_report_window or "1d")
        future = self.heavy_work.submit(
            HeavyWorkKind.SITE_REPORT,
            created_at=intent.created_at,
            payload={"window": window},
        )
        sequence = self.heavy_work.register.snapshot()[HeavyWorkKind.SITE_REPORT].sequence
        future.add_done_callback(
            lambda completed: self._heavy_work_completed(
                completed,
                kind=HeavyWorkKind.SITE_REPORT,
                sequence=sequence,
                label="Site report",
            )
        )
        self._publish_gui_settings(time(), force=True)

    @staticmethod
    def _route_intelligence_history_status(intent: UserIntent) -> None:
        """Review typed Intelligence status already supplied through PIM."""

        if str(intent.payload.get("view") or "") not in {"history", "import"}:
            raise ValueError("intelligence_history_status_view_invalid")

    def _route_intelligence_autopilot(self, intent: UserIntent) -> None:
        self._require_login()
        self.runtime.submit_intelligence_autopilot(
            enabled=bool(intent.payload["enabled"]),
            created_at=intent.created_at,
        )

    def _route_intelligence_explore(self, intent: UserIntent) -> None:
        self._require_login()
        self.runtime.submit_intelligence_explore(
            enabled=bool(intent.payload["enabled"]),
            created_at=intent.created_at,
        )

    def _publish_gui_settings(self, now: float, *, force: bool = False) -> None:
        """Publish a complete local-settings transaction in display order."""

        with self._presentation_transaction_lock:
            self._publish_gui_settings_transaction(now, force=force)

    @staticmethod
    def _action_surface_values(
        manual: Mapping[str, Any],
        *,
        control_availability: Mapping[str, Any],
        visible_manual_sequence: int,
    ) -> dict[str, Any]:
        timing_summary = manual.get("user_timing_summary")
        timing_rows = (
            timing_summary.get("rows")
            if isinstance(timing_summary, Mapping)
            else {}
        )
        timing_rows = timing_rows if isinstance(timing_rows, Mapping) else {}

        def timing_text(row_name: str, missing: str) -> str:
            row = timing_rows.get(row_name)
            if not isinstance(row, Mapping):
                return missing
            return str(row.get("text") or missing)

        return {
            "manual.status": str(manual.get("status") or "ready"),
            "manual.message": str(manual.get("message") or "Ready"),
            "manual.action_pending": bool(
                manual.get("action_pending", False)
            ),
            "manual.core_received_ms": manual.get("core_received_ms"),
            "manual.modem_work_started_ms": manual.get(
                "modem_work_started_ms"
            ),
            "manual.pband_command_sent_ms": manual.get(
                "pband_command_sent_ms"
            ),
            "manual.ca_pool_command_sent_ms": manual.get(
                "ca_pool_command_sent_ms"
            ),
            "manual.target_first_verified_ms": manual.get(
                "target_first_verified_ms"
            ),
            "manual.next_chart_sample_ms": manual.get(
                "next_chart_sample_ms"
            ),
            "manual.chart_updated": bool(
                manual.get("chart_updated", False)
            ),
            "manual.target_sample_sequence": manual.get(
                "target_sample_sequence"
            ),
            "manual.chart_sample_sequence": manual.get(
                "chart_sample_sequence"
            ),
            "manual.lifecycle": dict(manual),
            "manual.command_received_timing": timing_text(
                "command_received",
                "---",
            ),
            "manual.action_sequence": int(visible_manual_sequence),
            "action.control_availability": dict(control_availability),
        }

    def _publish_local_batches(self, *batches: OwnerInputBatch) -> None:
        """Serialize local-owner input writes with Front publication."""

        with self._presentation_transaction_lock:
            for batch in batches:
                if not isinstance(batch, OwnerInputBatch):
                    raise TypeError(
                        "local NGUI publication requires typed owner batch"
                    )
            previous = self.inputs.snapshot()
            coordinator = self._runtime_generation_coordinator
            if coordinator is None:
                for batch in batches:
                    candidate = (
                        NGUIInputRegisterBank.prepare_local_batch_candidate(
                            batch,
                            previous,
                        )
                    )
                    self.inputs.install_prepared_local_snapshot(
                        previous,
                        candidate,
                        batch=batch,
                    )
                    previous = candidate
                return
            candidate = (
                NGUIInputRegisterBank.prepare_local_transaction_candidate(
                    tuple(batches),
                    previous,
                )
            )
            accepted = coordinator.snapshot()
            if previous != accepted.input_snapshot:
                raise RuntimeError(
                    "ngui_local_input_replica_root_mismatch"
                )
            accepted_at = time()
            presentation_candidate = (
                self.presentation.prepare_runtime_candidate(
                    candidate,
                    now=accepted_at,
                )
            )
            cause = NGUIFrozenValue.from_value(
                tuple(
                    {
                        "owner": batch.owner.value,
                        "sequence": batch.sequence,
                        "fingerprint": owner_input_batch_fingerprint(batch),
                    }
                    for batch in batches
                )
            )
            local_candidate = coordinator.prepare_local_owner_update(
                cause_identity=(
                    "local_transaction:"
                    + ",".join(
                        f"{batch.owner.value}:{batch.sequence}"
                        for batch in batches
                    )
                ),
                cause_digest=cause.digest,
                prepared=NGUIRuntimeGenerationPreparedComponents(
                    input_snapshot=candidate,
                    presentation_policy_state=(
                        NGUIFrozenValue.from_value(
                            presentation_candidate.policy.policy_state_payload()
                        )
                    ),
                    front=presentation_candidate.front,
                    edits=self.edits.snapshot(),
                    lifecycle_state=accepted.lifecycle_state,
                    command_context=accepted.command_context,
                ),
                accepted_at=accepted_at,
            )
            coordinator.commit_local_owner_update(local_candidate)
            self.inputs.install_prepared_local_transaction(
                previous,
                candidate,
                batches=tuple(batches),
            )
            self.presentation.commit_runtime_candidate(
                presentation_candidate
            )
            self._last_consumed_input_generation = max(
                self._last_consumed_input_generation,
                candidate.generation,
            )
            self._last_presented_input_generation = max(
                self._last_presented_input_generation,
                presentation_candidate.front.input_generation,
            )
            self._drain_runtime_generation_outbox(
                coordinator,
                NGUIOutboxKind.CHART_WORK,
                requested_at=accepted_at,
            )
            self._drain_runtime_generation_outbox(
                coordinator,
                NGUIOutboxKind.PAGE_TRUTH,
                requested_at=accepted_at,
            )

    def _publish_gui_settings_transaction(
        self,
        now: float,
        *,
        force: bool = False,
    ) -> None:
        pending_lifecycle = self._claim_pending_lifecycle_application()
        if (
            pending_lifecycle is not None
            and not pending_lifecycle.applied
            and pending_lifecycle.claim.event_kind
            is RuntimeEventKind.MANUAL_NMI_RESULT
        ):
            pending_lifecycle.application_key = (
                self._apply_manual_delivery_claim(
                    pending_lifecycle.claim
                )
            )
            pending_lifecycle.applied = True
        if (
            pending_lifecycle is not None
            and not pending_lifecycle.applied
            and pending_lifecycle.claim.event_kind
            in {
                RuntimeEventKind.OPERATIONAL_RECEIPT,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
            }
        ):
            pending_lifecycle.application_key = (
                self._apply_operational_lifecycle_claim(
                    pending_lifecycle.claim
                )
            )
            pending_lifecycle.applied = True
        if self._sinr_voice.expire(now=monotonic()):
            voice = self._sinr_voice.snapshot()
            self._operator_events.record(
                f"SINR voice failed: {voice.message}",
                timestamp=now,
                tone="warning",
            )
            self.ports.report_error(f"SINR voice: {voice.message}")
        utility_lifecycle = project_utility_lifecycle(
            self.heavy_work.register.readback(),
            self.external_actions.register.snapshot(),
        )
        site_report = self.heavy_work.register.snapshot().get(HeavyWorkKind.SITE_REPORT)
        if site_report is None:
            status = "Idle"
            progress_pct = 0.0
        else:
            status = _heavy_work_status_text(site_report.state, site_report.message)
            progress_pct = float(site_report.progress_pct)
        values = {
            "site_report.selected_window": self._site_report_window,
            "site_report.status": status,
            "site_report.progress_pct": progress_pct,
            "utility.lifecycle": utility_lifecycle,
            "edit.lifecycle": self.edits.lifecycle_payload(),
            "network.apn_profiles": self._apn_library,
            "voice.sinr_lifecycle": self._sinr_voice.snapshot().as_dict(),
        }
        manual = self._project_manual_action(now=now)
        if (
            pending_lifecycle is not None
            and not pending_lifecycle.applied
            and pending_lifecycle.claim.event_kind
            is RuntimeEventKind.CORE_COMMAND_OUTCOME
        ):
            application_key = self._command_lifecycle_application_key(
                pending_lifecycle.claim,
                transaction=self._manual_projection_input(),
            )
            if application_key:
                pending_lifecycle.application_key = application_key
                pending_lifecycle.applied = True
        timing_summary = manual.get("user_timing_summary")
        timing_rows = (
            timing_summary.get("rows")
            if isinstance(timing_summary, Mapping)
            else {}
        )
        timing_rows = timing_rows if isinstance(timing_rows, Mapping) else {}

        def timing_text(row_name: str, missing: str) -> str:
            row = timing_rows.get(row_name)
            if not isinstance(row, Mapping):
                return missing
            return str(row.get("text") or missing)

        values.update(
            {
                "manual.status": str(manual.get("status") or "ready"),
                "manual.message": str(manual.get("message") or "Ready"),
                "manual.action_pending": bool(manual.get("action_pending", False)),
                "manual.core_received_ms": manual.get("core_received_ms"),
                "manual.modem_work_started_ms": manual.get(
                    "modem_work_started_ms"
                ),
                "manual.pband_command_sent_ms": manual.get(
                    "pband_command_sent_ms"
                ),
                "manual.ca_pool_command_sent_ms": manual.get(
                    "ca_pool_command_sent_ms"
                ),
                "manual.target_first_verified_ms": manual.get(
                    "target_first_verified_ms"
                ),
                "manual.next_chart_sample_ms": manual.get(
                    "next_chart_sample_ms"
                ),
                "manual.chart_updated": bool(manual.get("chart_updated", False)),
                "manual.target_sample_sequence": manual.get(
                    "target_sample_sequence"
                ),
                "manual.chart_sample_sequence": manual.get(
                    "chart_sample_sequence"
                ),
                "manual.lifecycle": manual,
                "manual.command_received_timing": timing_text(
                    "command_received",
                    "---",
                ),
            }
        )
        visible_manual_sequence = self._manual_action_sequence
        if (
            pending_lifecycle is not None
            and pending_lifecycle.applied
            and pending_lifecycle.claim.event_kind
            is RuntimeEventKind.MANUAL_NMI_RESULT
        ):
            payload = pending_lifecycle.claim.event.payload
            if not isinstance(payload, RuntimeManualResultReceipt):
                raise TypeError(
                    "visible manual lifecycle typed receipt required"
                )
            visible_manual_sequence = payload.sequence
        values["manual.action_sequence"] = visible_manual_sequence
        control_availability = self._build_control_availability(
            manual_pending=bool(manual.get("action_pending", False))
        )
        values["action.control_availability"] = control_availability
        status = str(manual.get("status") or "ready")
        manual_signature = (
            str(manual.get("action_id") or ""),
            str(manual.get("terminal_ts") or ""),
            status,
            str(manual.get("error") or ""),
        )
        if (
            manual_signature != self._operator_manual_signature
            and status in _MANUAL_EVENT_TERMINAL_STATUSES
            and _manual_terminal_belongs_in_recent_events(manual)
        ):
            self._operator_events.record_operator_action(
                str(manual.get("lever") or "MANUAL"),
                _manual_event_message(manual),
                timestamp=now,
                suppressed=status == "blocked",
                failed=status in _MANUAL_EVENT_FAILED_STATUSES,
            )
        self._operator_manual_signature = manual_signature
        values["event.recent"] = self._operator_events.text()
        force = bool(
            force
            or (
                pending_lifecycle is not None
                and pending_lifecycle.applied
            )
        )
        with self._lock:
            unchanged = bool(
                not force and values == self._last_settings_values
            )
            sequence = self._settings_sequence + 1
        if unchanged:
            if (
                pending_lifecycle is not None
                and pending_lifecycle.applied
                and pending_lifecycle.target_input_generation > 0
            ):
                self._commit_visible_lifecycle_application(
                    self.front_register.snapshot()
                )
            return
        values_by_owner: dict[SourceOwner, dict[str, Any]] = {}
        for source_id, value in values.items():
            owner = input_source_spec(source_id).owner
            values_by_owner.setdefault(owner, {})[source_id] = value
        expected_local_owners = (
            SourceOwner.GUI_UTILITY,
            SourceOwner.GUI_EDIT,
            SourceOwner.GUI_ACTION,
            SourceOwner.GUI_VOICE,
            SourceOwner.GUI_EVENT,
        )
        if set(values_by_owner) != set(expected_local_owners):
            raise RuntimeError("ngui_settings_owner_partition_mismatch")
        batches = tuple(
            adapt_owner_register(
                owner=owner,
                sequence=sequence,
                published_at=now,
                values=values_by_owner[owner],
                sampled_at={
                    source_id: now for source_id in values_by_owner[owner]
                },
                sample_sequences={
                    source_id: sequence
                    for source_id in values_by_owner[owner]
                },
            )
            for owner in expected_local_owners
        )
        self._publish_local_batches(*batches)
        installed_generation = self.inputs.snapshot().generation
        with self._lock:
            self._settings_sequence = sequence
            self._last_settings_values = dict(values)
            pending_worker_health = (
                self._pending_runtime_worker_health_transition
            )
            if pending_worker_health is not None:
                (
                    self._runtime_worker_health,
                    self._runtime_worker_health_signature,
                    self._runtime_worker_health_fault_generation,
                ) = pending_worker_health
                self._pending_runtime_worker_health_transition = None
        if pending_lifecycle is not None and pending_lifecycle.applied:
            pending_lifecycle.target_input_generation = installed_generation
            self._commit_visible_lifecycle_application(
                self.front_register.snapshot()
            )

    def _project_manual_action(self, *, now: float) -> dict[str, Any]:
        """Isolate an invalid action receipt without stopping NGUI output."""

        transaction = self._manual_projection_input()
        candidate = self._prepare_manual_lifecycle_candidate(
            transaction=transaction,
            chart_status=self.charts.status(),
            now=now,
        )
        return dict(self._manual_lifecycle.commit_observe(candidate))

    def _prepare_manual_lifecycle_candidate(
        self,
        *,
        transaction: Mapping[str, Any],
        chart_status: Mapping[str, Any],
        now: float,
    ) -> NGUIActionLifecycleProjectionCandidate:
        """Keep a manual receipt fault inside the manual-action surface."""

        revision = _manual_projection_revision(transaction)
        try:
            candidate = self._manual_lifecycle.prepare_observe(
                transaction=transaction,
                chart_status=chart_status,
                now=now,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if revision != self._manual_projection_rejected_revision:
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner="ngui_manual_lifecycle_projection",
                    operation="project_command_authority_receipt",
                    generation=int(
                        transaction.get("target_generation") or 0
                    ),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior=(
                        "manual_timing_reports_failure_other_ngui_pages_continue"
                    ),
                    error=exc,
                    suppression_budget=0,
                )
                try:
                    self.ports.report_error(
                        "manual action timing: " + detail
                    )
                except Exception:
                    # FaultSupervisor already kept the failure. A broken
                    # secondary error sink must not reject an accepted frame.
                    pass
            self._manual_projection_rejected_revision = revision
            return self._manual_lifecycle.prepare_projection_failure(
                pending=bool(transaction.get("action_pending", False)),
                error=detail,
            )
        if self._manual_projection_rejected_revision is not None:
            self._fault_supervisor.record_recovery(
                boundary=FaultBoundaryClass.GUI,
                owner="ngui_manual_lifecycle_projection",
                operation="project_command_authority_receipt",
                generation=int(
                    transaction.get("target_generation") or 0
                ),
                detail="changed authoritative action receipt projected",
            )
        self._manual_projection_rejected_revision = None
        return candidate

    def _claim_pending_lifecycle_application(
        self,
    ) -> _PendingLifecycleApplication | None:
        pending = self._pending_lifecycle_application
        if pending is not None:
            return pending
        claim = self.runtime.claim_lifecycle_event()
        if claim is None:
            return None
        pending = _PendingLifecycleApplication(claim=claim)
        self._pending_lifecycle_application = pending
        return pending

    def _apply_operational_lifecycle_claim(
        self,
        claim: RuntimeLifecycleClaim,
    ) -> str:
        if claim.event_kind not in {
            RuntimeEventKind.OPERATIONAL_RECEIPT,
            RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
        }:
            raise RuntimeError(
                "operational lifecycle application kind mismatch"
            )
        receipt = claim.event.payload
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("operational lifecycle typed receipt required")
        if (receipt.kind is OperationalKind.READ_DIAGNOSTICS
                and claim.event_kind is RuntimeEventKind.OPERATIONAL_RECEIPT
                and receipt.disposition in {OperationalDisposition.FAILED, OperationalDisposition.REJECTED}
                and self._router_check_pending == (self._parent_async_generation, receipt.request_id)):
            self._router_check_pending = None
            self.ports.publish_utility_terminal(NGUIUtilityTerminal(
                kind=UtilityKind.ROUTER_CHECK, state=UtilityTerminalState(receipt.disposition.value),
                sequence=claim.sequence, completed_at=receipt.occurred_at,
                result={}, message=receipt.message, request_id=receipt.request_id))
        if (receipt.kind is OperationalKind.INSPECT_NETWORK_SETTINGS
                and self._network_settings_monitoring
                and receipt.runtime_namespace == self._network_inspection_namespace
                and self._pending_network_inspected_at
                and receipt.occurred_at >= self._pending_network_inspected_at):
            if receipt.disposition in {OperationalDisposition.FAILED, OperationalDisposition.REJECTED}:
                self._network_settings_monitoring = False
                if self._pending_connect_network is not None:
                    self._pending_connect_network = None
                    self._report_network_settings_rejection(
                        "The router's settings could not be read. Your choices are kept; "
                        "no settings were sent. Disconnect to review and retry.")
        # Routine read requests are diagnostic receipts, not operator warnings.
        # Every receipt still completes its normal acknowledgement below.
        announce_receipt = not (receipt.kind is OperationalKind.INSPECT_NETWORK_SETTINGS
                                and receipt.disposition is OperationalDisposition.ACCEPTED)
        if (
            claim.event_kind is RuntimeEventKind.OPERATIONAL_RECEIPT
            and announce_receipt
            and receipt.disposition is not OperationalDisposition.UNATTACHED
            and receipt.kind
            is not OperationalKind.SET_INTELLIGENCE_AUTOPILOT
            and receipt.kind is not OperationalKind.SET_INTELLIGENCE_EXPLORE
            and not (
                receipt.kind is OperationalKind.WIFI_SET_RADIO
                and receipt.disposition
                not in {
                    OperationalDisposition.FAILED,
                    OperationalDisposition.REJECTED,
                }
            )
        ):
            self._operator_events.record(
                receipt.message,
                timestamp=receipt.occurred_at,
                tone=(
                    "warning"
                    if receipt.disposition
                    in {
                        OperationalDisposition.FAILED,
                        OperationalDisposition.REJECTED,
                    }
                    else "action"
                ),
            )
        return f"operational:{receipt.request_id}:{receipt.kind.value}"

    def _apply_manual_delivery_claim(
        self,
        claim: RuntimeLifecycleClaim,
    ) -> str:
        if claim.event_kind is not RuntimeEventKind.MANUAL_NMI_RESULT:
            raise RuntimeError(
                "manual delivery lifecycle application kind mismatch"
            )
        receipt = claim.event.payload
        if not isinstance(receipt, RuntimeManualResultReceipt):
            raise TypeError("manual delivery lifecycle typed receipt required")
        if not receipt.submitted and not receipt.gateway_outcome_expected:
            self._manual_lifecycle.submission_failed(
                receipt.correlation_id,
                receipt.reason,
            )
        return (
            f"manual-delivery:{receipt.sequence}:{receipt.correlation_id}"
        )

    def _command_lifecycle_application_key(
        self,
        claim: RuntimeLifecycleClaim,
        *,
        transaction: Mapping[str, Any],
    ) -> str:
        if claim.event_kind is not RuntimeEventKind.CORE_COMMAND_OUTCOME:
            raise RuntimeError(
                "command lifecycle application kind mismatch"
            )
        receipt = claim.event.payload
        if not isinstance(receipt, CommandTerminalReceipt):
            raise TypeError(
                "command lifecycle terminal receipt required"
            )
        authority_receipt = transaction.get("command_authority_receipt")
        if not isinstance(authority_receipt, Mapping):
            return ""
        if dict(authority_receipt) != receipt.as_dict():
            return ""
        try:
            matrix_identity = MatrixGenerationIdentity.from_mapping(
                transaction.get("matrix_identity")
            )
        except Exception as exc:
            raise RuntimeError(
                "command lifecycle visible Matrix identity invalid"
            ) from exc
        if (
            str(transaction.get("command_id") or "")
            != receipt.action_key.command_id
            or str(transaction.get("physical_action_root_id") or "")
            != receipt.transaction.root_id
            or matrix_identity != receipt.transaction.matrix_identity
            or str(transaction.get("identity_key") or "")
            != receipt.transaction.identity_key
        ):
            raise RuntimeError(
                "command lifecycle visible projection identity mismatch"
            )
        target = receipt.action_key.target
        if isinstance(target, CoreWifiRadioTarget):
            label = f"Wi-Fi {target.band.replace('ghz', ' GHz')}"
            if receipt.terminal_status is CommandTerminalStatus.SETTLED:
                self._operator_events.record_operator_action(
                    label,
                    "enabled." if target.enabled else "disabled.",
                    timestamp=receipt.terminal_ts,
                    suppressed=False,
                )
            else:
                self._operator_events.record_operator_action(
                    label,
                    receipt.reason or receipt.terminal_status.value,
                    timestamp=receipt.terminal_ts,
                    suppressed=False,
                    failed=True,
                )
        return (
            "command:"
            f"{receipt.receipt_sequence}:"
            f"{receipt.transaction.root_id}"
        )

    def _commit_visible_lifecycle_application(
        self,
        front: FrontRegisterSnapshot,
    ) -> bool:
        pending = self._pending_lifecycle_application
        if (
            pending is None
            or not pending.applied
            or pending.target_input_generation <= 0
            or front.input_generation
            < pending.target_input_generation
        ):
            return False
        matrix_matches = (
            front.matrix_identity is not None
            and pending.claim.matrix_identity is not None
            and front.matrix_identity.namespace
            == pending.claim.matrix_identity.namespace
        )
        if not matrix_matches:
            raise RuntimeError(
                "lifecycle visible front Matrix identity mismatch"
            )
        receipt = RuntimeLifecycleApplicationReceipt(
            generation_identity=pending.claim.generation_identity,
            sequence=pending.claim.sequence,
            event_kind=pending.claim.event_kind,
            event_digest=pending.claim.event_digest,
            input_generation=front.input_generation,
            front_sequence=front.sequence,
            application_key=pending.application_key,
        )
        self.runtime.commit_lifecycle_event(
            pending.claim,
            receipt,
        )
        if (
            pending.claim.event_kind
            is RuntimeEventKind.MANUAL_NMI_RESULT
        ):
            payload = pending.claim.event.payload
            if not isinstance(payload, RuntimeManualResultReceipt):
                raise TypeError(
                    "committed manual lifecycle typed receipt required"
                )
            self._manual_action_sequence = payload.sequence
        self._pending_lifecycle_application = None
        return True

    def _record_runtime_input_events(self, snapshot: InputRegisterSnapshot) -> bool:
        changed = False
        event_sources = _snapshot_input_values(
            snapshot,
            (
                "event.connectivity_transition",
                "quality.internet_outage_event",
                "quality.internet_reachable",
                "quality.internet_outage_started_at",
            ),
        )
        source = event_sources["event.connectivity_transition"]
        net_source = event_sources["quality.internet_reachable"]
        net = net_source.value if net_source is not None and net_source.valid else None
        if net is False:
            onset = event_sources["quality.internet_outage_started_at"]
            if onset is None or not onset.valid:
                raise ValueError("ngui_core_internet_onset_required")
            changed = self._operator_events.record_internet_transition(
                started_at=onset.value) or changed
        value = source.value if source is not None else None
        if (
            isinstance(value, Mapping)
            and value.get("schema") == "FoundationConnectivityTransition.v2"
            and value.get("owner")
            == "54guc.foundation.connectivity_transition"
        ):
            sequence = int(value.get("sequence", 0) or 0)
            if sequence > self._operator_connectivity_transition_sequence:
                if not bool(value.get("intentional_switch", False)):
                    new_state = str(
                        value.get("new_state") or ""
                    ).strip().lower()
                    if new_state == "ok":
                        duration_s = max(
                            0.0,
                            float(
                                value.get("outage_duration_s", 0.0) or 0.0
                            ),
                        )
                        text = (
                            "Modem connection restored after "
                            f"{duration_s:.1f} seconds."
                        )
                        tone = "info"
                    elif new_state == "degraded":
                        text = "Modem connection is degraded; recovery continues."
                        tone = "warning"
                    elif new_state == "reconnecting":
                        text = "Modem connection was interrupted; reconnecting."
                        tone = "warning"
                    else:
                        text = (
                            "Modem connection state changed to "
                            f"{new_state or 'unknown'}."
                        )
                        tone = "warning"
                    changed = self._operator_events.record(
                        text,
                        timestamp=float(
                            value.get("observed_at", 0.0) or time()
                        ),
                        tone=tone,
                    ) or changed
                self._operator_connectivity_transition_sequence = sequence

        outage_source = event_sources["quality.internet_outage_event"]
        outage_value = (
            outage_source.value
            if outage_source is not None and outage_source.valid
            else None
        )
        if outage_value:
            if not isinstance(outage_value, Mapping):
                raise TypeError("ngui_core_internet_outage_mapping_required")
            outage = ReachabilityOutageInterval.from_mapping(outage_value)
            outage_key = (outage.started_at, outage.restored_at)
            previous_key = self._operator_internet_outage_key
            if previous_key is not None and outage.restored_at < previous_key[1]:
                raise RuntimeError("ngui_core_internet_outage_regressed")
            if outage_key != previous_key:
                changed = self._operator_events.record_internet_transition(
                    started_at=outage.started_at,
                    restored_at=outage.restored_at) or changed
                self._operator_internet_outage_key = outage_key

        source_ids = (
            "device.health_status",
            "device.persistence_status",
            "quality.congestion_pct",
            "diagnostic.congestion_rtt_base_pct",
            "diagnostic.congestion_jitter_addition_pct",
            "quality.congestion_rsrq_pct",
            "radio.pband",
            "radio.ca_pool",
            "command.manual_transaction",
            "intelligence.recovery_status",
        )
        values = _snapshot_input_values(snapshot, source_ids)

        def accepted(source_id: str) -> object | None:
            item = values[source_id]
            if item is None or not item.valid:
                return None
            return item.value

        health = accepted("device.health_status")
        persistence = accepted("device.persistence_status")
        congestion_value = accepted("quality.congestion_pct")
        congestion_pct = (
            float(congestion_value)
            if type(congestion_value) in {int, float}
            else None
        )
        congestion_high = congestion_pct > 90.0 if congestion_pct is not None else None
        congestion_sampled_at = float(
            values["quality.congestion_pct"].sampled_at
            if values["quality.congestion_pct"] is not None
            else time()
        )
        severe_started_at = float(
            getattr(self, "_operator_severe_congestion_started_at", 0.0)
            or 0.0
        )
        severe_announced = bool(
            getattr(self, "_operator_severe_congestion_announced", False)
        )
        recovery_started_at = float(
            getattr(
                self,
                "_operator_severe_congestion_recovery_started_at",
                0.0,
            )
            or 0.0
        )
        severe_event = False
        if congestion_pct is None:
            if not severe_announced:
                self._operator_severe_congestion_started_at = 0.0
                self._operator_severe_congestion_recovery_started_at = 0.0
        elif congestion_pct > 90.0:
            self._operator_severe_congestion_recovery_started_at = 0.0
            if severe_started_at <= 0.0:
                self._operator_severe_congestion_started_at = congestion_sampled_at
                severe_started_at = congestion_sampled_at
            if (
                not severe_announced
                and congestion_sampled_at - severe_started_at >= 30.0
            ):
                self._operator_severe_congestion_announced = True
                severe_event = True
        elif congestion_pct < 80.0:
            if severe_announced:
                if recovery_started_at <= 0.0:
                    self._operator_severe_congestion_recovery_started_at = (
                        congestion_sampled_at
                    )
                    recovery_started_at = congestion_sampled_at
                if congestion_sampled_at - recovery_started_at >= 30.0:
                    self._operator_severe_congestion_started_at = 0.0
                    self._operator_severe_congestion_announced = False
                    self._operator_severe_congestion_recovery_started_at = 0.0
            else:
                self._operator_severe_congestion_started_at = 0.0
                self._operator_severe_congestion_recovery_started_at = 0.0
        elif not severe_announced:
            self._operator_severe_congestion_started_at = 0.0
            self._operator_severe_congestion_recovery_started_at = 0.0
        else:
            self._operator_severe_congestion_recovery_started_at = 0.0
        pband = accepted("radio.pband")
        ca_pool = accepted("radio.ca_pool")
        transaction_source = values["command.manual_transaction"]
        transaction = _manual_transaction_from_input_value(
            transaction_source.value
            if transaction_source is not None and transaction_source.valid
            else None
        )
        command_root = str(
            transaction.get("command_root_id") or ""
        ).strip()
        radio_command = bool(
            command_root
            and str(transaction.get("lever") or "").strip().upper()
            in _RADIO_EVENT_LEVERS
        )
        radio_command_pending = bool(
            radio_command
            and transaction.get("action_pending") is True
        )
        current = {
            "health": health,
            "persistence": persistence,
            "internet_reachable": net,
            "congestion_high": congestion_high,
            "pband": pband,
            "ca_pool": ca_pool,
        }
        previous = dict(self._operator_runtime_fact_state)
        recovery_source = values["intelligence.recovery_status"]
        current["radio_recovery"] = previous.get("radio_recovery")
        if (recovery_source is not None and recovery_source.valid
                and recovery_source.freshness is FreshnessState.FRESH
                and isinstance(recovery_source.value, Mapping)):
            recovery = recovery_source.value
            signature = (recovery["state"], recovery["message"], recovery["warning"], recovery["retry_at"])
            current["radio_recovery"] = signature
            if signature != previous.get("radio_recovery") and recovery["message"]:
                message = (
                    "Started regular status monitoring."
                    if recovery["state"] == "monitoring" and not recovery["warning"]
                    else ("WARNING: " if recovery["warning"] else "Recovery: ") + recovery["message"]
                )
                if recovery["retry_at"]:
                    message += " Scheduled retry: " + strftime("%H:%M:%S", localtime(recovery["retry_at"])) + "."
                changed = self._operator_events.record(message,
                    timestamp=recovery_source.sampled_at,
                    tone="warning" if recovery["warning"] else "info") or changed
        previous_radio_complete = bool(
            previous.get("pband") is not None
            and previous.get("ca_pool")
        )
        radio_truth_complete = bool(
            pband is not None and ca_pool
        )
        if radio_command_pending:
            if command_root != self._operator_pending_radio_command_root:
                self._operator_pending_radio_pband_path = []
                if previous_radio_complete:
                    self._operator_pending_radio_pband_path.append(
                        _operator_pband_text(previous.get("pband"))
                    )
            self._operator_pending_radio_command_root = command_root
            self._operator_pending_radio_actor = _operator_radio_actor(
                transaction.get("action_origin")
            )
            target = transaction.get("target")
            self._operator_pending_radio_target = (
                dict(target) if isinstance(target, Mapping) else {}
            )
            if radio_truth_complete:
                observed_pband = _operator_pband_text(pband)
                if (
                    not self._operator_pending_radio_pband_path
                    or self._operator_pending_radio_pband_path[-1]
                    != observed_pband
                ):
                    self._operator_pending_radio_pband_path.append(
                        observed_pband
                    )
            if previous:
                current["pband"] = previous.get("pband")
                current["ca_pool"] = previous.get("ca_pool")
        elif previous and not radio_truth_complete:
            # An incomplete Matrix observation cannot advance the radio event
            # cursor. Keep the last complete pair so a later complete sample
            # can report one truthful transition instead of a false restore.
            current["pband"] = previous.get("pband")
            current["ca_pool"] = previous.get("ca_pool")
        self._operator_runtime_fact_state = current
        if severe_event:
            changed = self._operator_events.record(
                f"Congestion remained above 90% for 30 seconds ({congestion_pct:.1f}%).",
                timestamp=congestion_sampled_at,
                tone="warning",
            ) or changed

            def diagnostic(source_id: str) -> float | None:
                value = accepted(source_id)
                return (
                    float(value)
                    if type(value) in {int, float}
                    else None
                )

            log.warning(
                "Sustained severe congestion: rtt_base_pct=%s jitter_addition_pct=%s rsrq_pct=%s final_pct=%.1f",
                diagnostic("diagnostic.congestion_rtt_base_pct"),
                diagnostic("diagnostic.congestion_jitter_addition_pct"),
                diagnostic("quality.congestion_rsrq_pct"),
                congestion_pct,
            )
        if not previous and health != "DEGRADED" and persistence != "DEGRADED":
            return changed

        if (health is not None and health != previous.get("health")) or (
            persistence == "DEGRADED" and persistence != previous.get("persistence")
        ):
            health_text = str(health).strip().upper()
            changed = self._operator_events.record(
                (
                    "Some data could not be saved. Check disk space and folder permissions."
                    if persistence == "DEGRADED"
                    else "System health is normal."
                    if health_text == "OK"
                    else "System health changed to "
                    + (health_text or "unknown")
                    + "."
                ),
                timestamp=float(
                    values["device.health_status"].sampled_at
                    if values["device.health_status"] is not None
                    else time()
                ),
                tone="info" if health_text == "OK" else "warning",
            ) or changed

        if not previous:
            return changed

        pband_changed = bool(
            pband is not None and pband != previous.get("pband")
        )
        ca_pool_changed = bool(
            ca_pool is not None and ca_pool != previous.get("ca_pool")
        )
        radio_changed = bool(
            previous_radio_complete
            and radio_truth_complete
            and (pband_changed or ca_pool_changed)
        )
        pending_root = self._operator_pending_radio_command_root
        pending_actor = self._operator_pending_radio_actor
        pending_target = dict(self._operator_pending_radio_target)
        pending_pband_path = tuple(self._operator_pending_radio_pband_path)
        matching_radio_terminal = bool(
            radio_command
            and not radio_command_pending
            and pending_root
            and command_root == pending_root
        )
        observed_at = max(
            float(
                values[source_id].sampled_at
                if values[source_id] is not None
                else 0.0
            )
            for source_id in ("radio.pband", "radio.ca_pool")
        ) or time()
        failed_attempt_message = ""
        if radio_command_pending:
            radio_changed = False
        elif matching_radio_terminal:
            if not radio_truth_complete:
                # A partial Matrix pair cannot close the presentation
                # correlation. Keep the actor, target and observed path until
                # one complete terminal truth sample arrives.
                radio_changed = False
            else:
                final_pband_text = _operator_pband_text(pband)
                if (
                    not pending_pband_path
                    or pending_pband_path[-1] != final_pband_text
                ):
                    pending_pband_path += (final_pband_text,)
                lever = str(
                    pending_target.get("lever") or ""
                ).strip().upper()
                pband_matches, pool_matches = _radio_event_target_dimensions(
                    pending_target,
                    pband=pband,
                    ca_pool=ca_pool,
                )
                target_matches = bool(
                    (
                        lever in {"BAND_SELECTION", "BAND_LOCK"}
                        and pool_matches
                    )
                    or (
                        lever == "PBAND"
                        and pband_matches
                        and (
                            pending_target.get("ca_pool") is None
                            or pool_matches
                        )
                    )
                )
                if not (
                    transaction.get("success") is True and target_matches
                ):
                    failed_attempt_message = _failed_radio_attempt_message(
                        actor=pending_actor,
                        target=pending_target,
                        observed_pband_path=pending_pband_path,
                        final_pband=pband,
                        final_ca_pool=ca_pool,
                    )
                self._operator_pending_radio_command_root = ""
                self._operator_pending_radio_actor = ""
                self._operator_pending_radio_target = {}
                self._operator_pending_radio_pband_path = []
        if failed_attempt_message:
            changed = self._operator_events.record(
                failed_attempt_message,
                timestamp=observed_at,
                tone="warning",
            ) or changed
        if radio_changed:
            pband_text = _operator_pband_text(pband)
            lever = str(pending_target.get("lever") or "").strip().upper()
            pband_matches, pool_matches = _radio_event_target_dimensions(
                pending_target,
                pband=pband,
                ca_pool=ca_pool,
            )
            actor_owns_pool_only = bool(
                matching_radio_terminal
                and transaction.get("success") is True
                and pending_actor
                and lever in {"BAND_SELECTION", "BAND_LOCK"}
                and pool_matches
            )
            actor_owns_compound = bool(
                matching_radio_terminal
                and transaction.get("success") is True
                and pending_actor
                and lever == "PBAND"
                and pband_matches
                and (
                    pending_target.get("ca_pool") is None
                    or pool_matches
                )
            )
            if actor_owns_pool_only and ca_pool_changed:
                changed = self._operator_events.record(
                    f"{pending_actor} request completed: CA pool "
                    f"{_operator_ca_pool_text(ca_pool)}.",
                    timestamp=observed_at,
                    tone="info",
                ) or changed
            if actor_owns_compound:
                changed = self._operator_events.record(
                    f"{pending_actor} request completed: "
                    f"{pband_text} ({_operator_ca_pool_text(ca_pool)}).",
                    timestamp=observed_at,
                    tone="info",
                ) or changed
            elif (
                not failed_attempt_message
                and (pband_changed or (ca_pool_changed and not actor_owns_pool_only))
            ):
                changed = self._operator_events.record(
                    f"Modem changed to "
                    f"{pband_text} ({_operator_ca_pool_text(ca_pool)}).",
                    timestamp=observed_at,
                    tone="info",
                ) or changed
        return changed

    def _record_intent_event(self, intent: UserIntent) -> bool:
        # Asynchronous work publishes its own final receipt or callback. Only a
        # completed synchronous local edit may create an intent event.
        text = (
            f"Site report window selected: {intent.payload.get('window', '1d')}."
            if intent.kind is IntentKind.SELECT_SITE_REPORT_WINDOW
            else ""
        )
        return bool(text) and self._operator_events.record(
            text,
            timestamp=time(),
            tone="action",
        )

    def _record_intent_failure(self, intent: UserIntent, exc: Exception) -> bool:
        reason = str(exc) if isinstance(exc, UserIntentInputError) else f"{type(exc).__name__}: {exc}"
        if isinstance(exc, UserIntentInputError) and intent.kind in {IntentKind.SUBMIT_LOGIN, IntentKind.APPLY_NETWORK_SETTINGS} and "network_draft" in intent.payload:
            self._project_network_settings_rejection(reason, created_at=intent.created_at)
        manual_label = _MANUAL_INTENT_EVENT_LABELS.get(intent.kind)
        if manual_label:
            return self._operator_events.record_operator_action(
                manual_label,
                reason,
                suppressed=True,
                timestamp=time(),
            )
        event_text = _RECEIPT_BACKED_EVENT_LABELS.get(
            intent.kind,
            _INTENT_EVENT_LABELS.get(intent.kind, intent.kind.value),
        )
        label = str(event_text or intent.kind.value).removesuffix(".")
        return self._operator_events.record(
            f"{label} rejected: {reason}",
            timestamp=time(),
            tone="warning",
        )

    def _manual_projection_input(
        self,
    ) -> dict[str, Any]:
        """Read the CommandAuthority action projection."""

        sources = _snapshot_input_values(
            self.inputs.snapshot(),
            ("command.manual_transaction",),
        )
        transaction_source = sources["command.manual_transaction"]
        transaction = _manual_transaction_from_input_value(
            transaction_source.value
            if transaction_source is not None
            else None
        )
        return transaction

    def _build_control_availability(
        self,
        *,
        snapshot: InputRegisterSnapshot | None = None,
        manual_pending: bool | None = None,
    ):
        snapshot = snapshot or self.inputs.snapshot()
        source_ids = (
            "profile.license_status",
            "profile.runtime_mode",
            "command.manual_available",
            "command.manual_reason",
            "command.action_capabilities",
            "manual.action_pending",
            "connection.modem_comm_alive",
            "command.command_protection_active",
            "command.command_protection_reason",
            "command.operator_band_cooldown_active",
            "command.operator_band_cooldown_remaining_s",
        )
        source_values = _snapshot_input_values(
            snapshot,
            source_ids,
        )

        def current_value(source_id: str) -> Any | None:
            source = source_values[source_id]
            if (
                source is None
                or not source.valid
                or source.freshness is not FreshnessState.FRESH
            ):
                return None
            return source.value

        with self._lock:
            login = self._login
            identity_confirmed = self._login_action_identity_ready_unlocked()
        service_running = (
            self.runtime.status().state is RuntimeServiceState.RUNNING
        )
        action_capability_contract = current_value(
            "command.action_capabilities"
        )
        action_capability_contract = (
            action_capability_contract
            if isinstance(action_capability_contract, Mapping)
            and action_capability_contract.get("schema")
            == "CoreActionCapabilities.v1"
            and action_capability_contract.get("owner")
            == "54guc.core.action_status"
            else {}
        )
        capability_rows = (
            action_capability_contract.get("actions")
            if isinstance(action_capability_contract.get("actions"), Mapping)
            else {}
        )
        capabilities: dict[str, bool | None] = {}
        capability_reasons: dict[str, str] = {}
        for control in PHYSICAL_CONTROLS:
            row = capability_rows.get(control)
            row = row if isinstance(row, Mapping) else {}
            enabled = row.get("enabled")
            capabilities[control] = (
                enabled
                if row.get("valid") is True and isinstance(enabled, bool)
                else None
            )
            capability_reasons[control] = str(
                row.get("reason") or ""
            )

        license_status = str(
            current_value("profile.license_status") or ""
        ).strip().lower()
        runtime_mode = str(
            current_value("profile.runtime_mode") or ""
        ).strip().lower()
        manual_available = current_value("command.manual_available")
        manual_reason = str(
            current_value("command.manual_reason") or ""
        ).strip()
        if manual_pending is None:
            manual_pending = (
                current_value("manual.action_pending") is True
            )
        return build_control_availability(
            {
                "profile_id": login.contract.profile_id if login is not None else "",
                "connected": login is not None,
                "identity_confirmed": identity_confirmed,
                "runtime_running": service_running,
                "communication_alive": current_value(
                    "connection.modem_comm_alive"
                ),
                "live_mode": (
                    login is not None
                    and not login.observe_only
                    and runtime_mode == "live control"
                ),
                "licensed": license_status == "licensed",
                "manual_available": (
                    manual_available
                    if isinstance(manual_available, bool)
                    else None
                ),
                "manual_reason": manual_reason,
                "manual_pending": bool(manual_pending),
                "command_protection_active": current_value(
                    "command.command_protection_active"
                )
                is True,
                "command_protection_reason": current_value(
                    "command.command_protection_reason"
                )
                or "",
                "operator_band_cooldown_active": current_value(
                    "command.operator_band_cooldown_active"
                )
                is True,
                "operator_band_cooldown_remaining_s": current_value(
                    "command.operator_band_cooldown_remaining_s"
                )
                or 0.0,
                "capabilities": capabilities,
                "capability_reasons": capability_reasons,
            }
        )

    def _submit_external_action(
        self,
        kind: ExternalActionKind,
        intent: UserIntent,
        *,
        label: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Future[ExternalActionResult]:
        future = self.external_actions.submit(
            kind,
            created_at=intent.created_at,
            payload=payload,
        )
        future.add_done_callback(
            lambda completed, action_kind=kind, action_label=label: self._external_action_completed(
                completed,
                action_kind,
                action_label,
            )
        )
        self._publish_gui_settings(time(), force=True)
        return future

    def _external_action_completed(
        self,
        future: Future[ExternalActionResult],
        kind: ExternalActionKind,
        label: str,
    ) -> None:
        with self._presentation_transaction_lock:
            self._external_action_completed_locked(
                future,
                kind,
                label,
            )

    def _external_action_completed_locked(
        self,
        future: Future[ExternalActionResult],
        kind: ExternalActionKind,
        label: str,
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:
            message = f"{label}: {type(exc).__name__}: {exc}"
            self._operator_events.record(
                f"{label} failed: {type(exc).__name__}: {exc}",
                timestamp=time(),
                tone="warning",
            )
            self.ports.report_error(message)
            self._publish_gui_settings(time(), force=True)
            return
        if result.state in {
            ExternalActionState.FAILED,
            ExternalActionState.REJECTED,
            ExternalActionState.CANCELLED,
        }:
            detail = str(result.message or "").strip()
            self._operator_events.record(
                f"{label} failed" + (f": {detail}" if detail else "."),
                timestamp=time(),
                tone="warning",
            )
            self.ports.report_error(f"{label}: {result.message}")
        else:
            self._operator_events.record(
                f"{label} completed.",
                timestamp=time(),
                tone="ok",
            )
        utility_kind = EXTERNAL_UTILITY_KINDS.get(kind)
        if utility_kind is not None and not self._stop.is_set():
            self.ports.publish_utility_terminal(
                terminal_from_external(result, kind=utility_kind)
            )
        self._publish_gui_settings(time(), force=True)

    def _traffic_plan_action_completed(
        self,
        future: Future[ExternalActionResult],
    ) -> None:
        """Accept a traffic-plan draft only from its matching Foundation receipt."""

        with self._presentation_transaction_lock:
            self._traffic_plan_action_completed_locked(future)

    def _traffic_plan_action_completed_locked(
        self,
        future: Future[ExternalActionResult],
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self.edits.reject_external(
                TRAFFIC_PLAN_GROUP,
                completed_at=monotonic(),
                reason=f"external_action_future_failed:{type(exc).__name__}",
            )
            self._external_action_completed(
                future,
                ExternalActionKind.SAVE_TRAFFIC_PLAN,
                "Traffic plan",
            )
            return
        if result.state is not ExternalActionState.COMPLETED:
            self.edits.reject_external(
                TRAFFIC_PLAN_GROUP,
                completed_at=result.completed_at,
                reason=f"foundation_terminal_{result.state.value}",
            )
            self._external_action_completed(
                future,
                ExternalActionKind.SAVE_TRAFFIC_PLAN,
                "Traffic plan",
            )
            return
        try:
            receipt = TrafficPlanSaveResult.from_payload(result.result)
            self.edits.accept_external(
                TRAFFIC_PLAN_GROUP,
                scope_id=receipt.scope_id,
                accepted={
                    "traffic.plan_limit_gb": receipt.limit_gb,
                    "traffic.plan_cycle_day": receipt.cycle_day,
                },
                completed_at=result.completed_at,
            )
        except Exception as exc:
            self.edits.reject_external(
                TRAFFIC_PLAN_GROUP,
                completed_at=result.completed_at,
                reason=f"foundation_receipt_rejected:{type(exc).__name__}",
            )
            self._operator_events.record(
                f"Traffic plan failed: {type(exc).__name__}: {exc}",
                timestamp=time(),
                tone="warning",
            )
            self.ports.report_error(
                f"Traffic plan: {type(exc).__name__}: {exc}"
            )
            self._publish_gui_settings(time(), force=True)
            return
        self._external_action_completed(
            future,
            ExternalActionKind.SAVE_TRAFFIC_PLAN,
            "Traffic plan",
        )

    def _heavy_work_completed(
        self,
        future: Future[Mapping[str, Any]],
        *,
        kind: HeavyWorkKind,
        sequence: int,
        label: str,
    ) -> None:
        with self._presentation_transaction_lock:
            self._heavy_work_completed_locked(
                future,
                kind=kind,
                sequence=sequence,
                label=label,
            )

    def _heavy_work_completed_locked(
        self,
        future: Future[Mapping[str, Any]],
        *,
        kind: HeavyWorkKind,
        sequence: int,
        label: str,
    ) -> None:
        failure: Exception | None = None
        try:
            future.result()
        except Exception as exc:
            failure = exc
            self._operator_events.record(
                f"{label} failed: {type(exc).__name__}: {exc}",
                timestamp=time(),
                tone="warning",
            )
            self.ports.report_error(f"{label}: {type(exc).__name__}: {exc}")
        else:
            self._operator_events.record(
                f"{label} completed.",
                timestamp=time(),
                tone="ok",
            )
        receipt = self.heavy_work.register.terminal_snapshot().get(kind)
        if receipt is None or receipt.sequence != sequence:
            mismatch = RuntimeError(
                f"{label} terminal identity mismatch: expected {sequence}"
            )
            self.ports.report_error(str(mismatch))
            self._publish_gui_settings(time(), force=True)
            return
        if failure is None and receipt.state is not HeavyWorkState.COMPLETED:
            self.ports.report_error(f"{label}: terminal state {receipt.state.value}")
        if (
            failure is None
            and receipt.state is HeavyWorkState.COMPLETED
            and kind is HeavyWorkKind.SCAN
        ):
            try:
                self._publish_local_batches(
                    self.profile.discovery_publication(
                        receipt.result,
                        now=time(),
                    )
                )
            except Exception as exc:
                self.ports.report_error(
                    "Network scan suggestion: "
                    f"{type(exc).__name__}: {exc}"
                )
        utility_kind = HEAVY_UTILITY_KINDS.get(kind)
        if utility_kind is not None and not self._stop.is_set():
            self.ports.publish_utility_terminal(
                terminal_from_heavy(receipt, kind=utility_kind)
            )
        self._publish_gui_settings(time(), force=True)

    def _license_action_completed(
        self,
        future: Future[ExternalActionResult],
    ) -> None:
        with self._presentation_transaction_lock:
            self._license_action_completed_locked(future)

    def _license_action_completed_locked(
        self,
        future: Future[ExternalActionResult],
    ) -> None:
        try:
            result = future.result()
        except Exception:
            return
        if result.state is ExternalActionState.COMPLETED:
            try:
                self._publish_license_state()
            except Exception as exc:
                self.ports.report_error(
                    f"License profile refresh: {type(exc).__name__}: {exc}"
                )
        self._external_action_completed(
            future,
            ExternalActionKind.SAVE_LICENSE,
            "License",
        )

    def _publish_license_state(self, *, now: float | None = None) -> None:
        with self._presentation_transaction_lock:
            with self._lock:
                login = self._login
                serial = self._confirmed_serial
            published_at = float(time() if now is None else now)
            text = self._license_service.status(
                LicenseStatusRequest(
                    dev_full_access=self._dev_full_access,
                    serial=str(serial or "").strip(),
                    entered_license=(
                        login.entered_license if login is not None else ""
                    ),
                )
            )
            self._license_sequence += 1
            self._publish_local_batches(
                adapt_owner_register(
                    owner=SourceOwner.FOUNDATION_LICENSE,
                    sequence=self._license_sequence,
                    published_at=published_at,
                    values={"profile.license_status": text},
                    sampled_at={"profile.license_status": published_at},
                    sample_sequences={
                        "profile.license_status": self._license_sequence
                    },
                )
            )

    def _resolve_and_start_login(self, payload: Mapping[str, Any]) -> None:
        from arch4.foundation.network_settings import (
            validate_connect_network_draft_shape,
            admit_connect_network_draft,
        )
        network_draft = payload.get("network_draft")
        if network_draft is not None:
            validate_connect_network_draft_shape(network_draft)
        connection_draft = {
            key: str(payload.get(key) or "")
            for key in (
                "model",
                "address",
                "username",
                "password",
                "router_serial",
                "license",
                "mode",
                "network_country",
                "network_provider",
                "network_bands",
            )
        }
        with self._lock:
            self._operator_connection_draft = connection_draft
            self._router_serial_prompt_requested = False
            if self._connection_stop_pending:
                return
        login = self.profile.resolve_login(
            selection=str(payload["model"]),
            address=str(payload["address"]),
            username=str(payload["username"]),
            password=str(payload["password"]),
            router_serial=str(payload.get("router_serial") or ""),
            entered_license=str(payload["license"]),
            observe_only=str(payload["mode"]).strip().lower() == "observe",
            network_country=str(payload.get("network_country") or ""),
            network_provider=str(payload.get("network_provider") or ""),
            network_bands=str(payload.get("network_bands") or ""),
        )
        network_draft, network_notice = admit_connect_network_draft(
            network_draft, writable=login.contract.network_settings_writable,
            observe_only=login.observe_only,
            apn_write_requires_explicit_password=login.contract.apn_write_requires_explicit_password,
        )
        self.charts.configure_native_sample_interval(
            login.config.sample_interval_s
        )
        self.runtime.restart(
            login.config,
            observe_only=login.observe_only,
            timeout_s=RUNTIME_WORKER_SHUTDOWN_BUDGET_S,
            trace_selected_at=payload.get("trace_selected_at") or None,
        )
        with self._presentation_transaction_lock:
            self._publish_local_batches(
                *self.profile.begin_connection(login, now=time()),
            )
            with self._lock:
                self._login = login
                self._pending_connect_network = network_draft
                self._network_choice_sequence += 1
                self._apn_library_requested = False
                self._pending_network_inspected_at = 0.0
                self._network_settings_monitoring = True
                self._network_inspection_namespace = None
                self._network_recovery_read_token = None
                self._login_identity_confirmed = False
                self._confirmed_serial = ""
                self._confirmed_profile_id = ""
                self._reset_login_completion_unlocked()
            self._request_router_serial_dialog()
            self._publish_license_state(now=time())
            self.edits.clear_scope(TRAFFIC_PLAN_GROUP)
            self.edits.clear_scope(SMS_SENDER_GROUP)
            if network_notice:
                self._report_network_settings_rejection(network_notice)

    def _login_task_completed(self, future: Future[None]) -> None:
        with self._presentation_transaction_lock:
            self._login_task_completed_locked(future)

    def _login_task_completed_locked(self, future: Future[None]) -> None:
        error: Exception | None = None
        try:
            future.result()
        except Exception as exc:
            error = exc
        with self._lock:
            self._login_pending = False
            retained = (
                error is not None
                and self._login is not None
                and self._login_identity_confirmed
                and self.runtime.status().state
                not in {
                    RuntimeServiceState.IDLE,
                    RuntimeServiceState.STOPPED,
                    RuntimeServiceState.FAILED,
                }
            )
        if error is not None:
            self._publish_local_batches(
                self.profile.connection_failure_publication(
                    message=str(error),
                    connected_session_retained=retained,
                    now=time(),
                )
            )
            self.ports.report_error(
                f"submit_login: {type(error).__name__}: {error}"
            )

    def _route_support_bundle(self, intent: UserIntent) -> None:
        future = self.heavy_work.submit(
            HeavyWorkKind.SUPPORT_BUNDLE,
            created_at=intent.created_at,
            payload=self._support_bundle_request_payload(),
        )
        sequence = self.heavy_work.register.snapshot()[HeavyWorkKind.SUPPORT_BUNDLE].sequence
        future.add_done_callback(
            lambda completed: self._heavy_work_completed(
                completed,
                kind=HeavyWorkKind.SUPPORT_BUNDLE,
                sequence=sequence,
                label="Support bundle",
            )
        )
        self._publish_gui_settings(time(), force=True)

    def _route_scan_network(self, intent: UserIntent) -> None:
        future = self.heavy_work.submit(
            HeavyWorkKind.SCAN,
            created_at=intent.created_at,
            payload={"seed_host": str(intent.payload.get("seed_host") or "")},
        )
        sequence = self.heavy_work.register.snapshot()[HeavyWorkKind.SCAN].sequence
        future.add_done_callback(
            lambda completed: self._heavy_work_completed(
                completed,
                kind=HeavyWorkKind.SCAN,
                sequence=sequence,
                label="Network scan",
            )
        )
        self._publish_gui_settings(time(), force=True)

    def _route_buy(self, intent: UserIntent) -> None:
        identity, identity_kind = self._confirmed_license_identity()
        self._submit_external_action(
            ExternalActionKind.OPEN_BUY,
            intent,
            label="Buy license",
            payload={"identity": identity, "identity_kind": identity_kind},
        )

    def _route_info(self, intent: UserIntent) -> None:
        self._submit_external_action(
            ExternalActionKind.OPEN_INFO,
            intent,
            label="Version",
        )

    def _route_updates(self, intent: UserIntent) -> None:
        self._submit_external_action(
            ExternalActionKind.CHECK_UPDATES,
            intent,
            label="Update check",
        )

    def _route_update_license(self, intent: UserIntent) -> None:
        with self._lock:
            self._operator_connection_draft = {
                **self._operator_connection_draft,
                "license": str(intent.payload["license"]),
            }
        identity, identity_kind = self._confirmed_license_identity()
        future = self.external_actions.submit(
            ExternalActionKind.SAVE_LICENSE,
            created_at=intent.created_at,
            payload={
                "license": str(intent.payload["license"]),
                "identity": identity,
                "identity_kind": identity_kind,
            },
        )
        future.add_done_callback(self._license_action_completed)
        self._publish_gui_settings(time(), force=True)

    def _support_bundle_request_payload(self) -> Mapping[str, Any]:
        with self._lock:
            login = self._login
        identity = _confirmed_identity_payload_from_input_snapshot(
            self.inputs.snapshot(), login
        )
        profile = login.contract.profile_id if login is not None else ""
        runtime_mode = (
            "observe"
            if login is not None and login.observe_only
            else "live" if login is not None else "neutral"
        )
        return {
            "profile": profile,
            "runtime_mode": runtime_mode,
            "probe_mode": False,
            "modem_identity": identity,
        }

    def _confirmed_license_identity(self) -> tuple[str, str]:
        snapshot = self.inputs.snapshot()
        identity_values = _snapshot_input_values(
            snapshot,
            ("device.serial", "device.imei"),
        )
        for source_id, kind in (("device.serial", "serial"), ("device.imei", "imei")):
            value = identity_values[source_id]
            token = str(
                value.value if value is not None and value.valid else ""
            ).strip()
            if _usable_identity_token(token):
                return token, kind
        return "", ""

    def _route_login(self, intent: UserIntent) -> None:
        if not str(intent.payload.get("model") or "").strip():
            raise UserIntentInputError("select a modem model")
        with self._lock:
            if self.runtime.status().state is RuntimeServiceState.RUNNING and self._login_identity_confirmed:
                raise UserIntentInputError("Disconnect before connecting again.")
            if self._login_pending or self._connection_stop_pending or self.runtime.status().state in {
                RuntimeServiceState.STARTING,
                RuntimeServiceState.IDENTITY_REQUIRED,
                RuntimeServiceState.RECONNECTING,
                RuntimeServiceState.STOPPING,
            } or (
                self.runtime.status().state is RuntimeServiceState.RUNNING
                and not self._login_identity_confirmed
            ):
                raise RuntimeError("profile login is already pending")
            self._login_pending = True
        future = self._submit_profile_task(
            self._resolve_and_start_login,
            dict(intent.payload),
        )
        future.add_done_callback(self._login_task_completed)

    def _route_cancel_login(self, _intent: UserIntent) -> None:
        self._request_connection_stop(disconnect=False)

    def _route_disconnect_session(self, _intent: UserIntent) -> None:
        self._request_connection_stop(disconnect=True)

    def _request_connection_stop(self, *, disconnect: bool) -> None:
        with self._lock:
            if self._connection_stop_pending:
                return
            state = self.runtime.status().state
            connected = state is RuntimeServiceState.RUNNING and self._login_identity_confirmed
            pending = (
                self._login_pending
                or state in {
                    RuntimeServiceState.STARTING,
                    RuntimeServiceState.IDENTITY_REQUIRED,
                    RuntimeServiceState.RECONNECTING,
                }
                or state is RuntimeServiceState.RUNNING and not self._login_identity_confirmed
            )
            if disconnect and not connected:
                raise UserIntentInputError("No confirmed connection to disconnect.")
            if not disconnect and not pending:
                raise UserIntentInputError("No connection attempt is pending.")
            self._network_choice_sequence += 1
            self._pending_connect_network = None
            self._connection_stop_pending = True
            self._network_settings_monitoring = False
            self._login = None
            self._login_identity_confirmed = False
            self._reset_login_completion_unlocked()
        self._publish_local_batches(
            self.profile.connection_stop_publication(pending=True, disconnect=disconnect, now=time())
        )
        try:
            future = self._submit_profile_task(
                self._stop_connection, presentation_locked=False,
            )
        except Exception:
            with self._lock:
                self._connection_stop_pending = False
            raise
        future.add_done_callback(lambda result: self._stop_connection_completed(result, disconnect=disconnect))

    def _stop_connection(self) -> Mapping[str, Any]:
        receipt = self.runtime.stop(timeout_s=RUNTIME_WORKER_SHUTDOWN_BUDGET_S)
        if receipt.get("terminated") is not True:
            raise RuntimeError("Runtime has not stopped; session stop is not complete.")
        return receipt

    def _stop_connection_completed(self, future: Future[Mapping[str, Any]], *, disconnect: bool) -> None:
        with self._presentation_transaction_lock:
            error = ""
            try:
                receipt = future.result()
                if receipt.get("status") != "complete":
                    error = "Runtime stopped, but cleanup was not clean; see support log."
            except Exception as exc:
                error = str(exc)
            with self._lock:
                self._connection_stop_pending = False
                self._login_pending = False
                self._confirmed_serial = ""
                self._confirmed_profile_id = ""
            self._publish_local_batches(
                self.profile.connection_stop_publication(
                    pending=False, disconnect=disconnect, error=error, now=time(),
                )
            )
            if error:
                self.ports.report_error(f"connection_stop: {error}")

    def _route_exit(self, _intent: UserIntent) -> None:
        self.ports.request_application_exit()


def _parse_bands(value: Any) -> list[int]:
    text = str(value or "").upper().replace("B", "")
    tokens = text.replace("+", ",").replace("/", ",").split(",")
    bands: list[int] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        try:
            band = int(token)
        except ValueError as exc:
            raise UserIntentInputError("bands must be numeric") from exc
        if band <= 0:
            raise UserIntentInputError("bands must be positive")
        if band not in bands:
            bands.append(band)
    if not bands:
        raise UserIntentInputError("at least one band is required")
    return bands


def _parse_band(value: Any) -> int:
    bands = _parse_bands(value)
    if len(bands) != 1:
        raise UserIntentInputError("one band is required")
    return bands[0]


def _parse_lte_pci(value: Any) -> int:
    from arch4.core_contracts.command_targets import core_lte_pci
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError("missing PCI")
        return core_lte_pci(int(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise UserIntentInputError("Enter a whole-number PCI from 0 to 503. Zero is a valid PCI.") from exc


def _parse_positive_int(value: Any, *, field: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise UserIntentInputError(f"{field} must be numeric") from exc
    if number <= 0:
        raise UserIntentInputError(f"{field} must be positive")
    return number


def _parse_duration_s(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            raise UserIntentInputError("lock duration must be HH:MM or seconds")
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
        except ValueError as exc:
            raise UserIntentInputError(
                "lock duration must be HH:MM or seconds"
            ) from exc
        if hours < 0 or not 0 <= minutes < 60:
            raise UserIntentInputError("lock duration must be HH:MM or seconds")
        return float(hours * 3600 + minutes * 60)
    try:
        if text.endswith("m"):
            return float(text[:-1]) * 60.0
        if text.endswith("s"):
            return float(text[:-1])
        return float(text)
    except ValueError as exc:
        raise UserIntentInputError(
            "lock duration must be HH:MM or seconds"
        ) from exc


def _confirmed_identity_from_input_snapshot(
    snapshot: InputRegisterSnapshot,
    login: ResolvedProfileLogin | None,
) -> tuple[bool, str]:
    identity = _confirmed_identity_payload_from_input_snapshot(snapshot, login)
    return bool(identity), str(identity.get("serial") or identity.get("imei") or "")


def _confirmed_identity_payload_from_input_snapshot(
    snapshot: InputRegisterSnapshot,
    login: ResolvedProfileLogin | None,
) -> dict[str, object]:
    if login is None:
        return {}
    identity_sources = {
        "serial": "device.serial",
        "imei": "device.imei",
        "imsi": "device.imsi",
        "model": "device.model",
        "firmware": "device.firmware",
        "backend": "device.backend",
        "backend_family": "device.backend_family",
        "profile_id": "profile.profile_id",
        "provider_name": "provider.name",
        "provider_plmn": "provider.plmn",
        "serving_band": "radio.pband",
        "pci": "radio.pci",
        "earfcn": "radio.earfcn",
    }
    source_values = _snapshot_input_values(
        snapshot,
        tuple(identity_sources.values()),
    )
    values = {
        key: source_values[source_id]
        for key, source_id in identity_sources.items()
    }
    tokens = {
        key: str(item.value if item is not None and item.valid else "").strip()
        for key, item in values.items()
    }
    if not any(
        _usable_identity_token(tokens[key]) for key in ("serial", "imei")
    ):
        return {}
    identity: dict[str, object] = {
        key: value for key, value in tokens.items() if _usable_identity_token(value)
    }
    identity["target"] = login.address
    return identity


def _snapshot_input_values(
    snapshot: InputRegisterSnapshot,
    source_ids: tuple[str, ...],
) -> dict[str, InputValue | None]:
    return {
        source_id: snapshot.values.get(source_id)
        for source_id in source_ids
    }


def _traffic_plan_scope_context(
    identity: Mapping[str, object],
    *,
    operator_config_service: OperatorConfigServicePort,
) -> dict[str, str]:
    from arch4.core_contracts.network_settings import subscription_key
    subscription = subscription_key(str(identity.get("imsi") or ""))
    if not subscription:
        raise RuntimeError("traffic plan requires confirmed active SIM")
    provider_name = str(identity.get("provider_name") or "").strip()
    provider_plmn = str(identity.get("provider_plmn") or "").strip()
    if not (provider_name or provider_plmn):
        raise RuntimeError("traffic plan requires confirmed provider name or PLMN")
    backend = str(identity.get("backend") or "").strip()
    profile_id = str(identity.get("profile_id") or "").strip()
    if not backend or not profile_id:
        raise RuntimeError("traffic plan requires Modem Live Matrix identity")
    scope = operator_config_service.resolve_scope(
        OperatorScopeRequest(
            subscription_key=subscription,
            backend=backend,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            profile_id=profile_id,
        )
    )
    return {
        "subscription_key": scope.subscription_key,
        "backend": scope.backend,
        "provider_name": scope.provider_name,
        "provider_plmn": scope.provider_plmn,
        "profile_id": scope.profile_id,
        "scope_id": scope.scope_id,
    }


def _identity_has_operator_scope(identity: Mapping[str, object]) -> bool:
    from arch4.core_contracts.network_settings import subscription_key
    return bool(
        subscription_key(str(identity.get("imsi") or ""))
        and (str(identity.get("provider_name") or "").strip()
             or str(identity.get("provider_plmn") or "").strip())
    )


def _usable_identity_token(token: str) -> bool:
    normalized = str(token or "").strip()
    return bool(normalized) and normalized.upper() not in {
        "---",
        "N/A",
        "NA",
        "UNKNOWN",
        "NONE",
    }


def _heavy_work_status_text(state: HeavyWorkState, message: str) -> str:
    text = {
        HeavyWorkState.QUEUED: "Queued",
        HeavyWorkState.RUNNING: "Running",
        HeavyWorkState.COMPLETED: "Completed",
        HeavyWorkState.FAILED: "Failed",
        HeavyWorkState.REJECTED: "Rejected",
        HeavyWorkState.CANCELLED: "Cancelled",
    }[state]
    if message:
        return f"{text}: {message}"
    return text


def _manual_projection_revision(
    transaction: Mapping[str, Any],
) -> tuple[Any, ...]:
    """Identify a CommandAuthority projection revision."""

    if not transaction:
        return ()
    receipt = transaction.get("command_authority_receipt")
    authority = receipt if isinstance(receipt, Mapping) else {}
    return (
        transaction.get("command_id"),
        transaction.get("command_root_id"),
        transaction.get("physical_action_root_id"),
        transaction.get("status"),
        transaction.get("published_ts"),
        transaction.get("accepted_ts"),
        transaction.get("confirmed_ts"),
        transaction.get("terminal_ts"),
        transaction.get("target_generation"),
        authority.get("schema"),
        authority.get("stage"),
        authority.get("terminal_status"),
        authority.get("receipt_sequence"),
        authority.get("checksum"),
    )


def _manual_transaction_from_input_value(value: Any) -> dict[str, Any]:
    """Restore an immutable NGUI value to its JSON contract shape."""

    thawed = thaw_ngui_input_value(value)
    return dict(thawed) if isinstance(thawed, Mapping) else {}


__all__ = ["NGUIOrchestrator", "NGUIOutputPorts"]
