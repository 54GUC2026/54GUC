"""Typed user-intent register for NGUI interactions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, IntEnum
from threading import Condition, RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.payload_budget import PayloadBudgetExceeded, retained_payload_bytes


class IntentKind(str, Enum):
    NAVIGATE_PAGE = "navigate_page"
    SET_CHART_WINDOW = "set_chart_window"
    APPLY_PBAND_CA_POOL = "apply_pband_ca_pool"
    APPLY_NR_BAND = "apply_nr_band"
    TOGGLE_BAND_LOCK = "toggle_band_lock"
    TOGGLE_CELL_LOCK = "toggle_cell_lock"
    TOGGLE_WIFI_RADIO = "toggle_wifi_radio"
    SET_MOBILE_DATA = "set_mobile_data"
    APPLY_NETWORK_SETTINGS = "apply_network_settings"
    RESTART_MODEM = "restart_modem"
    SET_BAND_AUTO = "set_band_auto"
    START_SPEED_TEST = "start_speed_test"
    START_SINR_VOICE = "start_sinr_voice"
    ANTENNA_GUIDE_ACTION = "antenna_guide_action"
    PLAN_BAND_SEQUENCE = "plan_band_sequence"
    CANCEL_BAND_SEQUENCE = "cancel_band_sequence"
    PLAN_BAND_SCHEDULE = "plan_band_schedule"
    CANCEL_BAND_SCHEDULE = "cancel_band_schedule"
    CONFIGURE_SMS_COMMAND_CONTROL = "configure_sms_command_control"
    SET_PING_MONITOR = "set_ping_monitor"
    SET_TRAFFIC_PLAN = "set_traffic_plan"
    SELECT_SITE_REPORT_WINDOW = "select_site_report_window"
    CREATE_SITE_REPORT = "create_site_report"
    SHOW_INTELLIGENCE_HISTORY_STATUS = "show_intelligence_history_status"
    SET_INTELLIGENCE_AUTOPILOT = "set_intelligence_autopilot"
    SET_INTELLIGENCE_EXPLORE = "set_intelligence_explore"
    CREATE_SUPPORT_BUNDLE = "create_support_bundle"
    READ_DIAGNOSTICS = "read_diagnostics"
    SCAN_NETWORK = "scan_network"
    OPEN_BUY = "open_buy"
    OPEN_INFO = "open_info"
    CHECK_UPDATES = "check_updates"
    UPDATE_LICENSE = "update_license"
    SUBMIT_LOGIN = "submit_login"
    CANCEL_LOGIN = "cancel_login"
    DISCONNECT_SESSION = "disconnect_session"
    EXIT_APPLICATION = "exit_application"


class IntentPriority(IntEnum):
    MANUAL_NMI = 0
    NORMAL = 1
    BACKGROUND = 2


class ControlFamily(str, Enum):
    """Shared activation behavior for controls with the same meaning."""

    MOMENTARY = "momentary"
    SELECTOR = "selector"
    READBACK_TOGGLE = "readback_toggle"
    TWO_STAGE_CONFIRM = "two_stage_confirm"
    VALUE_SUBMIT = "value_submit"
    LOCAL_DIALOG = "local_dialog"
    APPLICATION_EVENT = "application_event"


class IntentDestination(str, Enum):
    """Owner that receives an adapted user intent."""

    NGUI_PRESENTATION = "54guc.ngui.presentation"
    CORE_COMMAND_GATEWAY = "54guc.core.command_gateway"
    CORE_DEVICE_CONTROL = "54guc.core.device_control"
    CORE_ACTIVE_CONFIGURATION = "54guc.core.active_configuration"
    CORE_DIAGNOSTICS = "54guc.core.diagnostics"
    FOUNDATION_AUTOMATION = "54guc.foundation.automation"
    FOUNDATION_ANTENNA_GUIDE = "54guc.foundation.antenna_guide"
    FOUNDATION_SCHEDULE = "54guc.foundation.schedule"
    FOUNDATION_HEAVY_WORK = "54guc.foundation.heavy_work"
    FOUNDATION_OPERATOR_CONFIG = "54guc.foundation.operator_config"
    FOUNDATION_LICENSE = "54guc.foundation.license"
    NGUI_EXTERNAL_ACTION = "54guc.ngui.external_action"
    NGUI_PROFILE_LIFECYCLE = "54guc.ngui.profile_lifecycle"
    PRODUCT_SUPERVISOR = "54guc.product_supervisor"
    PRODUCT_INTELLIGENCE_MATRIX = "54guc.product.intelligence_matrix"


PayloadType = type | tuple[type, ...]


@dataclass(frozen=True, slots=True)
class IntentSpec:
    kind: IntentKind
    priority: IntentPriority
    required: Mapping[str, PayloadType]
    optional: Mapping[str, PayloadType] = MappingProxyType({})
    handler_name: str = ""
    boundary_call: str = ""
    destination: IntentDestination = IntentDestination.NGUI_PRESENTATION
    control_families: frozenset[ControlFamily] = frozenset(
        {ControlFamily.MOMENTARY}
    )


def _spec(
    kind: IntentKind,
    *,
    handler_name: str,
    boundary_call: str,
    destination: IntentDestination,
    control_families: frozenset[ControlFamily] | None = None,
    priority: IntentPriority = IntentPriority.NORMAL,
    required: Mapping[str, PayloadType] | None = None,
    optional: Mapping[str, PayloadType] | None = None,
) -> IntentSpec:
    normalized_handler = str(handler_name or "").strip()
    normalized_boundary = str(boundary_call or "").strip()
    if not normalized_handler.startswith("_route_"):
        raise ValueError(f"intent route handler is invalid: {kind.value}")
    if not normalized_boundary:
        raise ValueError(f"intent route boundary is missing: {kind.value}")
    families = frozenset(control_families or {ControlFamily.MOMENTARY})
    if not families:
        raise ValueError(f"intent control family is missing: {kind.value}")
    return IntentSpec(
        kind=kind,
        priority=priority,
        required=MappingProxyType(dict(required or {})),
        optional=MappingProxyType(dict(optional or {})),
        handler_name=normalized_handler,
        boundary_call=normalized_boundary,
        destination=destination,
        control_families=families,
    )


_NUMBER = (int, float)
_TEXT_OR_NUMBER = (str, int, float)
_MANUAL = IntentPriority.MANUAL_NMI
_BACKGROUND = IntentPriority.BACKGROUND
_MOMENTARY = frozenset({ControlFamily.MOMENTARY})
_SELECTOR = frozenset({ControlFamily.SELECTOR})
_READBACK_TOGGLE = frozenset({ControlFamily.READBACK_TOGGLE})
_TWO_STAGE = frozenset({ControlFamily.TWO_STAGE_CONFIRM})

MAX_NGUI_INTENT_BYTES = 64 * 1024
NGUI_INTENT_PENDING_LIMITS: Mapping[IntentPriority, int] = MappingProxyType({
    IntentPriority.MANUAL_NMI: 64,
    IntentPriority.NORMAL: 256,
    IntentPriority.BACKGROUND: 64,
})
NGUI_INTENT_PENDING_BYTE_LIMITS: Mapping[IntentPriority, int] = MappingProxyType({
    IntentPriority.MANUAL_NMI: 512 * 1024,
    IntentPriority.NORMAL: 2 * 1024 * 1024,
    IntentPriority.BACKGROUND: 512 * 1024,
})

INTENT_REGISTRY: Mapping[IntentKind, IntentSpec] = MappingProxyType({
    IntentKind.NAVIGATE_PAGE: _spec(
        IntentKind.NAVIGATE_PAGE,
        handler_name="_route_navigation",
        boundary_call="self.charts.set_render_active",
        destination=IntentDestination.NGUI_PRESENTATION,
        control_families=_SELECTOR,
        required={"page": str},
    ),
    IntentKind.SET_CHART_WINDOW: _spec(
        IntentKind.SET_CHART_WINDOW,
        handler_name="_route_chart_window",
        boundary_call="self.charts.refresh_if_active",
        destination=IntentDestination.NGUI_PRESENTATION,
        control_families=_SELECTOR,
        required={"seconds": int},
    ),
    IntentKind.APPLY_PBAND_CA_POOL: _spec(
        IntentKind.APPLY_PBAND_CA_POOL,
        handler_name="_route_pband",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"pband": str, "ca_pool": str, "matrix_identity": Mapping},
    ),
    IntentKind.APPLY_NR_BAND: _spec(
        IntentKind.APPLY_NR_BAND,
        handler_name="_route_nr_band",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"nr": str, "matrix_identity": Mapping},
    ),
    IntentKind.TOGGLE_BAND_LOCK: _spec(
        IntentKind.TOGGLE_BAND_LOCK,
        handler_name="_route_band_lock",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"desired_locked": bool, "matrix_identity": Mapping},
        optional={"band": str},
    ),
    IntentKind.TOGGLE_CELL_LOCK: _spec(
        IntentKind.TOGGLE_CELL_LOCK,
        handler_name="_route_cell_lock",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"desired_locked": bool, "matrix_identity": Mapping},
        optional={
            "pci": _TEXT_OR_NUMBER,
            "earfcn": _TEXT_OR_NUMBER,
            "band": _TEXT_OR_NUMBER,
            "lock_duration": _TEXT_OR_NUMBER,
        },
    ),
    IntentKind.READ_DIAGNOSTICS: _spec(
        IntentKind.READ_DIAGNOSTICS, handler_name="_route_read_diagnostics",
        boundary_call="self.runtime.submit_read_diagnostics",
        destination=IntentDestination.CORE_DEVICE_CONTROL,
        required={"action": str},
    ),
    IntentKind.TOGGLE_WIFI_RADIO: _spec(
        IntentKind.TOGGLE_WIFI_RADIO,
        handler_name="_route_wifi_radio",
        boundary_call="self.runtime.submit_wifi_radio",
        destination=IntentDestination.CORE_DEVICE_CONTROL,
        control_families=_READBACK_TOGGLE,
        priority=_MANUAL,
        required={"radio": str, "enabled": bool},
    ),
    IntentKind.SET_MOBILE_DATA: _spec(
        IntentKind.SET_MOBILE_DATA,
        handler_name="_route_mobile_data",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_READBACK_TOGGLE,
        priority=_MANUAL,
        required={"enabled": bool, "matrix_identity": Mapping},
    ),
    IntentKind.APPLY_NETWORK_SETTINGS: _spec(
        IntentKind.APPLY_NETWORK_SETTINGS,
        handler_name="_route_network_settings",
        boundary_call="self._apply_connect_network_settings",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"network_draft": Mapping, "matrix_identity": Mapping},
    ),
    IntentKind.RESTART_MODEM: _spec(
        IntentKind.RESTART_MODEM,
        handler_name="_route_restart",
        boundary_call="self._manual",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"matrix_identity": Mapping},
    ),
    IntentKind.SET_BAND_AUTO: _spec(
        IntentKind.SET_BAND_AUTO,
        handler_name="_route_band_auto",
        boundary_call="self._band_auto",
        destination=IntentDestination.CORE_COMMAND_GATEWAY,
        control_families=_TWO_STAGE,
        priority=_MANUAL,
        required={"matrix_identity": Mapping, "domain": str},
    ),
    IntentKind.START_SPEED_TEST: _spec(
        IntentKind.START_SPEED_TEST,
        handler_name="_route_speed_test",
        boundary_call="self._submit_external_action",
        destination=IntentDestination.NGUI_EXTERNAL_ACTION,
    ),
    IntentKind.START_SINR_VOICE: _spec(
        IntentKind.START_SINR_VOICE,
        handler_name="_route_sinr_voice",
        boundary_call="self.ports.request_sinr_voice",
        destination=IntentDestination.NGUI_PRESENTATION,
        control_families=_READBACK_TOGGLE,
        required={"enabled": bool},
    ),
    IntentKind.ANTENNA_GUIDE_ACTION: _spec(
        IntentKind.ANTENNA_GUIDE_ACTION,
        handler_name="_route_antenna_guide",
        boundary_call="self.runtime.submit_antenna_guide",
        destination=IntentDestination.FOUNDATION_ANTENNA_GUIDE,
        control_families=frozenset(
            {ControlFamily.MOMENTARY, ControlFamily.READBACK_TOGGLE}
        ),
        required={"action": str},
    ),
    IntentKind.PLAN_BAND_SEQUENCE: _spec(
        IntentKind.PLAN_BAND_SEQUENCE,
        handler_name="_route_band_sequence",
        boundary_call="self.runtime.submit_band_sequence_plan",
        destination=IntentDestination.FOUNDATION_SCHEDULE,
        priority=_MANUAL,
        required={"bands": str, "delay_s": int},
    ),
    IntentKind.CANCEL_BAND_SEQUENCE: _spec(
        IntentKind.CANCEL_BAND_SEQUENCE,
        handler_name="_route_cancel_band_sequence",
        boundary_call="self.runtime.submit_band_sequence_cancel",
        destination=IntentDestination.FOUNDATION_SCHEDULE,
        priority=_MANUAL,
    ),
    IntentKind.PLAN_BAND_SCHEDULE: _spec(
        IntentKind.PLAN_BAND_SCHEDULE,
        handler_name="_route_band_schedule",
        boundary_call="self.runtime.submit_band_schedule_plan",
        destination=IntentDestination.FOUNDATION_SCHEDULE,
        priority=_MANUAL,
        required={"bands": str, "operation": str, "start_time": str},
    ),
    IntentKind.CANCEL_BAND_SCHEDULE: _spec(
        IntentKind.CANCEL_BAND_SCHEDULE,
        handler_name="_route_cancel_band_schedule",
        boundary_call="self.runtime.submit_band_schedule_cancel",
        destination=IntentDestination.FOUNDATION_SCHEDULE,
        priority=_MANUAL,
    ),
    IntentKind.CONFIGURE_SMS_COMMAND_CONTROL: _spec(
        IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,
        handler_name="_route_sms",
        boundary_call="self.runtime.submit_sms_command_control",
        destination=IntentDestination.FOUNDATION_AUTOMATION,
        control_families=_SELECTOR,
        required={"enabled": bool, "sender": str, "keyword": str, "every_s": int},
    ),
    IntentKind.SET_PING_MONITOR: _spec(
        IntentKind.SET_PING_MONITOR,
        handler_name="_route_ping",
        boundary_call="self.runtime.submit_downstream_health",
        destination=IntentDestination.CORE_DIAGNOSTICS,
        control_families=_SELECTOR,
        required={"enabled": bool, "host": str, "count": int, "every_s": int},
    ),
    IntentKind.SET_TRAFFIC_PLAN: _spec(
        IntentKind.SET_TRAFFIC_PLAN,
        handler_name="_route_traffic_plan",
        boundary_call="self.external_actions.submit",
        destination=IntentDestination.FOUNDATION_OPERATOR_CONFIG,
        control_families=frozenset(
            {ControlFamily.MOMENTARY, ControlFamily.VALUE_SUBMIT}
        ),
        required={"limit_gb": _NUMBER, "cycle_day": int},
    ),
    IntentKind.SELECT_SITE_REPORT_WINDOW: _spec(
        IntentKind.SELECT_SITE_REPORT_WINDOW,
        handler_name="_route_site_report_window",
        boundary_call="self._publish_gui_settings",
        destination=IntentDestination.NGUI_PRESENTATION,
        control_families=_SELECTOR,
        required={"window": str},
    ),
    IntentKind.CREATE_SITE_REPORT: _spec(
        IntentKind.CREATE_SITE_REPORT,
        handler_name="_route_site_report",
        boundary_call="self.heavy_work.submit",
        destination=IntentDestination.FOUNDATION_HEAVY_WORK,
        priority=_BACKGROUND,
        optional={"window": str},
    ),
    IntentKind.SHOW_INTELLIGENCE_HISTORY_STATUS: _spec(
        IntentKind.SHOW_INTELLIGENCE_HISTORY_STATUS,
        handler_name="_route_intelligence_history_status",
        boundary_call="local:intelligence_history_status",
        destination=IntentDestination.NGUI_PRESENTATION,
        required={"view": str},
    ),
    IntentKind.SET_INTELLIGENCE_AUTOPILOT: _spec(
        IntentKind.SET_INTELLIGENCE_AUTOPILOT,
        handler_name="_route_intelligence_autopilot",
        boundary_call="self.runtime.submit_intelligence_autopilot",
        destination=IntentDestination.PRODUCT_INTELLIGENCE_MATRIX,
        control_families=_READBACK_TOGGLE,
        required={"enabled": bool},
    ),
    IntentKind.SET_INTELLIGENCE_EXPLORE: _spec(
        IntentKind.SET_INTELLIGENCE_EXPLORE,
        handler_name="_route_intelligence_explore",
        boundary_call="self.runtime.submit_intelligence_explore",
        destination=IntentDestination.PRODUCT_INTELLIGENCE_MATRIX,
        control_families=_READBACK_TOGGLE,
        required={"enabled": bool},
    ),
    IntentKind.CREATE_SUPPORT_BUNDLE: _spec(
        IntentKind.CREATE_SUPPORT_BUNDLE,
        handler_name="_route_support_bundle",
        boundary_call="self.heavy_work.submit",
        destination=IntentDestination.FOUNDATION_HEAVY_WORK,
        priority=_BACKGROUND,
    ),
    IntentKind.SCAN_NETWORK: _spec(
        IntentKind.SCAN_NETWORK,
        handler_name="_route_scan_network",
        boundary_call="self.heavy_work.submit",
        destination=IntentDestination.FOUNDATION_HEAVY_WORK,
        priority=_BACKGROUND,
        optional={"seed_host": str},
    ),
    IntentKind.OPEN_BUY: _spec(
        IntentKind.OPEN_BUY,
        handler_name="_route_buy",
        boundary_call="self._submit_external_action",
        destination=IntentDestination.FOUNDATION_LICENSE,
    ),
    IntentKind.OPEN_INFO: _spec(
        IntentKind.OPEN_INFO,
        handler_name="_route_info",
        boundary_call="self._submit_external_action",
        destination=IntentDestination.NGUI_EXTERNAL_ACTION,
    ),
    IntentKind.CHECK_UPDATES: _spec(
        IntentKind.CHECK_UPDATES,
        handler_name="_route_updates",
        boundary_call="self._submit_external_action",
        destination=IntentDestination.NGUI_EXTERNAL_ACTION,
        priority=_BACKGROUND,
    ),
    IntentKind.UPDATE_LICENSE: _spec(
        IntentKind.UPDATE_LICENSE,
        handler_name="_route_update_license",
        boundary_call="self.external_actions.submit",
        destination=IntentDestination.FOUNDATION_LICENSE,
        control_families=frozenset(
            {ControlFamily.LOCAL_DIALOG, ControlFamily.VALUE_SUBMIT}
        ),
        required={"license": str},
    ),
    IntentKind.SUBMIT_LOGIN: _spec(
        IntentKind.SUBMIT_LOGIN,
        handler_name="_route_login",
        boundary_call="self._submit_profile_task",
        destination=IntentDestination.NGUI_PROFILE_LIFECYCLE,
        control_families=_READBACK_TOGGLE,
        priority=_MANUAL,
        optional={"router_serial": str, "network_country": str, "network_provider": str, "network_bands": str, "trace_selected_at": float, "network_draft": Mapping},
        required={
            "model": str,
            "address": str,
            "username": str,
            "password": str,
            "license": str,
            "mode": str,
        },
    ),
    IntentKind.CANCEL_LOGIN: _spec(
        IntentKind.CANCEL_LOGIN,
        handler_name="_route_cancel_login",
        boundary_call="self._request_connection_stop",
        destination=IntentDestination.NGUI_PROFILE_LIFECYCLE,
        control_families=_MOMENTARY,
        priority=_MANUAL,
    ),
    IntentKind.DISCONNECT_SESSION: _spec(
        IntentKind.DISCONNECT_SESSION,
        handler_name="_route_disconnect_session",
        boundary_call="self._request_connection_stop",
        destination=IntentDestination.NGUI_PROFILE_LIFECYCLE,
        control_families=_MOMENTARY,
        priority=_MANUAL,
    ),
    IntentKind.EXIT_APPLICATION: _spec(
        IntentKind.EXIT_APPLICATION,
        handler_name="_route_exit",
        boundary_call="self.ports.request_application_exit",
        destination=IntentDestination.PRODUCT_SUPERVISOR,
        control_families=frozenset(
            {
                ControlFamily.MOMENTARY,
                ControlFamily.APPLICATION_EVENT,
                ControlFamily.TWO_STAGE_CONFIRM,
            }
        ),
        priority=_MANUAL,
    ),
})


@dataclass(frozen=True, slots=True)
class UserIntent:
    sequence: int
    kind: IntentKind
    priority: IntentPriority
    destination: IntentDestination
    created_at: float
    payload: Mapping[str, Any]
    retained_bytes: int


class NGUIInteractionRegister:
    """Bounded local NMI lane. Consumers run work outside the GUI thread."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._sequence = 0
        self._pending: dict[IntentPriority, deque[UserIntent]] = {
            priority: deque(maxlen=NGUI_INTENT_PENDING_LIMITS[priority])
            for priority in IntentPriority
        }
        self._pending_bytes: dict[IntentPriority, int] = {
            priority: 0 for priority in IntentPriority
        }
        self._high_water_count: dict[IntentPriority, int] = {
            priority: 0 for priority in IntentPriority
        }
        self._high_water_bytes: dict[IntentPriority, int] = {
            priority: 0 for priority in IntentPriority
        }
        self._rejected_count: dict[IntentPriority, int] = {
            priority: 0 for priority in IntentPriority
        }

    def publish(
        self,
        kind: IntentKind,
        *,
        created_at: float,
        payload: Mapping[str, Any] | None = None,
    ) -> UserIntent:
        if created_at <= 0:
            raise ValueError("intent created_at must be positive")
        spec = INTENT_REGISTRY[kind]
        normalized = dict(payload or {})
        _validate_payload(spec, normalized)
        immutable_payload = _freeze_mapping(normalized)
        with self._changed:
            next_sequence = self._sequence + 1
            intent = UserIntent(
                sequence=next_sequence,
                kind=kind,
                priority=spec.priority,
                destination=spec.destination,
                created_at=created_at,
                payload=immutable_payload,
                retained_bytes=0,
            )
            try:
                retained_bytes = retained_payload_bytes(
                    intent,
                    max_bytes=MAX_NGUI_INTENT_BYTES,
                    label=f"NGUI intent {kind.value}",
                )
            except PayloadBudgetExceeded:
                self._rejected_count[spec.priority] += 1
                raise
            intent = UserIntent(
                sequence=next_sequence,
                kind=kind,
                priority=spec.priority,
                destination=spec.destination,
                created_at=created_at,
                payload=immutable_payload,
                retained_bytes=retained_bytes,
            )
            queue = self._pending[spec.priority]
            pending_bytes = self._pending_bytes[spec.priority]
            if (
                len(queue) >= NGUI_INTENT_PENDING_LIMITS[spec.priority]
                or pending_bytes + retained_bytes
                > NGUI_INTENT_PENDING_BYTE_LIMITS[spec.priority]
            ):
                self._rejected_count[spec.priority] += 1
                raise PayloadBudgetExceeded(
                    f"NGUI {spec.priority.name} intent lane is full: "
                    f"pending={len(queue)} bytes={pending_bytes}"
                )
            self._sequence = next_sequence
            queue.append(intent)
            self._pending_bytes[spec.priority] = pending_bytes + retained_bytes
            self._high_water_count[spec.priority] = max(
                self._high_water_count[spec.priority],
                len(queue),
            )
            self._high_water_bytes[spec.priority] = max(
                self._high_water_bytes[spec.priority],
                self._pending_bytes[spec.priority],
            )
            self._changed.notify_all()
            return intent

    def drain(self, *, limit: int | None = None) -> tuple[UserIntent, ...]:
        if limit is not None and limit < 1:
            raise ValueError("drain limit must be positive")
        with self._lock:
            return self._drain_unlocked(limit=limit)

    def wait_and_drain(
        self,
        *,
        timeout_s: float | None = None,
        limit: int | None = None,
    ) -> tuple[UserIntent, ...]:
        if timeout_s is not None and timeout_s < 0:
            raise ValueError("timeout_s cannot be negative")
        with self._changed:
            self._changed.wait_for(lambda: self._pending_count_unlocked() > 0, timeout=timeout_s)
            return self._drain_unlocked(limit=limit)

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count_unlocked()

    def pending_bytes(self) -> int:
        with self._lock:
            return sum(self._pending_bytes.values())

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return MappingProxyType({
                "pending_count": self._pending_count_unlocked(),
                "pending_bytes": sum(self._pending_bytes.values()),
                "lanes": MappingProxyType({
                    priority.name: MappingProxyType({
                        "pending_count": len(self._pending[priority]),
                        "pending_bytes": self._pending_bytes[priority],
                        "max_pending_count": NGUI_INTENT_PENDING_LIMITS[priority],
                        "max_pending_bytes": NGUI_INTENT_PENDING_BYTE_LIMITS[priority],
                        "high_water_count": self._high_water_count[priority],
                        "high_water_bytes": self._high_water_bytes[priority],
                        "rejected": self._rejected_count[priority],
                    })
                    for priority in IntentPriority
                }),
            })

    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def reset_runtime_state(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Quarantine intents accepted before this runtime cutover."""

        with self._changed:
            prior = self._reset_observation_unlocked()
            self._sequence = 0
            self._pending = {
                priority: deque(maxlen=NGUI_INTENT_PENDING_LIMITS[priority])
                for priority in IntentPriority
            }
            self._pending_bytes = {
                priority: 0 for priority in IntentPriority
            }
            self._high_water_count = {
                priority: 0 for priority in IntentPriority
            }
            self._high_water_bytes = {
                priority: 0 for priority in IntentPriority
            }
            self._rejected_count = {
                priority: 0 for priority in IntentPriority
            }
            result = self._reset_observation_unlocked()
            self._changed.notify_all()
            return MappingProxyType(prior), MappingProxyType(result)

    def _drain_unlocked(self, *, limit: int | None) -> tuple[UserIntent, ...]:
        remaining = self._pending_count_unlocked() if limit is None else limit
        drained: list[UserIntent] = []
        for priority in IntentPriority:
            queue = self._pending[priority]
            while queue and remaining > 0:
                intent = queue.popleft()
                self._pending_bytes[priority] = max(
                    0,
                    self._pending_bytes[priority] - int(intent.retained_bytes),
                )
                drained.append(intent)
                remaining -= 1
        return tuple(drained)

    def _pending_count_unlocked(self) -> int:
        return sum(len(queue) for queue in self._pending.values())

    def _reset_observation_unlocked(self) -> dict[str, Any]:
        return {
            "sequence": self._sequence,
            "pending": {
                priority.name: tuple(
                    {
                        "sequence": intent.sequence,
                        "kind": intent.kind.value,
                        "priority": intent.priority.name,
                        "destination": intent.destination.value,
                        "created_at": intent.created_at,
                        "payload": dict(intent.payload),
                        "retained_bytes": intent.retained_bytes,
                    }
                    for intent in self._pending[priority]
                )
                for priority in IntentPriority
            },
            "pending_bytes": {
                priority.name: self._pending_bytes[priority]
                for priority in IntentPriority
            },
            "high_water_count": {
                priority.name: self._high_water_count[priority]
                for priority in IntentPriority
            },
            "high_water_bytes": {
                priority.name: self._high_water_bytes[priority]
                for priority in IntentPriority
            },
            "rejected_count": {
                priority.name: self._rejected_count[priority]
                for priority in IntentPriority
            },
        }


def _validate_payload(spec: IntentSpec, payload: Mapping[str, Any]) -> None:
    required = set(spec.required)
    allowed = required | set(spec.optional)
    missing = required - set(payload)
    unexpected = set(payload) - allowed
    if missing:
        raise ValueError(f"{spec.kind.value}: missing payload keys {sorted(missing)}")
    if unexpected:
        raise ValueError(f"{spec.kind.value}: unexpected payload keys {sorted(unexpected)}")
    for key, value in payload.items():
        expected = spec.required.get(key, spec.optional.get(key))
        if expected is None:
            continue
        if isinstance(value, bool) and expected is not bool:
            raise TypeError(f"{spec.kind.value}.{key}: bool is not a numeric value")
        if not isinstance(value, expected):
            raise TypeError(f"{spec.kind.value}.{key}: expected {_type_name(expected)}")
    if "matrix_identity" in spec.required:
        MatrixGenerationIdentity.from_mapping(payload["matrix_identity"])
    if spec.kind is IntentKind.TOGGLE_BAND_LOCK:
        if payload["desired_locked"] and not str(payload.get("band") or "").strip():
            raise ValueError("toggle_band_lock: lock target band is required")
    if spec.kind is IntentKind.TOGGLE_CELL_LOCK and payload["desired_locked"]:
        if payload.get("pci") is None or not str(payload["pci"]).strip():
            raise ValueError("toggle_cell_lock: lock target PCI is required")
        if not str(payload.get("earfcn") or "").strip():
            raise ValueError("toggle_cell_lock: lock target EARFCN is required")


def _type_name(value: PayloadType) -> str:
    if isinstance(value, tuple):
        return "|".join(item.__name__ for item in value)
    return value.__name__


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value
