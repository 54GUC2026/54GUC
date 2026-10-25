"""Contracts for operator-editable NGUI controls.

This module does not use a GUI toolkit. Renderer widgets implement these contracts. They
cannot choose their own destination or own draft values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .interactions import INTENT_REGISTRY, IntentKind


CONTROL_CONTRACT_VERSION = "NGUIControlRoute.v2"


class GUIControlKind(str, Enum):
    BUTTON = "button"
    TEXT_EDITOR = "text_editor"
    SELECTOR = "selector"
    INTEGER_EDITOR = "integer_editor"


class GUIDraftOwner(str, Enum):
    LOGIN = "54guc.ngui.login_draft"
    MANUAL_ACTION = "54guc.ngui.manual_action_draft"
    ACTIVE_CONFIGURATION = "54guc.ngui.active_configuration_draft"
    DIAGNOSTICS = "54guc.ngui.diagnostics_draft"
    AUTOMATION = "54guc.ngui.automation_draft"
    SCHEDULE = "54guc.ngui.schedule_draft"
    TRAFFIC_PLAN = "54guc.ngui.traffic_plan_draft"


@dataclass(frozen=True, slots=True)
class GUIControlContract:
    object_name: str
    kind: GUIControlKind
    submit_intents: tuple[IntentKind, ...]
    draft_owner: GUIDraftOwner | None = None

    @property
    def destinations(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                INTENT_REGISTRY[intent].destination.value
                for intent in self.submit_intents
            )
        )


_BUTTON_GROUPS: tuple[tuple[IntentKind, tuple[str, ...]], ...] = (
    (IntentKind.NAVIGATE_PAGE, (
        "nguiOperationalNav_device", "nguiOperationalNav_graphs",
        "nguiOperationalNav_dashboard", "nguiOperationalNav_developer_control",
        "nguiOperationalNav_referee", "nguiOperationalNav_intelligence",
        "nguiDeviceNav_device", "nguiDeviceNav_graphs", "nguiDeviceNav_dashboard",
        "nguiDeviceNav_developer_control",
        "nguiDeviceNav_referee", "nguiDeviceNav_intelligence",
    )),
    (IntentKind.RESTART_MODEM, ("nguiDeveloperReboot",)),
    (IntentKind.SET_MOBILE_DATA, ("nguiDeviceMobileDataToggle",)),
    (IntentKind.APPLY_NETWORK_SETTINGS, ("nguiDeviceNetworkApply",)),
    (IntentKind.SET_BAND_AUTO, ("nguiDeveloperBandAuto", "nguiDeveloperNrAuto")),
    (IntentKind.APPLY_PBAND_CA_POOL, (
        "nguiDeveloperRadioApply", "nguiGraphsBandSetApply",
    )),
    (IntentKind.APPLY_NR_BAND, ("nguiDeveloperNrApply",)),
    (IntentKind.TOGGLE_BAND_LOCK, ("nguiDeveloperBandLock",)),
    (IntentKind.TOGGLE_CELL_LOCK, ("nguiDeveloperCellLock",)),
    (IntentKind.SET_PING_MONITOR, (
        "nguiDeveloperPingOn", "nguiDeveloperPingOff",
    )),
    (IntentKind.CONFIGURE_SMS_COMMAND_CONTROL, (
        "nguiDeveloperSmsOn", "nguiDeveloperSmsOff",
    )),
    (IntentKind.PLAN_BAND_SEQUENCE, ("nguiDeveloperSequencePlan",)),
    (IntentKind.CANCEL_BAND_SEQUENCE, ("nguiDeveloperSequenceCancel",)),
    (
        IntentKind.PLAN_BAND_SCHEDULE,
        ("nguiDeveloperScheduleAdd", "nguiDeveloperScheduleRemove"),
    ),
    (IntentKind.CANCEL_BAND_SCHEDULE, ("nguiDeveloperScheduleCancel",)),
    (IntentKind.ANTENNA_GUIDE_ACTION, (
        "ngui_antenna_guide_start",
    )),
    (IntentKind.START_SPEED_TEST, ("nguiGraphsSpeedTest",)),
    (IntentKind.START_SINR_VOICE, ("nguiGraphsSinrVoice",)),
    (IntentKind.SET_CHART_WINDOW, (
        "nguiGraphsWindow_300", "nguiGraphsWindow_900", "nguiGraphsWindow_3600",
        "nguiGraphsWindow_7200", "nguiGraphsWindow_21600",
        "nguiGraphsWindow_43200", "nguiGraphsWindow_86400",
    )),
    (IntentKind.CREATE_SUPPORT_BUNDLE, ("nguiDeviceUtility_create_support_bundle",)),
    (IntentKind.READ_DIAGNOSTICS, ("nguiReadDiagnosticsStart",)),
    (IntentKind.SCAN_NETWORK, ("nguiDeviceUtility_scan_network",)),
    (IntentKind.OPEN_BUY, ("nguiDeviceUtility_open_buy",)),
    (IntentKind.UPDATE_LICENSE, ("nguiDeviceUtility_update_license",)),
    (IntentKind.OPEN_INFO, ("nguiDeviceUtility_open_info",)),
    (IntentKind.CHECK_UPDATES, ("nguiDeviceUtility_check_updates",)),
    (IntentKind.EXIT_APPLICATION, ("nguiDeviceUtility_exit_application",)),
    (IntentKind.SUBMIT_LOGIN, ("deviceConnectButton",)),
    (IntentKind.DISCONNECT_SESSION, ("deviceDisconnectButton",)),
    (IntentKind.CANCEL_LOGIN, ("deviceCancelConnectButton",)),
    (IntentKind.TOGGLE_WIFI_RADIO, (
        "nguiDeviceWifi_2_4GHzToggle", "nguiDeviceWifi_5GHzToggle",
        "nguiDeviceWifi_6GHzToggle",
    )),
    (IntentKind.SET_TRAFFIC_PLAN, ("nguiGraphsTrafficPlanApply",)),
    (IntentKind.CREATE_SITE_REPORT, ("ngui_site_report_generate",)),
    (IntentKind.SELECT_SITE_REPORT_WINDOW, (
        "ngui_site_report_1h", "ngui_site_report_6h", "ngui_site_report_12h",
        "ngui_site_report_1d",
        "ngui_site_report_1w", "ngui_site_report_14d",
    )),
    (IntentKind.SHOW_INTELLIGENCE_HISTORY_STATUS, (
        "nguiIntelligenceCheckLiveHistory",
        "nguiIntelligenceCheckImportStatus",
    )),
    (IntentKind.SET_INTELLIGENCE_AUTOPILOT, (
        "nguiDeveloperAutopilotOn",
        "nguiDeveloperAutopilotOff",
    )),
    (IntentKind.SET_INTELLIGENCE_EXPLORE, (
        "nguiDeveloperExploreOn",
        "nguiDeveloperExploreOff",
    )),
)


def _draft(
    kind: GUIControlKind,
    owner: GUIDraftOwner,
    intents: tuple[IntentKind, ...],
    *names: str,
) -> tuple[GUIControlContract, ...]:
    return tuple(
        GUIControlContract(name, kind, intents, owner)
        for name in names
    )


_DRAFT_CONTROLS: tuple[GUIControlContract, ...] = (
    *_draft(GUIControlKind.SELECTOR, GUIDraftOwner.LOGIN, (IntentKind.SUBMIT_LOGIN, IntentKind.APPLY_NETWORK_SETTINGS),
        *("nguiDeviceNetwork" + name for name in ('Roaming', 'SimCountry', 'Network', 'VisitedCountry',
                                                  'Selection', 'ApnName', 'Authentication', 'Protocol'))),
    *_draft(GUIControlKind.TEXT_EDITOR, GUIDraftOwner.LOGIN, (IntentKind.SUBMIT_LOGIN, IntentKind.APPLY_NETWORK_SETTINGS),
        *("nguiDeviceNetwork" + name for name in ('ApnValue', 'ApnUser', 'ApnPassword'))),

    *_draft(
        GUIControlKind.SELECTOR, GUIDraftOwner.LOGIN, (IntentKind.SUBMIT_LOGIN,),
        "nguiDeviceManufacturer", "nguiDeviceModel", "nguiDeviceStartMode",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.LOGIN, (IntentKind.SUBMIT_LOGIN,),
        "nguiDeviceAddress", "nguiDeviceUsername", "nguiDevicePassword",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.MANUAL_ACTION,
        (IntentKind.APPLY_PBAND_CA_POOL,),
        "nguiDeveloperPbandInput",
        "nguiDeveloperCaPoolInput", "nguiGraphsBandSetInput",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.MANUAL_ACTION,
        (IntentKind.APPLY_NR_BAND,), "nguiDeveloperNrBandInput",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.MANUAL_ACTION,
        (IntentKind.TOGGLE_CELL_LOCK,),
        "nguiDeveloperCellTimer", "nguiDeveloperCellPci",
        "nguiDeveloperCellEarfcn",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.MANUAL_ACTION,
        (IntentKind.TOGGLE_BAND_LOCK, IntentKind.TOGGLE_CELL_LOCK),
        "nguiDeveloperCellBand",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.DIAGNOSTICS,
        (IntentKind.SET_PING_MONITOR,), "nguiDeveloperPingHost",
    ),
    *_draft(
        GUIControlKind.INTEGER_EDITOR, GUIDraftOwner.DIAGNOSTICS,
        (IntentKind.SET_PING_MONITOR,),
        "nguiDeveloperPingCount", "nguiDeveloperPingEvery",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.AUTOMATION,
        (IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,),
        "nguiDeveloperSmsSender", "nguiDeveloperSmsKeyword",
    ),
    *_draft(
        GUIControlKind.INTEGER_EDITOR, GUIDraftOwner.AUTOMATION,
        (IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,), "nguiDeveloperSmsEvery",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.SCHEDULE,
        (IntentKind.PLAN_BAND_SEQUENCE,), "nguiDeveloperSequenceBands",
    ),
    *_draft(
        GUIControlKind.INTEGER_EDITOR, GUIDraftOwner.SCHEDULE,
        (IntentKind.PLAN_BAND_SEQUENCE,), "nguiDeveloperSequenceDelay",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.SCHEDULE,
        (IntentKind.PLAN_BAND_SCHEDULE,),
        "nguiDeveloperScheduleBands",
        "nguiDeveloperScheduleTime",
    ),
    *_draft(
        GUIControlKind.TEXT_EDITOR, GUIDraftOwner.TRAFFIC_PLAN,
        (IntentKind.SET_TRAFFIC_PLAN,),
        "nguiGraphsTrafficPlanLimitGb", "nguiGraphsTrafficPlanCycleDay",
    ),
)


def _build_manifest() -> Mapping[str, GUIControlContract]:
    entries: list[GUIControlContract] = [
        GUIControlContract(name, GUIControlKind.BUTTON, (intent,))
        for intent, names in _BUTTON_GROUPS
        for name in names
    ]
    entries.extend(_DRAFT_CONTROLS)
    by_name = {entry.object_name: entry for entry in entries}
    if len(by_name) != len(entries):
        raise RuntimeError("duplicate NGUI control manifest identity")
    if len(by_name) != 106:
        raise RuntimeError(
            f"NGUI control manifest must contain 106 controls, got {len(by_name)}"
        )
    return MappingProxyType(by_name)


CGF_GUI_CONTROL_MANIFEST = _build_manifest()


__all__ = (
    "CGF_GUI_CONTROL_MANIFEST",
    "CONTROL_CONTRACT_VERSION",
    "GUIControlContract",
    "GUIControlKind",
    "GUIDraftOwner",
)
