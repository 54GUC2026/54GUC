"""Render current and historical Referee assessments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from arch4.intelligence_presentation_contract import (
    BAND_CONTEXT_OUTCOME_DIMENSIONS,
    BAND_CONTEXT_RECENT_LIMIT,
    BAND_CONTEXT_TIMELINE_ROLES,
    project_intelligence_display_grade,
    saved_nr_identity_text,
)

from ..front_register import PageTruthSnapshot, PresentationState
from ..display_text import display_value_text
from ..page_owners import require_exact_page_truth
from ..types import PageId


BAND_CONTEXT_SUMMARY_DIMENSIONS = BAND_CONTEXT_OUTCOME_DIMENSIONS
REFEREE_VISIBLE_ROW_LIMIT = (
    len(BAND_CONTEXT_TIMELINE_ROLES) + BAND_CONTEXT_RECENT_LIMIT
)

_DIMENSION_HEADERS = {
    "performance_capacity": "CAPACITY\nECI",
    "performance_latency": "LATENCY\nms",
    "performance_jitter": "JITTER\nms",
    "performance_communication_quality": "COMM QUALITY\nCQ",
    "performance_internet_availability": "INTERNET\n%",
    "performance_overall_outcome": "ABSOLUTE QUALITY\nAQ",
}
if tuple(_DIMENSION_HEADERS) != BAND_CONTEXT_SUMMARY_DIMENSIONS:
    raise RuntimeError("referee_renderer_dimension_denominator_mismatch")

_HISTORY_CONTEXT_HEADERS = (
    "TIME",
    "PBAND",
    "CA",
    "CELL",
    "PCI",
    "EARFCN",
    "NR",
    "SAMPLES",
)
_FORECAST_CONTEXT_HEADERS = (
    "FORECAST",
    *_HISTORY_CONTEXT_HEADERS[1:-1],
    "PROOFS",
)
_CONTEXT_COLUMN_COUNT = len(_HISTORY_CONTEXT_HEADERS)
_COMPARISON_COLUMN_COUNT = _CONTEXT_COLUMN_COUNT + len(
    BAND_CONTEXT_SUMMARY_DIMENSIONS
)
_SHARED_CONTEXT_COLUMN_WIDTHS = (230, 70, 155, 90, 70, 90, 170, 80)
_METRIC_COLUMN_WIDTHS = (125, 110, 110, 130, 110, 145)
if len(_METRIC_COLUMN_WIDTHS) != len(BAND_CONTEXT_SUMMARY_DIMENSIONS):
    raise RuntimeError("referee_renderer_metric_width_mismatch")

_REFEREE_TABLE_STYLE = (
    "QTableWidget { "
    "gridline-color: #90999e; border: 1px solid #90999e; "
    "background-color: #071116; alternate-background-color: #101c22; "
    "color: #edf5f7; font-size: 9pt; }"
    "QHeaderView, QHeaderView::section { font-size: 9pt; }"
    "QTableWidget::item { background-color: #071116; }"
    "QTableWidget::item:alternate { "
    "background-color: #101c22; }"
    "QTableWidget::item:selected { "
    "color: #edf5f7; background-color: #17333d; }"
)
_REFEREE_HISTORY_MIN_HEIGHT = 190
_REFEREE_HISTORY_MAX_HEIGHT = 430


def _configure_comparison_columns(
    table: QTableWidget,
) -> None:
    """Apply the one comparison grid to observed and forecast values."""

    if table.columnCount() != _COMPARISON_COLUMN_COUNT:
        raise ValueError("referee_comparison_column_denominator_mismatch")
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    table.setWordWrap(True)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    header = table.horizontalHeader()
    widths = _SHARED_CONTEXT_COLUMN_WIDTHS + _METRIC_COLUMN_WIDTHS
    for column, width in enumerate(widths):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    header.setMinimumSectionSize(40)


class _ComparisonTable(QTableWidget):
    """Keep observed and forecast columns aligned at the available width."""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        weights = _SHARED_CONTEXT_COLUMN_WIDTHS + _METRIC_COLUMN_WIDTHS
        available = self.viewport().width()
        if available < len(weights) * 40:
            return
        widths = [available * weight // sum(weights) for weight in weights]
        widths[-1] += available - sum(widths)
        if widths == [self.columnWidth(c) for c in range(self.columnCount())]:
            return
        for column, width in enumerate(widths):
            self.setColumnWidth(column, width)
        self._resize_rows()

    def _resize_rows(self) -> None:
        self.resizeRowsToContents()


def _split_history_target(row: Mapping[str, object]) -> tuple[str, str]:
    """Split the Referee target without adding radio identity."""

    target = str(row["target"]).strip()
    if target == "N/A":
        return "----", "----"
    if " / " not in target:
        if target.startswith("n") and row["nr_band"] is not None:
            return "----", "----"
        raise ValueError("referee_history_target_contract_invalid")
    pband, pool_and_nr = target.split(" / ", 1)
    ca_pool = pool_and_nr.split(" + n", 1)[0].strip()
    if not pband or not ca_pool:
        raise ValueError("referee_history_target_contract_invalid")
    return pband, ca_pool


class RefereeHistoryTable(_ComparisonTable):
    """Show the current window and recent completed assessments."""

    def __init__(self) -> None:
        super().__init__(0, _COMPARISON_COLUMN_COUNT)
        self.setHorizontalHeaderLabels(
            list(_HISTORY_CONTEXT_HEADERS)
            + [
                _DIMENSION_HEADERS[dimension]
                for dimension in BAND_CONTEXT_SUMMARY_DIMENSIONS
            ]
        )
        self.verticalHeader().hide()
        self.setCornerButtonEnabled(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setShowGrid(True)
        self.setStyleSheet(_REFEREE_TABLE_STYLE)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        _configure_comparison_columns(self)
        self.verticalHeader().setMinimumSectionSize(40)
        self.verticalHeader().setDefaultSectionSize(42)

    @staticmethod
    def _identity_values(row: Mapping[str, object]) -> tuple[str, ...]:
        """Return configuration values in header order.

        Referee owns these values. The renderer only splits the target string
        into display columns.
        """

        pband, ca_pool = _split_history_target(row)
        cell_values = tuple(
            str(row[name]) if row[name] is not None else "----"
            for name in ("cell_id", "pci", "earfcn")
        )
        nr = saved_nr_identity_text(row)
        return (
            str(row["label"]),
            pband,
            ca_pool,
            *cell_values,
            nr,
            str(row["evidence_count"]),
        )

    @staticmethod
    def _dimension_item(
        dimension_id: str,
        cell: Mapping[str, object],
    ) -> QTableWidgetItem:
        grade = cell["grade"]
        displayed_grade = project_intelligence_display_grade(grade,dimension=dimension_id)
        display = display_value_text(cell["text"])
        if (
            dimension_id != "performance_overall_outcome"
            and displayed_grade is not None
        ):
            display += f"  {displayed_grade:+.1f}"
        item = QTableWidgetItem(display)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if displayed_grade is not None:
            item.setForeground(
                QColor(
                    "#32d17b"
                    if displayed_grade > 0
                    else "#ff5c5c"
                    if displayed_grade < 0
                    else "#ffbf47"
                )
            )
        item.setToolTip(
            _DIMENSION_HEADERS[dimension_id].replace("\n", " ")
            + " · "
            + str(cell["state"])
        )
        return item

    def render_rows(self, rows: object) -> int:
        follow_latest = (
            self.verticalScrollBar().value() == self.verticalScrollBar().maximum()
        )
        typed_rows = rows if isinstance(rows, (list, tuple)) else ()
        if len(typed_rows) > REFEREE_VISIBLE_ROW_LIMIT:
            raise ValueError("referee_history_visible_row_limit_exceeded")
        self.clearContents()
        self.setRowCount(max(5, len(typed_rows)))
        for row_index, row in enumerate(typed_rows):
            if not isinstance(row, Mapping):
                raise ValueError("referee history typed row required")
            identity_values = self._identity_values(row)
            identity_tooltip = (
                " · ".join(identity_values)
                + "\n"
                + str(row["reason"])
                + "\n"
                + str(row["scope_key"])
            )
            for column_index, text in enumerate(identity_values):
                item = QTableWidgetItem(display_value_text(text))
                item.setTextAlignment(
                    (
                        Qt.AlignmentFlag.AlignLeft
                        if column_index == 0
                        else Qt.AlignmentFlag.AlignHCenter
                    )
                    | Qt.AlignmentFlag.AlignVCenter
                )
                item.setToolTip(identity_tooltip)
                self.setItem(row_index, column_index, item)
            dimensions = row["dimensions"]
            if not isinstance(dimensions, Mapping):
                raise ValueError("referee history dimension mapping required")
            for column_index, dimension_id in enumerate(
                BAND_CONTEXT_SUMMARY_DIMENSIONS,
                start=_CONTEXT_COLUMN_COUNT,
            ):
                cell = dimensions[dimension_id]
                if not isinstance(cell, Mapping):
                    raise ValueError("referee history typed cell required")
                self.setItem(
                    row_index,
                    column_index,
                    self._dimension_item(dimension_id, cell),
                )
        self._resize_rows()
        if follow_latest:
            self.scrollToBottom()
        return len(typed_rows)

    def _resize_rows(self) -> None:
        super()._resize_rows()
        content_height = (
            self.horizontalHeader().height()
            + sum(
                self.rowHeight(row)
                for row in range(max(0, self.rowCount() - 5), self.rowCount())
            )
            + (2 * self.frameWidth())
            + 2
        )
        self.setFixedHeight(
            max(
                _REFEREE_HISTORY_MIN_HEIGHT,
                min(_REFEREE_HISTORY_MAX_HEIGHT, content_height),
            )
        )


class _CurrentForecastPanel(QWidget):
    """Render current New Horizon forecasts without adding inference."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("nguiRefereeCurrentForecastPanel")
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.forecasts = _ComparisonTable(0, _COMPARISON_COLUMN_COUNT)
        self.forecasts.setObjectName("nguiRefereeForecastHistory")
        self.forecasts.setHorizontalHeaderLabels(
            list(_FORECAST_CONTEXT_HEADERS)
            + [
                "ESTIMATED\nAQ" if dimension == "performance_overall_outcome" else _DIMENSION_HEADERS[dimension]
                for dimension in BAND_CONTEXT_SUMMARY_DIMENSIONS
            ]
        )
        self.forecasts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.forecasts.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.forecasts.setShowGrid(True)
        self.forecasts.setAlternatingRowColors(True)
        self.forecasts.setStyleSheet(_REFEREE_TABLE_STYLE)
        self.forecasts.verticalHeader().setVisible(False)
        _configure_comparison_columns(self.forecasts)
        self.forecasts.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.forecasts.setMinimumHeight(230)
        self.forecasts.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumHeight(230)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self.forecasts, 0, 0, 1, 2)
        layout.setColumnStretch(1, 1)

    def render(self, forecast: object) -> None:
        rows = forecast.get("candidates") if isinstance(forecast, Mapping) else None
        rows = rows if isinstance(rows, Sequence) and not isinstance(rows, str) else ()
        table_rows = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            count = int(row["evidence_count"])
            accuracy = row["accuracy_pct"]
            metrics = row["metrics"]

            def number(name: str, digits: int = 1, *, signed: bool = False) -> str:
                value = metrics[name]
                if value is None:
                    return "----"
                if signed:
                    return f"{project_intelligence_display_grade(value, dimension='service_outcome'):+.1f}"
                return f"{float(value):.{digits}f}"

            accuracy_text = (
                "----" if accuracy is None else f"{float(accuracy):.0f}%"
            )
            table_rows.append(
                (
                    f"{str(row['role']).upper()}\nConf {float(row['confidence_pct']):.0f}% / Acc {accuracy_text}",
                    row["band"],
                    row["ca_pool"],
                    row["cell_id"],
                    row["pci"],
                    row["earfcn"],
                    str(row["nr"]),
                    str(count),
                    number("capacity_points"),
                    number("latency_ms", 0),
                    number("jitter_ms"),
                    number("communication_quality", 2),
                    number("internet_availability_pct", 1),
                    number("estimated_aq", signed=True),
                )
            )
        self.forecasts.clearContents()
        self.forecasts.setRowCount(max(5, len(table_rows)))
        for row_index, values in enumerate(table_rows):
            for column, text in enumerate(values):
                item = QTableWidgetItem(display_value_text(text))
                item.setTextAlignment(
                    (
                        Qt.AlignmentFlag.AlignLeft
                        if column in (0, 2)
                        else Qt.AlignmentFlag.AlignHCenter
                    )
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.forecasts.setItem(row_index, column, item)
        self.forecasts._resize_rows()


class RefereePage(QWidget):
    """Render current Referee state and its recent history."""

    FIELD_IDS = frozenset(
        {
            "intelligence.referee_status",
            "intelligence.band_context_evidence",
            "intelligence.forecast",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel("NEW HORIZON — REFEREE AND GRADE HISTORY")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        self.table = RefereeHistoryTable()
        self.table.setObjectName("nguiRefereeHistoryTable")
        root.addWidget(self.table)
        root.addSpacing(12)

        forecast_title = QLabel("CURRENT FORECASTS")
        forecast_title.setObjectName("nguiRefereeCurrentForecastRegion")
        forecast_title.setStyleSheet("color: #d9e2ec; font-weight: bold;")
        root.addWidget(forecast_title)
        self.current_forecast_panel = _CurrentForecastPanel()
        root.addWidget(self.current_forecast_panel, 1)
        forecast_scroll = self.current_forecast_panel.forecasts.horizontalScrollBar()
        self.table.horizontalScrollBar().valueChanged.connect(forecast_scroll.setValue)
        forecast_scroll.valueChanged.connect(self.table.horizontalScrollBar().setValue)

    @property
    def field_ids(self) -> frozenset[str]:
        return self.FIELD_IDS

    @property
    def visible_field_ids(self) -> frozenset[str]:
        return frozenset(
            {
                "intelligence.band_context_evidence",
                "intelligence.forecast",
            }
        )

    @property
    def intentionally_absent_field_ids(self) -> frozenset[str]:
        return frozenset({"intelligence.referee_status"})

    def render(self, truth: PageTruthSnapshot) -> None:
        accepted = require_exact_page_truth(truth, PageId.REFEREE)
        if set(accepted.fields) != set(self.field_ids):
            raise ValueError("referee page field denominator mismatch")
        evidence = accepted.fields["intelligence.band_context_evidence"]
        value = (
            evidence.value
            if evidence.state is PresentationState.VALID
            and isinstance(evidence.value, Mapping)
            else {}
        )
        current = value.get("current_timeline", ())
        history = value.get("recent_assessments", ())
        current_rows = current if isinstance(current, (list, tuple)) else ()
        history_rows = history if isinstance(history, (list, tuple)) else ()
        self.table.render_rows((*history_rows, *current_rows))
        forecast = accepted.fields["intelligence.forecast"]
        self.current_forecast_panel.render(
            forecast.value
            if forecast.state is PresentationState.VALID
            else None,
        )


__all__ = (
    "BAND_CONTEXT_SUMMARY_DIMENSIONS",
    "REFEREE_VISIBLE_ROW_LIMIT",
    "_CurrentForecastPanel",
    "RefereeHistoryTable",
    "RefereePage",
)
