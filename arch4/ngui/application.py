"""GUI application entry point for NGUI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from arch4.core.runtime_defaults import CoreRuntimeDefaultsStore
from arch4.application_lease import (
    application_lease_identity_from_environment,
)
from arch4.customer_contracts.settings import CoreRuntimeDefaults
from arch4.interfaces import (
    ARCH_NGUI_PAGE_TRUTH_FILENAME,
    LUC_BUILD_DATE,
    LUC_VERSION,
)
from arch4.modem_live.profile_catalog import modem_profile_selection
from arch4.foundation.operational_service_contracts import (
    DiscoveryRequest,
    LicensePurchaseRequest,
    LicenseSaveRequest,
    OperatorScopeResult,
    ReportRequest,
    SupportRequest,
    TrafficPlanSaveRequest,
)
from arch4.foundation.operational_root import FoundationOperationalRoot
from arch4.foundation.runtime_artifacts import (
    publish_shutdown_receipt_artifact,
)
from arch4.foundation.external_settings_import import (
    import_foundation_external_settings_once,
)
from arch4.foundation.active_job_admission import (
    ActiveJobAdmissionAuthority,
    ngui_utility_job_policies,
)
from arch4.product_intelligence_runtime import (
    build_intelligence_site_report_history,
)
from arch4.runtime_paths import CGF_STATE_DIRNAME

from .chart_model import NGUIChartProcessor
from .heavy_work import HeavyWorkKind, NGUIHeavyWorkProcessor
from .debug_surface import (
    NGUIPageTruthGenerationManager,
    NGUIPageTruthLogWriter,
)
from .external_actions import (
    ExternalActionKind,
    NGUIExternalActionProcessor,
)
from .orchestrator import NGUIOrchestrator, NGUIOutputPorts
from .profile_authority import ExactProfileAuthority
from .settings_store import (
    NGUISettingsStore, import_r144_run_hints_once, r144_run_hint_store,
)
from .runtime_service import RUNTIME_WORKER_SHUTDOWN_BUDGET_S
from .safe_start_advisory import SAFE_START_FILENAME
from .renderer.interactions import bind_timer
from .renderer.window import NGUI_WINDOW_TITLE, NGUIWindow
from .utility_lifecycle import (
    NGUIUtilityTerminal,
    UtilityKind,
    UtilityTerminalState,
)
from .voice_lifecycle import NGUISINRVoiceRequest


log = logging.getLogger("arch4.ngui.application")


_TRUE_ENV = {"1", "true", "yes", "on"}
PUBLIC_VERSION_OPEN_SOURCE_HEADER = "Open Source Licenses"
_SOURCE_UI_DRIVER_FLAG = "ARCH_NGUI_SOURCE_UI_DRIVER"
_SOURCE_UI_DRIVER_KEYS = {
    "model": "ARCH_NGUI_SOURCE_UI_MODEL",
    "address": "ARCH_NGUI_SOURCE_UI_ADDRESS",
    "username": "ARCH_NGUI_SOURCE_UI_USERNAME",
    "password": "ARCH_NGUI_SOURCE_UI_PASSWORD",
    "license": "ARCH_NGUI_SOURCE_UI_LICENSE",
    "mode": "ARCH_NGUI_SOURCE_UI_MODE",
}


class _VoiceBridge(QObject):
    """Own GUI speech calls through the Windows SAPI boundary."""

    ENGINE = "sapi"
    request_received = Signal(object)

    def __init__(self, orchestrator: NGUIOrchestrator) -> None:
        application = QApplication.instance()
        if application is None:
            raise RuntimeError("SINR voice owner requires QApplication")
        super().__init__(application)
        if QThread.currentThread() is not application.thread():
            raise RuntimeError("SINR voice owner must be created on the GUI thread")

        self._orchestrator = orchestrator
        self._active = False
        self._active_sequence = 0
        self._pending_request: NGUISINRVoiceRequest | None = None
        self._speech = None
        self._initialization_error = ""
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech
        except (ImportError, OSError) as exc:
            self._initialization_error = (
                "SINR speech boundary is unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            available = tuple(str(value) for value in QTextToSpeech.availableEngines())
            if self.ENGINE not in available:
                self._initialization_error = (
                    f"required Windows speech engine {self.ENGINE!r} is unavailable"
                )
            else:
                self._speech = QTextToSpeech(self.ENGINE, self)
                self._speech.stateChanged.connect(self._on_state_changed)
                self._speech.errorOccurred.connect(self._on_error)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        bind_timer(self._timer, self._speak)
        self.request_received.connect(self._apply_request)

    def _apply_request(self, value: object) -> None:
        if not isinstance(value, NGUISINRVoiceRequest):
            raise TypeError("SINR voice bridge requires NGUISINRVoiceRequest")
        if value.enabled:
            if self._speech is None:
                self._acknowledge_failed_start(
                    value,
                    self._initialization_error or "SINR speech engine is unavailable",
                )
                return
            self._pending_request = value
            self._activate_pending_if_ready()
            return

        self._pending_request = None
        self._timer.stop()
        error = ""
        try:
            if self._speech is not None:
                self._speech.stop()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self._active = False
        self._active_sequence = 0
        self._orchestrator.acknowledge_sinr_voice(
            sequence=value.sequence,
            active=False,
            error=error,
        )

    def _activate_pending_if_ready(self) -> None:
        request = self._pending_request
        speech = self._speech
        if request is None or speech is None:
            return
        state = speech.state()
        if state == speech.State.Error:
            self._acknowledge_failed_start(
                request,
                speech.errorString() or "SINR speech engine entered error state",
            )
            return
        if state != speech.State.Ready:
            return
        self._pending_request = None
        self._active = True
        self._active_sequence = request.sequence
        self._timer.start()
        self._orchestrator.acknowledge_sinr_voice(
            sequence=request.sequence,
            active=True,
        )

    def _acknowledge_failed_start(
        self,
        request: NGUISINRVoiceRequest,
        error: str,
    ) -> None:
        self._pending_request = None
        self._timer.stop()
        self._active = False
        self._active_sequence = 0
        detail = str(error or "SINR speech engine failed")
        log.warning("SINR voice is unavailable: %s", detail)
        self._orchestrator.acknowledge_sinr_voice(
            sequence=request.sequence,
            active=False,
            error=detail,
        )

    def _on_state_changed(self, state: object) -> None:
        speech = self._speech
        if speech is None:
            return
        if state == speech.State.Ready:
            self._activate_pending_if_ready()
        elif state == speech.State.Error:
            self._handle_engine_failure(
                speech.errorString() or "SINR speech engine entered error state"
            )

    def _on_error(self, _reason: object, error_string: str) -> None:
        self._handle_engine_failure(
            str(error_string or "SINR speech engine reported an error")
        )

    def _handle_engine_failure(self, error: str) -> None:
        if self._pending_request is not None:
            self._acknowledge_failed_start(self._pending_request, error)
            return
        if not self._active:
            return
        sequence = self._active_sequence
        self._timer.stop()
        self._active = False
        self._active_sequence = 0
        try:
            if self._speech is not None:
                self._speech.stop()
        except Exception:
            # The exact engine failure is already authoritative. A second
            # failure while stopping must not terminate the GUI.
            pass
        self._orchestrator.report_sinr_voice_failure(
            sequence=sequence,
            error=str(error or "SINR speech engine failed"),
        )

    def _speak(self) -> None:
        speech = self._speech
        if not self._active or speech is None:
            return
        if speech.state() == speech.State.Error:
            self._handle_engine_failure(
                speech.errorString() or "SINR speech engine entered error state"
            )
            return
        if speech.state() != speech.State.Ready:
            return
        field = self._orchestrator.front_register.snapshot().fields.get("rf.sinr_db")
        if field is None or field.value is None:
            return
        try:
            speech.say(f"{float(field.value):.1f}")
        except Exception as exc:
            self._handle_engine_failure(f"{type(exc).__name__}: {exc}")


class _ApplicationExitBridge(QObject):
    """Deliver an accepted shutdown to the application window."""

    exit_requested = Signal()

    def __init__(self, window: NGUIWindow) -> None:
        super().__init__(window)
        self.exit_requested.connect(window.request_application_exit)


class _MessageBridge(QObject):
    """Show completed action results on the GUI thread."""

    show_requested = Signal(str, str, bool)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._parent = parent
        self._dialogs: list[QMessageBox] = []
        self.show_requested.connect(self._show)

    def _show(self, title: str, message: str, warning: bool) -> None:
        dialog = QMessageBox(self._parent)
        dialog.setWindowTitle(title)
        dialog.setIcon(
            QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information
        )
        dialog.setText(message)
        dialog.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.button(QMessageBox.StandardButton.Ok).setObjectName(
            "nguiTerminalMessageOk"
        )
        self._dialogs.append(dialog)

        def release(_result: int) -> None:
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(release)
        dialog.show()


class _InfoBridge(QObject):
    """Show the public version in a non-modal renderer dialog."""

    show_requested = Signal(str)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._parent = parent
        self._dialogs: list[QMessageBox] = []
        self.show_requested.connect(self._show)

    def _show(self, inventory_text: str) -> None:
        dialog = QDialog(self._parent)
        dialog.setWindowTitle(f"Version — {LUC_VERSION}")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(6)

        title = QLabel("5G/4G Ultimate Controller")
        title.setObjectName("nguiPublicVersionProduct")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)

        version = QLabel(LUC_VERSION)
        version.setObjectName("nguiPublicVersionLabel")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setTextInteractionFlags(Qt.TextSelectableByMouse)

        build = QLabel(f"Build: {LUC_BUILD_DATE or 'unknown'}")
        build.setObjectName("nguiPublicVersionBuild")
        build.setAlignment(Qt.AlignmentFlag.AlignCenter)
        build.setTextInteractionFlags(Qt.TextSelectableByMouse)

        header = QLabel(PUBLIC_VERSION_OPEN_SOURCE_HEADER)
        header.setObjectName("nguiPublicVersionOpenSourceHeader")
        header.setTextInteractionFlags(Qt.TextSelectableByMouse)

        rows = [
            line.strip()
            for line in str(inventory_text or "").splitlines()
            if line.strip()
        ]
        inventory = QLabel("\n".join(f"  • {line}" for line in rows) or "  • Not available")
        inventory.setObjectName("nguiPublicVersionOpenSourceList")
        inventory.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setObjectName(
            "nguiPublicVersionClose"
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(title)
        layout.addWidget(version)
        layout.addWidget(build)
        layout.addSpacing(16)
        layout.addWidget(header)
        layout.addWidget(inventory)
        layout.addSpacing(12)
        layout.addWidget(buttons)
        self._dialogs.append(dialog)

        def release(_result: int) -> None:
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(release)
        dialog.show()


class _BuyLicenseBridge(QObject):
    """Show the modem token without exposing the raw modem identity."""

    show_requested = Signal(str, str)

    def __init__(self, parent, messages: _MessageBridge) -> None:
        super().__init__(parent)
        self._parent = parent
        self._messages = messages
        self._dialogs: list[QDialog] = []
        self.show_requested.connect(self._show)

    def _show(self, token: str, identity_kind: str) -> None:
        dialog = QDialog(self._parent)
        dialog.setWindowTitle("Request License")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        title = QLabel("Request 54GUC License")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel(
            "This token is bound internally to the connected modem's "
            f"{str(identity_kind or 'device').upper()}. The raw identity is not shown."
        )
        hint.setWordWrap(True)
        token_edit = QLineEdit(token)
        token_edit.setReadOnly(True)
        token_edit.setObjectName("nguiBuyLicenseToken")
        email = QLineEdit()
        email.setPlaceholderText("your@email.com")
        email.setObjectName("nguiBuyLicenseEmail")
        info = QLabel(
            "Copy this token and send it to the beta support contact supplied "
            "with your invitation. The license is made for this modem."
        )
        info.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        proceed_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        proceed_button.setObjectName("nguiBuyLicenseProceed")
        proceed_button.setText("Request License")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName(
            "nguiBuyLicenseCancel"
        )

        def proceed() -> None:
            address = email.text().strip()
            if not address or "@" not in address:
                self._messages.show_requested.emit(
                    "Email Required",
                    "Please enter a valid email address.",
                    True,
                )
                return
            self._messages.show_requested.emit(
                "License Request",
                "No payment is processed by this application. Send the token "
                "and email address to the beta support contact supplied with "
                "your invitation.",
                False,
            )
            dialog.accept()

        buttons.accepted.connect(proceed)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(QLabel("Token:"))
        layout.addWidget(token_edit)
        layout.addWidget(QLabel("Email:"))
        layout.addWidget(email)
        layout.addWidget(info)
        layout.addWidget(buttons)
        self._dialogs.append(dialog)

        def release(_result: int) -> None:
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(release)
        dialog.show()


def _format_network_scan_summary(result: Mapping[str, Any]) -> str:
    error = str(result.get("error") or "").strip()
    if error:
        return f"Network scan failed:\n{error}"
    from ipaddress import IPv4Address
    hosts = set()
    for entry in result.get("entries") or ():
        if not isinstance(entry, Mapping):
            continue
        try:
            hosts.add(IPv4Address(str(entry.get("host") or "")))
        except ValueError:
            continue
    seed = str(result.get("seed_host") or "")
    prefixes = {str(host).rsplit(".", 1)[0] for host in hosts}
    prefix = next(iter(prefixes)) if len(prefixes) == 1 else None
    lines = [f"Detected addresses: {prefix}.*" if prefix else "Detected addresses:"]
    for host in sorted(hosts):
        address = str(host)
        shown = "." + address.rsplit(".", 1)[1] if prefix else address
        lines.append(f"{shown}  {'Router' if address == seed else 'In use'}")
    if not hosts:
        lines.append("No responding addresses detected.")
    return "\n".join(lines)


class _ScanBridge(QObject):
    """Present the existing scan's responding addresses on Device."""

    show_requested = Signal(object)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._parent = parent
        self.show_requested.connect(self._show)

    def _show(self, result: object) -> None:
        if isinstance(result, Mapping):
            self._parent.show_network_scan_result(
                _format_network_scan_summary(result)
            )


