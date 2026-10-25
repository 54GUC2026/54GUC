"""Render the user-facing New Horizon advisory page."""

from __future__ import annotations
from arch4.intelligence_presentation_contract import saved_radio_label

from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..front_register import PageTruthSnapshot, PresentationState
from ..interactions import IntentKind, NGUIInteractionRegister
from ..page_owners import require_exact_page_truth
from ..registry import fields_for_page
from ..types import PageId
from .interactions import bind_button


_CURRENT_DECISION_FIELDS = (
    "intelligence.state",
    "intelligence.selected_advisory",
    "intelligence.decision_reason",
)

_LEARNING_UPDATE_FIELDS = (
    "intelligence.learning",
)

_HISTORY_STATUS_FIELDS = frozenset(
    {
        "intelligence.history_health",
        "intelligence.history_continuity",
    }
)

# Keep these owner facts in PageTruth for diagnostics and other NGUI users, but
# do not show them as page status. Product Matrix transport details are not
# useful on the operator dashboard.
_NON_VISIBLE_FIELDS = frozenset(
    {
        "intelligence.explore_session_id",
        "intelligence.explore_deadline_at",
        "intelligence.explore_reason",
        "intelligence.control_available",
        "intelligence.control_mode",
        "intelligence.control_reason",
        "intelligence.qualification_remaining_s",
        "intelligence.evidence_count",
        "intelligence.autopilot_requested",
        "intelligence.explore_requested",
        "intelligence.supervision",
        "intelligence.bouncer_status",
        "intelligence.bandit_status",
        "intelligence.referee_summary",
    }
)

_LEARNING_CYCLE_HISTORY_FIELD = "intelligence.learning_cycle_history"


_GRADE_COLORS = {"improved": "#32d17b", "neutral": "#ffbf47", "worse": "#ff5c5c", "unavailable": "#e6edf0"}


class _GradePartsDelegate(QStyledItemDelegate):
    """Paint owner-supplied AQ and change tones independently, without grading."""

    def paint(self, painter, option, index):
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        document.setHtml(index.data(Qt.ItemDataRole.UserRole) or "")
        document.setTextWidth(max(1, option.rect.width() - 6))
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        option.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget)
        painter.save()
        painter.setClipRect(option.rect)
        painter.translate(option.rect.left() + 3, option.rect.top() + max(0, (option.rect.height() - document.size().height()) / 2))
        document.drawContents(painter)
        painter.restore()


