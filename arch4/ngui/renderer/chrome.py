"""Render the shared header, footer, and navigation chrome."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from collections.abc import Callable, Mapping

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..front_register import PageTruthSnapshot
from ..interactions import ControlFamily, IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import PageId
from .confirm_button import NGUIConfirmationCoordinator, NGUITwoStageConfirmButton
from .interactions import (
    bind_button,
    bind_dialog_buttons,
    bind_local_button,
    publish_intent,
)
from .style import FONT_FAMILY, FOOTER_FONT_PT, PAGE_TAB_FONT_PT


OPERATIONAL_HEADER_FIELDS = (
    "radio.header_badge",
    "connection.led_level",
    "radio.pband",
    "nr.band",
    "radio.ca_pool",
    "radio.frequency_set",
    "runtime.state",
    "connection.quality_pct",
    "runtime.sequence",
    "runtime.uptime_s",
)

OPERATIONAL_FOOTER_FIELDS = (
    ("radio.pband", "PBAND"),
    ("radio.ca_pool", "CA Pool"),
    ("traffic.dl_mbps", "DL"),
    ("rf.sinr_db", "SINR"),
    ("quality.congestion_pct", "CONGESTION"),
    ("timing.modem_sample_interval_s", "SAMPLE"),
    ("timing.modem_read_ms", "READ"),
)


class OperationalHeader(QWidget):
    """Render the compact header from one immutable page snapshot."""

    def __init__(self) -> None:
        super().__init__()
        declared = {spec.field_id for spec in fields_for_page(PageId.HEADER)}
        if declared != set(OPERATIONAL_HEADER_FIELDS):
            raise ValueError(
                "operational Header field contract mismatch: "
                f"missing={sorted(set(OPERATIONAL_HEADER_FIELDS) - declared)}, "
                f"extra={sorted(declared - set(OPERATIONAL_HEADER_FIELDS))}"
            )
        self.labels: dict[str, QLabel] = {}
        root = QVBoxLayout(self)
        # Keep the vertical spacing from the earlier nested layout without
        # restoring those containers as live owners.
        root.setContentsMargins(8, 4, 8, 12)
        root.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(12)

        badge = QLabel("----")
        badge.setObjectName("radioHeaderBadge")
        badge.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        badge.setAlignment(Qt.AlignCenter)
        badge.setMinimumWidth(80)
        badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.labels["radio.header_badge"] = badge
        row.addWidget(badge)

        leds = QWidget()
        led_row = QHBoxLayout(leds)
        led_row.setContentsMargins(0, 0, 0, 0)
        led_row.setSpacing(4)
        self._quality_leds: list[QLabel] = []
        for _index in range(5):
            led = QLabel()
            led.setProperty("lit", False)
            led.setFixedSize(14, 14)
            self._quality_leds.append(led)
            led_row.addWidget(led)
        leds.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        row.addWidget(leds)
        row.addStretch(1)

        for field_id, title in (
            ("radio.pband", "PBAND"),
            ("nr.band", "NR"),
            ("radio.ca_pool", "CA Pool"),
            ("radio.frequency_set", "Freq Set"),
            ("runtime.state", ""),
            ("connection.quality_pct", "Conn Qual"),
        ):
            label = QLabel(f"{title + ': ' if title else ''}---")
            label.setObjectName("radioHeaderChip")
            label.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            self.labels[field_id] = label
            row.addWidget(label)

        clock = QVBoxLayout()
        clock.setSpacing(2)
        for field_id, title in (("runtime.sequence", "Cycle"), ("runtime.uptime_s", "Uptime")):
            label = QLabel(f"{title}: ---")
            label.setObjectName("radioHeaderClock")
            label.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT))
            self.labels[field_id] = label
            clock.addWidget(label)
        row.addLayout(clock)
        root.addLayout(row)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset((*self.labels, "connection.led_level"))

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, title in (
            ("radio.header_badge", ""),
            ("radio.pband", "PBAND"),
            ("nr.band", "NR"),
            ("radio.ca_pool", "CA Pool"),
            ("radio.frequency_set", "Freq Set"),
            ("runtime.state", ""),
            ("connection.quality_pct", "Conn Qual"),
            ("runtime.sequence", "Cycle"),
            ("runtime.uptime_s", "Uptime"),
        ):
            text = truth.fields[field_id].text
            if field_id == 'nr.band' and text.isdigit():
                text = 'n' + text
            rendered = f"{title}: {text}" if title else text
            label = self.labels[field_id]
            if label.text() != rendered:
                label.setText(rendered)
        led_value = truth.fields["connection.led_level"].value
        level = max(0, min(5, int(led_value or 0)))
        for index, led in enumerate(self._quality_leds):
            lit = index < level
            if bool(led.property("lit")) != lit:
                led.setProperty("lit", lit)
                led.style().unpolish(led)
                led.style().polish(led)


class OperationalFooter(QStatusBar):
    """Render the persistent one-line footer from Footer PageTruth."""

    def __init__(self) -> None:
        super().__init__()
        declared = {spec.field_id for spec in fields_for_page(PageId.FOOTER)}
        expected = {field_id for field_id, _title in OPERATIONAL_FOOTER_FIELDS}
        if declared != expected:
            raise ValueError(
                "operational Footer field contract mismatch: "
                f"missing={sorted(expected - declared)}, "
                f"extra={sorted(declared - expected)}"
            )
        self.labels: dict[str, QLabel] = {}
        line = QWidget()
        row = QHBoxLayout(line)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(8)
        for index, (field_id, title) in enumerate(OPERATIONAL_FOOTER_FIELDS):
            if index:
                separator = QLabel("|")
                separator.setObjectName("operationalFooterSeparator")
                row.addWidget(separator)
            label = QLabel(f"{title} ---")
            label.setObjectName("operationalFooterValue")
            label.setFont(QFont(FONT_FAMILY, FOOTER_FONT_PT, QFont.Weight.Bold))
            self.labels[field_id] = label
            row.addWidget(label)
        row.addStretch(1)
        self.addPermanentWidget(line, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self.labels)

    def render(self, truth: PageTruthSnapshot | None) -> None:
        if truth is None:
            return
        if truth.page is not PageId.FOOTER:
            raise ValueError("OperationalFooter requires Footer page truth")
        for field_id, title in OPERATIONAL_FOOTER_FIELDS:
            rendered = f"{title} {truth.fields[field_id].text}"
            label = self.labels[field_id]
            if label.text() != rendered:
                label.setText(rendered)


def render_utility_button_lifecycle(
    lifecycle: Mapping[str, object],
    buttons: Mapping[IntentKind, QPushButton],
) -> None:
    """Show utility lifecycle state on its visible buttons."""

    for intent, button in buttons.items():
        if intent is IntentKind.EXIT_APPLICATION:
            continue
        raw = lifecycle.get(intent.value, {})
        row = raw if isinstance(raw, Mapping) else {}
        pending = str(row.get("state") or "") in {"queued", "running"}
        if button.isChecked() != pending:
            button.setChecked(pending)
        button.setEnabled(not pending)


class DeviceCommandHeader(QWidget):
    """Render Device commands from accepted page-one state."""

    def __init__(
        self,
        register: NGUIInteractionRegister,
        page_labels: Mapping[PageId, str],
        navigate_local: Callable[[PageId], None],
        confirmation: NGUIConfirmationCoordinator,
    ) -> None:
        super().__init__()
        self.navigation_buttons: dict[PageId, QPushButton] = {}
        self.utility_buttons: dict[IntentKind, QPushButton] = {}
        self._license_dialog: LicenseKeyDialog | None = None
        self.license_state = QLabel("----")
        self.license_state.setObjectName("deviceLicenseState")
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(6)

        for page, label in page_labels.items():
            button = QPushButton(label)
            button.setObjectName(f"nguiDeviceNav_{page.value}")
            button.setCheckable(True)
            button.setProperty("nguiRole", "page_nav")
            button.setProperty("pageSelected", False)
            self.navigation_buttons[page] = button
            bind_button(
                button,
                register,
                IntentKind.NAVIGATE_PAGE,
                lambda page=page: {"page": page.value},
                local_action=lambda page=page: navigate_local(page),
            )
            root.addWidget(button)
        root.addStretch(1)

        # Support Bundle and Scan Network belong beside Sign In because they
        # apply to the current session, not the whole application.
        # DevicePageShell owns their renderer bindings.
        for label, intent in (("BUY LICENSE", IntentKind.OPEN_BUY),):
            button = QPushButton(label)
            button.setObjectName(f"nguiDeviceUtility_{intent.value}")
            button.setCheckable(True)
            button.setProperty("nguiRole", "utility_primary")
            bind_button(button, register, intent)
            self.utility_buttons[intent] = button
            root.addWidget(button)

        license_button = QPushButton("LICENSE")
        license_button.setObjectName("nguiDeviceUtility_update_license")
        license_button.setCheckable(True)
        license_button.setProperty("nguiRole", "utility_outline")
        bind_local_button(
            license_button,
            lambda: self._show_license_editor(register),
            intent_kind=IntentKind.UPDATE_LICENSE,
            family=ControlFamily.LOCAL_DIALOG,
        )
        self.utility_buttons[IntentKind.UPDATE_LICENSE] = license_button
        root.addWidget(license_button)

        root.addWidget(self.license_state)
        for label, intent in (
            ("VERSION", IntentKind.OPEN_INFO),
            ("UPDATES", IntentKind.CHECK_UPDATES),
            ("SHUTDOWN", IntentKind.EXIT_APPLICATION),
        ):
            if intent is IntentKind.EXIT_APPLICATION:
                button = NGUITwoStageConfirmButton(
                    lambda: {},
                    register,
                    intent,
                    label=label,
                    coordinator=confirmation,
                    destructive=True,
                )
            else:
                button = QPushButton(label)
                button.setCheckable(True)
                button.setProperty("nguiRole", "utility_outline")
                bind_button(button, register, intent)
            button.setObjectName(f"nguiDeviceUtility_{intent.value}")
            self.utility_buttons[intent] = button
            root.addWidget(button)

    def set_active_page(self, page: PageId) -> None:
        for nav_page, button in self.navigation_buttons.items():
            checked = nav_page is page
            if button.isChecked() != checked:
                button.setChecked(checked)
            if bool(button.property("pageSelected")) != checked:
                button.setProperty("pageSelected", checked)
                _repolish(button)

    def _show_license_editor(self, register: NGUIInteractionRegister) -> None:
        if self._license_dialog is not None:
            self._license_dialog.raise_()
            self._license_dialog.activateWindow()
            return

        def submit(raw: str, accepted_at: float) -> None:
            publish_intent(
                register,
                IntentKind.UPDATE_LICENSE,
                created_at=accepted_at,
                payload={"license": raw},
                family=ControlFamily.VALUE_SUBMIT,
            )

        dialog = LicenseKeyDialog(self, submit=submit)
        self._license_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._license_dialog = None
            button = self.utility_buttons[IntentKind.UPDATE_LICENSE]
            if button.isChecked():
                button.setChecked(False)

    def render(self, truth: PageTruthSnapshot | None) -> None:
        if truth is None:
            return
        license_field = truth.fields.get("profile.license_status")
        text = license_field.text if license_field is not None else "----"
        if text == "----":
            text = ""
        if self.license_state.text() != text:
            self.license_state.setText(text)
        lifecycle_field = truth.fields.get("utility.lifecycle")
        lifecycle = (
            lifecycle_field.value
            if lifecycle_field is not None
            and isinstance(lifecycle_field.value, Mapping)
            else {}
        )
        render_utility_button_lifecycle(lifecycle, self.utility_buttons)
        dialog = self._license_dialog
        if dialog is not None and dialog.submission_pending:
            license_state = lifecycle.get(IntentKind.UPDATE_LICENSE.value, {})
            state = str(license_state.get("state") or "")
            if state == "failed":
                detail = str(license_state.get("message") or "").strip()
                dialog.show_failure(detail or "License validation failed.")
            elif state == "completed":
                dialog.accept()

class LicenseKeyDialog(QDialog):
    """Keep the NONCE-KEY editor open until its owner accepts the value."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        submit: Callable[[str, float], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._accepted_at = 0.0
        self._submit = submit
        self._submission_pending = False
        self.setWindowTitle("54GUC License")
        self.setModal(True)
        layout = QVBoxLayout(self)
        prompt = QLabel(
            "Paste NONCE-KEY or the full license block generated for this modem"
        )
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("nguiLicenseKeyEditor")
        self.editor.setPlaceholderText("NONCE-KEY or generator block")
        self.editor.setMaximumHeight(76)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.validate_button = buttons.button(QDialogButtonBox.Ok)
        self.validate_button.setObjectName("nguiLicenseKeyValidateActivate")
        self.validate_button.setText("Validate && Activate")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        cancel_button.setObjectName("nguiLicenseKeyCancel")
        bind_dialog_buttons(buttons, self._request_validation, self.reject)
        layout.addWidget(prompt)
        layout.addWidget(self.editor)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.resize(560, 180)

    @property
    def license_key(self) -> str:
        return self.editor.toPlainText().strip()

    @property
    def accepted_at(self) -> float:
        return self._accepted_at

    @property
    def submission_pending(self) -> bool:
        return self._submission_pending

    def _request_validation(self) -> None:
        import time

        raw = self.license_key
        if not raw:
            self.show_failure(
                "Enter the license text generated for this modem."
            )
            return
        self._accepted_at = time.monotonic()
        self._submission_pending = True
        self.validate_button.setEnabled(False)
        self.status.setText("Validating against the connected modem...")
        if self._submit is not None:
            self._submit(raw, self._accepted_at)

    def show_failure(self, message: str) -> None:
        self._submission_pending = False
        self.validate_button.setEnabled(True)
        self.status.setText(str(message or "License validation failed."))

    def accept(self) -> None:
        self._submission_pending = False
        super().accept()