class _RouterSerialBridge(QObject):
    """Collect a missing serial while the authenticated router session stays open."""

    show_requested = Signal()

    def __init__(
        self,
        parent,
        *,
        submit: Callable[[str], None],
        cancel: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._submit = submit
        self._cancel = cancel
        self._dialog: QDialog | None = None
        self.show_requested.connect(self._show)

    def _show(self) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        dialog = QDialog(self._parent)
        dialog.setWindowTitle("Router serial required")
        dialog.setModal(True)
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "Router login succeeded, but the router did not report a serial "
            "number or IMEI. Enter the serial printed on the router. The "
            "current login will continue after you submit it."
        )
        note.setWordWrap(True)
        serial = QLineEdit()
        serial.setObjectName("nguiRouterSerialInput")
        serial.setPlaceholderText("Router serial number")
        error = QLabel("")
        error.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        submit_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        submit_button.setObjectName("nguiRouterSerialSubmit")
        submit_button.setText("SUBMIT")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName(
            "nguiRouterSerialCancel"
        )
        accepted = {"value": False}

        def submit_serial() -> None:
            try:
                self._submit(serial.text())
            except Exception as exc:
                error.setText(str(exc))
                return
            accepted["value"] = True
            dialog.accept()

        def cancel_serial() -> None:
            if accepted["value"]:
                return
            try:
                self._cancel()
            except Exception as exc:
                log.error("Router serial prompt cancellation failed: %s", exc)

        buttons.accepted.connect(submit_serial)
        buttons.rejected.connect(dialog.reject)
        dialog.rejected.connect(cancel_serial)
        dialog.finished.connect(lambda _result: setattr(self, "_dialog", None))
        layout.addWidget(note)
        layout.addWidget(serial)
        layout.addWidget(error)
        layout.addWidget(buttons)
        self._dialog = dialog
        dialog.show()
        serial.setFocus()


