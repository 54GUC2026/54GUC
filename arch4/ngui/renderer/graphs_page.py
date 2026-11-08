"""Render the Graphs page from declared PageTruth fields."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..chart_model import (
    CHART_WINDOW_OPTIONS, DEFAULT_CHART_WINDOW_SECONDS,
    SUPPORTED_CHART_WINDOW_SECONDS, ChartRenderSnapshot,
)
from ..front_register import PageTruthSnapshot
from ..edit_register import NGUIEditRegister
from ..interactions import IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import PageId
from .charts import AtomicChartView
from .confirm_button import NGUIConfirmationCoordinator
from .interactions import bind_button
from .style import FONT_FAMILY, user_button_stylesheet

WINDOWS = MappingProxyType(dict(CHART_WINDOW_OPTIONS))

_USER_BUTTON_STYLE = user_button_stylesheet(font_pt=9,min_height=20,padding="1px 5px",border_radius=3)
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
CELL_INFO_FIELD_IDS = frozenset({
    "radio.pci",
    "radio.cell_id",
    "nr.pci",
    "nr.cell_id",
    "quality.rtt_ms",
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
        self.page = PageId.GRAPHS
        self.controls = self
        self.charts = charts or AtomicChartView()
        self._register = register
        self._build_window_row()
        self.navigation_chrome = self.window_bar
        root = QVBoxLayout(self)
        # Keep the old page inset after removing extra shell layers.
        root.setContentsMargins(6, 2 + self.cell_info.sizeHint().height() + 4, 6, 12)
        root.setSpacing(2)
        root.addWidget(self.charts, 1)
        self._assert_field_contract()

    @property
    def field_ids(self) -> frozenset[str]:
        return (
            self.charts.field_ids
            | CELL_INFO_FIELD_IDS
        )

    @property
    def external_field_ids(self) -> frozenset[str]:
        return HEADER_FIELD_IDS

    @property
    def suppressed_field_ids(self) -> frozenset[str]:
        return SUPPRESSED_CONTEXT_FIELD_IDS

    def render(self, truth: PageTruthSnapshot) -> None:
        self.charts.render_truth(truth)
        self.cell_info.setText(_cell_info_text(truth))

    def render_chart(self, snapshot: ChartRenderSnapshot | None) -> None:
        if snapshot is None:
            return
        if snapshot.window_seconds not in SUPPORTED_CHART_WINDOW_SECONDS:
            raise ValueError("chart snapshot has unsupported operator window")
        if self.charts.render(snapshot):
            self._effective_window_s = snapshot.window_seconds
            self._style_window_buttons(self._effective_window_s)


    def _build_window_row(self) -> None:
        self.window_bar = QWidget()
        row = QHBoxLayout(self.window_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.speed_test = QPushButton("SPEED TEST")
        self.speed_test.setObjectName("nguiGraphsSpeedTest")
        self.speed_test.setFixedHeight(24)
        self.speed_test.setStyleSheet(_USER_BUTTON_STYLE)
        bind_button(self.speed_test, self._register, IntentKind.START_SPEED_TEST)
        row.addWidget(self.speed_test)
        label = QLabel("TIME")
        label.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.Bold))
        label.setStyleSheet("color: #00d37f; font-size: 9pt; font-weight: bold;")
        row.addWidget(label)
        self.window_buttons: dict[int, QPushButton] = {}
        self._effective_window_s = DEFAULT_CHART_WINDOW_SECONDS
        for text, seconds in WINDOWS.items():
            button = QPushButton(text)
            button.setObjectName(f"nguiGraphsWindow_{seconds}")
            button.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.Bold))
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setFixedSize(36, 24)
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
        self.cell_info.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.Bold))
        self.cell_info.setStyleSheet("color: #77a0c5; font-size: 9pt; font-weight: bold;")
        self.cell_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cell_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)


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


__all__ = [
    "CELL_INFO_FIELD_IDS",
    "GraphsPageShell",
    "HEADER_FIELD_IDS",
    "SUPPRESSED_CONTEXT_FIELD_IDS",
    "WINDOWS",
]
