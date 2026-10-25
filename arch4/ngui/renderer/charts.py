"""Render Graphs charts from complete NGUI snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass
from math import floor, isfinite
from typing import Iterable

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsPathItem,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..chart_model import ChartRenderSnapshot, RF_CHART_FIELDS, mobile_data_is_off
from ..front_register import PageTruthSnapshot, PresentationState
from ..registry import fields_for_chart
from ..types import ChartPane, FieldSpec
from .style import FONT_FAMILY

AXIS_WIDTH = 58
LEGEND_WIDTH = 132
MIN_PANE_HEIGHT = 140
PANE_SPACING = 8
PERCENT_MIN = -2.0
PERCENT_MAX = 102.0
OUTAGE_LABEL_TOP_Y = 96.0
OUTAGE_LABEL_LANE_STEP = 11.0
OUTAGE_LABEL_X_OFFSET_RATIO = 0.004
RF_MIN = -125.0
RF_MAX = 25.0
UPPER_OVERLAY_FIELDS = frozenset(
    {
        "quality.congestion_pct",
        "quality.jitter_trimmed_ms",
        "quality.internet_reachable",
    }
)
LOWER_OVERLAY_FIELDS = frozenset({"quality.ca_ld_pct", "quality.cq_pct"})
UPPER_SERIES_ORDER = (
    "traffic.dl_mbps",
    "traffic.ul_mbps",
    "quality.congestion_pct",
    "quality.jitter_trimmed_ms",
    "quality.internet_reachable",
)
LOWER_SERIES_ORDER = (
    "rf.sinr_db",
    "rf.rsrq_db",
    "rf.rsrp_dbm",
    "rf.rssi_dbm",
    "quality.ca_ld_pct",
    "quality.cq_pct",
)
SERIES_WIDTHS = {
    "traffic.dl_mbps": 2.0,
    "traffic.ul_mbps": 2.0,
    "quality.congestion_pct": 1.0,
    "quality.jitter_trimmed_ms": 1.5,
    "quality.internet_reachable": 1.5,
    "rf.sinr_db": 2.0,
    "rf.rsrq_db": 1.5,
    "rf.rsrp_dbm": 1.5,
    "rf.rssi_dbm": 1.5,
    "quality.ca_ld_pct": 2.6,
    "quality.cq_pct": 1.5,
}


@dataclass(slots=True)
class _RenderedPathChunk:
    """One disposable GUI drawing path and the exact segments that produced it."""

    item: QGraphicsPathItem
    segments: tuple[tuple[float, float, float, float], ...]


class _IncrementalSeries:
    """Paint a complete series without rebuilding unchanged time chunks."""

    def __init__(self, view: object, *, pen: QPen, step_left: bool = False) -> None:
        self._view = view
        self._pen = QPen(pen)
        self._step_left = bool(step_left)
        self._chunks: dict[int, _RenderedPathChunk] = {}
        self.last_connections: tuple[int, ...] | None = None
        self.rebuilt_chunks = 0
        self.reused_chunks = 0

    @property
    def chunk_items(self) -> tuple[QGraphicsPathItem, ...]:
        return tuple(chunk.item for chunk in self._chunks.values())

    @property
    def pen(self) -> QPen:
        return QPen(self._pen)

    def is_empty(self) -> bool:
        return not self._chunks or all(
            chunk.item.path().isEmpty() for chunk in self._chunks.values()
        )

    def set_pen(self, pen: QPen) -> None:
        next_pen = QPen(pen)
        if next_pen == self._pen:
            return
        self._pen = next_pen
        for chunk in self._chunks.values():
            chunk.item.setPen(self._pen)

    def clear(self) -> None:
        for chunk in self._chunks.values():
            self._view.removeItem(chunk.item)
        self._chunks.clear()
        self.last_connections = None

    def render(
        self,
        timestamps: tuple[float, ...],
        values: tuple[float, ...],
        *,
        connections: tuple[int, ...] | None,
        chunk_seconds: float,
    ) -> None:
        if len(timestamps) != len(values):
            raise ValueError("chart renderer series length mismatch")
        if connections is not None and len(connections) != len(values):
            raise ValueError("chart renderer connection length mismatch")
        self.last_connections = connections
        grouped: dict[int, list[tuple[float, float, float, float]]] = {}
        for index in range(1, len(timestamps)):
            left_y = float(values[index - 1])
            right_y = float(values[index])
            if not isfinite(left_y) or not isfinite(right_y):
                continue
            if connections is not None and connections[index] != 1:
                continue
            left_x = float(timestamps[index - 1])
            right_x = float(timestamps[index])
            bucket = floor(right_x / chunk_seconds)
            grouped.setdefault(bucket, []).append(
                (left_x, left_y, right_x, right_y)
            )

        expected = {key: tuple(segments) for key, segments in grouped.items()}
        for key in tuple(self._chunks):
            if key not in expected:
                self._view.removeItem(self._chunks.pop(key).item)
        for key, segments in expected.items():
            current = self._chunks.get(key)
            if current is not None and current.segments == segments:
                self.reused_chunks += 1
                continue
            if current is not None:
                self._view.removeItem(current.item)
            path = QPainterPath()
            for left_x, left_y, right_x, right_y in segments:
                path.moveTo(left_x, left_y)
                if self._step_left:
                    path.lineTo(left_x, right_y)
                path.lineTo(right_x, right_y)
            item = QGraphicsPathItem(path)
            item.setPen(self._pen)
            item.setZValue(2.0)
            self._view.addItem(item)
            self._chunks[key] = _RenderedPathChunk(item=item, segments=segments)
            self.rebuilt_chunks += 1


def _finite_connections(values: Iterable[float]) -> tuple[int, ...]:
    """Connect each finite value to the preceding finite value."""

    materialized = tuple(float(value) for value in values)
    return tuple(
        1
        if index > 0
        and isfinite(materialized[index - 1])
        and isfinite(value)
        else 0
        for index, value in enumerate(materialized)
    )


def _render_chunk_seconds(window_seconds: int) -> float:
    """Bound each series to roughly twenty reusable absolute-time chunks."""

    return max(60.0, float(window_seconds) / 20.0)


class TimeAxis(pg.AxisItem):
    """Show owner-selected epoch timestamps as clock time."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        del scale, spacing
        return [time.strftime("%H:%M", time.localtime(value)) for value in values]