class _UtilityResultBridge(QObject):
    """Dispatch completed utility results on the GUI thread."""

    show_requested = Signal(object)

    def __init__(
        self,
        *,
        messages: _MessageBridge,
        info: _InfoBridge,
        buy: _BuyLicenseBridge,
        scan: _ScanBridge,
    ) -> None:
        super().__init__(messages)
        self._messages = messages
        self._info = info
        self._buy = buy
        self._scan = scan
        self.show_requested.connect(self._show)

    def _show(self, terminal: object) -> None:
        if not isinstance(terminal, NGUIUtilityTerminal):
            raise TypeError("typed_utility_terminal_required")
        failed = terminal.state in {
            UtilityTerminalState.FAILED,
            UtilityTerminalState.CANCELLED,
            UtilityTerminalState.REJECTED,
        }
        if terminal.kind is UtilityKind.LICENSE:
            # The modal NONCE-KEY editor consumes typed lifecycle state so an
            # invalid key remains editable and a valid refresh closes it.
            return
        if failed:
            title, prefix = {
                UtilityKind.SUPPORT_BUNDLE: (
                    "Support Bundle Error",
                    "Could not create support bundle",
                ),
                UtilityKind.SCAN_NETWORK: (
                    "Scan Network Error",
                    "Network scan failed",
                ),
                UtilityKind.BUY_LICENSE: (
                    "Modem Identity Unavailable",
                    "Connect to the modem first and wait for a current serial or IMEI",
                ),
                UtilityKind.VERSION: ("Version Error", "Could not show version information"),
                UtilityKind.UPDATES: ("Update Check Failed", "Could not check for updates"),
                UtilityKind.SITE_REPORT: ("Site Report Error", "Report generation failed"),
                UtilityKind.ROUTER_CHECK: ("Router Check Failed", "Router check could not finish"),
            }[terminal.kind]
            detail = str(terminal.message or "").strip()
            self._messages.show_requested.emit(
                title,
                prefix + (f":\n{detail}" if detail else "."),
                True,
            )
            return

        result = terminal.result
        if terminal.kind is UtilityKind.SUPPORT_BUNDLE:
            path = str(result.get("path") or "")
            self._messages.show_requested.emit(
                "Support Bundle Ready",
                "Support bundle created.\n\n"
                f"{path}\n\n"
                "Before sending it, open BUNDLE_FILE_LIST.txt and "
                "PRIVACY_NOTE.txt inside the zip.",
                False,
            )
        elif terminal.kind is UtilityKind.SITE_REPORT:
            path = str(result.get("path") or "")
            message = (
                f"Site report created and opened.\n\n{path}"
                if path
                else "The report service completed without returning an artifact."
            )
            self._messages.show_requested.emit("Site Report", message, False)
        elif terminal.kind is UtilityKind.SCAN_NETWORK:
            self._scan.show_requested.emit(dict(result))
        elif terminal.kind is UtilityKind.BUY_LICENSE:
            self._buy.show_requested.emit(
                str(result.get("token") or ""),
                str(result.get("identity_kind") or "device"),
            )
        elif terminal.kind is UtilityKind.VERSION:
            self._info.show_requested.emit(
                str(result.get("open_source_inventory") or "")
            )
        elif terminal.kind is UtilityKind.UPDATES:
            current = str(result.get("current") or LUC_VERSION)
            status = str(result.get("status") or "")
            if status == "unpublished":
                detail = str(result.get("message") or "").strip()
                self._messages.show_requested.emit(
                    "Update Channel Not Published",
                    (detail or "No public update channel is available.")
                    + f"\n\nCurrent version: {current}\n"
                    "No update comparison was made.",
                    False,
                )
            elif not bool(result.get("available", False)):
                self._messages.show_requested.emit(
                    "Check for Updates",
                    f"You are running the latest version ({current}).",
                    False,
                )
            else:
                latest = str(result.get("latest") or "unknown")
                url = str(result.get("url") or "")
                self._messages.show_requested.emit(
                    "Update Available",
                    f"A newer version is available: {latest}\n\n"
                    f"Current: {current}\nLatest: {latest}\n\n"
                    f"Download URL:\n{url}",
                    False,
                )


