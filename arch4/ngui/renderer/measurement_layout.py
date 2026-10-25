"""Presentation primitives shared by Dashboard and Developer Control.

These widgets display already formatted PageTruth. They never infer a missing
measurement or interpret a modem/backend mapping.
"""

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..front_register import PageTruthSnapshot, PresentationState


def render_nr_support(truth: PageTruthSnapshot, labels: dict[str, QLabel],
                      headings: list[tuple[QLabel, str]]) -> None:
    """Collapse confirmed NR-wide unsupported truth; never infer it from silence."""
    fields = [field for key, field in truth.fields.items() if key.startswith("nr.") and key not in {"nr.nsa_active", "nr.sa_active", "nr.cells"}]
    unsupported = bool(fields) and all(field.state is PresentationState.UNSUPPORTED for field in fields)
    for heading, normal_text in headings:
        heading.setText(normal_text + " UNSUP." if unsupported else normal_text)
    if unsupported and headings:
        for key, label in labels.items():
            if key.startswith("nr."):
                label.setText("")


def align_measurement_columns(grid: QGridLayout) -> None:
    """Use the same label, LTE and NR guides in neighbouring panels."""
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(2)
    grid.setColumnMinimumWidth(0, 180)
    grid.setColumnMinimumWidth(1, 55)
    grid.setColumnMinimumWidth(2, 55)
    for column, stretch in enumerate((0, 1, 1)):
        grid.setColumnStretch(column, stretch)
    grid.parentWidget().setMinimumWidth(grid.minimumSize().width())


class PairedMeasurements(QGroupBox):
    """One metric per row, with consistent LTE and NR value columns."""

    def __init__(self, title: str, rows: Iterable[tuple[str, str | None, str | None]],
                 *, notes: Iterable[tuple[str, str]] = ()) -> None:
        super().__init__(title)
        self._labels: dict[str, QLabel] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        for column, text in enumerate(("Measurement", "LTE", "NR")):
            label = QLabel(text)
            label.setProperty("nguiRole", "section_heading")
            grid.addWidget(label, 0, column)
        row_index = 0
        for row_index, (name, lte, nr) in enumerate(rows, 1):
            label = QLabel(name)
            label.setObjectName("truthFieldName")
            grid.addWidget(label, row_index, 0)
            for column, field_id in enumerate((lte, nr), 1):
                value = self._value(field_id)
                grid.addWidget(value, row_index, column)
        for row_index, (name, field_id) in enumerate(notes, row_index + 1):
            label = QLabel(name)
            label.setObjectName("truthFieldName")
            grid.addWidget(label, row_index, 0)
            value = self._value(field_id)
            value.setWordWrap(True)
            grid.addWidget(value, row_index, 1, 1, 2)
        align_measurement_columns(grid)

    def _value(self, field_id: str | None) -> QLabel:
        value = QLabel("---")
        value.setMinimumWidth(55)
        if field_id is not None:
            if field_id in self._labels:
                raise ValueError(f"Repeated measurement sink: {field_id}")
            value.setObjectName(f"value__{field_id.replace('.', '__')}")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._labels[field_id] = value
        return value

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
            label.setToolTip(field.reason if field.state is not PresentationState.VALID else "")


def page_scroll(content: QWidget) -> QScrollArea:
    """Keep all retained controls reachable when a page exceeds the viewport."""
    scroll = QScrollArea()
    scroll.setObjectName("nguiMeasurementScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(content)
    return scroll


def page_heading(title: str, subtitle: str) -> QWidget:
    heading = QWidget()
    layout = QVBoxLayout(heading)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(3)
    label = QLabel(title)
    label.setProperty("nguiRole", "page_heading")
    layout.addWidget(label)
    label = QLabel(subtitle)
    label.setObjectName("truthFieldName")
    label.setWordWrap(True)
    layout.addWidget(label)
    return heading