class CompactLegend(QWidget):
    """Show complete owner-provided values without deriving signal data."""

    def __init__(self, specs: tuple[FieldSpec, ...]) -> None:
        super().__init__()
        self._specs = {spec.field_id: spec for spec in specs}
        self._labels: dict[str, QLabel] = {}
        self._row_widgets: dict[str, tuple[QWidget, ...]] = {}
        self._row_layouts: list[QHBoxLayout] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(6)
        for spec in specs:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(1)
            swatch = QLabel("-")
            swatch.setFont(QFont(FONT_FAMILY, 13, QFont.Weight.Bold))
            swatch.setStyleSheet(
                f"color: {spec.chart_color}; font-size: 13pt; font-weight: bold;"
            )
            name = QLabel(f"{_legend_name(spec)}=")
            name.setFont(QFont(FONT_FAMILY, 12, QFont.Weight.Bold))
            name.setStyleSheet("color: #77a0c5; font-size: 12pt; font-weight: bold;")
            value = QLabel("----")
            value.setFont(QFont(FONT_FAMILY, 12, QFont.Weight.Bold))
            value.setObjectName(f"legend_{spec.field_id}")
            value.setStyleSheet(
                f"color: {spec.chart_color}; font-size: 12pt; font-weight: bold;"
            )
            row.addWidget(swatch)
            row.addWidget(name)
            row.addWidget(value)
            row.addStretch(1)
            layout.addLayout(row)
            self._row_layouts.append(row)
            self._labels[spec.field_id] = value
            self._row_widgets[spec.field_id] = (swatch, name, value)
        self._mobile_status = QLabel("Mobile disconnected")
        self._mobile_status.setWordWrap(True)
        self._mobile_status.hide()
        layout.addWidget(self._mobile_status)
        layout.addStretch(1)
        self.setFixedWidth(LEGEND_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def content_width(self) -> int:
        """Reserve actual name/value fonts and units, including row margins."""
        layout = self.layout()
        margins = layout.contentsMargins()
        widths = [LEGEND_WIDTH]
        for row, labels in zip(self._row_layouts, self._row_widgets.values()):
            row_margins = row.contentsMargins()
            widths.append(
                margins.left() + margins.right()
                + row_margins.left() + row_margins.right()
                + row.spacing() * (len(labels) - 1)
                + sum(label.sizeHint().width() for label in labels)
            )
        return max(widths)

    def render(self, truth: PageTruthSnapshot) -> None:
        missing = self.field_ids - set(truth.fields)
        if missing:
            raise ValueError(f"chart legend truth missing fields: {sorted(missing)}")
        off = mobile_data_is_off(truth.fields)
        self._mobile_status.setVisible(off and bool(self.field_ids & RF_CHART_FIELDS))
        for field_id, label in self._labels.items():
            text = "--" if off and field_id in RF_CHART_FIELDS else truth.fields[field_id].text
            if label.text() != text:
                label.setText(text)
            label.setToolTip("Mobile disconnected" if off and field_id in RF_CHART_FIELDS else "")

class AtomicChartStack(QWidget):
    """Resize the qualified device's chart panes together."""

    def __init__(self, upper: QWidget, lower: QWidget) -> None:
        super().__init__()
        self.upper_block = upper
        self.lower_block = lower
        self.setMinimumHeight((MIN_PANE_HEIGHT * 2) + PANE_SPACING)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        for child in (upper, lower):
            child.setParent(self)
            child.setMinimumHeight(MIN_PANE_HEIGHT)
            child.show()

    def apply_geometry(self) -> None:
        width = self.width()
        blocks = (self.upper_block, self.lower_block)
        count = len(blocks)
        available = max(count, self.height() - PANE_SPACING * (count - 1))
        height, remainder = divmod(available, count)
        y = 0
        for index, block in enumerate(blocks):
            block_height = height + (index >= count - remainder)
            block.setGeometry(0, y, width, block_height)
            y += block_height + PANE_SPACING

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.apply_geometry()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.apply_geometry()


class AtomicChartView(QWidget):
    """Update one device's GUI panes from one complete ``ChartRenderSnapshot``."""

    def __init__(self) -> None:
        super().__init__()
        pg.setConfigOptions(background="#0b1118", foreground="#d9e2ec", antialias=True)
        self._upper_specs = _ordered_specs(ChartPane.UPPER, UPPER_SERIES_ORDER)
        self._lower_specs = _ordered_specs(ChartPane.LOWER, LOWER_SERIES_ORDER)
        self.upper = self._make_plot(left_label="Mbps")
        self.lower = self._make_plot(left_label="dB / dBm")
        self._upper_overlay = self._make_percent_overlay(self.upper, label="%", color="#ffb000")
        self._lower_overlay = self._make_percent_overlay(self.lower, label="CA %", color="#f000ff")
        self.lower.setYRange(RF_MIN, RF_MAX, padding=0.0)
        self.lower.setXLink(self.upper)

        self._upper_curves: dict[str, _IncrementalSeries] = {}
        for spec in self._upper_specs:
            view = (
                self._upper_overlay
                if spec.field_id in UPPER_OVERLAY_FIELDS
                else self.upper
            )
            curve = _IncrementalSeries(
                view,
                pen=_series_pen(spec),
                step_left=spec.field_id == "quality.internet_reachable",
            )
            self._upper_curves[spec.field_id] = curve

        self._lower_curves: dict[str, _IncrementalSeries] = {}
        for spec in self._lower_specs:
            view = (
                self._lower_overlay
                if spec.field_id in LOWER_OVERLAY_FIELDS
                else self.lower
            )
            curve = _IncrementalSeries(view, pen=_series_pen(spec))
            self._lower_curves[spec.field_id] = curve
        self._cq_spec = next(
            spec for spec in self._lower_specs if spec.field_id == "quality.cq_pct"
        )
        self._cq_stars = pg.ScatterPlotItem(
            symbol="star",
            symbolBrush=pg.mkBrush(self._cq_spec.chart_color),
            symbolPen=pg.mkPen(self._cq_spec.chart_color),
            symbolSize=7,
        )
        self._lower_overlay.addItem(self._cq_stars)

        self.upper_legend = CompactLegend(self._upper_specs)
        self.lower_legend = CompactLegend(self._lower_specs)
        lte_heading = QLabel("LTE")
        lte_heading.setObjectName("nguiGraphLteHeading")
        lte_heading.setStyleSheet("color: #ffffff; font-size: 13pt; font-weight: bold;")
        self.lower_legend.layout().insertWidget(0, lte_heading)
        self.upper_block = _plot_block(self.upper, self.upper_legend)
        self.lower_block = _plot_block(self.lower, self.lower_legend)
        self.stack = AtomicChartStack(self.upper_block, self.lower_block)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, 1)
        self._sequence = 0
        self._render_namespace: tuple[str, str] | None = None
        self._render_window_seconds: int | None = None
        self._marker_items: list[object] = []
        self._marker_cache: dict[tuple[float, str, int], tuple[object, object]] = {}
        self._outage_items: list[object] = []
        self._outage_cache: dict[
            tuple[float, float, str, int], tuple[QGraphicsLineItem, object]
        ] = {}
        self._range_cache: tuple[
            tuple[float, float], tuple[float, float], tuple[float, float]
        ] | None = None
        self._cq_star_points: tuple[tuple[float, float], ...] | None = None
        self._layout_namespace: tuple[str, str] | None = None

    @property
    def field_ids(self) -> frozenset[str]:
        return self.upper_legend.field_ids | self.lower_legend.field_ids | {"device.mobile_data_enabled"}

    def render_truth(self, truth: PageTruthSnapshot) -> None:
        namespace = truth.matrix_identity.namespace if truth.matrix_identity else None
        self._set_layout_namespace(
            (namespace.session_scope_key, namespace.source_epoch) if namespace else None
        )
        self.upper_legend.render(truth)
        self.lower_legend.render(truth)
        legends = (self.upper_legend, self.lower_legend)
        # One shared width keeps time axes aligned. Retain the widest rendered
        # text so shorter changing measurements do not repeatedly resize plots.
        width = max(max(legend.width(), legend.content_width()) for legend in legends)
        for legend in legends:
            if legend.width() != width:
                legend.setFixedWidth(width)

    def _set_layout_namespace(self, namespace: tuple[str, str] | None) -> None:
        if namespace != self._layout_namespace:
            self._layout_namespace = namespace

    def render(self, snapshot: ChartRenderSnapshot | None) -> bool:
        if snapshot is None:
            return False
        namespace = (snapshot.session_scope_key, snapshot.source_epoch)
        self._set_layout_namespace(namespace)
        if namespace != self._render_namespace:
            self._clear_render_cache()
            self._render_namespace = namespace
        if snapshot.request_sequence <= self._sequence:
            return False
        if snapshot.window_seconds != self._render_window_seconds:
            self._clear_render_cache()
            self._render_window_seconds = snapshot.window_seconds
        timestamps, upper, lower = snapshot.timestamps, snapshot.upper, snapshot.lower
        chunk_seconds = _render_chunk_seconds(snapshot.window_seconds)
        for field_id, curve in self._upper_curves.items():
            values = upper[field_id]
            curve.render(
                timestamps,
                values,
                connections=(snapshot.measurement_connect[field_id]
                             if field_id in snapshot.measurement_connect
                             else _finite_connections(values)),
                chunk_seconds=chunk_seconds,
            )
        for field_id, curve in self._lower_curves.items():
            values = lower[field_id]
            curve.render(
                timestamps,
                values,
                connections=(
                    snapshot.measurement_connect[field_id]
                    if field_id in snapshot.measurement_connect
                    else _finite_connections(values)
                ),
                chunk_seconds=chunk_seconds,
            )
        cq_color = pg.mkColor(self._cq_spec.chart_color)
        cq_color.setAlpha(snapshot.cq_paint.line_opacity)
        self._lower_curves["quality.cq_pct"].set_pen(
            pg.mkPen(cq_color, width=snapshot.cq_paint.line_width)
        )
        if snapshot.cq_paint.star_points != self._cq_star_points:
            self._cq_stars.setData(
                [point[0] for point in snapshot.cq_paint.star_points],
                [point[1] for point in snapshot.cq_paint.star_points],
                brush=pg.mkBrush(self._cq_spec.chart_color),
                pen=pg.mkPen(self._cq_spec.chart_color),
            )
            self._cq_star_points = snapshot.cq_paint.star_points
        ranges = (
            snapshot.x_range,
            snapshot.upper_y_range,
            snapshot.lower_y_range,
        )
        if ranges != self._range_cache:
            # The upper pane owns the time range; linked panes and overlays
            # follow it. No follower may set the master's time range.
            self.upper.setXRange(*snapshot.x_range, padding=0.0)
            self.upper.setYRange(*snapshot.upper_y_range, padding=0.0)
            self.lower.setYRange(*snapshot.lower_y_range, padding=0.0)
            self.upper.enableAutoRange(axis="y", enable=False)
            self.lower.enableAutoRange(axis="y", enable=False)
            self._range_cache = ranges
        self._render_markers(snapshot)
        self._render_outages(snapshot)
        self._sequence = snapshot.request_sequence
        return True

    def _clear_render_cache(self) -> None:
        for curve in (*self._upper_curves.values(), *self._lower_curves.values()):
            curve.clear()
        for line, label in self._marker_cache.values():
            self.lower.removeItem(line)
            self.lower.removeItem(label)
        self._marker_cache.clear()
        self._marker_items.clear()
        for connector, label in self._outage_cache.values():
            self._upper_overlay.removeItem(connector)
            self._upper_overlay.removeItem(label)
        self._outage_cache.clear()
        self._outage_items.clear()
        self._cq_stars.clear()
        self._cq_star_points = None
        self._range_cache = None
        self._sequence = 0

    def _make_plot(self, *, left_label: str) -> pg.PlotWidget:
        plot = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        plot.setMinimumHeight(MIN_PANE_HEIGHT)
        plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        plot.showGrid(x=False, y=True, alpha=0.12)
        plot.setLabel("left", left_label, color="#77a0c5")
        for axis_name in ("left", "bottom"):
            axis = plot.getAxis(axis_name)
            axis.setTextPen(pg.mkPen("#77a0c5" if axis_name == "left" else "#ffffff"))
            if axis_name == "bottom":
                axis.setPen(pg.mkPen("#ffffff"))
            axis.setStyle(
                tickTextOffset=4,
                tickFont=QFont(FONT_FAMILY, 11),
                autoExpandTextSpace=False,
                autoReduceTextSpace=True,
            )
        plot.getAxis("left").setWidth(AXIS_WIDTH)
        plot.enableAutoRange(axis="x", enable=False)
        plot.setMenuEnabled(False)
        return plot

    @staticmethod
    def _make_percent_overlay(plot: pg.PlotWidget, *, label: str, color: str) -> pg.ViewBox:
        plot.showAxis("right")
        axis = plot.getAxis("right")
        axis.setLabel(label, color=color)
        axis.setPen(pg.mkPen(color))
        axis.setTextPen(pg.mkPen(color))
        axis.setWidth(AXIS_WIDTH)
        axis.setStyle(
            tickTextOffset=4,
            tickFont=QFont(FONT_FAMILY, 11),
            autoExpandTextSpace=False,
            autoReduceTextSpace=True,
        )
        overlay = pg.ViewBox(enableMenu=False)
        overlay.setMouseEnabled(x=False, y=False)
        overlay.setYRange(PERCENT_MIN, PERCENT_MAX, padding=0.0)
        plot.scene().addItem(overlay)
        axis.linkToView(overlay)
        overlay.setXLink(plot)

        def sync() -> None:
            primary = plot.getViewBox()
            # Inner geometry excludes the master's border. The overlay adds
            # its own border; copying sceneBoundingRect would count it twice
            # and make linked time coordinates differ by one screen pixel.
            overlay.setGeometry(primary.mapRectToScene(primary.rect()))
            overlay.linkedViewChanged(plot.getViewBox(), overlay.XAxis)

        plot.getViewBox().sigResized.connect(sync)
        sync()
        return overlay

    def _render_markers(self, snapshot: ChartRenderSnapshot) -> None:
        window_start, window_end = snapshot.x_range
        window_span = window_end - window_start
        lower_y_min, lower_y_max = snapshot.lower_y_range
        lower_y_span = lower_y_max - lower_y_min
        x_offset = max(2.0, window_span * 0.005)
        x_margin = max(1.0, window_span * 0.002)
        base_y = lower_y_min + (lower_y_span * 0.22)
        lane_height = max(3.0, lower_y_span * 0.12)
        keys = tuple(
            (marker.timestamp, marker.label, marker.lane)
            for marker in snapshot.markers
        )
        for key in tuple(self._marker_cache):
            if key not in keys:
                line, label = self._marker_cache.pop(key)
                self.lower.removeItem(line)
                self.lower.removeItem(label)
        ordered: list[object] = []
        for marker, key in zip(snapshot.markers, keys, strict=True):
            pair = self._marker_cache.get(key)
            if pair is None:
                line = pg.InfiniteLine(
                    pos=marker.timestamp,
                    angle=90,
                    pen=pg.mkPen("#ffffff", width=1, style=Qt.PenStyle.DotLine),
                )
                label = pg.TextItem(
                    text=marker.label,
                    color="#ffffff",
                    anchor=(0, 1),
                )
                self.lower.addItem(line)
                self.lower.addItem(label)
                pair = (line, label)
                self._marker_cache[key] = pair
            line, label = pair
            label_x = marker.timestamp + x_offset
            if label_x > window_end - x_margin:
                label_x = marker.timestamp - (x_offset * 2.5)
            label_x = min(
                max(label_x, window_start + x_margin),
                window_end - x_margin,
            )
            label.setPos(label_x, base_y + (marker.lane * lane_height))
            ordered.extend((line, label))
        self._marker_items = ordered

    def _render_outages(self, snapshot: ChartRenderSnapshot) -> None:
        window_start, window_end = snapshot.x_range
        window_span = window_end - window_start
        x_margin = max(1.0, window_span * 0.002)
        label_offset = max(1.0, window_span * OUTAGE_LABEL_X_OFFSET_RATIO)
        keys = tuple(
            (outage.started_at, outage.restored_at, outage.label, outage.lane)
            for outage in snapshot.outages
        )
        for key in tuple(self._outage_cache):
            if key not in keys:
                connector, label = self._outage_cache.pop(key)
                self._upper_overlay.removeItem(connector)
                self._upper_overlay.removeItem(label)
        ordered: list[object] = []
        for outage, key in zip(snapshot.outages, keys, strict=True):
            connector_x = (outage.started_at + outage.restored_at) / 2.0
            connector_x = min(
                max(connector_x, window_start + x_margin),
                window_end - x_margin - label_offset,
            )
            label_x = connector_x + label_offset
            # Lane zero is the first label the user sees.  Keep it at the top
            # and move later nearby outages down, away from the NET line.
            label_y = OUTAGE_LABEL_TOP_Y - (outage.lane * OUTAGE_LABEL_LANE_STEP)
            pair = self._outage_cache.get(key)
            if pair is None:
                connector = QGraphicsLineItem()
                connector.setPen(
                    pg.mkPen("#00d8ff", width=1, style=Qt.PenStyle.DotLine)
                )
                label = pg.TextItem(
                    text=outage.label,
                    color="#00d8ff",
                    anchor=(0, 1),
                )
                label.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.Bold))
                self._upper_overlay.addItem(connector)
                self._upper_overlay.addItem(label)
                pair = (connector, label)
                self._outage_cache[key] = pair
            connector, label = pair
            connector.setLine(
                connector_x,
                0.0,
                connector_x,
                max(0.0, label_y - 2.0),
            )
            label.setPos(label_x, label_y)
            ordered.extend((connector, label))
        self._outage_items = ordered


