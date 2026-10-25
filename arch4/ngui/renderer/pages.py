"""NGUI page and field-grid renderer components."""

from __future__ import annotations

from ..display_text import display_value_text

from collections.abc import Callable, Mapping
from time import monotonic

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen

from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QComboBox,
    QFrame,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..control_availability import (
    control_availability_from_mapping,
)
from ..front_register import PageTruthSnapshot, PresentationState
from ..interactions import ControlFamily, IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import FieldSpec, PageId, SourceOwner
from ..login_lifecycle import login_lifecycle_state
from ..profile_discovery import (
    PROFILE_DISCOVERY_SUGGESTION_OWNER,
    PROFILE_DISCOVERY_SUGGESTION_SCHEMA,
)
from .chrome import DeviceCommandHeader, render_utility_button_lifecycle
from .confirm_button import NGUIConfirmationCoordinator
from .interactions import bind_button, bind_combo_changed, bind_tab_selected, bind_text_draft, bind_timer, stamp_draft_control, publish_intent


class NGUITextInput(QLineEdit):
    """Show the I-beam cursor only while the user edits the text."""

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.ArrowCursor)

    def focusInEvent(self, event) -> None:
        self.setCursor(Qt.IBeamCursor)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.setCursor(Qt.ArrowCursor)
        super().focusOutEvent(event)


_DEVICE_LOGIN_FIELDS = (
    "connection.user_link",
    "device.modem_status",
)
_DEVICE_IDENTITY_FIELDS = (
    "device.model",
    "device.serial",
    "device.imei",
    "device.firmware",
    "device.webui",
    "device.antenna",
    "device.wan_ip",
    "device.lan_mac",
    "provider.name",
    "radio.pband",
    "radio.ca_pool",
    "radio.cell_id",
    "radio.pci",
    "radio.earfcn",
    "radio.enb_id",
    "radio.tac",
    "nr.band",
    "nr.cell_id",
    "nr.pci",
    "nr.arfcn",
)
_DEVICE_WIFI_FIELDS = (
    "device.wifi_24ghz_enabled",
    "device.wifi_5ghz_enabled",
    "device.wifi_6ghz_enabled",
)
_DEVICE_WIFI_BANDS = (
    ("2.4", "2.4 GHz", "device.wifi_24ghz_enabled"),
    ("5", "5 GHz", "device.wifi_5ghz_enabled"),
    ("6", "6 GHz", "device.wifi_6ghz_enabled"),
)
_DEVICE_LIVE_CONTROL_FIELDS = (
    "capability.pband_control",
    "capability.ca_pool_control",
    "capability.band_lock",
    "capability.cell_lock",
    "capability.restart",
)
_DEVICE_ROUTER_CHECK_FIELDS = (
    "device.access_type",
    "capability.max_tcp_payload",
    "capability.ttl_ip",
    "capability.keepalive_enabled",
    "capability.dl_256qam",
    "capability.pci_lock",
    "capability.ul_64qam",
    "capability.keepalive",
    "capability.ue_category",
    "capability.ue_info",
    "capability.traffic_volume",
    "capability.ca_details",
    "capability.neighbors",
    "capability.plmn_read",
    "capability.band_priority",
)
_DEVICE_INTENTIONALLY_ABSENT_FIELDS = frozenset(
    {
        "device.health_status",
        "device.persistence_status",
        "device.router_system_uptime_s",
        "profile.detected_model",
        "profile.selected_profile",
        "profile.runtime_mode",
        "profile.license_status",
        "profile.login_status",
        "utility.lifecycle",
    }
)
DEVICE_SIGN_IN_MAX_WIDTH = 461
DEVICE_SIGN_IN_MAX_HEIGHT = 708
DEVICE_IDENTITY_MIN_WIDTH = 300
# Preserve the network share; give roughly 10 mm of identity width to capabilities.
DEVICE_NETWORK_STRETCH = 300
DEVICE_IDENTITY_STRETCH = 278
DEVICE_CAPABILITY_STRETCH = 222
DEVICE_ENTRY_CONTROL_WIDTH = 321
DEVICE_MANUFACTURER_WIDTH = DEVICE_ENTRY_CONTROL_WIDTH
DEVICE_MODEL_WIDTH = DEVICE_ENTRY_CONTROL_WIDTH
DEVICE_WIFI_BUTTON_WIDTH = 96
DEVICE_WIFI_BUTTON_HEIGHT = 32
DEVICE_IDENTITY_ROW_MIN_HEIGHT = 24
DEVICE_WIFI_INDICATOR_ON = "#ffffff"
DEVICE_WIFI_INDICATOR_OFF = "#344d66"