def _source_ui_driver_payload_from_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    env = environ if environ is not None else os.environ
    enabled = str(env.get(_SOURCE_UI_DRIVER_FLAG, "")).strip().lower() in _TRUE_ENV
    if not enabled:
        return None
    profile_id = str(env.get(_SOURCE_UI_DRIVER_KEYS["model"], "") or "").strip()
    address = str(env.get(_SOURCE_UI_DRIVER_KEYS["address"], "") or "").strip()
    missing = [
        key
        for key, value in (("model", profile_id), ("address", address))
        if not value
    ]
    if missing:
        names = ", ".join(_SOURCE_UI_DRIVER_KEYS[key] for key in missing)
        raise RuntimeError(f"NGUI source UI driver missing required environment: {names}")
    mode = str(env.get(_SOURCE_UI_DRIVER_KEYS["mode"], "live") or "live").strip().lower()
    if mode not in {"live", "observe"}:
        raise RuntimeError("NGUI source UI driver mode must be 'live' or 'observe'")
    contract = modem_profile_selection(profile_id)
    if contract is None:
        raise RuntimeError(
            f"NGUI source UI driver exact profile is unknown: {profile_id}"
        )
    return {
        "model": contract.label,
        "address": address,
        "username": str(
            env.get(_SOURCE_UI_DRIVER_KEYS["username"], "admin") or "admin"
        ).strip()
        or "admin",
        "password": str(env.get(_SOURCE_UI_DRIVER_KEYS["password"], "") or ""),
        "license": str(env.get(_SOURCE_UI_DRIVER_KEYS["license"], "") or ""),
        "mode": mode,
    }