def _plot_block(plot: QWidget, legend: QWidget) -> QWidget:
    block = QWidget()
    layout = QHBoxLayout(block)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(plot, 1)
    layout.addWidget(legend)
    block.setMinimumHeight(MIN_PANE_HEIGHT)
    block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return block


def _ordered_specs(pane: ChartPane, order: tuple[str, ...]) -> tuple[FieldSpec, ...]:
    declared = {spec.field_id: spec for spec in fields_for_chart(pane)}
    if set(declared) != set(order):
        raise ValueError(f"{pane.value} chart series order does not cover declared fields")
    return tuple(declared[field_id] for field_id in order)


def _series_pen(spec: FieldSpec):
    style = Qt.PenStyle.DotLine if spec.field_id in UPPER_OVERLAY_FIELDS else Qt.PenStyle.SolidLine
    if spec.field_id == "quality.ca_ld_pct":
        style = Qt.PenStyle.DashLine
    return pg.mkPen(spec.chart_color, width=SERIES_WIDTHS[spec.field_id], style=style)


def _legend_name(spec: FieldSpec) -> str:
    return {
        "quality.congestion_pct": "CONG",
        "quality.jitter_trimmed_ms": "JIT",
        "quality.internet_reachable": "NET",
        "quality.ca_ld_pct": "CA-LD",
    }.get(spec.field_id, spec.label.upper())


__all__ = [
    "AXIS_WIDTH",
    "AtomicChartStack",
    "AtomicChartView",
    "CompactLegend",
    "LEGEND_WIDTH",
    "LOWER_SERIES_ORDER",
    "LOWER_OVERLAY_FIELDS",
    "MIN_PANE_HEIGHT",
    "PANE_SPACING",
    "SERIES_WIDTHS",
    "UPPER_OVERLAY_FIELDS",
    "UPPER_SERIES_ORDER",
]