class _WifiSignalIndicator(QWidget):
    """Small three-arc Wi-Fi indicator drawn in code."""

    def __init__(self, band: str) -> None:
        super().__init__()
        self._on = False
        self.setObjectName(f"deviceWifiSignal_{band.replace('.', '_')}")
        self.setAccessibleName(f"{band} GHz Wi-Fi signal")
        self.setFixedSize(30, 24)
        self.setProperty("wifiBandState", "off")

    def set_on(self, on: bool) -> None:
        on = bool(on)
        if self._on == on:
            return
        self._on = on
        self.setProperty("wifiBandState", "on" if on else "off")
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(DEVICE_WIFI_INDICATOR_ON if self._on else DEVICE_WIFI_INDICATOR_OFF)
        painter.setPen(QPen(color, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center_x = self.width() / 2.0
        bottom = self.height() - 2.0
        for radius in (12.0, 8.0, 4.5):
            painter.drawArc(
                QRectF(center_x - radius, bottom - radius * 2.0, radius * 2.0, radius * 2.0),
                45 * 16,
                90 * 16,
            )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(center_x - 1.8, bottom - 3.6, 3.6, 3.6))


_DEVICE_READING_EXPLANATIONS = {
    PresentationState.UNSUPPORTED: "This setting or reading is not supported by the selected router profile.",
    PresentationState.NOT_APPLICABLE: "This item does not apply to the current connection.",
    PresentationState.PENDING: "Waiting for the router's reading.",
    PresentationState.MISSING: "No confirmed reading is available from the router.",
    PresentationState.UNATTACHED: "Connect to the router to read this item.",
    PresentationState.INVALID: "The returned reading could not be confirmed.",
    PresentationState.HELD: "Last confirmed reading; waiting for a fresh reading.",
    PresentationState.TRANSITION: "The connection is changing; waiting for confirmation.",
}


class _CompactFieldTable(QWidget):
    def __init__(
        self,
        specs: tuple[FieldSpec, ...],
        *,
        value_columns: int = 1,
        row_min_height: int = 0,
        alternating_rows: bool = False,
    ) -> None:
        super().__init__()
        self._labels: dict[str, QLabel] = {}
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred if alternating_rows else QSizePolicy.Policy.Maximum,
        )
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(2 if alternating_rows else 4)
        for index, spec in enumerate(specs):
            block = index % value_columns
            row = index if alternating_rows else index // value_columns
            if row_min_height:
                layout.setRowMinimumHeight(row, row_min_height)
            name = QLabel(f"{spec.label}:")
            name.setObjectName("truthFieldName")
            name.setWordWrap(True)
            value = QLabel("----")
            value.setProperty("nguiRole", "truthFieldValue")
            value.setObjectName(f"value__{spec.field_id.replace('.', '__')}")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(name, row, block * 2)
            layout.addWidget(value, row, block * 2 + 1)
            self._labels[spec.field_id] = value
        for block in range(value_columns):
            layout.setColumnStretch(block * 2 + 1, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            value = truth.fields[field_id]
            _render_device_value(label, value.text)
            display_state = value.state
            if display_state is PresentationState.VALID:
                display_state = {
                    "UNSUPPORTED": PresentationState.UNSUPPORTED,
                    "UNSUP.": PresentationState.UNSUPPORTED,
                    "----": PresentationState.MISSING,
                    "UNKNOWN": PresentationState.MISSING,
                }.get(value.text.strip().upper(), display_state)
            explanation = _DEVICE_READING_EXPLANATIONS.get(display_state, "")
            detail = value.text
            tooltip = detail if not explanation else f"{detail}\n{explanation}"
            if label.toolTip() != tooltip:
                label.setToolTip(tooltip)


def _device_specs(field_ids: tuple[str, ...]) -> tuple[FieldSpec, ...]:
    by_id = {spec.field_id: spec for spec in fields_for_page(PageId.DEVICE)}
    missing = set(field_ids) - set(by_id)
    if missing:
        raise ValueError(f"undeclared DEVICE renderer fields: {sorted(missing)}")
    return tuple(by_id[field_id] for field_id in field_ids)


def _device_value_state(text: str) -> str:
    normalized = text.strip().upper()
    if normalized in {"OK", "YES", "ON", "SUPPORTED", "AVAILABLE", "LICENSED", "CONNECTED"}:
        return "good"
    if normalized in {"NO", "MISS", "ERROR", "FAILED", "BLOCKED"}:
        return "bad"
    return "neutral"


def _render_device_value(label: QLabel, text: str) -> None:
    display_text = display_value_text(text)
    if label.text() != display_text:
        label.setText(display_text)
    state = _device_value_state(text)
    if label.property("nguiValueState") == state:
        return
    label.setProperty("nguiValueState", state)
    style = label.style()
    style.unpolish(label)
    style.polish(label)


class _DeviceLoginColumn(QWidget):
    def __init__(self, register: NGUIInteractionRegister) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controls = EntryInputs(register)
        self.status = _CompactFieldTable(_device_specs(_DEVICE_LOGIN_FIELDS))
        self.setMaximumWidth(DEVICE_SIGN_IN_MAX_WIDTH)
        self.setMaximumHeight(DEVICE_SIGN_IN_MAX_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setObjectName("deviceSignInCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        title = QLabel("Sign In")
        title.setObjectName("deviceColumnHeading")
        layout.addWidget(title)
        layout.addWidget(self.controls)
        self.important = _DeviceImportantBlock()
        layout.addWidget(self.important)
        layout.addWidget(self.status)
        self._truth: PageTruthSnapshot | None = None
        self.mobile_data = QPushButton("Mobile network")
        self.mobile_data.setObjectName("nguiDeviceMobileDataToggle")
        self.mobile_data.setEnabled(False)
        bind_button(self.mobile_data, register, IntentKind.SET_MOBILE_DATA, self._mobile_payload)
        self.mobile_data.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.mobile_data, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

    def _mobile_value(self) -> bool | None:
        if self._truth is None:
            return None
        field = self._truth.fields["device.mobile_data_enabled"]
        return field.value if field.state is PresentationState.VALID and type(field.value) is bool else None

    def _mobile_payload(self) -> dict[str, object]:
        current = self._mobile_value()
        if current is None or self._truth is None or self._truth.matrix_identity is None:
            raise RuntimeError("mobile_data_current_matrix_required")
        return {"enabled": not current, "matrix_identity": self._truth.matrix_identity.as_dict()}

    @property
    def field_ids(self) -> frozenset[str]:
        return self.status.field_ids

    def render(self, truth: PageTruthSnapshot) -> None:
        self._truth = truth
        self.controls.render_page_truth(truth)
        self.important.render(truth)
        self.status.render(truth)
        current = self._mobile_value()
        field = truth.fields["action.control_availability"]
        availability = control_availability_from_mapping(
            field.value if field.state is PresentationState.VALID and isinstance(field.value, Mapping) else None,
            "mobile_data",
        )
        self.mobile_data.setText("Mobile network" if current is None else "Mobile disconnect" if current else "Mobile connect")
        self.mobile_data.setStyleSheet(
            "QPushButton { background-color: #b32626; color: #ffffff; border: 1px solid #ff6666; padding: 4px 10px; }"
            "QPushButton:disabled { background-color: #111b27; color: #52708d; border-color: #20344a; }"
            if current is True else ""
        )
        self.mobile_data.setEnabled(current is not None and truth.matrix_identity is not None and availability.enabled)
        self.mobile_data.setToolTip(availability.reason if current is not None else "Waiting for the router's Mobile Data setting.")


class _DeviceUtilities(QWidget):
    """Render Device utilities outside the green Sign In card."""

    def __init__(
        self,
        register: NGUIInteractionRegister,
        controls: "EntryInputs",
    ) -> None:
        super().__init__()
        self.setObjectName("deviceUtilityControls")
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(1)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        from .read_diagnostics import ReadDiagnosticPanel
        self.read_diagnostics = ReadDiagnosticPanel(register)
        self.read_diagnostics.setMinimumWidth(280)
        layout.addWidget(self.read_diagnostics, 0, 0, Qt.AlignmentFlag.AlignLeft)
        self.read_diagnostics.report.hide()
        self.read_diagnostics.report.setMaximumWidth(280)
        self.read_diagnostics.report.setMaximumHeight(52)
        self.action_status = _CompactFieldTable(_device_specs(("command.status",)))
        self.action_status.hide()

        self.support_bundle_button = QPushButton("SUPPORT BUNDLE")
        self.support_bundle_button.setObjectName(
            "nguiDeviceUtility_create_support_bundle"
        )
        self.support_bundle_button.setProperty("nguiRole", "utility_primary")
        self.support_bundle_button.setCheckable(True)
        bind_button(
            self.support_bundle_button,
            register,
            IntentKind.CREATE_SUPPORT_BUNDLE,
        )
        self.scan_network_button = QPushButton("SCAN NETWORK")
        self.scan_network_button.setObjectName("nguiDeviceUtility_scan_network")
        self.scan_network_button.setProperty("nguiRole", "utility_primary")
        self.scan_network_button.setCheckable(True)
        bind_button(
            self.scan_network_button,
            register,
            IntentKind.SCAN_NETWORK,
            lambda: {"seed_host": controls.address.text()},
        )
        self._utility_buttons = {
            IntentKind.CREATE_SUPPORT_BUNDLE: self.support_bundle_button,
            IntentKind.SCAN_NETWORK: self.scan_network_button,
        }
        for button in (self.read_diagnostics.start, self.scan_network_button, self.support_bundle_button):
            button.setFixedHeight(22)
            button.setStyleSheet("QPushButton { min-height: 20px; padding: 0px 8px; }")
        self.status_labels: dict[IntentKind, QLabel] = {}
        for row, intent in enumerate(
            (IntentKind.SCAN_NETWORK, IntentKind.CREATE_SUPPORT_BUNDLE)
        ):
            button = self._utility_buttons[intent]
            status = QLabel("")
            status.setObjectName(f"nguiDeviceUtilityStatus_{intent.value}")
            status.setProperty("nguiRole", "utility_status")
            status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            status.setWordWrap(True)
            self.status_labels[intent] = status
            status.setMaximumWidth(280)
            status.setMaximumHeight(52)
            layout.addWidget(button, 1 if row == 0 else 4, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(status, 2 if row == 0 else 5, 0, Qt.AlignmentFlag.AlignTop)
            status.hide()

        self.scan_results = QPlainTextEdit()
        self.scan_results.setReadOnly(True)
        self.scan_results.setObjectName("deviceScanResults")
        self.scan_results.hide()
        self.scan_results.setMaximumHeight(100)
        layout.addWidget(self.scan_results, 3, 0)
        self.scan_clear_timer = QTimer(self)
        self.scan_clear_timer.setSingleShot(True)
        self.scan_clear_timer.setInterval(15000)
        bind_timer(self.scan_clear_timer, self.clear_scan_result)

    def show_scan_result(self, text: str) -> None:
        self.scan_results.setPlainText(text)
        self.scan_results.show()
        self.scan_clear_timer.start()

    def clear_scan_result(self) -> None:
        self.scan_results.clear()
        self.scan_results.hide()

    @staticmethod
    def _status_text(row: object) -> str:
        if not isinstance(row, Mapping):
            return ""
        state = str(row.get("state") or "").strip()
        if not state:
            return ""
        text = state.replace("_", " ").title()
        if state == "running":
            progress = row.get("progress_pct")
            if type(progress) in {int, float}:
                text += f" {float(progress):.0f}%"
        message = str(row.get("message") or "").strip()
        return text + (f" — {message}" if message else "")

    def render(self, truth: PageTruthSnapshot) -> None:
        self.read_diagnostics.render(truth)
        self.action_status.render(truth)
        lifecycle = truth.fields["utility.lifecycle"]
        values = (
            lifecycle.value
            if lifecycle.state is PresentationState.VALID
            and isinstance(lifecycle.value, Mapping)
            else {}
        )
        render_utility_button_lifecycle(
            values,
            self._utility_buttons,
        )
        for intent, label in self.status_labels.items():
            text = self._status_text(values.get(intent.value))
            if label.text() != text:
                label.setText(text)
            label.setVisible(bool(text))


from .network_settings import NetworkSettingsEditor


class _DeviceNetworkColumn(QScrollArea):
    """One form; connection lifecycle controls editing."""

    def __init__(self, register, editor):
        super().__init__()
        self.setObjectName("deviceNetworkColumn")
        self.setMinimumWidth(340)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor = editor
        self.setWidget(editor)

    def render(self, truth):
        self.editor.render(truth)


class _DeviceIdentityColumn(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(DEVICE_IDENTITY_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.identity = _CompactFieldTable(
            _device_specs(_DEVICE_IDENTITY_FIELDS),
            row_min_height=DEVICE_IDENTITY_ROW_MIN_HEIGHT,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.identity)
        self.identity.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        for row in range(self.identity.layout().rowCount()):
            self.identity.layout().setRowStretch(row, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return self.identity.field_ids

    def render(self, truth: PageTruthSnapshot) -> None:
        self.identity.render(truth)


class _DeviceWifiPanel(QGroupBox):
    def __init__(self, register: NGUIInteractionRegister) -> None:
        super().__init__("Wi-Fi")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._wifi_labels: dict[str, QLabel] = {}
        self._wifi_indicators: dict[str, _WifiSignalIndicator] = {}
        self._wifi_buttons: dict[str, QPushButton] = {}
        wifi_layout = QGridLayout(self)
        wifi_layout.setVerticalSpacing(5)
        wifi_layout.setColumnStretch(2, 1)
        for row, (radio, band_label, field_id) in enumerate(_DEVICE_WIFI_BANDS):
            name = QLabel(f"WiFi {band_label}:")
            name.setObjectName("truthFieldName")
            wifi_layout.addWidget(name, row, 0)
            indicator = _WifiSignalIndicator(radio)
            self._wifi_indicators[field_id] = indicator
            wifi_layout.addWidget(indicator, row, 1, Qt.AlignmentFlag.AlignCenter)
            label = QLabel("OFF")
            label.setProperty("nguiRole", "truthFieldValue")
            label.setObjectName(f"value__{field_id.replace('.', '__')}")
            self._wifi_labels[field_id] = label
            wifi_layout.addWidget(label, row, 2)
            button = QPushButton("OFF")
            button.setObjectName(
                f"nguiDeviceWifi_{radio.replace('.', '_')}GHzToggle"
            )
            button.setProperty("nguiRole", "wifi_toggle")
            button.setProperty("nguiWifiInteractionState", "unavailable")
            button.setCheckable(True)
            button.setFixedSize(DEVICE_WIFI_BUTTON_WIDTH, DEVICE_WIFI_BUTTON_HEIGHT)
            bind_button(
                button,
                register,
                IntentKind.TOGGLE_WIFI_RADIO,
                lambda radio=radio, field_id=field_id: self._wifi_intent_payload(
                    radio,
                    field_id,
                ),
            )
            self._wifi_buttons[field_id] = button
            wifi_layout.addWidget(button, row, 3)
        self._truth: PageTruthSnapshot | None = None

    def _wifi_value(self, field_id: str) -> bool | None:
        if self._truth is None:
            return None
        field = self._truth.fields[field_id]
        if field.state is not PresentationState.VALID or not isinstance(field.value, bool):
            return None
        return field.value

    def _wifi_intent_payload(self, radio: str, field_id: str) -> dict[str, object]:
        current = self._wifi_value(field_id)
        if current is None:
            raise RuntimeError(f"Wi-Fi intent requires valid Device page truth: {field_id}")
        return {"radio": radio, "enabled": not current}

    def _wifi_availability(self):
        if self._truth is None:
            return control_availability_from_mapping(
                None,
                "wifi_control",
            )
        field = self._truth.fields["action.control_availability"]
        return control_availability_from_mapping(
            field.value
            if field.state is PresentationState.VALID
            and isinstance(field.value, Mapping)
            else None,
            "wifi_control",
        )

    def _render_wifi_button(
        self,
        field_id: str,
    ) -> None:
        button = self._wifi_buttons[field_id]
        availability = self._wifi_availability()
        current = self._wifi_value(field_id)
        if current is None:
            text = "----"
            enabled = False
            interaction_state = "unavailable"
        elif not availability.enabled:
            text = "OFF" if current else "ON"
            enabled = False
            interaction_state = "blocked"
        else:
            text = "OFF" if current else "ON"
            enabled = True
            interaction_state = "readback"
        if button.text() != text:
            button.setText(text)
        if button.isEnabled() is not enabled:
            button.setEnabled(enabled)
        if current is None:
            tooltip = "Wi-Fi control is unavailable because this network's status has not been confirmed."
        elif not availability.enabled:
            tooltip = availability.reason
        else:
            tooltip = "Turn this Wi-Fi network off." if current else "Turn this Wi-Fi network on."
        button.setToolTip(tooltip)
        checked = interaction_state in {"unavailable", "blocked"}
        if button.isChecked() != checked:
            button.setChecked(checked)
        if button.property("nguiWifiInteractionState") != interaction_state:
            button.setProperty("nguiWifiInteractionState", interaction_state)
            style = button.style()
            style.unpolish(button)
            style.polish(button)

    def _render_wifi_buttons(self) -> None:
        for field_id in self._wifi_buttons:
            self._render_wifi_button(field_id)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(_DEVICE_WIFI_FIELDS) | {"action.control_availability"}

    def render(self, truth: PageTruthSnapshot) -> None:
        self._truth = truth
        for field_id, label in self._wifi_labels.items():
            current = self._wifi_value(field_id)
            _render_device_value(label, truth.fields[field_id].text)
            self._wifi_indicators[field_id].set_on(current is True)
        self._render_wifi_buttons()


class _DeviceCapabilityColumn(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("deviceCapabilityPanel")
        self.live = _CompactFieldTable(
            _device_specs(_DEVICE_LIVE_CONTROL_FIELDS),
            value_columns=2,
            alternating_rows=True,
        )
        self.checks = _CompactFieldTable(
            _device_specs(_DEVICE_ROUTER_CHECK_FIELDS),
            value_columns=2,
            alternating_rows=True,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(4)
        live_title = QLabel("LIVE CONTROLS")
        live_title.setObjectName("deviceSectionHeading")
        checks_title = QLabel("ROUTER CHECKS")
        checks_title.setObjectName("deviceSectionHeading")
        layout.addWidget(live_title)
        layout.addWidget(self.live, self.live.layout().rowCount())
        layout.addSpacing(6)
        layout.addWidget(checks_title)
        layout.addWidget(self.checks, self.checks.layout().rowCount())
        for table in (self.live, self.checks):
            for row in range(table.layout().rowCount()):
                table.layout().setRowStretch(row, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @property
    def field_ids(self) -> frozenset[str]:
        return self.live.field_ids | self.checks.field_ids

    def render(self, truth: PageTruthSnapshot) -> None:
        self.live.render(truth)
        self.checks.render(truth)


class DevicePageShell(QWidget):
    """Render the four-column Device page from one PageTruth snapshot."""

    def __init__(
        self,
        register: NGUIInteractionRegister,
        page_labels: Mapping[PageId, str],
        navigate_local: Callable[[PageId], None],
        confirmation: NGUIConfirmationCoordinator,
    ) -> None:
        super().__init__()
        specs = tuple(fields_for_page(PageId.DEVICE))
        login_column = _DeviceLoginColumn(register)
        self.login_column = login_column
        self.utility_controls = _DeviceUtilities(register, login_column.controls)
        self.device_header = DeviceCommandHeader(
            register,
            page_labels,
            navigate_local,
            confirmation,
        )
        body = QWidget()
        columns = QHBoxLayout(body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        self._grids = (
            login_column,
            _DeviceIdentityColumn(),
            _DeviceCapabilityColumn(),
            _DeviceWifiPanel(register),
        )
        left_column = QWidget()
        left_column.setObjectName("deviceSignInAndUtilities")
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(login_column, 0, Qt.AlignmentFlag.AlignLeft)
        left_layout.addWidget(self.utility_controls)
        self.wifi_panel = self._grids[3]
        left_layout.addStretch(1)
        left_layout.addWidget(self.wifi_panel)
        self._left_column = left_column
        self.controls = self._grids[0].controls
        self.network_column = _DeviceNetworkColumn(register, self.controls.network_settings)
        columns.addWidget(self._left_column, 0)
        network_body = QWidget()
        network_layout = QVBoxLayout(network_body)
        network_layout.setContentsMargins(0, 0, 0, 0)
        network_layout.addWidget(self.network_column, 1)
        columns.addWidget(network_body, DEVICE_NETWORK_STRETCH)
        columns.addWidget(self._grids[1], DEVICE_IDENTITY_STRETCH)
        columns.addWidget(self._grids[2], DEVICE_CAPABILITY_STRETCH)
        self._columns_layout = columns

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 18, 36, 8)
        root.setSpacing(8)
        root.addWidget(self.device_header)
        body_scroll = QScrollArea()
        body_scroll.setObjectName("deviceBodyScroll")
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_scroll.setWidget(body)
        root.addWidget(body_scroll, 1)

        self.intentionally_absent_field_ids = _DEVICE_INTENTIONALLY_ABSENT_FIELDS
        control_only = {spec.field_id for spec in specs if not spec.display_in_grid}
        if control_only != {
            "device.mobile_data_enabled",
            "network.settings",
            "network.diagnostic",
            "read.diagnostic",
            "sim.network",
            "network.apn_profiles",
            "event.recent",
            "capability.session_capability_comparison",
            "capability.session_capability_id",
            "capability.session_lte_bands",
            "capability.session_lte_status",
            "profile.available_models",
            "profile.models_by_manufacturer",
            "profile.connection_defaults",
            "profile.discovery_suggestion",
            "profile.login_lifecycle",
            "manual.lifecycle",
            "command.action_capabilities",
        }:
            raise ValueError(f"DEVICE control-only field contract failed: {sorted(control_only)}")
        assigned = tuple(field_id for grid in self._grids for field_id in grid.field_ids) + ("provider.plmn", "device.imsi") + tuple(self.utility_controls.action_status.field_ids)
        duplicates = {field_id for field_id in assigned if assigned.count(field_id) > 1}
        if duplicates:
            raise ValueError(f"DEVICE renderer fields assigned more than once: {sorted(duplicates)}")
        expected = {spec.field_id for spec in specs if spec.display_in_grid}
        covered = self.field_ids | self.intentionally_absent_field_ids
        if covered != expected:
            missing = expected - covered
            extra = self.field_ids - expected
            raise ValueError(f"DEVICE renderer field contract failed: missing={sorted(missing)}, extra={sorted(extra)}")

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(field_id for grid in self._grids for field_id in grid.field_ids) | {"provider.plmn", "device.imsi"} | self.utility_controls.action_status.field_ids

    def render(self, truth: PageTruthSnapshot) -> None:
        for grid in self._grids:
            grid.render(truth)
        self.utility_controls.render(truth)
        self.network_column.render(truth)


class EntryInputs(QWidget):
    MODEL_PLACEHOLDER = "Model"
    MANUFACTURER_PLACEHOLDER = "Mfg"

    def __init__(self, register: NGUIInteractionRegister) -> None:
        super().__init__()
        self._source_driver_license = ""
        self._trace_selected_model = ""
        self._trace_selected_at = 0.0
        self._all_models: list[str] = []
        self._models_by_manufacturer: dict[str, list[str]] = {}
        self._connection_defaults: dict[str, Mapping[str, object]] = {}
        self._last_profile_applied = False
        self._selected_profile_source_sequence = 0
        self._discovery_suggestion_source_sequence = 0
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.manufacturer = QComboBox()
        self.manufacturer.setObjectName("nguiDeviceManufacturer")
        self.manufacturer.addItem(self.MANUFACTURER_PLACEHOLDER)
        self.model = QComboBox()
        self.model.setObjectName("nguiDeviceModel")
        self.model.addItem(self.MODEL_PLACEHOLDER, "")
        self.address = NGUITextInput()
        self.address.setObjectName("nguiDeviceAddress")
        self.username = NGUITextInput()
        self.username.setObjectName("nguiDeviceUsername")
        self.password = NGUITextInput()
        self.password.setObjectName("nguiDevicePassword")
        self.password.setEchoMode(QLineEdit.Password)
        self._remembered_router_serial = ""
        self.start_mode = QComboBox()
        self.start_mode.setObjectName("nguiDeviceStartMode")
        self.start_mode.addItem("Live - allow modem actions", "live")
        self.start_mode.addItem("Observe only - no modem actions", "observe")
        for widget in (
            self.manufacturer,
            self.model,
            self.address,
            self.username,
            self.password,
            self.start_mode,
        ):
            widget.setProperty("nguiRole", "device_entry_input")
            stamp_draft_control(widget)
        self.manufacturer.setMinimumWidth(DEVICE_MANUFACTURER_WIDTH)
        self.manufacturer.setMaximumWidth(DEVICE_MANUFACTURER_WIDTH)
        self.model.setMinimumWidth(DEVICE_MODEL_WIDTH)
        self.model.setMaximumWidth(DEVICE_MODEL_WIDTH)
        for widget in (self.manufacturer, self.model):
            widget.setMinimumHeight(44)
            widget.setMaximumHeight(44)
        for widget in (self.address, self.username, self.password, self.start_mode):
            widget.setMinimumWidth(DEVICE_ENTRY_CONTROL_WIDTH)
            widget.setMaximumWidth(DEVICE_ENTRY_CONTROL_WIDTH)
            widget.setMinimumHeight(44)
            widget.setMaximumHeight(44)
        bind_combo_changed(self.manufacturer, self._refresh_model_options_for_manufacturer)
        bind_combo_changed(self.model, self._apply_connection_defaults)
        bind_text_draft(self.address, lambda _text: self._update_connection_context())
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(4)
        self.connect = QPushButton("CONNECT")
        self.connect.setProperty("nguiRole", "user_action")
        self.connect.setObjectName("deviceConnectButton")
        self.connect.setCheckable(True)
        self.connect.setMinimumWidth(DEVICE_ENTRY_CONTROL_WIDTH)
        self.connect.setMaximumWidth(DEVICE_ENTRY_CONTROL_WIDTH)
        self.connect.setMinimumHeight(44)
        self.connect.setMaximumHeight(44)
        bind_button(
            self.connect,
            register,
            IntentKind.SUBMIT_LOGIN,
            self._prepare_login_payload,
        )
        self.disconnect = QPushButton("DISCONNECT")
        self.disconnect.setObjectName("deviceDisconnectButton")
        self.disconnect.setProperty("nguiRole", "user_action")
        self.disconnect.setFixedSize(DEVICE_ENTRY_CONTROL_WIDTH, 44)
        self.disconnect.setToolTip("Disconnect 54GUC and keep your entries. The router stays online.")
        bind_button(self.disconnect, register, IntentKind.DISCONNECT_SESSION)
        self.connection_action = QStackedWidget()
        self.connection_action.setFixedWidth(DEVICE_ENTRY_CONTROL_WIDTH)
        self.connection_action.addWidget(self.connect)
        self.connection_action.addWidget(self.disconnect)
        layout.addWidget(self.connection_action, 0, 1)
        self.cancel_connect = QPushButton("CANCEL")
        self.cancel_connect.setProperty("nguiRole", "user_action")
        self.cancel_connect.setObjectName("deviceCancelConnectButton")
        self.cancel_connect.setEnabled(False)
        self.cancel_connect.setMinimumHeight(44)
        self.cancel_connect.setToolTip("Stop this connection attempt and keep your entries.")
        bind_button(self.cancel_connect, register, IntentKind.CANCEL_LOGIN)
        layout.addWidget(self.cancel_connect, 0, 0)
        for row, (label, widget) in enumerate((
            ("Mfg", self.manufacturer),
            ("Model", self.model),
            ("Address", self.address),
            ("User name", self.username),
            ("Password", self.password),
            ("Start mode", self.start_mode),
        ), 1):
            layout.addWidget(QLabel(label), row, 0)
            layout.addWidget(widget, row, 1)
        self.network_settings = NetworkSettingsEditor(register)

    def _prepare_login_payload(self) -> dict[str, object]:
        # Capture edits before locking, and establish the result cutoff before
        # the intent publisher timestamps this request.
        draft = self.network_settings.draft()
        payload = {
            "model": "" if self.model.currentIndex() == 0 else str(self.model.currentData()),
            "address": self.address.text(),
            "username": self.username.text(),
            "password": self.password.text(),
            "license": self._source_driver_license,
            "mode": str(self.start_mode.currentData()),
            "trace_selected_at": self._trace_selected_at,
            **({"router_serial": self._remembered_router_serial} if self._remembered_router_serial else {}),
            **({"network_draft": draft} if draft else {}),
        }
        self.network_settings.start_connection()
        self.connect.setEnabled(False)
        self.connect.setText("CONNECTING...")
        self.cancel_connect.setEnabled(True)
        return payload

    def set_source_driver_license(self, value: str) -> None:
        """Set source-run input used by the normal Connect action."""

        self._source_driver_license = str(value or "")

    def render_page_truth(self, truth: PageTruthSnapshot) -> None:
        self.network_settings.set_connection_context((self.model.currentData(), self.address.text().strip()))
        self._render_connect_state(truth.fields["profile.login_lifecycle"].value)
        models = truth.fields["profile.available_models"].value
        grouped = truth.fields["profile.models_by_manufacturer"].value
        defaults = truth.fields["profile.connection_defaults"].value
        if not isinstance(models, (list, tuple)) or not isinstance(grouped, Mapping):
            return
        profiles = (
            defaults.get("profiles")
            if isinstance(defaults, Mapping)
            and defaults.get("schema") == "NGUIProfileConnectionDefaults.v1"
            and defaults.get("owner") == SourceOwner.PROFILE_SELECTION.value
            and isinstance(defaults.get("profiles"), Mapping)
            else {}
        )
        connection_defaults = {
            str(label): dict(row)
            for label, row in profiles.items()
            if isinstance(row, Mapping)
        }
        all_models = [str(model) for model in models]
        models_by_manufacturer = {
            str(manufacturer): [str(model) for model in items]
            for manufacturer, items in grouped.items()
            if isinstance(items, (list, tuple))
        }
        defaults_changed = connection_defaults != self._connection_defaults
        self._connection_defaults = connection_defaults
        catalogs_changed = (
            all_models != self._all_models
            or models_by_manufacturer != self._models_by_manufacturer
        )
        if catalogs_changed:
            self._all_models = all_models
            self._models_by_manufacturer = models_by_manufacturer
            self._refresh_manufacturer_options()
            self._refresh_model_options_for_manufacturer()
        elif defaults_changed:
            self._apply_connection_defaults()
        self._apply_last_profile(defaults)
        self._apply_owner_selected_profile(
            truth.fields["profile.selected_profile"]
        )
        self._apply_discovery_suggestion(
            truth.fields["profile.discovery_suggestion"]
        )

    def _apply_last_profile(self, defaults: object) -> None:
        if self._last_profile_applied or not isinstance(defaults, Mapping):
            return
        profile_id = str(defaults.get("last_profile_id") or "")
        if not profile_id:
            return
        canonical = next((label for label, row in self._connection_defaults.items()
                          if row.get("profile_id") == profile_id), "")
        manufacturer = next((name for name, models in self._models_by_manufacturer.items()
                             if canonical in models), "")
        if not manufacturer:
            return
        self._last_profile_applied = True
        if self.model.currentIndex() > 0 or self.model.hasFocus():
            return
        self.manufacturer.setCurrentText(manufacturer)
        self._refresh_model_options_for_manufacturer()
        index = self.model.findData(canonical)
        if index > 0:
            self.model.setCurrentIndex(index)

    def _apply_owner_selected_profile(self, selected_profile) -> None:
        sequence = int(selected_profile.source_sequence or 0)
        if sequence <= self._selected_profile_source_sequence:
            return
        if selected_profile.state not in {
            PresentationState.VALID,
            PresentationState.HELD,
        }:
            return
        canonical = str(
            selected_profile.value or selected_profile.text or ""
        ).strip()
        if not canonical:
            return
        matching_manufacturer = next(
            (
                manufacturer
                for manufacturer, models in self._models_by_manufacturer.items()
                if canonical in models
            ),
            "",
        )
        if not matching_manufacturer:
            return
        self.manufacturer.blockSignals(True)
        self.manufacturer.setCurrentText(matching_manufacturer)
        self.manufacturer.blockSignals(False)
        self._refresh_model_options_for_manufacturer()
        selected_index = self.model.findData(canonical)
        if selected_index < 1:
            return
        self.model.blockSignals(True)
        self.model.setCurrentIndex(selected_index)
        self.model.blockSignals(False)
        self._selected_profile_source_sequence = sequence
        self._apply_connection_defaults()

    def _render_connect_state(self, lifecycle: object) -> None:
        state = login_lifecycle_state(lifecycle)
        checked = state in {
            "connecting",
            "starting",
            "identity_required",
            "waiting_for_data",
            "confirming_modem",
            "reconnecting",
            "connected",
        }
        if self.connect.isChecked() != checked:
            self.connect.setChecked(checked)
        pending = checked and state != "connected"
        stopping = state == "stopping"
        self.connection_action.setCurrentWidget(self.disconnect if state == "connected" else self.connect)
        self.disconnect.setEnabled(state == "connected")
        self.connect.setEnabled(not pending and not stopping)
        self.connect.setText("STOPPING..." if stopping else "CONNECTING..." if pending else "CONNECT")
        self.cancel_connect.setEnabled(pending)

    def _refresh_manufacturer_options(self) -> None:
        manufacturers = [self.MANUFACTURER_PLACEHOLDER, *self._models_by_manufacturer]
        current_manufacturers = [
            self.manufacturer.itemText(index) for index in range(self.manufacturer.count())
        ]
        if manufacturers == current_manufacturers:
            return
        selected_manufacturer = self.manufacturer.currentText()
        self.manufacturer.blockSignals(True)
        self.manufacturer.clear()
        self.manufacturer.addItems(manufacturers)
        if selected_manufacturer in manufacturers and selected_manufacturer != self.MANUFACTURER_PLACEHOLDER:
            self.manufacturer.setCurrentText(selected_manufacturer)
        else:
            self.manufacturer.setCurrentIndex(0)
        self.manufacturer.blockSignals(False)

    def _refresh_model_options_for_manufacturer(self) -> None:
        selected_manufacturer = self.manufacturer.currentText()
        selected_model = str(self.model.currentData() or "")
        models = _models_for_manufacturer(
            self._all_models,
            self._models_by_manufacturer,
            selected_manufacturer,
        )
        options = [
            (self.MODEL_PLACEHOLDER, ""),
            *(
                (_model_display_name(selected_manufacturer, model), model)
                for model in models
            ),
        ]
        current_options = [
            (self.model.itemText(index), str(self.model.itemData(index) or ""))
            for index in range(self.model.count())
        ]
        if options == current_options:
            return
        self.model.blockSignals(True)
        self.model.clear()
        for display, canonical in options:
            self.model.addItem(display, canonical)
        selected_index = self.model.findData(selected_model)
        if selected_index > 0:
            self.model.setCurrentIndex(selected_index)
        else:
            self.model.setCurrentIndex(0)
        self.model.blockSignals(False)
        self._apply_connection_defaults()

    def _apply_connection_defaults(self) -> None:
        selected = str(self.model.currentData() or "").strip()
        if selected != self._trace_selected_model:
            self._trace_selected_model = selected
            self._trace_selected_at = monotonic() if selected else 0.0
        row = self._connection_defaults.get(selected, {})
        if not isinstance(row, Mapping):
            return
        for widget, key in (
            (self.address, "address"),
            (self.username, "username"),
        ):
            value = str(row.get(key) or "").strip()
            current = widget.text().strip()
            if (
                value
                and not widget.hasFocus()
                and not widget.isModified()
                and (
                    not current
                    or (
                        key == "address"
                        and current in {"192.168.1.1", "192.168.8.1"}
                    )
                )
            ):
                widget.setText(value)
                widget.setModified(False)
        self._remembered_router_serial = str(row.get("router_serial") or "").strip()
        self._update_connection_context()
        self.network_settings.apply_remembered_preset(row.get("network_preset"))

    def _update_connection_context(self) -> None:
        self.network_settings.set_connection_context(
            (str(self.model.currentData() or ""), self.address.text().strip())
        )
        row = self._connection_defaults.get(str(self.model.currentData() or ""))
        if row is not None:
            self.network_settings.set_write_permission(row["network_settings_writable"])
            self.network_settings.set_profile_creation(row["apn_profile_creation"])

    def _apply_discovery_suggestion(self, field) -> None:
        sequence = int(field.source_sequence or 0)
        if sequence <= self._discovery_suggestion_source_sequence:
            return
        if field.state not in {
            PresentationState.VALID,
            PresentationState.HELD,
        }:
            return
        suggestion = field.value
        if not isinstance(suggestion, Mapping) or not suggestion:
            self._discovery_suggestion_source_sequence = sequence
            return
        if (
            suggestion.get("schema") != PROFILE_DISCOVERY_SUGGESTION_SCHEMA
            or suggestion.get("owner") != PROFILE_DISCOVERY_SUGGESTION_OWNER
        ):
            raise RuntimeError("typed profile discovery suggestion required")
        suggested_host = str(suggestion.get("host") or "").strip()
        if (
            suggested_host
            and not self.address.text().strip()
            and not self.address.hasFocus()
            and not self.address.isModified()
        ):
            self.address.setText(suggested_host)
        suggested_model = str(suggestion.get("model") or "").strip()
        suggested_manufacturer = str(
            suggestion.get("manufacturer") or ""
        ).strip()
        if (
            suggested_model
            and suggested_manufacturer
            and self.model.currentIndex() == 0
            and not self.model.hasFocus()
        ):
            self.manufacturer.setCurrentText(suggested_manufacturer)
            self._refresh_model_options_for_manufacturer()
            suggested_index = self.model.findData(suggested_model)
            if suggested_index >= 0:
                self.model.setCurrentIndex(suggested_index)
        self._discovery_suggestion_source_sequence = sequence


def _models_for_manufacturer(
    models: list[str],
    models_by_manufacturer: Mapping[str, list[str]],
    manufacturer: str,
) -> list[str]:
    selected = str(manufacturer or "").strip()
    if not selected or selected == EntryInputs.MANUFACTURER_PLACEHOLDER:
        return list(models)
    return list(models_by_manufacturer.get(selected, ()))


def _model_display_name(manufacturer: str, canonical_model: str) -> str:
    manufacturer_text = str(manufacturer or "").strip()
    model_text = str(canonical_model or "").strip()
    prefix = f"{manufacturer_text} "
    if manufacturer_text and model_text.lower().startswith(prefix.lower()):
        model_text = model_text[len(prefix) :]
    if manufacturer_text.casefold() == "tp-link":
        model_text = model_text.replace("NX520 A1 Serbia SRBA1", "NX520 SRBA1")
    return model_text


class _DeviceImportantBlock(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("deviceImportantBlock")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        title = QLabel("IMPORTANT")
        title.setObjectName("deviceImportantTitle")
        self.body = QLabel(
            "Live mode starts the real controller and allows\n"
            "actions. Disconnect before connecting in a\n"
            "different mode. Your entries are retained.\n\n"
            "Enter router details"
        )
        self.body.setObjectName("deviceImportantBody")
        self.body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.body)

    def render(self, truth: PageTruthSnapshot) -> None:
        status = truth.fields["profile.login_status"].text
        detail = "" if status == "Connected" else f"\n\n{status}"
        text = (
            "Live mode starts the real controller and allows\n"
            "actions. Disconnect before connecting in a\n"
            f"different mode. Your entries are retained.{detail}"
        )
        if self.body.text() != text:
            self.body.setText(text)