def _schedule_source_run_ui_driver(window: NGUIWindow) -> None:
    payload = _source_ui_driver_payload_from_env()
    if payload is None:
        return
    attempts_remaining = 200

    def populate_and_click() -> None:
        nonlocal attempts_remaining
        controls = window.device_controls
        selected = controls.model.findData(payload["model"])
        if selected < 1:
            for index in range(1, controls.manufacturer.count()):
                controls.manufacturer.setCurrentIndex(index)
                selected = controls.model.findData(payload["model"])
                if selected >= 1:
                    break
        if selected < 1:
            attempts_remaining -= 1
            if attempts_remaining <= 0:
                log.error(
                    "NGUI source UI driver could not select profile %s",
                    payload["model"],
                )
                return
            QTimer.singleShot(50, populate_and_click)
            return
        controls.model.setCurrentIndex(selected)
        controls.address.setText(payload["address"])
        controls.username.setText(payload["username"])
        controls.password.setText(payload["password"])
        controls.set_source_driver_license(payload["license"])
        mode_index = controls.start_mode.findData(payload["mode"])
        if mode_index < 0:
            log.error("NGUI source UI driver could not select mode %s", payload["mode"])
            return
        controls.start_mode.setCurrentIndex(mode_index)
        controls.connect.click()

    QTimer.singleShot(0, populate_and_click)