class OperationalNavigation(QWidget):
    """Navigation row below the shared header on operational pages."""

    def __init__(
        self,
        register: NGUIInteractionRegister,
        page_labels: Mapping[PageId, str],
        navigate_local: Callable[[PageId], None],
    ) -> None:
        super().__init__()
        self.navigation_buttons: dict[PageId, QPushButton] = {}
        self._action_widget: QWidget | None = None
        self._root = QHBoxLayout(self)
        self._root.setContentsMargins(8, 0, 8, 0)
        self._root.setSpacing(8)
        for page, label in page_labels.items():
            button = QPushButton(label)
            button.setObjectName(f"nguiOperationalNav_{page.value}")
            button.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
            button.setMinimumHeight(30)
            button.setCheckable(True)
            button.setProperty("nguiRole", "page_nav")
            button.setProperty("pageSelected", False)
            self.navigation_buttons[page] = button
            bind_button(
                button,
                register,
                IntentKind.NAVIGATE_PAGE,
                lambda page=page: {"page": page.value},
                local_action=lambda page=page: navigate_local(page),
            )
            self._root.addWidget(button)
        self._root.addStretch(1)
        self._action_host = QWidget()
        self._action_host.setObjectName("ngui_navigation_action_host")
        self._action_layout = QHBoxLayout(self._action_host)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(6)
        self._action_host.setVisible(False)
        self._root.addWidget(self._action_host)

    def set_active_page(self, page: PageId) -> None:
        for nav_page, button in self.navigation_buttons.items():
            checked = nav_page is page
            if button.isChecked() != checked:
                button.setChecked(checked)
            if bool(button.property("pageSelected")) != checked:
                button.setProperty("pageSelected", checked)
                _repolish(button)

    @property
    def action_widget(self) -> QWidget | None:
        return self._action_widget

    def set_action_widget(self, widget: QWidget | None) -> None:
        if self._action_widget is widget:
            return
        while self._action_layout.count():
            item = self._action_layout.takeAt(0)
            existing = item.widget()
            if existing is not None:
                existing.hide()
        self._action_widget = widget
        if widget is None:
            self._action_host.setVisible(False)
            return
        self._action_layout.addWidget(widget)
        widget.show()
        self._action_host.setVisible(True)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "DeviceCommandHeader",
    "OPERATIONAL_HEADER_FIELDS",
    "OperationalHeader",
    "OperationalNavigation",
    "render_utility_button_lifecycle",
]