class _LearningCycleTable(QTableWidget):
    """Give five recent results room to wrap, keeping older rows scrollable."""

    def resizeEvent(self, event) -> None:
        follow_latest = (
            self.verticalScrollBar().value() == self.verticalScrollBar().maximum()
        )
        super().resizeEvent(event)
        self.fit_visible_rows()
        if follow_latest:
            self.scrollToBottom()

    def fit_visible_rows(self) -> None:
        self.verticalHeader().setMinimumSectionSize(
            max(30, self.viewport().height() // 5) if self.rowCount() >= 5 else 30
        )
        self.doItemsLayout()


class _LearningCycleHistory(QWidget):
    """Render bounded history without recalculating learning state."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.table = _LearningCycleTable(0, 6)
        self.table.setObjectName("nguiIntelligenceLearningCycleHistory")
        self.table.setItemDelegateForColumn(4, _GradePartsDelegate(self.table))
        self.table.setItemDelegateForColumn(5, _GradePartsDelegate(self.table))
        self.table.setHorizontalHeaderLabels(
            (
                "DATE / TIME",
                "BAND / CA / CELL / PCI / NR",
                "WHY",
                "EXPECTED",
                "ACTUAL GRADE",
                "RESULT",
            )
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(True)
        self.table.setShowGrid(True)
        self.table.setGridStyle(Qt.PenStyle.SolidLine)
        self.table.setStyleSheet(
            "QTableWidget#nguiIntelligenceLearningCycleHistory "
            "{ gridline-color: #274650; }"
        )
        header = self.table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        # Keep the total fixed width stable.  The radio context may wrap, while
        # the result columns need enough horizontal room to keep their endings
        # readable.  Result remains the flexible last column.
        for column, width in enumerate((90, 495, 310, 165, 220)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(130)
        self.table.setMaximumHeight(190)
        vertical_header = self.table.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset({_LEARNING_CYCLE_HISTORY_FIELD})

    def render(self, value: object) -> None:
        follow_latest = (
            self.table.verticalScrollBar().value()
            == self.table.verticalScrollBar().maximum()
        )
        rows = value.get("rows") if isinstance(value, Mapping) else None
        rows = rows if isinstance(rows, Sequence) and not isinstance(rows, str) else ()
        self.table.clearContents()
        self.table.setRowCount(max(5, len(rows)))
        for row_index, row in enumerate(rows):
            assert isinstance(row, Mapping)
            decided = datetime.fromtimestamp(float(row["decided_at"]))
            completed = datetime.fromtimestamp(float(row["completed_at"]))
            completion_text = (
                completed.strftime("%H:%M")
                if completed.date() == decided.date()
                else completed.strftime("%d %b %y %H:%M").upper()
            )
            actual_grade = row["actual_grade"]
            if row["impact_result"] is not None:
                actual_grade += "\n" + row["impact_result"]
            values = (
                decided.strftime("%d %b %y\n%H:%M").upper()
                + " -> "
                + completion_text,
                "BEFORE: " + saved_radio_label(row["before_context"], row["before_nr"]) + "\n"
                "AFTER: " + saved_radio_label(row["after_context"], row["after_nr"]),
                row["why"],
                row["expected"],
                actual_grade,
                row["result_detail"],
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    (
                        Qt.AlignmentFlag.AlignHCenter
                        if column in {0, 3, 4}
                        else Qt.AlignmentFlag.AlignLeft
                    )
                    | Qt.AlignmentFlag.AlignVCenter
                )
                if column == 4:
                    parts = row["grade_parts"]
                    spans = []
                    for part_index, part in enumerate(parts):
                        if part_index:
                            spans.append("<br>" if part["text"] == row["impact_result"] else " · ")
                        spans.append('<span style="color:' + _GRADE_COLORS[part["outcome"]] + '">' + escape(part["text"]) + '</span>')
                    item.setData(Qt.ItemDataRole.UserRole, '<div style="text-align:center">' + ''.join(spans) + '</div>')
                elif column == 5:
                    spans = ['<span style="color:' + _GRADE_COLORS[part["outcome"]] + '">' + escape(part["text"]) + '</span>' for part in row["result_parts"]]
                    item.setData(Qt.ItemDataRole.UserRole, '<div>' + ' · '.join(spans) + '</div>')
                self.table.setItem(row_index, column, item)
        self.table.fit_visible_rows()
        if follow_latest:
            self.table.scrollToBottom()


class _IntelligenceImpactPanel(QWidget):
    """Render impact counts without regrading evidence."""

    _COLORS = {
        "improved": "#32d17b",
        "neutral": "#ffbf47",
        "worse": "#ff5c5c",
    }

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.summary = QLabel("Impact evidence is not available yet.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.summary.hide()

        # Three small bars are easier to read in this half-width panel than one
        # proportional strip.
        self.bars: dict[str, QProgressBar] = {}
        self.bar_labels: dict[str, QLabel] = {}
        for name, color in self._COLORS.items():
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            label = QLabel(name.upper())
            label.setObjectName("nguiIntelligenceImpact" + name.title() + "Label")
            label.setMinimumWidth(68)
            bar = QProgressBar()
            bar.setObjectName("nguiIntelligenceImpact" + name.title())
            bar.setTextVisible(False)
            bar.setFixedHeight(9)
            bar.setStyleSheet(
                "QProgressBar { background-color: #33434b; border: 0; }"
                f"QProgressBar::chunk {{ background-color: {color}; }}"
            )
            row.addWidget(label)
            row.addWidget(bar, 1)
            layout.addLayout(row)
            self.bar_labels[name] = label
            self.bars[name] = bar

        self.latest = QLabel(
            "LATEST OUTCOME   No completed attributable result yet."
        )
        self.latest.setWordWrap(True)
        self.latest.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.latest)
        self.latest.hide()

    @staticmethod
    def _metric(vector: object, name: str, digits: int) -> str:
        value = vector.get(name) if isinstance(vector, Mapping) else None
        return (
            f"{float(value):.{digits}f}"
            if type(value) in {int, float} and not isinstance(value, bool)
            else "----"
        )

    def render(self, impact: object) -> None:
        if impact is None:
            self.summary.setText("Impact evidence is not available yet.")
            self.latest.setText(
                "LATEST OUTCOME   No completed attributable result yet."
            )
            self._render_bar({name: 0 for name in self._COLORS})
            return
        if not isinstance(impact, Mapping):
            raise ValueError("intelligence impact mapping required")
        counts = impact["counts"]
        forecast = impact["forecast_direction"]
        if not isinstance(counts, Mapping) or not isinstance(forecast, Mapping):
            raise ValueError("intelligence impact summary invalid")
        accuracy = (
            f"{forecast['correct']} of {forecast['assessed']}"
            if int(forecast["assessed"]) > 0
            else "Not proven"
        )
        self.summary.setText(
            f"Improved {counts['improved']}   ·   Neutral {counts['neutral']}   ·   "
            f"Worse {counts['worse']}   ·   Not proven {counts['not_proven']}   ·   "
            f"Forecast direction correct {accuracy}"
        )
        self._render_bar(counts)
        recent_outcomes = impact["recent_outcomes"]
        if not isinstance(recent_outcomes, Sequence) or isinstance(
            recent_outcomes,
            (str, bytes),
        ):
            raise ValueError("intelligence recent impact outcomes invalid")
        if not recent_outcomes:
            self.latest.setText(
                "LATEST OUTCOME   No completed attributable result yet."
            )
            return
        latest = recent_outcomes[-1]
        if not isinstance(latest, Mapping):
            raise ValueError("intelligence latest impact invalid")
        baseline = latest["baseline_vector"]
        observed = latest["observed_vector"]
        actor = (
            "Brain action"
            if latest["attribution"] == "brain_action"
            else "Validated hold"
        )
        stamp = datetime.fromtimestamp(float(latest["completed_at"])).strftime(
            "%d %b %H:%M"
        ).upper()
        self.latest.setText(
            f"LATEST OUTCOME   {stamp} · {actor} · {saved_radio_label(latest['before_text'], latest['before_nr'])} → "
            f"{saved_radio_label(latest['after_text'], latest['after_nr'])} · ECI {self._metric(baseline, 'eci_points', 1)} → "
            f"{self._metric(observed, 'eci_points', 1)} · Latency "
            f"{self._metric(baseline, 'latency_ms', 0)} → "
            f"{self._metric(observed, 'latency_ms', 0)} ms · Jitter "
            f"{self._metric(baseline, 'jitter_ms', 1)} → "
            f"{self._metric(observed, 'jitter_ms', 1)} ms · CQ "
            f"{self._metric(baseline, 'cqb_cell', 2)} → "
            f"{self._metric(observed, 'cqb_cell', 2)} · Service impact "
            f"{float(latest['impact_grade']):+.1f}"
        )

    def _render_bar(self, counts: Mapping[str, object]) -> None:
        total = sum(int(counts[name]) for name in self._COLORS)
        maximum = max(total, 1)
        for name, bar in self.bars.items():
            count = int(counts[name])
            self.bar_labels[name].setText(f"{name.upper()} {count}")
            bar.setRange(0, maximum)
            bar.setValue(count)


class IntelligencePage(QWidget):
    def __init__(self, interactions: NGUIInteractionRegister) -> None:
        super().__init__()
        if not isinstance(interactions, NGUIInteractionRegister):
            raise TypeError("intelligence_page_interaction_register_required")
        self._labels: dict[str, QLabel] = {}
        self._latest_text: dict[str, str] = {}
        self.learning_cycle_history = _LearningCycleHistory()
        self.impact_panel = _IntelligenceImpactPanel()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        specs = {
            spec.field_id: spec
            for spec in fields_for_page(PageId.INTELLIGENCE)
        }

        decision = QHBoxLayout()
        decision.setContentsMargins(4, 0, 4, 0)
        decision.setSpacing(8)
        current_labels = ("", "ACTION", "WHY")
        for column, (field_id, label_text) in enumerate(
            zip(_CURRENT_DECISION_FIELDS, current_labels, strict=True),
            start=1,
        ):
            if label_text:
                decision.addWidget(QLabel(label_text + ":"))
            value = QLabel("----")
            value.setObjectName(f"value__{field_id.replace('.', '__')}")
            value.setWordWrap(False)
            decision.addWidget(value)
            if column < len(_CURRENT_DECISION_FIELDS):
                decision.addWidget(QLabel(";"))
            self._labels[field_id] = value
        decision.addStretch(1)
        root.addLayout(decision)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grade_box = QGroupBox("LEARNING CYCLE & PROOF")
        grade_layout = QVBoxLayout(grade_box)
        grade_layout.setContentsMargins(8, 10, 8, 8)
        grade_layout.addWidget(self.learning_cycle_history)
        self.learning_cycle_history.table.setMinimumHeight(220)
        self.learning_cycle_history.table.setMaximumHeight(16777215)
        grid.addWidget(grade_box, 0, 0, 1, 4)

        history = QGroupBox("LEARNING UPDATE & HISTORY")
        history.setObjectName("nguiIntelligenceHistoryRegion")
        history.setMaximumHeight(155)
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(8, 10, 8, 8)
        history_layout.setSpacing(6)

        learning_grid = QGridLayout()
        for row, field_id in enumerate(_LEARNING_UPDATE_FIELDS):
            spec = specs[field_id]
            name = QLabel(spec.label + ":")
            value = QLabel("----")
            value.setWordWrap(True)
            value.setObjectName(f"value__{field_id.replace('.', '__')}")
            learning_grid.addWidget(name, row, 0)
            learning_grid.addWidget(value, row, 1)
            self._labels[field_id] = value
        learning_grid.setColumnStretch(1, 1)
        history_layout.addLayout(learning_grid)

        self.history_status = QLabel("Retained for up to 14 days.")
        self.history_status.setWordWrap(True)
        self.history_status.setObjectName("intelligenceHistoryStatus")
        history_layout.addWidget(self.history_status)

        self.check_history_button = QPushButton("VERIFY CURRENT HISTORY")
        self.check_history_button.setObjectName(
            "nguiIntelligenceCheckLiveHistory"
        )
        self.check_history_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        bind_button(
            self.check_history_button,
            interactions,
            IntentKind.SHOW_INTELLIGENCE_HISTORY_STATUS,
            lambda: {"view": "history"},
            local_action=self._show_live_history,
        )

        self.check_import_button = QPushButton("CHECK PREVIOUS VERSION IMPORT")
        self.check_import_button.setObjectName(
            "nguiIntelligenceCheckImportStatus"
        )
        self.check_import_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        bind_button(
            self.check_import_button,
            interactions,
            IntentKind.SHOW_INTELLIGENCE_HISTORY_STATUS,
            lambda: {"view": "import"},
            local_action=self._show_import_status,
        )
        history_buttons = QHBoxLayout()
        history_buttons.addWidget(self.check_history_button)
        history_buttons.addWidget(self.check_import_button)
        history_buttons.addStretch(1)
        history_layout.addLayout(history_buttons)

        status = QGroupBox("BRAIN STATUS")
        status.setObjectName("nguiIntelligenceStatusRegion")
        status.setMinimumHeight(316)
        status_layout = QVBoxLayout(status)
        status_box = QFrame()
        status_box.setObjectName("nguiIntelligenceStatusLogBox")
        status_box.setFrameShape(QFrame.Shape.StyledPanel)
        status_box.setMinimumWidth(280)
        status_box.setMinimumHeight(110)
        status_box_layout = QVBoxLayout(status_box)
        status_box_layout.setContentsMargins(10, 10, 10, 10)
        self.status_messages = QPlainTextEdit("----")
        self.status_messages.setObjectName("nguiIntelligenceStatusLog")
        self.status_messages.setReadOnly(True)
        self.status_messages.setFrameShape(QFrame.Shape.NoFrame)
        status_box_layout.addWidget(self.status_messages, 1)
        status_layout.addWidget(status_box, 1)
        impact = QGroupBox("BRAIN IMPACT — LAST 14 DAYS")
        impact.setObjectName("nguiIntelligenceImpactRegion")
        impact_layout = QVBoxLayout(impact)
        impact_layout.setContentsMargins(10, 7, 10, 7)
        impact_layout.addWidget(self.impact_panel)

        grid.addWidget(impact, 1, 0, 1, 2)
        grid.addWidget(history, 2, 0, 1, 2)
        grid.addWidget(status, 1, 2, 2, 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 0)
        grid.setRowStretch(2, 0)
        root.addLayout(grid, 1)

        if set(self.field_ids) != set(specs):
            raise RuntimeError("intelligence_page_region_denominator_mismatch")

    def _show_live_history(self) -> None:
        health = self._latest_text.get("intelligence.history_health", "----")
        evidence = self._latest_text.get("intelligence.evidence_count", "----")
        if health == "----":
            self.history_status.setText("History is not available yet.")
            return
        self.history_status.setText(
            f"History: {health} · {evidence} evidence records."
        )

    def _show_import_status(self) -> None:
        continuity = self._latest_text.get(
            "intelligence.history_continuity",
            "----",
        )
        if continuity == "----":
            self.history_status.setText(
                "Previous-version history is not available yet."
            )
            return
        self.history_status.setText("Previous version: " + continuity + ".")

    @property
    def field_ids(self) -> frozenset[str]:
        return (
            frozenset(self._labels)
            | self.learning_cycle_history.field_ids
            | _HISTORY_STATUS_FIELDS
            | _NON_VISIBLE_FIELDS
            | {
                "intelligence.control_status_message",
                "intelligence.impact",
                "intelligence.status_messages",
            }
        )

    @property
    def visible_field_ids(self) -> frozenset[str]:
        return self.field_ids - _NON_VISIBLE_FIELDS

    @property
    def intentionally_absent_field_ids(self) -> frozenset[str]:
        return _NON_VISIBLE_FIELDS

    def render(self, truth: PageTruthSnapshot) -> None:
        accepted = require_exact_page_truth(truth, PageId.INTELLIGENCE)
        if set(accepted.fields) != set(self.field_ids):
            raise ValueError("intelligence page field denominator mismatch")
        for field_id, label in self._labels.items():
            value = accepted.fields[field_id]
            text = (
                value.text
                if value.state is PresentationState.VALID
                else value.text or "----"
            )
            label.setText(text)
            self._latest_text[field_id] = text
        for field_id in _HISTORY_STATUS_FIELDS:
            value = accepted.fields[field_id]
            self._latest_text[field_id] = (
                value.text
                if value.state is PresentationState.VALID
                else value.text or "----"
            )
        for field_id in _NON_VISIBLE_FIELDS:
            value = accepted.fields[field_id]
            self._latest_text[field_id] = (
                value.text
                if value.state is PresentationState.VALID
                else value.text or "----"
            )
        history = accepted.fields[_LEARNING_CYCLE_HISTORY_FIELD]
        impact = accepted.fields["intelligence.impact"]
        self.impact_panel.render(
            impact.value if impact.state is PresentationState.VALID else None,
        )
        control_status = accepted.fields[
            "intelligence.control_status_message"
        ]
        intelligence_status = accepted.fields["intelligence.status_messages"]
        status_parts = tuple(
            text
            for text in (
                control_status.text.strip(),
                intelligence_status.text.strip(),
            )
            if text
        )
        status_text = "\n".join(status_parts) or "----"
        if self.status_messages.toPlainText() != status_text:
            self.status_messages.setPlainText(status_text)
            scrollbar = self.status_messages.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        self.learning_cycle_history.render(
            history.value if history.state is PresentationState.VALID else None
        )


__all__ = ("IntelligencePage",)
