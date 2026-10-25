"""Render the Graphs page from declared PageTruth fields."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from types import MappingProxyType

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIntValidator
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

from ..control_availability import (
    control_availability_from_mapping,
)
from ..chart_model import (
    CHART_WINDOW_OPTIONS,
    DEFAULT_CHART_WINDOW_SECONDS,
    SUPPORTED_CHART_WINDOW_SECONDS,
    ChartRenderSnapshot,
)
from ..front_register import PageTruthSnapshot, PresentationState
from ..edit_register import NGUIEditRegister, TRAFFIC_PLAN_GROUP
from ..interactions import IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import PageId
from .charts import AtomicChartView
from .confirm_button import (
    APPLY_BUTTON_STYLE,
    NGUIConfirmationCoordinator,
    NGUITwoStageConfirmButton,
)
from .interactions import (
    bind_button,
    bind_return_intent,
    bind_text_draft,
    stamp_draft_control,
)
from .style import (
    BODY_FONT_PT,
    BUTTON_FONT_PT,
    FONT_FAMILY,
    PAGE_TAB_FONT_PT,
    user_button_stylesheet,
)

WINDOWS = MappingProxyType(dict(CHART_WINDOW_OPTIONS))

_USER_BUTTON_STYLE = user_button_stylesheet(font_pt=PAGE_TAB_FONT_PT)
HEADER_FIELD_IDS = frozenset({
    "runtime.state",
    "connection.user_link",
    "connection.quality_pct",
    "radio.network_type",
    "radio.ca_active",
    "radio.pband",
    "radio.ca_pool",
    "radio.active_bands",
    "radio.frequency_set",
})
TRAFFIC_FIELD_IDS = frozenset({
    "traffic.budget_day_used_bytes",
    "traffic.budget_month_used_bytes",
    "traffic.budget_total_used_bytes",
    "traffic.budget_left_bytes",
    "traffic.budget_cycle_day",
    "traffic.budget_limit_bytes",
})
CELL_INFO_FIELD_IDS = frozenset({
    "radio.pci",
    "radio.cell_id",
    "nr.pci",
    "nr.cell_id",
    "quality.rtt_ms",
})
ACTION_GATE_FIELD_IDS = frozenset({
    "action.control_availability",
    "voice.sinr_lifecycle",
})
SUPPRESSED_CONTEXT_FIELD_IDS = frozenset({
    # Upstream context declared for Graphs but not shown on the page.
    "manual.action_pending",
    "manual.status",
    "manual.message",
    "quality.internet_outage_event",
    "quality.internet_outage_started_at",
    "quality.internet_coverage_event",
    "radio.earfcn",
    "radio.enb_id",
    "nr.band",
    "nr.arfcn",
    "command.manual_available",
    "command.status",
})


class GraphsPageShell(QWidget):
    """Graphs page shell without a field grid."""

    def __init__(
        self,
        register: NGUIInteractionRegister,
        edits: NGUIEditRegister,
        charts: AtomicChartView | None = None,
        confirmation: NGUIConfirmationCoordinator | None = None,
    ) -> None:
        super().__init__()
        if confirmation is None:
            raise TypeError("GraphsPageShell requires the window confirmation coordinator")
        self._confirmation = confirmation
        self.page = PageId.GRAPHS
        self.controls = self
        self.charts = charts or AtomicChartView()
        self._register = register
        self._matrix_identity: MatrixGenerationIdentity | None = None
        self._band_action_enabled = True
        self.traffic_plan = TrafficPlanInputs(register, edits)
        self.navigation_chrome = self.traffic_plan
        self._build_action_row()
        self._build_window_row()
        root = QVBoxLayout(self)
        # Keep the old page inset after removing extra shell layers.
        root.setContentsMargins(6, 10, 6, 12)
        root.setSpacing(2)
        root.addWidget(self.action_bar)
        root.addWidget(self.window_bar)
        root.addWidget(self.charts, 1)
        self._assert_field_contract()

    @property
    def field_ids(self) -> frozenset[str]:
        return (
            self.charts.field_ids
            | CELL_INFO_FIELD_IDS
            | ACTION_GATE_FIELD_IDS
            | self.traffic_plan.field_ids
        )

    @property
    def external_field_ids(self) -> frozenset[str]:
        return HEADER_FIELD_IDS

    @property
    def suppressed_field_ids(self) -> frozenset[str]:
        return SUPPRESSED_CONTEXT_FIELD_IDS

    def render(self, truth: PageTruthSnapshot) -> None:
        self._matrix_identity = truth.matrix_identity
        self.traffic_plan.render(truth)
        self.charts.render_truth(truth)
        self.cell_info.setText(_cell_info_text(truth))
        self._render_action_gate(truth)
        self._render_sinr_voice(truth)

    def render_chart(self, snapshot: ChartRenderSnapshot | None) -> None:
        if snapshot is None:
            return
        if snapshot.window_seconds not in SUPPORTED_CHART_WINDOW_SECONDS:
            raise ValueError("chart snapshot has unsupported operator window")
        if self.charts.render(snapshot):
            self._effective_window_s = snapshot.window_seconds
            self._style_window_buttons(self._effective_window_s)

    def _build_action_row(self) -> None:
        self.action_bar = QWidget()
        self.action_bar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self.action_bar)
        row.setContentsMargins(0, 2, 4, 2)
        row.setSpacing(8)
        self.speed_test = QPushButton("SPEED TEST")
        self.speed_test.setObjectName("nguiGraphsSpeedTest")
        self.speed_test.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        self.speed_test.setMinimumSize(104, 32)
        self.sinr_voice = QPushButton("SINR VOICE")
        self.sinr_voice.setObjectName("nguiGraphsSinrVoice")
        self.sinr_voice.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        self.sinr_voice.setMinimumSize(110, 32)
        self.sinr_voice.setCheckable(True)
        band_label = QLabel("BAND SET")
        band_label.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        band_label.setStyleSheet("color: #00d37f; font-size: 10pt; font-weight: bold;")
        self.band_input = QLineEdit()
        self.band_input.setObjectName("nguiGraphsBandSetInput")
        stamp_draft_control(self.band_input)
        self.band_input.setPlaceholderText("Bands")
        self.band_input.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        self.band_input.setMinimumHeight(34)
        self.band_input.setMaximumWidth(160)
        self.band_input.setStyleSheet(
            "QLineEdit { background-color: #0d141c; color: #d9e2ec; "
            "border: 1px solid #29405a; border-radius: 6px; font-size: 10pt; "
            "font-weight: bold; min-height: 34px; padding: 4px 8px; }"
        )
        self.band_apply = NGUITwoStageConfirmButton(
            self._band_set_request,
            self._register,
            IntentKind.APPLY_PBAND_CA_POOL,
            payload_from_request=self._band_set_payload,
            coordinator=self._confirmation,
        )
        self.band_apply.setObjectName("nguiGraphsBandSetApply")
        self.band_apply.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        self.band_apply.setMinimumSize(60, 32)
        bind_button(self.speed_test, self._register, IntentKind.START_SPEED_TEST)
        bind_button(
            self.sinr_voice,
            self._register,
            IntentKind.START_SINR_VOICE,
            self._sinr_voice_request,
            local_action=self._restore_sinr_voice_readback,
        )
        bind_text_draft(self.band_input, lambda _text: self.band_apply.disarm())
        self.speed_test.setStyleSheet(_USER_BUTTON_STYLE)
        self.sinr_voice.setStyleSheet(_USER_BUTTON_STYLE)
        self.band_apply.setStyleSheet(APPLY_BUTTON_STYLE)
        for widget in (self.speed_test, self.sinr_voice, band_label, self.band_input, self.band_apply):
            row.addWidget(widget)
        row.addStretch(1)

    def _build_window_row(self) -> None:
        self.window_bar = QWidget()
        row = QHBoxLayout(self.window_bar)
        row.setContentsMargins(0, 2, 6, 2)
        row.setSpacing(4)
        label = QLabel("WINDOW")
        label.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        label.setStyleSheet("color: #00d37f; font-size: 10pt; font-weight: bold;")
        row.addWidget(label)
        self.window_buttons: dict[int, QPushButton] = {}
        self._effective_window_s = DEFAULT_CHART_WINDOW_SECONDS
        for text, seconds in WINDOWS.items():
            button = QPushButton(text)
            button.setObjectName(f"nguiGraphsWindow_{seconds}")
            button.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setMinimumSize(48, 32)
            button.setProperty("chart_window_seconds", seconds)
            bind_button(
                button,
                self._register,
                IntentKind.SET_CHART_WINDOW,
                lambda seconds=seconds: {"seconds": seconds},
                local_action=self._restore_effective_window,
            )
            self.window_buttons[seconds] = button
            row.addWidget(button)
        self.window_buttons[DEFAULT_CHART_WINDOW_SECONDS].setChecked(True)
        self._style_window_buttons(DEFAULT_CHART_WINDOW_SECONDS)
        self.cell_info = QLabel("LTE: --- | NR: --- | RTT ---")
        self.cell_info.setFont(QFont(FONT_FAMILY, PAGE_TAB_FONT_PT, QFont.Weight.Bold))
        self.cell_info.setStyleSheet("color: #77a0c5; font-size: 10pt; font-weight: bold;")
        self.cell_info.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cell_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        row.addWidget(self.cell_info, 1)

    def _band_set_request(self) -> str | None:
        if not self._band_action_enabled:
            return None
        bands = _normalized_bands(self.band_input.text())
        return bands or None

    def _band_set_payload(self, bands: object) -> dict[str, object]:
        identity = self._matrix_identity
        if not isinstance(identity, MatrixGenerationIdentity):
            raise RuntimeError("manual command Matrix identity is unavailable")
        return {
            "pband": "",
            "ca_pool": str(bands),
            "matrix_identity": identity.as_dict(),
        }

    def _render_action_gate(self, truth: PageTruthSnapshot) -> None:
        availability = truth.fields["action.control_availability"]
        decision = control_availability_from_mapping(
            availability.value
            if availability.state is PresentationState.VALID
            and isinstance(availability.value, Mapping)
            else None,
            "band_selection",
        )
        enabled = decision.enabled
        if self._band_action_enabled != enabled:
            self._band_action_enabled = enabled
            if not enabled:
                self.band_apply.disarm()
        tooltip = "" if enabled else decision.reason
        for widget in (self.band_input, self.band_apply):
            if widget.isEnabled() != enabled:
                widget.setEnabled(enabled)
            if widget.toolTip() != tooltip:
                widget.setToolTip(tooltip)

    def _sinr_voice_request(self) -> dict[str, bool]:
        lifecycle = getattr(self, "_voice_lifecycle", {})
        if str(lifecycle.get("state") or "") == "pending":
            raise RuntimeError("SINR voice request is already pending")
        return {"enabled": not bool(lifecycle.get("active", False))}

    def _render_sinr_voice(self, truth: PageTruthSnapshot) -> None:
        field = truth.fields["voice.sinr_lifecycle"]
        lifecycle = (
            field.value
            if field.state is PresentationState.VALID
            and isinstance(field.value, Mapping)
            else {}
        )
        self._voice_lifecycle = lifecycle
        state = str(lifecycle.get("state") or "inactive")
        active = bool(lifecycle.get("active", False))
        pending = state == "pending"
        if self.sinr_voice.isChecked() != active:
            self.sinr_voice.setChecked(active)
        text = (
            "SINR Voice …"
            if pending
            else ("SINR Voice ■" if active else "SINR Voice")
        )
        if self.sinr_voice.text() != text:
            self.sinr_voice.setText(text)
        self.sinr_voice.setEnabled(not pending)
        self.sinr_voice.setToolTip(
            str(lifecycle.get("message") or "") if state == "failed" else ""
        )
        self.sinr_voice.setStyleSheet(_USER_BUTTON_STYLE)

    def _restore_sinr_voice_readback(self) -> None:
        lifecycle = getattr(self, "_voice_lifecycle", {})
        active = bool(lifecycle.get("active", False))
        self.sinr_voice.setChecked(active)
        self.sinr_voice.setText("SINR Voice ■" if active else "SINR Voice")

    def _restore_effective_window(self) -> None:
        self._style_window_buttons(self._effective_window_s)

    def _style_window_buttons(self, selected_seconds: int) -> None:
        for seconds, button in self.window_buttons.items():
            button.setChecked(seconds == selected_seconds)
            button.setStyleSheet(_USER_BUTTON_STYLE)

    def _assert_field_contract(self) -> None:
        declared = {spec.field_id for spec in fields_for_page(PageId.GRAPHS)}
        groups = (self.field_ids, self.external_field_ids, self.suppressed_field_ids)
        overlap = set().union(*(
            left & right
            for index, left in enumerate(groups)
            for right in groups[index + 1 :]
        ))
        if overlap:
            raise ValueError(f"Graphs field has two presentation dispositions: {sorted(overlap)}")
        covered = set().union(*groups)
        if covered != declared:
            raise ValueError(
                "Graphs renderer field contract mismatch: "
                f"missing={sorted(declared - covered)}, extra={sorted(covered - declared)}"
            )


class TrafficPlanInputs(QWidget):
    """Render traffic-plan inputs from Graphs PageTruth."""

    _STAT_FIELDS = (
        ("DAY", "traffic.budget_day_used_bytes"),
        ("MONTH", "traffic.budget_month_used_bytes"),
        ("TOTAL", "traffic.budget_total_used_bytes"),
        ("LEFT", "traffic.budget_left_bytes"),
        ("CYCLE", "traffic.budget_cycle_day"),
        ("LIMIT", "traffic.budget_limit_bytes"),
    )

    def __init__(
        self,
        register: NGUIInteractionRegister,
        edits: NGUIEditRegister,
    ) -> None:
        super().__init__()
        self._edits = edits
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._labels: dict[str, QLabel] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        stats = QHBoxLayout()
        stats.setContentsMargins(0, 0, 0, 0)
        stats.setSpacing(22)
        stats.addStretch(1)
        for title, field_id in self._STAT_FIELDS:
            label = QLabel(f"{title} ---")
            label.setFont(QFont(FONT_FAMILY, BODY_FONT_PT, QFont.Weight.Bold))
            label.setObjectName("trafficPlanStat")
            label.setStyleSheet(
                f"color: #77a0c5; font-size: {BODY_FONT_PT}pt; font-weight: bold;"
            )
            self._labels[field_id] = label
            stats.addWidget(label)
        root.addLayout(stats)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.limit_gb = QLineEdit()
        self.limit_gb.setObjectName("nguiGraphsTrafficPlanLimitGb")
        self.limit_gb.setValidator(QIntValidator(0, 100_000, self.limit_gb))
        self.limit_gb.setFixedWidth(92)
        self.limit_gb.setMinimumHeight(26)
        self.limit_gb.setFont(QFont(FONT_FAMILY, BODY_FONT_PT))
        self.limit_gb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.limit_gb.setPlaceholderText("GB")
        self.limit_gb.setToolTip("Monthly data limit in GB.")
        self.cycle_day = QLineEdit()
        self.cycle_day.setObjectName("nguiGraphsTrafficPlanCycleDay")
        stamp_draft_control(self.limit_gb)
        stamp_draft_control(self.cycle_day)
        self.cycle_day.setValidator(QIntValidator(1, 31, self.cycle_day))
        self.cycle_day.setFixedWidth(64)
        self.cycle_day.setMinimumHeight(26)
        self.cycle_day.setFont(QFont(FONT_FAMILY, BODY_FONT_PT))
        self.cycle_day.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cycle_day.setPlaceholderText("1-31")
        self.cycle_day.setToolTip("Monthly cycle start day, 1 to 31.")
        bind_text_draft(self.limit_gb, self._draft_limit_gb)
        bind_text_draft(self.cycle_day, self._draft_cycle_day)
        apply_button = QPushButton("APPLY")
        apply_button.setObjectName("nguiGraphsTrafficPlanApply")
        apply_button.setMinimumHeight(26)
        apply_button.setFont(QFont(FONT_FAMILY, BUTTON_FONT_PT, QFont.Weight.Bold))
        apply_button.setStyleSheet(
            user_button_stylesheet(
                font_pt=BUTTON_FONT_PT,
                min_height=26,
                padding="2px 10px",
            )
        )
        self.edit_status = QLabel("")
        self.edit_status.setObjectName("nguiGraphsTrafficPlanEditStatus")
        bind_button(
            apply_button,
            register,
            IntentKind.SET_TRAFFIC_PLAN,
            self._payload,
        )
        bind_return_intent(
            self.limit_gb,
            register,
            IntentKind.SET_TRAFFIC_PLAN,
            self._payload,
        )
        bind_return_intent(
            self.cycle_day,
            register,
            IntentKind.SET_TRAFFIC_PLAN,
            self._payload,
        )
        layout.addStretch(1)
        plan_label = QLabel("PLAN")
        plan_label.setFont(QFont(FONT_FAMILY, BODY_FONT_PT, QFont.Weight.Bold))
        plan_label.setStyleSheet(
            f"color: #77a0c5; font-size: {BODY_FONT_PT}pt; font-weight: bold;"
        )
        layout.addWidget(plan_label)
        layout.addWidget(self.limit_gb)
        layout.addWidget(self.cycle_day)
        layout.addWidget(apply_button)
        layout.addWidget(self.edit_status)
        root.addLayout(layout)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset((*self._labels, "edit.lifecycle"))

    def render(self, truth: PageTruthSnapshot) -> None:
        values = self._edits.snapshot().visible
        self._sync_text(self.limit_gb, values["traffic.plan_limit_gb"])
        self._sync_text(self.cycle_day, values["traffic.plan_cycle_day"])
        for title, field_id in self._STAT_FIELDS:
            field = truth.fields[field_id]
            rendered = f"{title} {field.text}"
            label = self._labels[field_id]
            if label.text() != rendered:
                label.setText(rendered)
        lifecycle = truth.fields["edit.lifecycle"].value
        groups = lifecycle.get("groups", {}) if isinstance(lifecycle, Mapping) else {}
        traffic = (
            groups.get(TRAFFIC_PLAN_GROUP, {})
            if isinstance(groups, Mapping)
            else {}
        )
        message = (
            str(traffic.get("message") or "")
            if isinstance(traffic, Mapping)
            else ""
        )
        self.edit_status.setText(message)

    def _payload(self) -> dict[str, int | float]:
        self._sync_drafts_from_text()
        return dict(self._edits.payload(TRAFFIC_PLAN_GROUP))

    @staticmethod
    def _sync_text(widget: QLineEdit, value: object) -> None:
        if widget.hasFocus():
            return
        rendered = str(int(round(float(value))))
        if widget.text() == rendered:
            return
        blocked = widget.blockSignals(True)
        widget.setText(rendered)
        widget.blockSignals(blocked)

    def _sync_drafts_from_text(self) -> None:
        self._draft_limit_gb(self.limit_gb.text())
        self._draft_cycle_day(self.cycle_day.text())

    def _draft_limit_gb(self, text: str) -> None:
        value = self._coerce_int(text, minimum=0, maximum=100_000)
        if value is None:
            self._edits.set_invalid_draft(
                "traffic.plan_limit_gb",
                reason="enter_whole_gigabytes_0_to_100000",
            )
            return
        self._edits.set_draft("traffic.plan_limit_gb", float(value))

    def _draft_cycle_day(self, text: str) -> None:
        value = self._coerce_int(text, minimum=1, maximum=31)
        if value is None:
            self._edits.set_invalid_draft(
                "traffic.plan_cycle_day",
                reason="enter_cycle_day_1_to_31",
            )
            return
        self._edits.set_draft("traffic.plan_cycle_day", value)

    @staticmethod
    def _coerce_int(text: str, *, minimum: int, maximum: int) -> int | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            return None
        if parsed < minimum or parsed > maximum:
            return None
        return parsed


def _cell_info_text(truth: PageTruthSnapshot) -> str:
    def value(field_id: str) -> str:
        return truth.fields[field_id].text

    lte = f"Cell {value('radio.cell_id')}  PCI {value('radio.pci')}"
    nr_parts = (value("nr.cell_id"), value("nr.pci"))
    nr = (
        "UNSUP." if all(part == "UNSUP." for part in nr_parts) else
        "----"
        if all(part in {"----", "--", ""} for part in nr_parts)
        else f"Cell {nr_parts[0]}  PCI {nr_parts[1]}"
    )
    return f"LTE: {lte} | NR: {nr} | RTT {value('quality.rtt_ms')}"


def _normalized_bands(raw: str) -> str:
    bands: list[int] = []
    for token in re.findall(r"\d+", raw):
        band = int(token)
        if band > 0 and band not in bands:
            bands.append(band)
    return ",".join(str(band) for band in bands)


__all__ = [
    "CELL_INFO_FIELD_IDS",
    "GraphsPageShell",
    "HEADER_FIELD_IDS",
    "SUPPRESSED_CONTEXT_FIELD_IDS",
    "TRAFFIC_FIELD_IDS",
    "TrafficPlanInputs",
    "WINDOWS",
]