def _resolve_launch_paths(
    launch_context: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    env = environ if environ is not None else os.environ
    app_root = Path(
        getattr(launch_context, "app_root", env.get("ARCH_APP_ROOT", Path.cwd()))
    ).resolve()
    runtime_dir = Path(
        getattr(
            launch_context,
            "runtime_dir",
            env.get(
                "ARCH_RUNTIME_OUTPUT_DIR", app_root / "portable_runtime" / "guc_output"
            ),
        )
    ).resolve()
    memory_dir = Path(
        getattr(
            launch_context,
            "memory_dir",
            env.get(
                "ARCH_RUNTIME_MEMORY_DIR", app_root / "portable_runtime" / "guc_memory"
            ),
        )
    ).resolve()
    return app_root, runtime_dir, memory_dir


def _prepare_launch_paths(launch_context: Any | None = None) -> tuple[Path, Path, Path]:
    app_root, runtime_dir, memory_dir = _resolve_launch_paths(launch_context)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ARCH_APP_ROOT"] = str(app_root)
    os.environ["ARCH_RUNTIME_OUTPUT_DIR"] = str(runtime_dir)
    os.environ["ARCH_RUNTIME_MEMORY_DIR"] = str(memory_dir)
    return app_root, runtime_dir, memory_dir


def launch(
    *,
    launch_context: Any | None = None,
    config: Any | None = None,
    observe_only: bool = False,
    windows_mc888: bool = False,
) -> int:
    """Launch NGUI without selecting a modem before Connect."""
    del config, observe_only, windows_mc888
    app_root, runtime_dir, memory_dir = _prepare_launch_paths(launch_context)
    try:
        import_foundation_external_settings_once(
            source_root=app_root,
            storage_root=runtime_dir,
        )
    except Exception as exc:
        # Core defaults keep monitoring available. Report rejected external
        # settings as a launch fault.
        log.error("External settings import rejected: %s", exc)
    ngui_settings_store = NGUISettingsStore(runtime_dir)
    remembered_settings_store = NGUISettingsStore(memory_dir.parent)
    try:
        old_run_store = r144_run_hint_store(app_root)
        if old_run_store is not None:
            import_r144_run_hints_once(old_run_store, remembered_settings_store)
    except Exception as exc:
        log.error("R14.4 connection hint import rejected: %s", exc)
    from .network_preset_store import NGUINetworkPresetStore
    remembered_network_store = NGUINetworkPresetStore(memory_dir.parent)
    try:
        core_runtime_defaults = CoreRuntimeDefaultsStore(runtime_dir).load()
    except Exception as exc:
        log.error("Core runtime defaults rejected: %s", exc)
        core_runtime_defaults = CoreRuntimeDefaults()
    from arch4.support_bundle_context import SupportBundleContext

    support_bundle_context = SupportBundleContext.from_runtime_paths(
        app_root=app_root,
        runtime_dir=runtime_dir,
        memory_dir=memory_dir,
        profile=str(getattr(launch_context, "profile", "") or "source_run"),
        source=(
            "windows_launch_context"
            if launch_context is not None
            else "ngui_source_runtime"
        ),
    )
    def site_report_history_projector(hours: float):
        return build_intelligence_site_report_history(
            state_dir=memory_dir / CGF_STATE_DIRNAME,
            hours=hours,
        )

    operational_root = FoundationOperationalRoot(
        app_root=app_root,
        storage_root=runtime_dir,
        support_context=support_bundle_context,
        current_version=LUC_VERSION,
        site_report_history_projector=site_report_history_projector,
    )
    services = operational_root.ports
    from arch4.ngui.supervision import build_ngui_supervision

    report_ngui_error = lambda message: log.error("NGUI action failed: %s", message)
    supervision = build_ngui_supervision(report_ngui_error, runtime_dir)
    holder: dict[str, Any] = {}
    holder["foundation_operational_root"] = operational_root
    charts = NGUIChartProcessor(
        worker_health=supervision.worker_health,
        fault_supervisor=supervision.fault_supervisor,
    )
    application_lease = application_lease_identity_from_environment()
    if application_lease is None:
        raise RuntimeError("ngui_application_lease_identity_required")
    def page_truth_writer_factory() -> NGUIPageTruthLogWriter:
        return NGUIPageTruthLogWriter(
            runtime_dir / ARCH_NGUI_PAGE_TRUTH_FILENAME,
            report_error=lambda message: log.error("%s", message),
            worker_health=supervision.worker_health,
            fault_supervisor=supervision.fault_supervisor,
            safe_start_advisory_path=runtime_dir / SAFE_START_FILENAME,
            application_lease=application_lease.to_payload(),
            minimum_interval_s=30.0,
        )

    page_truth = NGUIPageTruthGenerationManager(
        application_lease=application_lease,
        writer_factory=page_truth_writer_factory,
    )

    def site_report(request, progress):
        progress(10.0, request.created_at, "Starting report")
        result = services.report.generate(
            ReportRequest(
                window=str(request.payload["window"]),
                open_browser=True,
            )
        )
        return {"path": result.path}

    def support_bundle(request, progress):
        progress(10.0, time.monotonic(), "Collecting support data")
        result = services.support.create(
            SupportRequest(
                probe_mode=bool(request.payload.get("probe_mode", False)),
                runtime_mode=str(request.payload.get("runtime_mode") or ""),
                profile=str(request.payload.get("profile") or ""),
                modem_identity=dict(request.payload.get("modem_identity") or {}),
            ),
            progress=lambda pct, message: progress(pct, time.monotonic(), message),
            prepare_history=lambda cutoff, path: orchestrator.runtime.prepare_support_history(cutoff, path),
        )
        return {"path": result.path}

    def scout_scan(request, progress):
        progress(10.0, request.created_at, "Running read-only network scan")
        result = services.discovery.scan(
            DiscoveryRequest(
                seed_host=str(request.payload.get("seed_host") or "")
            )
        )
        value = {
            "mode": result.mode,
            "seed_host": result.seed_host,
            "scanned_host_count": result.scanned_host_count,
            "entries": [
                {
                    "host": entry.host,
                    "category": entry.category,
                    "detected_backend": entry.detected_backend,
                    "backend_family": entry.backend_family,
                    "confidence": entry.confidence,
                    "open_ports": list(entry.open_ports),
                    "note": entry.note,
                }
                for entry in result.entries
            ],
        }
        return value

    utility_admission = ActiveJobAdmissionAuthority(
        ngui_utility_job_policies(
            heavy_kinds=tuple(kind.value for kind in HeavyWorkKind),
            external_kinds=tuple(kind.value for kind in ExternalActionKind),
        )
    )

    heavy = NGUIHeavyWorkProcessor(
        {
            HeavyWorkKind.SITE_REPORT: site_report,
            HeavyWorkKind.SUPPORT_BUNDLE: support_bundle,
            HeavyWorkKind.SCAN: scout_scan,
        },
        admission=utility_admission,
        worker_health=supervision.worker_health,
        fault_supervisor=supervision.fault_supervisor,
    )

    def open_speed_test(_request):
        return {"opened": services.external.open_speed_test()}

    def open_buy(request):
        result = services.license.purchase_token(
            LicensePurchaseRequest(
                identity=str(request.payload.get("identity") or ""),
                identity_kind=str(
                    request.payload.get("identity_kind") or "device"
                ),
            )
        )
        return {
            "token_ready": True,
            "token": result.token,
            "identity_kind": result.identity_kind,
        }

    def open_info(_request):
        result = services.external.open_source_inventory()
        return {
            "shown": True,
            "open_source_inventory": result.rendered_text,
        }

    def check_updates(_request):
        result = services.external.check_updates()
        return {
            "status": result.status,
            "available": result.available,
            "current": result.current,
            "latest": result.latest,
            "url": result.url,
            "message": result.message,
        }

    def save_plan(request):
        payload = request.payload
        result = services.operator_config.save_traffic_plan(
            TrafficPlanSaveRequest(
                limit_gb=float(payload["limit_gb"]),
                cycle_day=int(payload["cycle_day"]),
                scope=OperatorScopeResult(
                    subscription_key=str(payload.get("subscription_key") or ""),
                    backend=str(payload.get("backend") or ""),
                    provider_name=str(payload.get("provider_name") or ""),
                    provider_plmn=str(payload.get("provider_plmn") or ""),
                    profile_id=str(payload.get("profile_id") or ""),
                    scope_id=str(payload.get("scope_id") or ""),
                ),
            )
        )
        return result.to_payload()

    def save_license(request):
        payload = request.payload
        result = services.license.save(
            LicenseSaveRequest(
                combined_license=str(payload.get("license") or ""),
                identity=str(payload.get("identity") or ""),
                identity_kind=str(payload.get("identity_kind") or "device"),
            )
        )
        return {
            "saved": True,
            "identity_kind": result.identity_kind,
            "tier": result.tier,
        }

    def router_check_report(request):
        import os
        from arch4.foundation.read_diagnostic_report import save_router_check_report
        path = save_router_check_report(runtime_dir, request.payload["envelope"])
        os.startfile(str(path))
        return {"path": str(path)}

    def apn_profiles(request):
        return services.operator_config.apn_profiles(request.payload["operation"], request.payload["choice"])

    external_actions = NGUIExternalActionProcessor(
        {
            ExternalActionKind.OPEN_SPEED_TEST: open_speed_test,
            ExternalActionKind.OPEN_BUY: open_buy,
            ExternalActionKind.OPEN_INFO: open_info,
            ExternalActionKind.CHECK_UPDATES: check_updates,
            ExternalActionKind.SAVE_TRAFFIC_PLAN: save_plan,
            ExternalActionKind.SAVE_LICENSE: save_license,
            ExternalActionKind.APN_PROFILES: apn_profiles,
            ExternalActionKind.ROUTER_CHECK_REPORT: router_check_report,
        },
        admission=utility_admission,
        worker_health=supervision.worker_health,
        fault_supervisor=supervision.fault_supervisor,
    )

    profile_authority = ExactProfileAuthority(
        storage_root=runtime_dir,
        settings_store=ngui_settings_store,
        core_runtime_defaults=core_runtime_defaults,
        remembered_settings_store=remembered_settings_store,
        remembered_network_store=remembered_network_store,
    )
    orchestrator = NGUIOrchestrator(
        application_lease=application_lease,
        app_root=app_root,
        storage_root=runtime_dir,
        memory_dir=memory_dir,
        chart_processor=charts,
        heavy_work=heavy,
        external_actions=external_actions,
        worker_health=supervision.worker_health,
        fault_supervisor=supervision.fault_supervisor,
        profile_authority=profile_authority,
        license_service=services.license,
        operator_config_service=services.operator_config,
        dev_full_access=str(
            os.environ.get("ARCH_NGUI_DEV_FULL_ACCESS", "")
        ).strip().lower()
        in _TRUE_ENV,
        ports=NGUIOutputPorts(
            publish_page_truth=lambda snapshot, worker_generation: (
                page_truth.publish(
                    snapshot,
                    worker_generation=worker_generation,
                )
            ),
            rotate_page_truth_generation=(
                lambda worker_generation, transaction_digest, timeout_s: (
                    page_truth.rotate_generation(
                        worker_generation=worker_generation,
                        reset_transaction_digest=transaction_digest,
                        timeout_s=timeout_s,
                    )
                )
            ),
            close_page_truth=lambda timeout_s: page_truth.close(
                timeout_s=timeout_s
            ),
            publish_utility_terminal=lambda terminal: holder[
                "utility_results"
            ].show_requested.emit(terminal),
            request_sinr_voice=lambda request: holder[
                "voice"
            ].request_received.emit(request),
            request_router_serial=lambda: holder[
                "router_serial"
            ].show_requested.emit(),
            request_application_exit=lambda: holder[
                "application_exit"
            ].exit_requested.emit(),
            report_error=report_ngui_error,
        ),
    )
    holder["orchestrator"] = orchestrator
    application = QApplication.instance() or QApplication(sys.argv)
    voice = _VoiceBridge(orchestrator)
    holder["voice"] = voice
    window = NGUIWindow(
        orchestrator.front_register,
        orchestrator.interactions,
        orchestrator.edits,
        report_render_error=lambda message: log.error(
            "NGUI render failed: %s", message
        ),
    )
    application_exit = _ApplicationExitBridge(window)
    messages = _MessageBridge(window)
    info = _InfoBridge(window)
    buy = _BuyLicenseBridge(window, messages)
    scan = _ScanBridge(window)
    router_serial = _RouterSerialBridge(
        window,
        submit=orchestrator.submit_startup_router_serial,
        cancel=orchestrator.cancel_startup_router_serial,
    )
    utility_results = _UtilityResultBridge(
        messages=messages,
        info=info,
        buy=buy,
        scan=scan,
    )
    holder["messages"] = messages
    holder["info"] = info
    holder["buy"] = buy
    holder["scan"] = scan
    holder["router_serial"] = router_serial
    holder["utility_results"] = utility_results
    holder["application_exit"] = application_exit
    orchestrator.start()
    window.show()
    _schedule_source_run_ui_driver(window)
    qt_exit_code = 1
    try:
        qt_exit_code = int(application.exec())
    finally:
        shutdown = dict(
            orchestrator.stop(
                timeout_s=RUNTIME_WORKER_SHUTDOWN_BUDGET_S
            )
        )
        try:
            publish_shutdown_receipt_artifact(runtime_dir, shutdown)
        except Exception as exc:
            log.error("NGUI shutdown receipt publication failed: %s", exc)
            qt_exit_code = 1
        if shutdown.get("status") != "complete":
            log.error("NGUI shutdown incomplete: %s", shutdown)
            qt_exit_code = 1
    return qt_exit_code


__all__ = ["launch"]
