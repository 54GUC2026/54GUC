"""Read-tool controls and report; only canonical PageTruth is consumed."""
from collections.abc import Mapping

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy
from PySide6.QtCore import Qt
from arch4.ngui.interactions import IntentKind
from arch4.ngui.login_lifecycle import login_lifecycle_state
from arch4.foundation.read_diagnostic_report import decode_read_diagnostic_data
from .interactions import bind_button


def report_text(envelope):
    data = envelope.get("data", {})
    if not data:
        return ""
    data = decode_read_diagnostic_data(data)
    state = data['state']
    if state in {'queued', 'running'}:
        return f"Checking router... {data['completed']} of {data['total']} checks finished."
    if state == 'completed':
        if (data['capture']['complete'] and data['total'] == 0
                and data['coverage_context'].get('monitoring_overview') is not None):
            return 'Router report ready. Based on current monitoring; no additional native probes.'
        return "Router check complete." if data['outcomes'].get('success', 0) == data['total'] and data['capture']['complete'] else "Check complete. Some information could not be read or saved."
    return {
        'cancelled': 'Check cancelled. Completed results are retained.',
        'timed_out': 'Check stopped because the router took too long.',
        'interrupted': 'Check stopped because the router connection changed.',
        'limited': 'Check stopped because the report reached its size limit.',
    }.get(state, 'Router check could not finish. Details are in the report.')



class ReadDiagnosticPanel(QWidget):
    def __init__(self, register):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        self.start = QPushButton("Check router")
        self._active = False
        self.start.setObjectName("nguiReadDiagnosticsStart")
        self.start.setProperty("nguiRole", "utility_primary")
        bind_button(self.start, register, IntentKind.READ_DIAGNOSTICS,
                    lambda: {"action": "cancel" if self._active else "start"})
        self.start.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        buttons.addWidget(self.start)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.report = QLabel("Connect to the router to check which information it can return.")
        self.report.setTextFormat(Qt.TextFormat.PlainText)
        self.report.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.report.setWordWrap(True)
        self._rendered = None
        layout.addWidget(self.report)

    def render(self, truth):
        connected = login_lifecycle_state(truth.fields["profile.login_lifecycle"].value) == "connected"
        fact = truth.fields.get("read.diagnostic")
        envelope = fact.value if fact and isinstance(fact.value, Mapping) else {}
        data = envelope.get("data", {})
        active = data.get("state") in {"queued", "running"}
        self._active = active
        self.start.setText("Cancel check" if active else "Check router")
        self.start.setEnabled(connected)
        # A report describes its recorded reads, not present measurement truth.
        # Keep its visible timestamp even after it ages, within this session.
        key = (connected, envelope.get("diagnostic_id"), data.get("state"), data.get("completed"))
        if key != self._rendered:
            text = report_text(envelope) if envelope else "Connect to the router to check which information it can return." if not connected else "Check what your router can report. Normal monitoring continues."
            self.report.setText(text)
            self._rendered = key
        self.report.setVisible(connected or bool(envelope))
