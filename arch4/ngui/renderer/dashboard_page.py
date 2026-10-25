"""Render the five Dashboard regions.

This module uses one complete Dashboard PageTruth snapshot. It does not read
runtime objects, logs, or sidecar files, and it does not derive visible values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..control_availability import (
    WORKFLOW_CONTROL,
    control_availability_from_mapping,
)
from ..front_register import PageTruthSnapshot
from ..interactions import ControlFamily, IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import FieldSpec, PageId
from .interactions import bind_button
from .measurement_layout import render_nr_support


DETAILS_FIELDS = (
    "radio.pband",
    "radio.ca_active",
    "radio.carrier_details",
    "radio.ca_pool",
    "radio.dl_bandwidth_mhz",
    "radio.ul_bandwidth_mhz",
    "radio.dl_frequency_mhz",
    "radio.ul_frequency_mhz",
    "nr.band",
    "nr.bandwidth_mhz",
    "nr.dl_frequency_mhz",
    "nr.ul_bandwidth_mhz",
    "nr.ul_frequency_mhz",
    "radio.transmission_mode",
    "nr.mode",
    "radio.neighbors",
)

SIGNAL_FIELDS = (
    "rf.sinr_db",
    "rf.rsrp_dbm",
    "rf.rsrq_db",
    "rf.rssi_dbm",
    "rf.tx_ppusch",
    "rf.tx_ppucch",
    "rf.tx_psrs",
    "rf.tx_pprach",
    "nr.tx_power_dbm",
    "nr.sinr_db",
    "nr.rsrp_dbm",
    "nr.rsrq_db",
)

PROVIDER_CELL_FIELDS = (
    "provider.name",
    "radio.network_type",
    "radio.rrc",
    "radio.pci",
    "radio.earfcn",
    "radio.cell_id",
    "radio.enb_id",
    "radio.ri",
    "nr.pci",
    "nr.arfcn",
    "nr.cell_id",
    "nr.ri",
)

QUALITY_FIELDS = (
    "quality.congestion_pct",
    "quality.congestion_trend",
    "quality.rtt_ms",
    "quality.jitter_trimmed_ms",
    "quality.cast",
    "quality.cart_activity",
    "quality.ca_ld_pct",
    "quality.ca_ld_activity",
    "quality.ri_stability_pct",
    "quality.ri_activity",
)

ANTENNA_FIELDS = (
    "antenna_guide.status",
    "antenna_guide.guidance",
    "antenna_guide.active_action",
    "antenna_guide.actions_enabled",
    "antenna_guide.antenna_score",
    "antenna_guide.antenna_score_role",
    "antenna_guide.nr_score",
    "antenna_guide.nr_score_role",
    "antenna_guide.confidence",
    "antenna_guide.trend",
    "antenna_guide.trend_role",
    "antenna_guide.hint",
    "antenna_guide.monitor_state",
    "command.antenna_guide_status",
    "antenna_guide.contributors",
    "antenna_guide.meter_segments",
)

SITE_REPORT_FIELDS = (
    "site_report.selected_window",
    "site_report.status",
    "site_report.progress_pct",
    "utility.lifecycle",
)
SITE_REPORT_CONTROL_FIELDS = ("site_report.selected_window",)
SITE_REPORT_INTENTIONALLY_ABSENT_FIELDS = (
    "site_report.status",
    "site_report.progress_pct",
    "utility.lifecycle",
)

EVENT_FIELDS = (
    "event.connectivity_conclusion",
    "event.recent",
)

DETAILS_MATRIX = (
    ("radio.transmission_mode", "nr.mode", "radio.ca_active", None),
    ("radio.pband", "nr.band", "radio.carrier_details", None),
    ("radio.ca_pool", None, None, None),
    (None, None, None, None),
    ("radio.dl_bandwidth_mhz", "radio.ul_bandwidth_mhz", "nr.bandwidth_mhz", "nr.ul_bandwidth_mhz"),
    ("radio.dl_frequency_mhz", "radio.ul_frequency_mhz", "nr.dl_frequency_mhz", "nr.ul_frequency_mhz"),
    ("radio.neighbors", None, None, None),
)

SIGNAL_MATRIX = (
    (None, None, None, None),
    ("rf.sinr_db", "nr.sinr_db", "rf.tx_ppusch", None),
    ("rf.rsrp_dbm", "nr.rsrp_dbm", "rf.tx_ppucch", "nr.tx_power_dbm"),
    ("rf.rsrq_db", "nr.rsrq_db", "rf.tx_psrs", None),
    ("rf.rssi_dbm", None, "rf.tx_pprach", None),
)

DASHBOARD_INTENTIONALLY_ABSENT_FIELDS = (
    # Scheduler detail remains on Developer Control, not Dashboard.
    "radio.lte_num_rbs", "nr.cqi", "nr.mcs_dl", "nr.mcs_ul",
    "nr.qam_dl", "nr.qam_ul", "nr.num_rbs",
    "action.control_availability",
    "connection.quality_pct",
    "connection.led_level",
    # The orchestrator converts each Foundation event once into event.recent.
    # Rendering the source mapping here would create a second interpretation
    # path and expose an internal envelope.
    "event.connectivity_transition",
    "radio.carrier_count",
    "radio.active_bands",
    "radio.frequency_set",
    "radio.neighbor_count",
    "quality.congestion_bucket",
    "quality.cq_pct",
    "quality.cart",
    "quality.cart_ld",
    "quality.cart_ld_activity",
)
INTENTIONALLY_ABSENT_FIELDS = frozenset(
    (*SITE_REPORT_INTENTIONALLY_ABSENT_FIELDS, *DASHBOARD_INTENTIONALLY_ABSENT_FIELDS)
)

REGION_FIELDS: Mapping[str, tuple[str, ...]] = {
    "details": DETAILS_FIELDS,
    "signals": SIGNAL_FIELDS,
    "provider_cell": PROVIDER_CELL_FIELDS,
    "quality": QUALITY_FIELDS,
    "antenna": ANTENNA_FIELDS,
    "events": EVENT_FIELDS,
}

SITE_REPORT_WINDOWS = ("1h", "6h", "12h", "1d", "1w", "14d")
ANTENNA_GUIDE_ACTIONS = (
    ("START GUIDE", "start"),
)


def _spec_index() -> dict[str, FieldSpec]:
    return {spec.field_id: spec for spec in fields_for_page(PageId.DASHBOARD)}


def _validate_field_contract(specs: Mapping[str, FieldSpec]) -> None:
    assignments = [field_id for fields in REGION_FIELDS.values() for field_id in fields]
    duplicates = sorted(
        {field_id for field_id in assignments if assignments.count(field_id) > 1}
    )
    if duplicates:
        raise RuntimeError(f"Dashboard fields assigned more than once: {duplicates}")

    declared = set(specs)
    visible = set(assignments) | set(SITE_REPORT_CONTROL_FIELDS)
    absent = set(INTENTIONALLY_ABSENT_FIELDS)
    overlap = sorted(visible & absent)
    if overlap:
        raise RuntimeError(f"Dashboard fields both visible and absent: {overlap}")
    undeclared = sorted((visible | absent) - declared)
    unassigned = sorted(declared - visible - absent)
    if undeclared or unassigned:
        raise RuntimeError(
            "Dashboard field contract mismatch: "
            f"undeclared={undeclared}, unassigned={unassigned}"
        )


class DashboardFieldRegion(QGroupBox):
    """Render a field region with an explicit field list."""

    def __init__(
        self,
        title: str,
        field_ids: Iterable[str],
        specs: Mapping[str, FieldSpec],
        *,
        margins: tuple[int, int, int, int] = (12, 10, 12, 10),
        horizontal_spacing: int = 12,
        vertical_spacing: int = 3,
        row_min_height: int = 0,
        label_overrides: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(title)
        self._field_ids = tuple(field_ids)
        self._labels: dict[str, QLabel] = {}
        self._nr_headings: list[tuple[QLabel, str]] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(*margins)
        grid.setHorizontalSpacing(horizontal_spacing)
        grid.setVerticalSpacing(vertical_spacing)
        labels = dict(label_overrides or {})
        row = -1
        for field_id in self._field_ids:
            row += 1
            if field_id.startswith("nr.") and not self._nr_headings:
                heading = QLabel("NR")
                grid.addWidget(heading, row, 0, 1, 2)
                self._nr_headings.append((heading, "NR"))
                row += 1
            if row_min_height:
                grid.setRowMinimumHeight(row, row_min_height)
            spec = specs[field_id]
            name = QLabel(f"{labels.get(field_id, spec.label)}:")
            value = QLabel("----")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
            self._labels[field_id] = value
        grid.setColumnStretch(1, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._field_ids)

    def value_text(self, field_id: str) -> str:
        return self._labels[field_id].text()

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
        render_nr_support(truth, self._labels, self._nr_headings)


class DashboardMatrixRegion(QGroupBox):
    """Render a matrix region at its declared Dashboard position."""

    def __init__(
        self,
        title: str,
        rows: Iterable[tuple[str | None, ...]],
        specs: Mapping[str, FieldSpec],
        *,
        margins: tuple[int, int, int, int] = (12, 10, 12, 10),
        horizontal_spacing: int = 10,
        vertical_spacing: int = 3,
        row_min_height: int = 0,
        line_height: int = 0,
        label_overrides: Mapping[str, str] | None = None,
        headings: Iterable[tuple[int, int, str]] = (),
        value_spans: Mapping[str, int] | None = None,
        stacked_fields: Iterable[str] = (),
    ) -> None:
        super().__init__(title)
        self._labels: dict[str, QLabel] = {}
        self._nr_headings: list[tuple[QLabel, str]] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(*margins)
        grid.setHorizontalSpacing(horizontal_spacing)
        grid.setVerticalSpacing(vertical_spacing)
        if line_height:
            grid.setAlignment(Qt.AlignTop)
        labels = dict(label_overrides or {})
        spans = dict(value_spans or {})
        stacked = frozenset(stacked_fields)
        rows = tuple(rows)
        self.line_units = 0

        for row_index, row in enumerate(rows):
            units = 2 if any(field_id in stacked for field_id in row) else 1
            self.line_units += units
            if line_height:
                grid.setRowMinimumHeight(row_index, line_height * units)
            elif row_min_height:
                grid.setRowMinimumHeight(row_index, row_min_height)
            for column_index, field_id in enumerate(row):
                if field_id is None:
                    continue
                spec = specs[field_id]
                name = QLabel(f"{labels.get(field_id, spec.label)}:")
                if field_id == "nr.mode":
                    self._nr_headings.append((name, "NR MODE:"))
                value = QLabel("----")
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                value.setWordWrap(True)
                if line_height:
                    name.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                    value.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                if field_id in {"radio.transmission_mode", "nr.mode"}:
                    name.setStyleSheet("font-weight: bold;")
                    value.setStyleSheet("font-weight: bold;")
                value.setMinimumWidth(70)
                base_col = column_index * 2
                if field_id in stacked:
                    tile = QWidget()
                    if line_height:
                        tile.setMinimumHeight(2 * line_height)
                        tile.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                    tile_layout = QVBoxLayout(tile)
                    tile_layout.setContentsMargins(0, 0, 0, 0)
                    tile_layout.setSpacing(0 if line_height else 2)
                    tile_layout.addWidget(name, 1 if line_height else 0)
                    tile_layout.addWidget(value, 1 if line_height else 0)
                    grid.addWidget(tile, row_index, base_col, 1, spans.get(field_id, 1) + 1)
                else:
                    grid.addWidget(name, row_index, base_col, alignment=Qt.AlignLeft)
                    grid.addWidget(
                        value, row_index, base_col + 1, 1, spans.get(field_id, 1)
                    )
                self._labels[field_id] = value

        for row_index, column_index, text in headings:
            heading = QLabel(text)
            if line_height:
                heading.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            if text.startswith("NR"):
                self._nr_headings.append((heading, text))
            grid.addWidget(heading, row_index, column_index * 2, 1, 2)

        for column_index in range(max(len(row) for row in rows)):
            grid.setColumnStretch(column_index * 2 + 1, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def value_text(self, field_id: str) -> str:
        return self._labels[field_id].text()

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
        render_nr_support(truth, self._labels, self._nr_headings)


class DashboardRecentEventsRegion(QGroupBox):
    """Render recent events from declared PageTruth fields."""

    def __init__(self) -> None:
        super().__init__("RECENT EVENTS")
        self._labels = {
            "event.connectivity_conclusion": QLabel("----"),
            "event.recent": QPlainTextEdit("----"),
        }
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        conclusion = self._labels["event.connectivity_conclusion"]
        conclusion.setWordWrap(True)
        conclusion.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(conclusion)

        event_box = QFrame()
        event_box.setObjectName("nguiDashboardEventLogBox")
        event_box.setFrameShape(QFrame.Shape.StyledPanel)
        event_box.setMinimumHeight(260)
        event_layout = QVBoxLayout(event_box)
        event_layout.setContentsMargins(10, 10, 10, 10)
        event_layout.setSpacing(4)
        recent = self._labels["event.recent"]
        recent.setObjectName("nguiDashboardEventLog")
        recent.setReadOnly(True)
        recent.setFrameShape(QFrame.Shape.NoFrame)
        event_layout.addWidget(recent, 1)
        root.addWidget(event_box, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def value_text(self, field_id: str) -> str:
        widget = self._labels[field_id]
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        return widget.text()

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            text = str(field.text or "")
            if isinstance(label, QPlainTextEdit):
                if label.toPlainText() == text:
                    continue
                label.setPlainText(text)
                label.verticalScrollBar().setValue(label.verticalScrollBar().maximum())
            elif label.text() != text:
                label.setText(text)


class DashboardAntennaGuideRegion(QWidget):
    """Render the antenna guide from Foundation-owned PageTruth."""

    _CONTRIBUTOR_ORDER = ("RF", "RI", "LAT", "CA", "NR_RF", "NR_RI", "NR_LAT")
    _OPTIONAL_LABELS = frozenset(
        {
            "antenna_guide.status",
            "antenna_guide.guidance",
            "antenna_guide.confidence",
            "antenna_guide.trend",
            "antenna_guide.trend_role",
            "antenna_guide.hint",
            "antenna_guide.monitor_state",
            "command.antenna_guide_status",
        }
    )
    _PLACEHOLDERS = frozenset({"", "-", "--", "----"})
    _MOUNTED_LABELS = frozenset(
        {
            "antenna_guide.antenna_score",
            "antenna_guide.nr_score",
            "antenna_guide.confidence",
            "antenna_guide.hint",
            "antenna_guide.monitor_state",
            "command.antenna_guide_status",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self._labels = {
            "antenna_guide.status": QLabel("----", self),
            "antenna_guide.guidance": QLabel("----", self),
            "antenna_guide.active_action": QLabel("----", self),
            "antenna_guide.actions_enabled": QLabel("----", self),
            "antenna_guide.antenna_score": QLabel("N/A", self),
            "antenna_guide.antenna_score_role": QLabel("neutral", self),
            "antenna_guide.nr_score": QLabel("N/A", self),
            "antenna_guide.nr_score_role": QLabel("neutral", self),
            "antenna_guide.confidence": QLabel("----", self),
            "antenna_guide.trend": QLabel("----", self),
            "antenna_guide.trend_role": QLabel("neutral", self),
            "antenna_guide.hint": QLabel("----", self),
            "antenna_guide.monitor_state": QLabel("----", self),
            "command.antenna_guide_status": QLabel("----", self),
            "antenna_guide.contributors": QLabel("----", self),
            "antenna_guide.meter_segments": QLabel("----", self),
        }
        for field_id, label in self._labels.items():
            if field_id not in self._MOUNTED_LABELS:
                label.hide()
        self._meter_segments: list[QLabel] = []
        self._contributor_values: dict[str, QLabel] = {}
        self._contributor_bars: dict[str, QProgressBar] = {}

        root = QVBoxLayout(self)
        self._layout = root
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        top_row = QHBoxLayout()
        self._top_row_layout = top_row
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(10)
        top_row.addStretch(1)

        self._radio_columns: dict[str, QWidget] = {}
        for radio, title, score_field, lanes in (
            ("LTE", "LTE", "antenna_guide.antenna_score", ("RF", "RI", "LAT", "CA")),
            ("NR", "5G", "antenna_guide.nr_score", ("NR_RF", "NR_RI", "NR_LAT")),
        ):
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(2)
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column_layout.addWidget(heading)
            content = QHBoxLayout()
            content.setSpacing(6)
            score = self._labels[score_field]
            score.setObjectName(f"nguiDashboardAntennaScore{radio}")
            score.setAlignment(Qt.AlignmentFlag.AlignCenter)
            score.setFixedWidth(112)
            score.setMinimumHeight(66)
            score.setStyleSheet("font-size: 52px; color: #d9e2ec; font-weight: bold;")
            content.addWidget(score)
            contributors = QWidget()
            contributors.setMinimumWidth(128)
            contributors.setMaximumWidth(168)
            grid = QGridLayout(contributors)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(2)
            for row in range(4):
                grid.setRowMinimumHeight(row, 20)
            for row, name in enumerate(lanes):
                label = QLabel("CA (info)" if name == "CA" else name.removeprefix("NR_"))
                grid.addWidget(label, row, 0)
                value = QLabel("----")
                value.setFixedWidth(38)
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(50)
                bar.setTextVisible(False)
                bar.setMinimumWidth(44)
                bar.setMaximumWidth(70)
                bar.setFixedHeight(9)
                bar.setObjectName(f"nguiDashboardContributor{name}")
                if name == "CA":
                    label.setToolTip("Traffic-dependent information; excluded from both scores.")
                    bar.setToolTip(label.toolTip())
                self._contributor_values[name] = value
                self._contributor_bars[name] = bar
                grid.addWidget(value, row, 1)
                grid.addWidget(bar, row, 2)
            if radio == "NR":
                grid.addWidget(QLabel(" "), 3, 0)
            content.addWidget(contributors)
            column_layout.addLayout(content)
            self._radio_columns[radio] = column
            if radio == "LTE":
                self._contributors_widget = contributors
            else:
                self._nr_contributors_widget = contributors
            top_row.addWidget(column, 0, Qt.AlignmentFlag.AlignVCenter)

        text_column = QVBoxLayout()
        self._text_layout = text_column
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(4)

        state = self._labels["antenna_guide.monitor_state"]
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.monitor_state_label = state

        command = self._labels["command.antenna_guide_status"]
        command.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        command.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.command_status_label = command

        confidence = self._labels["antenna_guide.confidence"]
        trend = self._labels["antenna_guide.trend"]
        for label in (confidence, trend):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        confidence.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        trend.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        text_column.addWidget(confidence, 0)

        meter = QWidget()
        self._meter_widget = meter
        meter_layout = QVBoxLayout(meter)
        meter_layout.setContentsMargins(0, 0, 0, 0)
        meter_layout.setSpacing(2)
        for index in range(10):
            segment = QLabel("")
            segment.setFixedSize(42, 7)
            segment.setObjectName(f"nguiDashboardAntennaMeter{index}")
            segment.setStyleSheet("border: 1px solid #d9e2ec; background: #080d12;")
            self._meter_segments.append(segment)
            meter_layout.addWidget(segment)
        meter.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        # The two signed numbers replace the redundant single-radio LED meter.
        meter.hide()
        top_row.addStretch(1)
        root.addLayout(top_row, 1)
        root.addLayout(text_column, 0)

        hint = self._labels["antenna_guide.hint"]
        hint.setWordWrap(True)
        hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hint.setFixedHeight(36)
        hint.setStyleSheet("color: #77a0c5; font-size: 12pt;")
        hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root.addWidget(hint, 0)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def value_text(self, field_id: str) -> str:
        return self._labels[field_id].text()

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            text = field.text
            if label.text() != text:
                label.setText(text)
            if field_id in self._OPTIONAL_LABELS:
                visible = (
                    field_id in self._MOUNTED_LABELS
                    and text.strip() not in self._PLACEHOLDERS
                )
                label.setVisible(visible)
        for score, role in (
            ("antenna_guide.antenna_score", "antenna_guide.antenna_score_role"),
            ("antenna_guide.nr_score", "antenna_guide.nr_score_role"),
        ):
            label = self._labels[score]
            font = label.font()
            font.setBold(True)
            font.setPixelSize(52)
            # Keep the existing score space and exact accepted text. Status
            # tokens and four-character signed scores also have to fit it.
            while (font.pixelSize() > 1 and QFontMetrics(font).horizontalAdvance(label.text())
                   > label.contentsRect().width() - 4):
                font.setPixelSize(font.pixelSize() - 1)
            label.setStyleSheet(
                f"font-size: {font.pixelSize()}px; color: {self._role_color(truth.fields[role].text)}; font-weight: bold;"
            )
        trend_role = truth.fields["antenna_guide.trend_role"].text
        self._labels["antenna_guide.trend"].setStyleSheet(
            f"font-size: 24px; color: {self._role_color(trend_role)}; font-weight: bold;"
        )
        self._apply_contributors(truth)
        self._apply_meter_segments(truth)

    @staticmethod
    def _role_color(role: str) -> str:
        return {
            "positive": "#00d37f",
            "negative": "#ff4055",
            "warning": "#ffb000",
            "neutral": "#d9e2ec",
        }.get(str(role or "").strip().lower(), "#d9e2ec")

    def _apply_contributors(self, truth: PageTruthSnapshot) -> None:
        field = truth.fields["antenna_guide.contributors"]
        raw = field.value if isinstance(field.value, Mapping) else {}
        for name in self._CONTRIBUTOR_ORDER:
            item = raw.get(name)
            item = item if isinstance(item, Mapping) else {}
            signed = str(item.get("text", "----"))
            value = self._contributor_values[name]
            if value.text() != signed:
                value.setText(signed)
            bar = self._contributor_bars[name]
            bar.setValue(int(item.get("bar_pct", 50) or 0))
            bar.setEnabled(bool(item.get("available", signed != "----")))
            color = self._role_color(str(item.get("role", "neutral")))
            value.setStyleSheet(f"color: {color};")
            bar.setStyleSheet(
                "QProgressBar { background: #080d12; border: 1px solid #29405a; border-radius: 2px; }"
                f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
            )

    def _apply_meter_segments(self, truth: PageTruthSnapshot) -> None:
        field = truth.fields["antenna_guide.meter_segments"]
        states = tuple(field.value) if isinstance(field.value, (list, tuple)) else ()
        for index, segment in enumerate(self._meter_segments):
            lit = bool(states[index]) if index < len(states) else False
            color = "#1d4ed8" if lit else "#080d12"
            segment.setStyleSheet(f"border: 1px solid #d9e2ec; background: {color};")


class DashboardPageRenderer(QWidget):
    """Render Dashboard geometry from one complete PageTruth snapshot."""

    def __init__(self, interactions: NGUIInteractionRegister) -> None:
        super().__init__()
        specs = _spec_index()
        _validate_field_contract(specs)
        self._declared_field_ids = frozenset(specs)
        self._regions = {
            "details": DashboardMatrixRegion(
                "",
                DETAILS_MATRIX,
                specs,
                margins=(10, 0, 10, 4),
                horizontal_spacing=12,
                vertical_spacing=0,
                line_height=22,
                label_overrides={
                    "radio.ca_active": "CA",
                    "radio.carrier_details": "CA details",
                    "radio.transmission_mode": "LTE MODE",
                    "nr.mode": "NR MODE",
                    "radio.dl_bandwidth_mhz": "DL BW",
                    "radio.ul_bandwidth_mhz": "UL BW",
                    "radio.dl_frequency_mhz": "DL freq",
                    "radio.ul_frequency_mhz": "UL freq",
                    "nr.bandwidth_mhz": "DL BW",
                    "nr.ul_bandwidth_mhz": "UL BW",
                    "nr.dl_frequency_mhz": "DL freq",
                    "nr.ul_frequency_mhz": "UL freq",
                },
                value_spans={"radio.neighbors": 7, "radio.ca_pool": 7,
                             "radio.ca_active": 3, "radio.carrier_details": 3},
                headings=((3, 0, "LTE"), (3, 2, "NR")),
                stacked_fields=("radio.carrier_details", "radio.dl_bandwidth_mhz", "radio.ul_bandwidth_mhz",
                                "nr.bandwidth_mhz", "nr.ul_bandwidth_mhz",
                                "radio.dl_frequency_mhz", "radio.ul_frequency_mhz",
                                "nr.dl_frequency_mhz", "nr.ul_frequency_mhz"),
            ),
            "signals": DashboardMatrixRegion(
                "",
                SIGNAL_MATRIX,
                specs,
                margins=(10, 0, 10, 4),
                horizontal_spacing=12,
                vertical_spacing=0,
                line_height=22,
                headings=((0, 0, "LTE RF"), (0, 1, "NR RF"),
                          (0, 2, "LTE Tx pwr"), (0, 3, "NR Tx pwr")),
                label_overrides={"nr.sinr_db": "SINR", "nr.rsrp_dbm": "RSRP",
                                 "nr.rsrq_db": "RSRQ", "nr.tx_power_dbm": "PUCCH",
                                 "rf.tx_ppusch": "PUSCH", "rf.tx_ppucch": "PUCCH",
                                 "rf.tx_psrs": "SRS", "rf.tx_pprach": "PRACH"},
            ),
            "provider_cell": DashboardMatrixRegion(
                "PROVIDER / CONNECTION",
                (("provider.name", None), ("radio.network_type", None),
                 ("radio.rrc", None), (None, None),
                 ("radio.pci", "nr.pci"), ("radio.earfcn", "nr.arfcn"),
                 ("radio.cell_id", "nr.cell_id"), ("radio.ri", "nr.ri"),
                 ("radio.enb_id", None)),
                specs, margins=(10, 4, 10, 8), horizontal_spacing=6,
                vertical_spacing=3, row_min_height=22,
                headings=((3, 0, "LTE"), (3, 1, "NR")),
                label_overrides={"nr.pci": "PCI", "nr.arfcn": "ARFCN",
                                 "nr.cell_id": "Cell ID", "nr.ri": "RI"},
                value_spans={"provider.name": 3, "radio.network_type": 3,
                             "radio.rrc": 3, "radio.enb_id": 3},
            ),
            "quality": DashboardFieldRegion(
                "",
                REGION_FIELDS["quality"],
                specs,
                margins=(8, 5, 8, 5),
                horizontal_spacing=16,
                vertical_spacing=1,
                row_min_height=18,
                label_overrides={
                    "quality.ri_stability_pct": "LTE RI stability",
                    "quality.ri_activity": "LTE RI activity",
                    "quality.ca_ld_pct": "CAST_LD",
                    "quality.ca_ld_activity": "CART_LD activity",
                },
            ),
            "antenna": DashboardAntennaGuideRegion(),
            "events": DashboardRecentEventsRegion(),
        }
        self._active_antenna_guide_action = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 12, 0, 0)
        root.setSpacing(0)
        self.site_report_bar = self._build_site_report_bar(interactions)
        self.navigation_chrome = self.site_report_bar

        self.root_splitter = QSplitter(Qt.Horizontal)
        self.root_splitter.setObjectName("ngui_dashboard_root_splitter")
        self.root_splitter.setChildrenCollapsible(False)

        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.setObjectName("ngui_dashboard_left_splitter")
        self.left_splitter.setChildrenCollapsible(False)
        self.left_splitter.setHandleWidth(1)

        self.top_splitter = QSplitter(Qt.Horizontal)
        self.top_splitter.setObjectName("ngui_dashboard_top_splitter")
        self.top_splitter.setChildrenCollapsible(False)

        signal_stack = QWidget()
        signal_stack.setObjectName("ngui_dashboard_signal_stack")
        signal_layout = QVBoxLayout(signal_stack)
        signal_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        signal_layout.setContentsMargins(0, 0, 0, 0)
        signal_layout.setSpacing(0)
        signal_layout.addWidget(self._regions["details"], self._regions["details"].line_units)
        signal_layout.addWidget(self._regions["signals"], self._regions["signals"].line_units)
        signal_stack.setMinimumWidth(600)
        antenna = self._build_antenna_area(interactions)
        self.antenna_area = self._build_bottom_region(antenna, top=0)
        self.antenna_area.setMinimumWidth(360)
        self.left_splitter.addWidget(signal_stack)
        self.left_splitter.addWidget(self.antenna_area)
        self.left_splitter.setStretchFactor(0, 3)
        self.left_splitter.setStretchFactor(1, 2)
        self.left_splitter.setSizes([450, 300])

        # One continuous connection column: identity above quality/activity.
        self.bottom_splitter = QSplitter(Qt.Vertical)
        self.bottom_splitter.setObjectName("ngui_dashboard_connection_splitter")
        self.bottom_splitter.setChildrenCollapsible(False)
        self.quality_area = self._build_bottom_region(self._regions["quality"], top=0)
        self.bottom_splitter.addWidget(self._regions["provider_cell"])
        self.bottom_splitter.addWidget(self.quality_area)
        self.bottom_splitter.setStretchFactor(0, 1)
        self.bottom_splitter.setStretchFactor(1, 1)
        self.bottom_splitter.setSizes([375, 375])
        self.top_splitter.addWidget(self.left_splitter)
        self.top_splitter.addWidget(self.bottom_splitter)
        self.top_splitter.setStretchFactor(0, 2)
        self.top_splitter.setStretchFactor(1, 1)
        self.top_splitter.setSizes([800, 400])

        self.events_area = self._build_events_area(interactions)
        self.events_area.setMinimumWidth(280)
        self.events_area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        self.root_splitter.addWidget(self.top_splitter)
        self.root_splitter.addWidget(self.events_area)
        self.root_splitter.setStretchFactor(0, 7)
        self.root_splitter.setStretchFactor(1, 3)
        self.root_splitter.setSizes([950, 350])
        root.addWidget(self.root_splitter, 1)

    def _build_site_report_bar(self, interactions: NGUIInteractionRegister) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ngui_dashboard_site_report_bar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(6)
        layout.addStretch(1)
        self.generate_site_report = QPushButton("SITE REPORT")
        self.generate_site_report.setCheckable(True)
        self.generate_site_report.setObjectName("ngui_site_report_generate")
        self.generate_site_report.setProperty("nguiRole", "user_action")
        bind_button(
            self.generate_site_report,
            interactions,
            IntentKind.CREATE_SITE_REPORT,
        )
        layout.addWidget(self.generate_site_report)
        self.site_report_buttons: dict[str, QPushButton] = {}
        self.site_report_button_group = QButtonGroup(self)
        self.site_report_button_group.setExclusive(True)
        for window in SITE_REPORT_WINDOWS:
            button = QPushButton(window)
            button.setCheckable(True)
            button.setProperty("nguiRole", "user_action")
            button.setObjectName(f"ngui_site_report_{window}")
            bind_button(
                button,
                interactions,
                IntentKind.SELECT_SITE_REPORT_WINDOW,
                lambda window=window: {"window": window},
            )
            layout.addWidget(button)
            self.site_report_button_group.addButton(button)
            self.site_report_buttons[window] = button
        return bar

    def _build_antenna_area(self, interactions: NGUIInteractionRegister) -> QWidget:
        area = QGroupBox("")
        area.setObjectName("ngui_dashboard_antenna_area")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(17, 12, 17, 4)
        layout.setSpacing(1)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        self.antenna_guide_buttons: dict[str, QPushButton] = {}
        for label, action in ANTENNA_GUIDE_ACTIONS:
            button = QPushButton(label)
            button.setObjectName(f"ngui_antenna_guide_{action}")
            button.setProperty("nguiRole", "user_action")
            button.setCheckable(action != "stop")
            tooltip = {
                    "start": "Measure the first position, then compare antenna movement",
                    "stop": "Stop and clear the current antenna guide session",
                }[action]
            button.setToolTip(tooltip)
            button.setProperty("nguiBaseToolTip", tooltip)
            bind_button(
                button,
                interactions,
                IntentKind.ANTENNA_GUIDE_ACTION,
                lambda action=action: self._antenna_guide_payload(action),
                family=(
                    ControlFamily.MOMENTARY
                    if action == "stop"
                    else ControlFamily.READBACK_TOGGLE
                ),
            )
            buttons.addWidget(button, 1)
            self.antenna_guide_buttons[action] = button
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Monitoring:"))
        monitor_state = self._regions["antenna"].monitor_state_label
        monitor_state.setObjectName("nguiDashboardMonitorStatus")
        monitor_state.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        buttons.addWidget(monitor_state, 1)
        layout.addLayout(buttons)
        layout.addWidget(self._regions["antenna"].command_status_label)
        heading = QLabel("ANTENNA GUIDE")
        heading.setObjectName("nguiDashboardAntennaHeading")
        layout.addWidget(heading)
        layout.addWidget(self._regions["antenna"], 1)
        return area

    @staticmethod
    def _build_bottom_region(content: QWidget, *, top: int) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, top, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(content, 1)
        return area

    def _build_events_area(self, interactions: NGUIInteractionRegister) -> QWidget:
        del interactions
        area = QWidget()
        area.setObjectName("ngui_dashboard_recent_events")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._regions["events"], 1)
        return area

    @property
    def regions(self) -> Mapping[str, DashboardFieldRegion]:
        return self._regions

    @property
    def visible_field_ids(self) -> frozenset[str]:
        return frozenset().union(
            *(region.field_ids for region in self._regions.values()),
            frozenset(SITE_REPORT_CONTROL_FIELDS),
        )

    @property
    def intentionally_absent_field_ids(self) -> frozenset[str]:
        return INTENTIONALLY_ABSENT_FIELDS

    @property
    def field_ids(self) -> frozenset[str]:
        """Return all PageTruth fields used by this renderer."""

        return self._declared_field_ids

    def render(self, truth: PageTruthSnapshot) -> None:
        for region in self._regions.values():
            region.render(truth)
        self._render_site_report_controls(truth)
        self._render_antenna_guide_controls(truth)

    def _render_site_report_controls(self, truth: PageTruthSnapshot) -> None:
        selected = str(truth.fields["site_report.selected_window"].text or "").strip()
        if selected in {"", "----"}:
            selected = "1d"
        for window, button in self.site_report_buttons.items():
            checked = window == selected
            if button.isChecked() != checked:
                button.setChecked(checked)
        lifecycle = truth.fields["utility.lifecycle"].value
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        raw = lifecycle.get(IntentKind.CREATE_SITE_REPORT.value, {})
        row = raw if isinstance(raw, Mapping) else {}
        pending = str(row.get("state") or "") in {"queued", "running"}
        self.generate_site_report.setChecked(pending)
        self.generate_site_report.setEnabled(not pending)
        for button in self.site_report_buttons.values():
            button.setEnabled(not pending)

    def _render_antenna_guide_controls(self, truth: PageTruthSnapshot) -> None:
        active = str(truth.fields["antenna_guide.active_action"].value or "").strip()
        enabled = bool(truth.fields["antenna_guide.actions_enabled"].value)
        availability_field = truth.fields["action.control_availability"]
        decision = control_availability_from_mapping(
            availability_field.value
            if isinstance(availability_field.value, Mapping)
            else None,
            WORKFLOW_CONTROL,
        )
        self._active_antenna_guide_action = active
        for action, button in self.antenna_guide_buttons.items():
            button.setText("STOP GUIDE" if active else "START GUIDE")
            button.setEnabled(decision.enabled and (enabled or bool(active)))
            button.setToolTip(
                str(button.property("nguiBaseToolTip") or "")
                if decision.enabled
                else decision.reason
            )
            checked = action == active and action != "stop"
            if button.isChecked() != checked:
                button.setChecked(checked)

    def _antenna_guide_payload(self, action: str) -> dict[str, str]:
        if action != "stop" and self._active_antenna_guide_action == action:
            return {"action": "stop"}
        return {"action": action}
