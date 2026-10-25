"""Render the Developer Control page.

These widgets use one complete :class:`PageTruthSnapshot` and publish typed
user intents. They do not interpret backend mappings or keep another copy of
visible values.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping

from PySide6.QtCore import QPoint, QSignalBlocker, QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from arch4.operational_service_contracts import (
    DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
    DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
)
from arch4.trajectory_contract import MIN_BAND_SEQUENCE_INTERVAL_S
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

from ..control_availability import (
    WORKFLOW_CONTROL,
    control_availability_from_mapping,
)
from ..edit_register import (
    EDIT_FIELD_SPECS,
    NGUIEditRegister,
    PING_GROUP,
    SMS_GROUP,
    SMS_SENDER_GROUP,
)
from ..front_register import (
    PageTruthSnapshot,
    PresentationState,
)
from ..interactions import IntentKind, NGUIInteractionRegister
from ..registry import fields_for_page
from ..types import FieldSpec, PageId
from .measurement_layout import render_nr_support
from .style import BODY_FONT_PT
from .confirm_button import (
    NGUIConfirmationCoordinator,
    NGUITwoStageConfirmButton,
)
from .interactions import (
    bind_button,
    bind_numeric_edited,
    bind_text_draft,
    stamp_draft_control,
)


def _spec_map(page: PageId) -> dict[str, FieldSpec]:
    return {spec.field_id: spec for spec in fields_for_page(page)}


def _require_specs(page: PageId, field_ids: Iterable[str]) -> tuple[FieldSpec, ...]:
    by_id = _spec_map(page)
    requested = tuple(field_ids)
    missing = set(requested) - set(by_id)
    if missing:
        raise ValueError(f"{page.value}: undeclared renderer fields: {sorted(missing)}")
    return tuple(by_id[field_id] for field_id in requested)


def _manual_timing_heading(*, status: str, action_pending: bool) -> str:
    """Choose the heading from lifecycle state without owning that state."""

    if action_pending:
        return "MANUAL REQUEST IN PROGRESS"
    if str(status or "").strip().lower() in {"", "ready"}:
        return "MANUAL REQUEST RESULT"
    return "LAST MANUAL REQUEST"


class _TruthTable(QWidget):
    """Compact table with no domain decisions."""

    def __init__(
        self,
        page: PageId,
        field_ids: Iterable[str],
        *,
        columns: int = 1,
        spread_rows: bool = False,
        label_overrides: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.spread_rows = spread_rows
        self._labels: dict[str, QLabel] = {}
        self._nr_headings: list[tuple[QLabel, str]] = []
        vertical_policy = (
            QSizePolicy.Policy.Expanding if spread_rows else QSizePolicy.Policy.Maximum
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, vertical_policy)
        specs = _require_specs(page, field_ids)
        overrides = dict(label_overrides or {})
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(3)
        nr_offset = 0
        for index, spec in enumerate(specs):
            block = index % columns
            row = index // columns + nr_offset
            if spec.field_id.startswith("nr.") and not self._nr_headings and columns == 1:
                heading = QLabel("NR")
                layout.addWidget(heading, row, 0, 1, 2)
                self._nr_headings.append((heading, "NR"))
                nr_offset = 1
                row += 1
            name = QLabel(f"{overrides.get(spec.field_id, spec.label)}:")
            name.setObjectName("truthFieldName")
            value = QLabel("----")
            value.setProperty("nguiRole", "truthFieldValue")
            value.setObjectName(f"value__{spec.field_id.replace('.', '__')}")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            layout.addWidget(name, row, block * 2)
            layout.addWidget(value, row, block * 2 + 1)
            self._labels[spec.field_id] = value
            if spread_rows:
                layout.setRowStretch(row, 1)
        for block in range(columns):
            layout.setColumnStretch(block * 2 + 1, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
            label.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )
        render_nr_support(truth, self._labels, self._nr_headings)


def _box(title: str, child: QWidget) -> QGroupBox:
    box = QGroupBox(title)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(5, 6, 5, 4)
    layout.setSpacing(2)
    if child.sizePolicy().verticalPolicy() is QSizePolicy.Policy.Maximum:
        layout.addWidget(child, 0, Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)
    else:
        layout.addWidget(child)
    return box


def _publish_button(
    label: str,
    register: NGUIInteractionRegister,
    kind: IntentKind,
    payload: Callable[[], dict] | None = None,
) -> QPushButton:
    button = QPushButton(label)
    button.setProperty("nguiRole", "user_action")
    bind_button(button, register, kind, payload)
    return button


_DC_LIVE_QUALITY = (
    "quality.cq_pct",
    "quality.rtt_ms",
    "quality.rtt_average_ms",
    "quality.rtt_delta_ms",
    "quality.congestion_pct",
    "quality.congestion_trend",
    "quality.jitter_trimmed_ms",
    "quality.cast",
    "quality.ca_ld_pct",
    "quality.cart_activity",
    "quality.ca_ld_activity",
    "quality.ri_stability_pct",
    "quality.ri_activity",
)

_DC_CONGESTION = (
    "quality.rsrq_delta_db",
    "quality.rtt_delta_ms",
    "quality.congestion_pct",
    "quality.rsrq_reference_db",
    "quality.rtt_reference_ms",
    "quality.congestion_trend",
    "rf.rsrq_db",
    "quality.rtt_ms",
    "quality.jitter_trimmed_ms",
)

_DC_CONTEXTUALLY_REPEATED = frozenset(
    {
        "radio.dl_bandwidth_mhz",
        "radio.ri",
        "radio.transmission_mode",
        "nr.mode",
        "nr.ri",
        "nr.bandwidth_mhz",
        "nr.dl_frequency_mhz",
        "rf.rsrq_db",
        "quality.rtt_ms",
        "quality.rtt_delta_ms",
        "quality.congestion_pct",
        "quality.congestion_trend",
        "quality.jitter_trimmed_ms",
    }
)

_DC_PROVIDER = (
    "provider.name",
    "radio.network_type",
    "radio.earfcn",
    "radio.enb_id",
    "radio.cell_id",
    "radio.pci",
    "radio.transmission_mode",
    "radio.rrc",
    "radio.ri",
    "radio.pband",
    "radio.dl_bandwidth_mhz",
    "nr.mode",
    "nr.arfcn",
    "nr.cell_id",
    "nr.pci",
    "nr.ri",
    "nr.dl_frequency_mhz",
    "nr.band",
    "nr.bandwidth_mhz",
)

_DC_MANUAL_CONSUMED = (
    "action.control_availability",
    "command.band_locked",
    "command.cell_locked",
    "manual.status",
    "manual.message",
    "manual.action_pending",
    "manual.command_received_timing",
    "manual.internet_pause_timing",
)

_DC_TOOL_READBACK = (
    "intelligence.autopilot_requested",
    "intelligence.explore_requested",
    "intelligence.control_available",
    "command.ping_status",
    "command.ping_enabled",
    "command.ping_host",
    "command.ping_count",
    "command.ping_every_s",
    "capability.sms_supported",
    "command.sms_enabled",
    "command.sms_keyword",
    "command.sms_every_s",
    "command.sms_message",
    "command.band_sequence_status",
    "command.band_schedule_status",
)


_DC_SUPPRESSED = (
    "tuning.congestion_rsrq_weight",
    "tuning.congestion_rtt_weight",
    "tuning.jitter_scale_ms",
    "tuning.jitter_gain",
    "antenna_guide.rsrp_scale",
    "antenna_guide.rsrq_scale",
    "antenna_guide.rssi_scale",
    "antenna_guide.sinr_scale",
    "command.manual_cell_lock_timer",
    "command.manual_available",
    "command.status",
    "capability.pband_control",
    "capability.ca_pool_control",
    "capability.band_lock",
    "capability.cell_lock",
    "capability.restart",
    "quality.congestion_bad_now",
    "quality.congestion_pressure_high",
    "quality.congestion_pressure_moderate",
    "quality.congestion_qam_gap",
    "quality.congestion_rsrq_available",
    "quality.congestion_rsrq_contributes",
    "quality.congestion_rsrq_gate_db",
    "quality.congestion_rsrq_gap_db",
    "quality.congestion_rsrq_pct",
    "quality.congestion_rsrq_reason",
    "quality.congestion_source",
    "quality.rtt_failure_reason",
    "quality.rtt_freshness",
    "quality.rtt_observed_at",
    "quality.rtt_probe_status",
    "connection.modem_comm_alive",
    "connection.quality_pct",
    "connection.user_link",
    "device.hardware_version",
    "device.imei",
    "device.imsi",
    "device.temperature_5g_c",
    "profile.profile_id",
    "profile.runtime_mode",
    "profile.selected_profile",
    "provider.plmn",
    "quality.cart_ld",
    "rf.tx_power",
    "radio.active_bands",
    "radio.carrier_count",
    "radio.frequency_set",
    "radio.tac",
    "runtime.state",
    "nr.cells",
    "nr.nsa_active",
    "nr.sa_active",
    "manual.action_sequence",
    "manual.core_received_ms",
    "manual.modem_work_started_ms",
    "manual.pband_command_sent_ms",
    "manual.ca_pool_command_sent_ms",
    "manual.target_first_verified_ms",
    "manual.next_chart_sample_ms",
    "manual.chart_updated",
    "manual.target_sample_sequence",
    "manual.chart_sample_sequence",
    "quality.cart_ld_activity",
    "quality.cart",
    "scheduler.mcs_collapse",
    "traffic.dl_mbps",
    "traffic.has_traffic",
    "traffic.invalid_reason",
    "traffic.rx_bytes_per_s",
    "traffic.session_uptime_s",
    "traffic.state_detail",
    "traffic.tx_bytes_per_s",
    "traffic.ul_mbps",
    "traffic.valid",
)

_DC_CONTROL_ONLY_CONSUMED = frozenset(
    {
        "capability.sms_supported",
        "manual.command_received_timing",
        "manual.internet_pause_timing",
    }
)


class _SchedulerTable(QWidget):
    """Render aligned LTE and NR scheduler values from PageTruth."""

    _ROWS = (
        ("CQI", "scheduler.cqi0", "scheduler.cqi1", None, "nr.cqi", None),
        ("MCS", "scheduler.mcs_dl0", "scheduler.mcs_dl1", "scheduler.mcs_ul", "nr.mcs_dl", "nr.mcs_ul"),
        ("ME", "scheduler.me0", "scheduler.me1", "scheduler.me_ul", "nr.me_dl", "nr.me_ul"),
        ("QAM", "scheduler.qam_dl0", "scheduler.qam_dl1", "scheduler.qam_ul", "nr.qam_dl", "nr.qam_ul"),
        ("RBs", "radio.lte_num_rbs", None, None, "nr.num_rbs", None),
    )

    def __init__(self) -> None:
        super().__init__()
        declared = tuple(
            field_id
            for _label, *fields in self._ROWS
            for field_id in fields
            if field_id is not None
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        _require_specs(PageId.DEVELOPER_CONTROL, declared)
        self._labels: dict[str, QLabel] = {}
        self._nr_headings: list[tuple[QLabel, str]] = []
        self._dl1_values: list[QLabel] = []
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(3)
        for column, heading in enumerate(("LTE DL 0", "LTE DL 1", "LTE UL", "NR DL", "NR UL"), 1):
            label = QLabel(heading)
            if heading == "LTE DL 1":
                self._dl1_heading = label
            if heading.startswith("NR"):
                self._nr_headings.append((label, heading))
            layout.addWidget(label, 0, column)
            label.setAlignment((Qt.AlignRight if column == 5 else Qt.AlignHCenter) | Qt.AlignTop)
            layout.setColumnMinimumWidth(column, 88)
            layout.setColumnStretch(column, 1)
        for row, (name, *fields) in enumerate(self._ROWS, 1):
            layout.addWidget(QLabel(f"{name}:"), row, 0)
            for column, field_id in enumerate(fields, 1):
                value = QLabel("" if field_id is None else "----")
                value.setAlignment((Qt.AlignRight if column == 5 else Qt.AlignHCenter) | Qt.AlignVCenter)
                layout.addWidget(value, row, column)
                if column == 2:
                    self._dl1_values.append(value)
                if field_id is not None:
                    self._labels[field_id] = value

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
            label.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )
        dl1_unsupported = all(truth.fields[key].state is PresentationState.UNSUPPORTED
            for key in ("scheduler.cqi1", "scheduler.mcs_dl1", "scheduler.qam_dl1"))
        self._dl1_heading.setText("LTE DL 1\nUNSUP." if dl1_unsupported else "LTE DL 1")
        for label in self._dl1_values:
            label.setVisible(not dl1_unsupported)
        # Compact display exception requested for individually absent UL metrics.
        for key in ("scheduler.mcs_ul", "scheduler.me_ul", "scheduler.qam_ul"):
            if truth.fields[key].state is PresentationState.UNSUPPORTED:
                self._labels[key].setText("----")
        render_nr_support(truth, self._labels, self._nr_headings)


class _RadioMatrix(QWidget):
    """Align existing LTE and NR radio facts; never derive absent measurements."""

    _SIGNAL_ROWS = (
        ("SINR", "rf.sinr_db", "nr.sinr_db"),
        ("RSRP", "rf.rsrp_dbm", "nr.rsrp_dbm"),
        ("RSRQ", "rf.rsrq_db", "nr.rsrq_db"),
        ("RSSI", "rf.rssi_dbm", None),
        ("RI", "radio.ri", "nr.ri"),
        ("RTT", "quality.rtt_ms", None),
    )
    _DETAIL_ROWS = (
        ("DL BW", "radio.dl_bandwidth_mhz", "nr.bandwidth_mhz"),
        ("UL BW", "radio.ul_bandwidth_mhz", "nr.ul_bandwidth_mhz"),
        ("DL freq", "radio.dl_frequency_mhz", "nr.dl_frequency_mhz"),
        ("UL freq", "radio.ul_frequency_mhz", "nr.ul_frequency_mhz"),
        ("PUCCH", "rf.tx_ppucch", "nr.tx_power_dbm"),
        ("Mode", "radio.transmission_mode", "nr.mode"),
    )
    _LTE_TX = (("PUSCH", "rf.tx_ppusch"), ("SRS", "rf.tx_psrs"), ("PRACH", "rf.tx_pprach"))

    def __init__(self) -> None:
        super().__init__()
        declared = tuple(field for rows in (self._SIGNAL_ROWS, self._DETAIL_ROWS)
                         for _name, *fields in rows for field in fields if field is not None)
        _require_specs(PageId.DEVELOPER_CONTROL, declared + tuple(field for _name, field in self._LTE_TX))
        self._labels: dict[str, QLabel] = {}
        self._nr_headings: list[tuple[QLabel, str]] = []
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(5)
        for offset, title, rows in ((0, "SIGNAL", self._SIGNAL_ROWS), (3, "", self._DETAIL_ROWS)):
            for column, heading in enumerate((title, "LTE", "NR")):
                if not heading:
                    continue
                label = QLabel(heading)
                if heading.startswith("NR"):
                    self._nr_headings.append((label, heading))
                label.setObjectName("truthFieldName")
                if offset + column == 5:
                    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                layout.addWidget(label, 0, offset + column)
            for row, (name, *fields) in enumerate(rows, 1):
                label = QLabel(name + ":")
                label.setObjectName("truthFieldName")
                layout.addWidget(label, row, offset)
                for column, field_id in enumerate(fields, 1):
                    value = self._value_label(field_id)
                    if offset + column == 5:
                        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    layout.addWidget(value, row, offset + column)
        for column in (1, 2, 4, 5):
            layout.setColumnStretch(column, 1)
        tx = QHBoxLayout()
        tx.setContentsMargins(0, 0, 0, 0)
        tx.setSpacing(8)
        tx.addWidget(QLabel("LTE TX PWR:"))
        for name, field_id in self._LTE_TX:
            tx.addWidget(QLabel(name + ":"))
            value = self._value_label(field_id)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            tx.addWidget(value, 1)
        layout.addLayout(tx, 7, 0, 1, 6)

    def _value_label(self, field_id: str | None) -> QLabel:
        value = QLabel("" if field_id is None else "----")
        if field_id is not None:
            value.setObjectName(f"value__{field_id.replace('.', '__')}")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._labels[field_id] = value
        return value

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
            label.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )
        render_nr_support(truth, self._labels, self._nr_headings)


class _CongestionMatrix(QWidget):
    """Render congestion delta, baseline, and current values."""

    _ROWS = (
        (
            ("Delta", "quality.rsrq_delta_db"),
            ("Delta", "quality.rtt_delta_ms"),
            ("Congestion", "quality.congestion_pct"),
        ),
        (
            ("Base", "quality.rsrq_reference_db"),
            ("Base", "quality.rtt_reference_ms"),
            ("Trend", "quality.congestion_trend"),
        ),
        (
            ("RSRQ", "rf.rsrq_db"),
            ("RTT", "quality.rtt_ms"),
            ("Jitter", "quality.jitter_trimmed_ms"),
        ),
    )

    def __init__(self) -> None:
        super().__init__()
        _require_specs(PageId.DEVELOPER_CONTROL, _DC_CONGESTION)
        self._labels: dict[str, QLabel] = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(9)
        layout.setVerticalSpacing(2)
        for row, cells in enumerate(self._ROWS):
            for column, (name, field_id) in enumerate(cells):
                base = column * 2
                layout.addWidget(QLabel(f"{name}:"), row, base)
                value = QLabel("----")
                value.setObjectName(f"value__{field_id.replace('.', '__')}")
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if column == 2:
                    value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                layout.addWidget(value, row, base + 1)
                self._labels[field_id] = value
        for column in (1, 3, 5):
            layout.setColumnStretch(column, 1)

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(self._labels)

    def render(self, truth: PageTruthSnapshot) -> None:
        for field_id, label in self._labels.items():
            field = truth.fields[field_id]
            if label.text() != field.text:
                label.setText(field.text)
            label.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )


class _ManualActions(QWidget):
    def __init__(
        self,
        register: NGUIInteractionRegister,
        confirmation: NGUIConfirmationCoordinator,
        size_reference: QPushButton,
    ) -> None:
        super().__init__()
        self._size_reference = size_reference
        self.setStyleSheet(
            f"QLabel, QLineEdit {{ font-size: {BODY_FONT_PT - 1}pt; }}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)
        self.timer = QLineEdit()
        self.timer.setObjectName("nguiDeveloperCellTimer")
        self.timer.setPlaceholderText("HH:MM")
        self.timer.setMaximumWidth(52)
        self.pci = QLineEdit()
        self.pci.setObjectName("nguiDeveloperCellPci")
        self.pci.setPlaceholderText("PCI")
        self.pci.setMaximumWidth(44)
        self.earfcn = QLineEdit()
        self.earfcn.setObjectName("nguiDeveloperCellEarfcn")
        self.earfcn.setPlaceholderText("EARFCN")
        self.earfcn.setMaximumWidth(68)
        self.pband = QLineEdit()
        self.pband.setObjectName("nguiDeveloperPbandInput")
        self.pband.setPlaceholderText("PB")
        self.pband.setFixedWidth(36)
        self.nr_band = QLineEdit()
        self.nr_band.setObjectName("nguiDeveloperNrBandInput")
        self.nr_band.setPlaceholderText("NR")
        self.nr_band.setFixedWidth(40)
        self.band = QLineEdit()
        self.band.setObjectName("nguiDeveloperCellBand")
        self.band.setPlaceholderText("3")
        self.band.setFixedWidth(32)
        self.ca_pool = QLineEdit()
        self.ca_pool.setObjectName("nguiDeveloperCaPoolInput")
        self.ca_pool.setFixedWidth(196)
        for widget in (
            self.timer,
            self.pci,
            self.earfcn,
            self.pband,
            self.nr_band,
            self.band,
            self.ca_pool,
        ):
            stamp_draft_control(widget)
        self._availability_payload: Mapping[str, object] = {}
        self._matrix_identity: MatrixGenerationIdentity | None = None
        self._displayed_pband = ""
        self._displayed_pci = ""
        self._displayed_earfcn = ""
        self._band_locked = False
        self._cell_locked: bool | None = None
        self._confirmation = confirmation
        self.apply_radio = NGUITwoStageConfirmButton(
            self._radio_request,
            register,
            IntentKind.APPLY_PBAND_CA_POOL,
            payload_from_request=self._command_payload,
            coordinator=self._confirmation,
            label="APPLY LTE",
            compact=True,
        )
        self.apply_radio.setObjectName("nguiDeveloperRadioApply")
        self.apply_nr = NGUITwoStageConfirmButton(
            self._nr_request, register, IntentKind.APPLY_NR_BAND,
            payload_from_request=self._command_payload,
            coordinator=self._confirmation, label="APPLY NR",
            compact=True,
        )
        self.apply_nr.setObjectName("nguiDeveloperNrApply")
        self.band_lock = NGUITwoStageConfirmButton(
            self._band_lock_request,
            register,
            IntentKind.TOGGLE_BAND_LOCK,
            payload_from_request=self._command_payload,
            label="LOCK BAND",
            compact=True,
            coordinator=self._confirmation,
        )
        self.cell_lock = NGUITwoStageConfirmButton(
            self._cell_lock_request,
            register,
            IntentKind.TOGGLE_CELL_LOCK,
            payload_from_request=self._command_payload,
            label="LOCK CELL",
            compact=True,
            coordinator=self._confirmation,
        )
        self.band_lock.setCheckable(True)
        self.cell_lock.setCheckable(True)
        self.restart = NGUITwoStageConfirmButton(
            lambda: True,
            register,
            IntentKind.RESTART_MODEM,
            payload_from_request=self._command_payload,
            label="REBOOT",
            compact=True,
            coordinator=self._confirmation,
            destructive=True,
        )
        self.band_auto = NGUITwoStageConfirmButton(
            lambda: {"domain": "lte_allocated_auto"},
            register,
            IntentKind.SET_BAND_AUTO,
            payload_from_request=self._command_payload,
            label="LTE AUTO",
            compact=True,
            coordinator=self._confirmation,
        )
        self.nr_auto = NGUITwoStageConfirmButton(
            lambda: {"domain": "nr_approved_auto"},
            register,
            IntentKind.SET_BAND_AUTO,
            payload_from_request=self._command_payload,
            label="NR AUTO",
            compact=True,
            coordinator=self._confirmation,
        )
        self.restart.setObjectName("nguiDeveloperReboot")
        self.band_auto.setObjectName("nguiDeveloperBandAuto")
        self.nr_auto.setObjectName("nguiDeveloperNrAuto")
        self.band_lock.setObjectName("nguiDeveloperBandLock")
        self.cell_lock.setObjectName("nguiDeveloperCellLock")
        for field in (
            self.timer,
            self.pci,
            self.earfcn,
            self.pband,
            self.nr_band,
            self.band,
            self.ca_pool,
        ):
            bind_text_draft(field, self._dangerous_draft_changed)
        def input_group(fields, button):
            group = QWidget()
            group.setObjectName("nguiManualInputGroup")
            group.setStyleSheet("QWidget#nguiManualInputGroup { border: 1px dotted #00b77b; }")
            grid = QGridLayout(group)
            grid.setContentsMargins(4, 2, 4, 2)
            grid.setHorizontalSpacing(2)
            grid.setVerticalSpacing(2)
            for column, (label, editor) in enumerate(fields):
                grid.addWidget(QLabel(label), 0, column)
                grid.addWidget(editor, 1, column)
            grid.addWidget(button, 2, 0, 1, len(fields), Qt.AlignmentFlag.AlignHCenter)
            group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            return group

        groups = (
            input_group((("CA POOL", self.ca_pool), ("PBAND", self.pband)), self.apply_radio),
            input_group((("NR", self.nr_band),), self.apply_nr),
            input_group((("TIMER", self.timer), ("BAND", self.band)), self.band_lock),
            input_group((("PCI", self.pci), ("EARFCN", self.earfcn)), self.cell_lock),
        )
        self._input_groups = groups
        self._input_columns = 4
        for column, group in enumerate(groups):
            layout.addWidget(group, 0, column, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        auto_row = QWidget()
        auto_layout = QVBoxLayout(auto_row)
        auto_layout.setContentsMargins(3, 0, 0, 0)
        auto_layout.setSpacing(3)
        auto_layout.addWidget(self.band_auto)
        auto_layout.addWidget(self.nr_auto)
        auto_layout.addSpacing(8)
        auto_layout.addWidget(self.restart)
        layout.addWidget(auto_row, 0, 4, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._delivery_labels: dict[str, QLabel] = {}
        timing = QWidget()
        timing.setObjectName("nguiManualTimingProgression")
        self.timing_progression = timing
        timing_layout = QGridLayout(timing)
        timing_layout.setContentsMargins(0, 0, 0, 0)
        timing_layout.setHorizontalSpacing(8)
        timing_layout.setVerticalSpacing(3)
        timing.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        timing_title = QLabel("MANUAL REQUEST RESULT")
        timing_title.setObjectName("nguiManualTimingHeading")
        timing_title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        timing_layout.addWidget(timing_title, 0, 0)
        self.timing_title = timing_title
        self.timing_rows = (
            (("COMMAND RECEIVED", "manual.command_received_timing", None),),
            (("LATEST RADIO PAUSE", "manual.internet_pause_timing", None),),
        )
        self.timing_stage_order = tuple(
            field_id
            for row in self.timing_rows
            for _label_text, field_id, _sequence_field_id in row
        )
        self.timing_stage_labels: dict[str, QLabel] = {}
        self.timing_heading_labels: dict[str, QLabel] = {}
        self.timing_sequence_fields: dict[str, str] = {}
        for row_index, row in enumerate(self.timing_rows):
            for pair_index, (
                label_text,
                field_id,
                sequence_field_id,
            ) in enumerate(row):
                heading = QLabel(label_text)
                heading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                heading.setWordWrap(False)
                grid_column = 1 + row_index * 2 + pair_index * 2
                timing_layout.addWidget(heading, 0, grid_column)
                value = QLabel("----")
                value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                value.setMinimumWidth(66)
                value.setObjectName(f"value__{field_id.replace('.', '__')}")
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                timing_layout.addWidget(value, 0, grid_column + 1)
                self._delivery_labels[field_id] = value
                self.timing_stage_labels[field_id] = value
                self.timing_heading_labels[field_id] = heading
                if sequence_field_id:
                    self.timing_sequence_fields[field_id] = sequence_field_id
        timing_layout.setColumnStretch(5, 1)
        self.action_result = QLabel("Ready")
        self.action_result.setObjectName("value__manual__message")
        self.action_result.setWordWrap(False)
        self.action_result.setMinimumWidth(0)
        self.action_result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.action_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._delivery_labels["manual.message"] = self.action_result
        timing_layout.addWidget(self.action_result, 1, 0, 1, 6)
        layout.addWidget(timing, 1, 0, 1, 6, Qt.AlignTop)
        layout.setRowStretch(2, 1)
        layout.setColumnStretch(5, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_result_text()

    def _refresh_result_text(self) -> None:
        if not hasattr(self, "action_result"):
            return
        full = str(self.action_result.property("nguiFullText") or "")
        self.action_result.setText(self.action_result.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideRight, max(0, self.action_result.width())))

    def showEvent(self, event) -> None:
        # The containing window's stylesheet is inherited only after parenting.
        actions = (self.apply_radio, self.apply_nr, self.band_lock,
                   self.cell_lock, self.band_auto, self.nr_auto, self.restart)
        self._size_reference.ensurePolished()
        action_height = self._size_reference.sizeHint().height()
        for button in actions:
            font = button.font()
            font.setPointSize(BODY_FONT_PT - 1)
            button.setFont(font)
            button.ensurePolished()
        labels = tuple(button.base_label for button in actions) + (
            "UNLOCK BAND", "UNLOCK CELL", "CONFIRM?",
        )
        action_width = max(
            button.fontMetrics().horizontalAdvance(label) + 18
            for button in actions for label in labels
            if "state unknown" not in label
        )
        for button in actions:
            button.set_compact_size(action_width, action_height)
        super().showEvent(event)
        # A hidden page may have selected two rows before compact sizes existed.
        for group in self._input_groups:
            group.layout().invalidate()
        self._refresh_result_text()

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(_DC_MANUAL_CONSUMED)

    def _control_availability(
        self,
        *controls: str,
    ):
        return control_availability_from_mapping(
            self._availability_payload,
            *controls,
        )

    def _dangerous_draft_changed(self, _text: str) -> None:
        self._confirmation.disarm()

    def _render_availability_button(
        self,
        button: NGUITwoStageConfirmButton,
        *controls: str,
    ) -> None:
        decision = self._control_availability(*controls)
        if button.isEnabled() != decision.enabled:
            button.setEnabled(decision.enabled)
        if not decision.enabled:
            button.disarm()
        button.setToolTip("" if decision.enabled else decision.reason)

    def _render_availability_buttons(self) -> None:
        self._render_availability_button(
            self.apply_radio,
            "band_selection",
        )
        self._render_availability_button(self.apply_nr, "nr_band_lock")
        self._render_availability_button(self.nr_auto, "nr_band_lock")
        self.nr_band.setEnabled(self.apply_nr.isEnabled())
        self._render_availability_button(
            self.band_lock,
            "band_selection" if self._band_locked else "band_lock",
        )
        self._render_availability_button(
            self.cell_lock,
            "cell_lock",
        )
        self._render_availability_button(
            self.restart,
            "restart",
        )
        self._render_availability_button(
            self.band_auto,
            "band_selection",
        )

    def _command_payload(self, request: object) -> dict[str, object]:
        identity = self._matrix_identity
        if not isinstance(identity, MatrixGenerationIdentity):
            raise RuntimeError("manual command Matrix identity is unavailable")
        payload = dict(request) if isinstance(request, Mapping) else {}
        payload["matrix_identity"] = identity.as_dict()
        return payload

    def _band_lock_request(self) -> dict[str, object] | None:
        desired_locked = not self._band_locked
        band = self.band.text().strip() or self._displayed_pband
        if desired_locked and not band:
            return None
        payload: dict[str, object] = {"desired_locked": desired_locked}
        if band:
            payload["band"] = band
        return payload

    def _radio_request(self) -> dict[str, str] | None:
        ca_pool = self.ca_pool.text().strip()
        if not ca_pool:
            return None
        return {
            "pband": self.pband.text().strip(),
            "ca_pool": ca_pool,
        }

    def _nr_request(self) -> dict[str, str] | None:
        text = self.nr_band.text().strip()
        return {"nr": text} if text else None

    def _cell_lock_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        for key, widget in (
            ("pci", self.pci),
            ("earfcn", self.earfcn),
            ("band", self.band),
            ("lock_duration", self.timer),
        ):
            value = widget.text().strip()
            if value:
                payload[key] = value
        payload.setdefault("pci", self._displayed_pci)
        payload.setdefault("earfcn", self._displayed_earfcn)
        payload.setdefault("band", self._displayed_pband)
        return {key: value for key, value in payload.items() if value}

    def _cell_lock_request(self) -> dict[str, object] | None:
        # Missing readback may permit an explicit lock, never an assumed unlock.
        desired_locked = self._cell_locked is not True
        payload: dict[str, object] = {
            "desired_locked": desired_locked,
            **self._cell_lock_payload(),
        }
        if desired_locked and (
            (payload.get("pci") is None or not str(payload["pci"]).strip())
            or not str(payload.get("earfcn") or "").strip()
        ):
            return None
        return payload

    def render(self, truth: PageTruthSnapshot) -> None:
        self._matrix_identity = truth.matrix_identity
        self._displayed_pband = str(
            truth.fields["radio.pband"].value
            or truth.fields["radio.pband"].text
            or ""
        ).strip()
        self._displayed_pci = str(
            truth.fields["radio.pci"].value
            or truth.fields["radio.pci"].text
            or ""
        ).strip()
        self._displayed_earfcn = str(
            truth.fields["radio.earfcn"].value
            or truth.fields["radio.earfcn"].text
            or ""
        ).strip()
        availability_field = truth.fields["action.control_availability"]
        self._availability_payload = (
            availability_field.value
            if availability_field.state is PresentationState.VALID
            and isinstance(availability_field.value, Mapping)
            else {}
        )
        for field_id, label in self._delivery_labels.items():
            field = truth.fields[field_id]
            text = field.text
            if label is self.action_result:
                label.setProperty("nguiFullText", text)
                label.setToolTip(text if not field.reason else f"{text}\n{field.reason}")
                self._refresh_result_text()
                continue
            if label.text() != text:
                label.setText(text)
            label.setToolTip(
                field.reason
                if field.state is not PresentationState.VALID
                else ""
            )
        status = truth.fields["manual.status"]
        pending = truth.fields["manual.action_pending"]
        action_pending = (
            pending.state in {PresentationState.VALID, PresentationState.HELD}
            and pending.value is True
        )
        timing_heading = _manual_timing_heading(
            status=str(status.value or status.text or ""),
            action_pending=action_pending,
        )
        if self.timing_title.text() != timing_heading:
            self.timing_title.setText(timing_heading)
        for widget in (
            self.timer,
            self.pci,
            self.earfcn,
            self.pband,
            self.nr_band,
            self.band,
            self.ca_pool,
        ):
            widget.setEnabled(not action_pending)
        for field_id, button, locked_text, unlocked_text in (
            ("command.band_locked", self.band_lock, "UNLOCK BAND", "LOCK BAND"),
            ("command.cell_locked", self.cell_lock, "UNLOCK CELL", "LOCK CELL"),
        ):
            field = truth.fields[field_id]
            locked = (
                field.state in {PresentationState.VALID, PresentationState.HELD}
                and field.value is True
            )
            if field_id == "command.band_locked":
                self._band_locked = locked
            else:
                self._cell_locked = (field.value if field.state is PresentationState.VALID
                                     and type(field.value) is bool else None)
                locked = self._cell_locked is True
            label = locked_text if locked else unlocked_text
            if (field_id == "command.cell_locked" and self._cell_locked is None
                    and truth.fields["capability.cell_lock"].state is not PresentationState.UNSUPPORTED
                    and truth.fields["capability.cell_lock"].value is not False):
                label = "LOCK CELL ?"
            button.set_base_label(label)
            button.setProperty("locked", locked)
            if button.isChecked() != locked:
                button.setChecked(locked)
            button.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )
        self._render_availability_buttons()
        if self.cell_lock.base_label == "LOCK CELL ?":
            self.cell_lock.setToolTip(
                "Cell lock state is unknown. " + self.cell_lock.toolTip()
            )


class _ToolStatus(QPlainTextEdit):
    """Keep complete long results readable without pushing controls off-screen."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802
        self.setPlainText(text)
        line_height = self.fontMetrics().lineSpacing()
        self.setMinimumHeight(line_height + 8)
        self.setMaximumHeight(line_height * 3 + 8)

    def sizeHint(self):  # noqa: N802
        return QSize(180, self.maximumHeight())

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, self.minimumHeight())


class _Tools(QWidget):
    CONTENT_WIDTH = 330

    def __init__(
        self,
        register: NGUIInteractionRegister,
        edits: NGUIEditRegister,
    ) -> None:
        super().__init__()
        self._edits = edits
        _require_specs(
            PageId.DEVELOPER_CONTROL,
            _DC_TOOL_READBACK,
        )
        self._status_labels: dict[str, _ToolStatus] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(14)
        self.autopilot_controls = QWidget()
        autopilot_layout = QHBoxLayout(self.autopilot_controls)
        autopilot_layout.setContentsMargins(0, 0, 0, 0)
        autopilot_layout.setSpacing(6)
        brain_title = QLabel("BRAIN")
        brain_title.setFixedWidth(120)
        autopilot_layout.addWidget(brain_title)
        self.autopilot_on_button = _publish_button(
            "ON",
            register,
            IntentKind.SET_INTELLIGENCE_AUTOPILOT,
            lambda: {"enabled": True},
        )
        self.autopilot_off_button = _publish_button(
            "OFF",
            register,
            IntentKind.SET_INTELLIGENCE_AUTOPILOT,
            lambda: {"enabled": False},
        )
        self.autopilot_on_button.setObjectName("nguiDeveloperAutopilotOn")
        self.autopilot_off_button.setObjectName("nguiDeveloperAutopilotOff")
        self.autopilot_button_group = QButtonGroup(self)
        self.autopilot_button_group.setExclusive(True)
        for button in (
            self.autopilot_on_button,
            self.autopilot_off_button,
        ):
            button.setCheckable(True)
            self.autopilot_button_group.addButton(button)
            autopilot_layout.addWidget(button)
        autopilot_layout.addStretch(1)
        self.explore_controls = QWidget()
        explore_layout = QHBoxLayout(self.explore_controls)
        explore_layout.setContentsMargins(0, 0, 0, 0)
        explore_layout.setSpacing(6)
        explore_title = QLabel("EXPLORE")
        explore_title.setFixedWidth(120)
        explore_layout.addWidget(explore_title)
        self.explore_on_button = _publish_button(
            "ON",
            register,
            IntentKind.SET_INTELLIGENCE_EXPLORE,
            lambda: {"enabled": True},
        )
        self.explore_off_button = _publish_button(
            "OFF",
            register,
            IntentKind.SET_INTELLIGENCE_EXPLORE,
            lambda: {"enabled": False},
        )
        self.explore_on_button.setObjectName("nguiDeveloperExploreOn")
        self.explore_off_button.setObjectName("nguiDeveloperExploreOff")
        self.explore_button_group = QButtonGroup(self)
        self.explore_button_group.setExclusive(True)
        for button in (self.explore_on_button, self.explore_off_button):
            button.setCheckable(True)
            self.explore_button_group.addButton(button)
            explore_layout.addWidget(button)
        explore_layout.addStretch(1)
        intelligence_controls = QWidget()
        intelligence_controls.setObjectName("nguiToolContent")
        intelligence_controls.setFixedWidth(self.CONTENT_WIDTH)
        intelligence_layout = QVBoxLayout(intelligence_controls)
        intelligence_layout.setContentsMargins(0, 0, 0, 0)
        intelligence_layout.setSpacing(14)
        intelligence_layout.addWidget(self.autopilot_controls)
        intelligence_layout.addWidget(self.explore_controls)
        root.addWidget(intelligence_controls, 0, Qt.AlignHCenter)
        self.ping_host = QLineEdit()
        self.ping_host.setObjectName("nguiDeveloperPingHost")
        self.ping_host.setPlaceholderText("gateway or host")
        self.ping_count = QSpinBox()
        self.ping_count.setObjectName("nguiDeveloperPingCount")
        self.ping_count.setRange(1, 10)
        self.ping_every = QSpinBox()
        self.ping_every.setObjectName("nguiDeveloperPingEvery")
        for widget in (self.ping_host, self.ping_count, self.ping_every):
            stamp_draft_control(widget)
        self.ping_every.setRange(
            int(DOWNSTREAM_HEALTH_INTERVAL_MIN_S),
            int(DOWNSTREAM_HEALTH_INTERVAL_MAX_S),
        )
        self.ping_every.setSuffix(" s")
        self.ping_host.setFixedWidth(132)
        self._configure_tool_spin(self.ping_count, width=72)
        self._configure_tool_spin(self.ping_every, width=96)
        bind_text_draft(
            self.ping_host,
            lambda value: self._edits.set_draft("command.ping_host", value),
        )
        bind_numeric_edited(
            self.ping_count,
            lambda value: self._edits.set_draft("command.ping_count", value),
        )
        bind_numeric_edited(
            self.ping_every,
            lambda value: self._edits.set_draft("command.ping_every_s", value),
        )
        ping = QWidget()
        grid = QGridLayout(ping)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)
        grid.addWidget(QLabel("Ping"), 0, 0)
        grid.addWidget(self.ping_host, 0, 1, 1, 3)
        grid.addWidget(QLabel("Count"), 1, 0)
        grid.addWidget(self.ping_count, 1, 1)
        grid.addWidget(QLabel("Every"), 1, 2)
        grid.addWidget(self.ping_every, 1, 3)
        self.ping_on_button = _publish_button(
            "ON", register, IntentKind.SET_PING_MONITOR, lambda: self._ping(True)
        )
        self.ping_off_button = _publish_button(
            "OFF", register, IntentKind.SET_PING_MONITOR, lambda: self._ping(False)
        )
        self.ping_on_button.setObjectName("nguiDeveloperPingOn")
        self.ping_off_button.setObjectName("nguiDeveloperPingOff")
        self.ping_button_group = QButtonGroup(self)
        self.ping_button_group.setExclusive(True)
        for button in (self.ping_on_button, self.ping_off_button):
            button.setCheckable(True)
            self.ping_button_group.addButton(button)
        root.addWidget(
            self._tool_section("PING", ping, self._status_label("command.ping_status"),
                               (self.ping_on_button, self.ping_off_button)),
            1,
        )
        self.sms_sender = QLineEdit()
        self.sms_sender.setObjectName("nguiDeveloperSmsSender")
        self.sms_sender.setPlaceholderText("+38164123456")
        self.sms_keyword = QLineEdit()
        self.sms_keyword.setObjectName("nguiDeveloperSmsKeyword")
        self.sms_every = QSpinBox()
        self.sms_every.setObjectName("nguiDeveloperSmsEvery")
        for widget in (self.sms_sender, self.sms_keyword, self.sms_every):
            stamp_draft_control(widget)
        self.sms_every.setRange(121, 3600)
        self.sms_sender.setFixedWidth(132)
        self.sms_keyword.setFixedWidth(88)
        self._configure_tool_spin(self.sms_every, width=72)
        sms_seconds = QWidget()
        seconds_layout = QHBoxLayout(sms_seconds)
        seconds_layout.setContentsMargins(0, 0, 0, 0)
        seconds_layout.setSpacing(2)
        seconds_layout.addWidget(self.sms_every)
        seconds_layout.addWidget(QLabel("s"))
        bind_text_draft(
            self.sms_sender,
            lambda value: self._edits.set_draft("command.sms_sender", value),
        )
        bind_text_draft(
            self.sms_keyword,
            lambda value: self._edits.set_draft("command.sms_keyword", value),
        )
        bind_numeric_edited(
            self.sms_every,
            lambda value: self._edits.set_draft("command.sms_every_s", value),
        )
        sms = QWidget()
        grid = QGridLayout(sms)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)
        self.sms_config_widgets: list[QWidget] = []
        for column, (label, widget) in enumerate(
            (
                ("Sender", self.sms_sender),
                ("Keyword", self.sms_keyword),
                ("Every", sms_seconds),
            )
        ):
            label_widget = QLabel(label)
            self.sms_config_widgets.extend((label_widget, widget))
            grid.addWidget(label_widget, 0, column)
            grid.addWidget(widget, 1, column)
        self.sms_on_button = QPushButton("ON")
        self.sms_on_button.setProperty("nguiRole", "user_action")
        bind_button(
            self.sms_on_button,
            register,
            IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,
            lambda: self._sms(True),
        )
        self.sms_on_button.setObjectName("nguiDeveloperSmsOn")
        self.sms_off_button = QPushButton("OFF")
        self.sms_off_button.setProperty("nguiRole", "user_action")
        bind_button(
            self.sms_off_button,
            register,
            IntentKind.CONFIGURE_SMS_COMMAND_CONTROL,
            lambda: self._sms(False),
        )
        self.sms_off_button.setObjectName("nguiDeveloperSmsOff")
        self.sms_button_group = QButtonGroup(self)
        self.sms_button_group.setExclusive(True)
        for button in (self.sms_on_button, self.sms_off_button):
            button.setCheckable(True)
            self.sms_button_group.addButton(button)
        root.addWidget(
            self._tool_section("SMS commands", sms, self._status_label("command.sms_message"),
                               (self.sms_on_button, self.sms_off_button)),
            1,
        )
        for button in (self.autopilot_on_button, self.autopilot_off_button,
                       self.explore_on_button, self.explore_off_button,
                       self.ping_on_button, self.ping_off_button,
                       self.sms_on_button, self.sms_off_button):
            button.setFixedWidth(40)
            button.setFixedHeight(28)
            button.setProperty("nguiRole", "state_choice")
            button.setStyleSheet(
                "QPushButton { min-height: 24px; max-height: 24px; padding: 1px 4px; }"
            )
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.sequence_bands = QLineEdit()
        self.sequence_bands.setObjectName("nguiDeveloperSequenceBands")
        self.sequence_bands.setPlaceholderText("1,3,20")
        self.sequence_delay = QSpinBox()
        self.sequence_delay.setObjectName("nguiDeveloperSequenceDelay")
        stamp_draft_control(self.sequence_bands)
        stamp_draft_control(self.sequence_delay)
        self.sequence_delay.setRange(MIN_BAND_SEQUENCE_INTERVAL_S, 3600)
        self.sequence_delay.setValue(MIN_BAND_SEQUENCE_INTERVAL_S)
        self.sequence_delay.setSuffix(" sec")
        self.sequence_delay.setToolTip(
            f"Minimum {MIN_BAND_SEQUENCE_INTERVAL_S} seconds. "
            "The next step waits this long after the previous step completes."
        )
        self.sequence_bands.setFixedWidth(92)
        self._configure_tool_spin(self.sequence_delay, width=132)
        sequence = QWidget()
        grid = QGridLayout(sequence)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)
        grid.addWidget(QLabel("Add bands"), 0, 0)
        grid.addWidget(self.sequence_bands, 0, 1)
        grid.addWidget(QLabel("Step delay"), 1, 0)
        grid.addWidget(self.sequence_delay, 1, 1)
        self.sequence_plan_button = _publish_button(
            "Plan", register, IntentKind.PLAN_BAND_SEQUENCE, self._sequence
        )
        self.sequence_cancel_button = _publish_button(
            "Cancel seq", register, IntentKind.CANCEL_BAND_SEQUENCE
        )
        self.sequence_plan_button.setObjectName("nguiDeveloperSequencePlan")
        self.sequence_cancel_button.setObjectName("nguiDeveloperSequenceCancel")
        grid.addWidget(self.sequence_plan_button, 0, 2)
        grid.addWidget(self.sequence_cancel_button, 1, 2)
        root.addWidget(
            self._tool_section(
                "BAND SEQUENCE",
                sequence,
                self._status_label("command.band_sequence_status"),
            ),
            1,
        )

        self.schedule_bands = QLineEdit()
        self.schedule_time = QLineEdit()
        self.schedule_bands.setObjectName("nguiDeveloperScheduleBands")
        self.schedule_time.setObjectName("nguiDeveloperScheduleTime")
        for widget in (
            self.schedule_bands,
            self.schedule_time,
        ):
            stamp_draft_control(widget)
        self.schedule_time.setPlaceholderText("08:30 or +60s")
        self.schedule_bands.setFixedWidth(92)
        self.schedule_time.setFixedWidth(132)
        schedule = QWidget()
        grid = QGridLayout(schedule)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)
        for row, (label, widget) in enumerate(
            (
                ("Bands", self.schedule_bands),
                ("Start time", self.schedule_time),
            )
        ):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        schedule_help = QLabel(
            "HH:MM repeats daily; +seconds runs once. ADD and REMOVE are separate rules."
        )
        schedule_help.setObjectName("nguiDeveloperScheduleTimeHelp")
        schedule_help.setWordWrap(True)
        grid.addWidget(schedule_help, 3, 0, 1, 3)
        self.schedule_add_button = _publish_button(
            "ADD", register, IntentKind.PLAN_BAND_SCHEDULE, self._schedule_add
        )
        self.schedule_remove_button = _publish_button(
            "REMOVE", register, IntentKind.PLAN_BAND_SCHEDULE, self._schedule_remove
        )
        self.schedule_cancel_button = _publish_button(
            "Clear all", register, IntentKind.CANCEL_BAND_SCHEDULE
        )
        self.schedule_add_button.setObjectName("nguiDeveloperScheduleAdd")
        self.schedule_remove_button.setObjectName("nguiDeveloperScheduleRemove")
        self.schedule_cancel_button.setObjectName("nguiDeveloperScheduleCancel")
        grid.addWidget(self.schedule_add_button, 0, 2)
        grid.addWidget(self.schedule_remove_button, 1, 2)
        grid.addWidget(self.schedule_cancel_button, 2, 2)
        root.addWidget(
            self._tool_section(
                "BAND SCHEDULE",
                schedule,
                self._status_label("command.band_schedule_status"),
            ),
            1,
        )
        self._availability_widgets = (
            self.ping_host,
            self.ping_count,
            self.ping_every,
            self.ping_on_button,
            self.ping_off_button,
            self.sms_sender,
            self.sms_keyword,
            self.sms_every,
            self.sms_on_button,
            self.sms_off_button,
            self.sequence_bands,
            self.sequence_delay,
            self.sequence_plan_button,
            self.sequence_cancel_button,
            self.schedule_bands,
            self.schedule_time,
            self.schedule_add_button,
            self.schedule_remove_button,
            self.schedule_cancel_button,
        )

    @property
    def field_ids(self) -> frozenset[str]:
        return frozenset(_DC_TOOL_READBACK)

    def render(self, truth: PageTruthSnapshot) -> None:
        edit_snapshot = self._edits.snapshot()
        values = edit_snapshot.visible
        autopilot = truth.fields["intelligence.autopilot_requested"]
        autopilot_available = truth.fields["intelligence.control_available"]
        requested = (
            autopilot.state is PresentationState.VALID
            and str(autopilot.value or autopilot.text).strip().upper() == "ON"
        )
        self.autopilot_on_button.setChecked(requested)
        self.autopilot_off_button.setChecked(not requested)
        explore = truth.fields["intelligence.explore_requested"]
        explore_requested = (
            explore.state is PresentationState.VALID
            and str(explore.value or explore.text).strip().upper() == "ON"
        )
        self.explore_on_button.setChecked(explore_requested)
        self.explore_off_button.setChecked(not explore_requested)
        control_available = (
            autopilot_available.state is PresentationState.VALID
            and autopilot_available.value is True
        )
        tooltip = (
            (
                autopilot_available.reason
                or "Brain becomes available after qualification."
            )
            if not control_available
            else (
                autopilot.reason
                if autopilot.state is not PresentationState.VALID
                else ""
            )
        )
        for button in (
            self.autopilot_on_button,
            self.autopilot_off_button,
        ):
            button.setEnabled(control_available)
            button.setToolTip(tooltip)
        self.explore_on_button.setEnabled(control_available)
        self.explore_on_button.setToolTip(
            "" if control_available
            else "Brain must be healthy before Explore starts."
        )
        self.explore_off_button.setEnabled(True)
        self.explore_off_button.setToolTip("")
        if (
            "command.sms_enabled" in edit_snapshot.dirty_fields
            or SMS_GROUP in edit_snapshot.pending_groups
        ):
            sms_enabled = bool(values["command.sms_enabled"])
        else:
            sms_enabled = (
                truth.fields["command.sms_enabled"].state is PresentationState.VALID
                and bool(truth.fields["command.sms_enabled"].value)
            )
        sms_capability = truth.fields["capability.sms_supported"]
        sms_supported = (
            sms_capability.state is PresentationState.VALID
            and sms_capability.value is True
        )
        for field_id, label in self._status_labels.items():
            field = truth.fields[field_id]
            text = field.text
            if text in {"Downstream health monitor is off.",
                        "Band sequence requests are idle.",
                        "Band scheduling requests are idle."}:
                text = ""
            if label.text() != text:
                label.setText(text)
            label.setVisible(bool(text))
            label.setToolTip(
                field.reason if field.state is not PresentationState.VALID else ""
            )
        self._sync_text(self.ping_host, values["command.ping_host"])
        self._sync_spin(self.ping_count, values["command.ping_count"])
        self._sync_spin(self.ping_every, values["command.ping_every_s"])
        ping_enabled = bool(values["command.ping_enabled"])
        self.ping_on_button.setChecked(ping_enabled)
        self.ping_off_button.setChecked(not ping_enabled)
        self._sync_text(self.sms_sender, values["command.sms_sender"])
        self._sync_text(self.sms_keyword, values["command.sms_keyword"])
        self._sync_spin(self.sms_every, values["command.sms_every_s"])
        self.sms_on_button.setChecked(sms_enabled)
        self.sms_off_button.setChecked(not sms_enabled)
        self._status_labels["command.sms_message"].setVisible(sms_enabled and sms_supported)
        for widget in self.sms_config_widgets:
            widget.setVisible(sms_enabled and sms_supported)
        availability = truth.fields["action.control_availability"]
        availability_value = (
            availability.value
            if availability.state is PresentationState.VALID
            and isinstance(availability.value, Mapping)
            else None
        )
        workflow_decision = control_availability_from_mapping(
            availability_value,
            WORKFLOW_CONTROL,
        )
        sequence_decision = control_availability_from_mapping(
            availability_value,
            WORKFLOW_CONTROL,
            "band_selection",
        )
        schedule_decision = control_availability_from_mapping(
            availability_value,
            WORKFLOW_CONTROL,
            "band_selection",
        )
        sms_availability_widgets = (
            self.sms_sender,
            self.sms_keyword,
            self.sms_every,
            self.sms_on_button,
            self.sms_off_button,
        )
        sequence_plan_widgets = (
            self.sequence_bands,
            self.sequence_delay,
            self.sequence_plan_button,
        )
        schedule_plan_widgets = (
            self.schedule_bands,
            self.schedule_time,
            self.schedule_add_button,
            self.schedule_remove_button,
        )
        for widget in self._availability_widgets:
            decision = workflow_decision
            if widget in sequence_plan_widgets:
                decision = sequence_decision
            elif widget in schedule_plan_widgets:
                decision = schedule_decision
            allowed = bool(decision.enabled)
            reason = "" if decision.enabled else decision.reason
            if widget in sms_availability_widgets and not sms_supported:
                allowed = False
                reason = "SMS is unsupported by this modem profile."
            widget.setEnabled(allowed)
            widget.setToolTip(reason)

    def _status_label(self, field_id: str) -> _ToolStatus:
        label = _ToolStatus()
        label.setText("----")
        label.setObjectName(f"value__{field_id.replace('.', '__')}")
        self._status_labels[field_id] = label
        return label

    @staticmethod
    def _tool_section(title: str, controls: QWidget, status: _ToolStatus,
                      buttons: tuple[QPushButton, ...] = ()) -> QWidget:
        section = QWidget()
        section.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        content = QWidget()
        content.setObjectName("nguiToolContent")
        content.setFixedWidth(_Tools.CONTENT_WIDTH)
        content.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(3)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)
        title_label = QLabel(title)
        if buttons:
            title_label.setFixedWidth(120)
        heading.addWidget(title_label)
        for button in buttons:
            heading.addWidget(button)
        heading.addStretch(1)
        content_layout.addLayout(heading)
        controls.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        controls.setMaximumWidth(_Tools.CONTENT_WIDTH)
        content_layout.addWidget(
            controls, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        layout.addWidget(content, 0, Qt.AlignHCenter | Qt.AlignTop)
        layout.addWidget(status, 1)
        return section

    def _ping(self, enabled: bool) -> dict:
        self._edits.set_draft("command.ping_enabled", enabled)
        return dict(self._edits.request_apply(PING_GROUP))

    def _sms(self, enabled: bool) -> dict:
        self._edits.set_draft("command.sms_enabled", enabled)
        payload = dict(self._edits.request_apply(SMS_GROUP))
        payload.update(self._edits.payload(SMS_SENDER_GROUP))
        return payload

    @staticmethod
    def _configure_tool_spin(widget: QSpinBox, *, width: int = 132) -> None:
        widget.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        widget.setSingleStep(1)
        widget.setFixedWidth(width)

    @staticmethod
    def _sync_text(widget: QLineEdit, value: object) -> None:
        text = str(value or "")
        if widget.text() == text:
            return
        blocker = QSignalBlocker(widget)
        widget.setText(text)
        del blocker

    @staticmethod
    def _sync_spin(widget: QSpinBox, value: object) -> None:
        numeric = max(widget.minimum(), min(widget.maximum(), int(round(float(value)))))
        if widget.value() == numeric:
            return
        blocker = QSignalBlocker(widget)
        widget.setValue(numeric)
        del blocker

    def _sequence(self) -> dict:
        return {
            "bands": self.sequence_bands.text(),
            "delay_s": self.sequence_delay.value(),
        }

    def _schedule(self, operation: str) -> dict:
        return {
            "bands": self.schedule_bands.text(),
            "operation": operation,
            "start_time": self.schedule_time.text(),
        }

    def _schedule_add(self) -> dict:
        return self._schedule("add")

    def _schedule_remove(self) -> dict:
        return self._schedule("remove")


class DeveloperControlPage(QWidget):
    """Render Developer Control with CA and manual actions inside its middle column."""

    LIVE_WIDTH = 260
    OPERATIONS_WIDTH = 736
    TOOLS_MIN_WIDTH = 260

    def __init__(
        self,
        register: NGUIInteractionRegister,
        edits: NGUIEditRegister,
        confirmation: NGUIConfirmationCoordinator | None = None,
    ) -> None:
        super().__init__()
        if confirmation is None:
            raise TypeError(
                "DeveloperControlPage requires the window confirmation coordinator"
            )
        self.live = _TruthTable(
            PageId.DEVELOPER_CONTROL,
            _DC_LIVE_QUALITY,
            label_overrides={
                "quality.rtt_delta_ms": "RTT spike",
                "quality.ca_ld_pct": "CAST_LD",
                "quality.ca_ld_activity": "CART_LD ACT.",
                "quality.cart_activity": "CART ACT.",
                "quality.ri_stability_pct": "LTE RI %",
                "quality.ri_activity": "LTE RI ACT.",
            },
            spread_rows=True,
        )
        self.scheduler = _SchedulerTable()
        self.router_health = _TruthTable(
            PageId.DEVELOPER_CONTROL,
            ("device.router_cpu_load_pct", "device.router_memory_used_pct",
             "device.temperature_cpu_c"),
            label_overrides={
                "device.router_cpu_load_pct": "CPU",
                "device.router_memory_used_pct": "Memory",
                "device.temperature_cpu_c": "Temp",
            },
        )
        self.rf = _RadioMatrix()
        self.congestion = _CongestionMatrix()
        self.provider = _TruthTable(PageId.DEVELOPER_CONTROL, _DC_PROVIDER, label_overrides={
            "radio.dl_bandwidth_mhz": "DL BW", "nr.bandwidth_mhz": "DL BW",
            "nr.dl_frequency_mhz": "DL freq",
        })
        self.tools = _Tools(register, edits)
        self.manual = _ManualActions(register, confirmation, self.tools.schedule_add_button)
        self.ca_summary = _TruthTable(
            PageId.DEVELOPER_CONTROL,
            ("radio.ca_pool", "radio.ca_active", "radio.carrier_details"),
            columns=3,
            label_overrides={"radio.ca_pool": "CA Pool", "radio.ca_active": "CA status",
                             "radio.carrier_details": "CA details"},
        )
        self.ca_summary._labels["radio.carrier_details"].setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.ca_summary.layout().setColumnStretch(1, 2)

        live_box = _box("LIVE QUALITY", self.live)
        live_box.setFixedWidth(self.LIVE_WIDTH)
        live_box.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        operations = QWidget()
        operations.setStyleSheet("QGroupBox { margin-top: 0px; padding-top: 0px; }")
        operations_layout = QVBoxLayout(operations)
        operations_layout.setContentsMargins(0, 0, 0, 0)
        operations_layout.setSpacing(0)
        operations_layout.addWidget(_box("", self.ca_summary), 0)
        self.scheduler.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        scheduler_box = _box("", self.scheduler)
        rf_box = _box("", self.rf)
        operations_layout.addWidget(rf_box, 8)
        congestion_body = QWidget()
        congestion_layout = QVBoxLayout(congestion_body)
        congestion_layout.setContentsMargins(0, 0, 0, 0)
        congestion_layout.setSpacing(4)
        congestion_layout.addWidget(self.congestion)
        congestion_box = _box("", congestion_body)
        operations_layout.addWidget(congestion_box, 3)
        operations_layout.addWidget(scheduler_box, 6)
        for matrix in (self.rf, self.congestion, self.scheduler):
            matrix.layout().setVerticalSpacing(5)
            for row in range(matrix.layout().rowCount()):
                matrix.layout().setRowStretch(row, 1)
        operations.setFixedWidth(self.OPERATIONS_WIDTH)
        provider_box = _box("", self.provider)
        provider_box.setFixedWidth(self.LIVE_WIDTH)
        tools_box = _box("", self.tools)
        tools_box.setMinimumWidth(self.TOOLS_MIN_WIDTH)

        manual_box = _box("", self.manual)
        manual_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        operations_layout.addWidget(manual_box, 6)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(provider_box, 1)
        left_layout.addWidget(live_box, 1)
        self.router_health_region = _box("ROUTER HEALTH", self.router_health)
        self.router_health_region.setMinimumHeight(110)
        self.router_health_region.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        left_layout.addWidget(self.router_health_region, 0)
        left.setFixedWidth(self.LIVE_WIDTH)

        root = QGridLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setHorizontalSpacing(8)
        root.setVerticalSpacing(6)
        root.addWidget(left, 0, 0)
        root.addWidget(operations, 0, 1)
        root.addWidget(tools_box, 0, 2)
        root.setColumnStretch(0, 0)
        root.setColumnStretch(1, 0)
        root.setColumnStretch(2, 1)
        root.setRowStretch(0, 1)
        self.live_column = live_box
        self.operations_column = operations
        self.scheduler_region = scheduler_box
        self.rf_region = rf_box
        self.congestion_region = congestion_box
        self.tools_column = tools_box
        self.provider_column = provider_box
        self.manual_region = manual_box
        self.left_region = left
        self._field_sinks = (
            self.ca_summary,
            self.live,
            self.router_health,
            self.scheduler,
            self.rf,
            self.congestion,
            self.provider,
            self.tools,
            self.manual,
        )
        self._validate_field_contract()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._align_middle_rows()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._align_middle_rows()

    def _align_middle_rows(self) -> None:
        # The manual controls define shared content bounds. The middle column
        # keeps its fitted width so the tools column receives the spare space.
        self.operations_column.layout().activate()
        self.manual_region.layout().activate()
        self.manual.layout().activate()
        first = self.manual._input_groups[0].layout().itemAtPosition(0, 0).widget()
        left = first.mapTo(self.operations_column, QPoint()).x()
        right = self.manual.restart.mapTo(self.operations_column, QPoint()).x() + self.manual.restart.width()
        for table in (self.ca_summary, self.rf, self.congestion, self.scheduler):
            origin = table.mapTo(self.operations_column, QPoint()).x()
            table.layout().setContentsMargins(
                max(0, left - origin), 0, max(0, origin + table.width() - right), 0,
            )
            table.layout().activate()

    @property
    def field_ids(self) -> frozenset[str]:
        return getattr(
            self,
            "_field_ids",
            frozenset(
                field_id for sink in self._field_sinks for field_id in sink.field_ids
            ),
        )

    def _validate_field_contract(self) -> None:
        assigned = [
            field_id for sink in self._field_sinks for field_id in sink.field_ids
        ]
        duplicates = {field_id for field_id in assigned if assigned.count(field_id) > 1}
        page_specs = tuple(fields_for_page(PageId.DEVELOPER_CONTROL))
        page_fields = {spec.field_id for spec in page_specs}
        expected = {spec.field_id for spec in page_specs if spec.display_in_grid}
        control_only_consumed = set(assigned) - expected
        suppressed = expected - set(assigned)
        unexpected_duplicates = duplicates - _DC_CONTEXTUALLY_REPEATED
        missing_duplicates = _DC_CONTEXTUALLY_REPEATED - duplicates
        unexpected_control_only = control_only_consumed - _DC_CONTROL_ONLY_CONSUMED
        missing_control_only = _DC_CONTROL_ONLY_CONSUMED - control_only_consumed
        if (
            unexpected_duplicates
            or missing_duplicates
            or unexpected_control_only
            or missing_control_only
            or ((set(assigned) - control_only_consumed) | suppressed) != expected
        ):
            raise ValueError(
                "developer_control field contract failed: "
                f"unexpected_duplicates={sorted(unexpected_duplicates)}, "
                f"missing_contextual_sinks={sorted(missing_duplicates)}, "
                f"unexpected_control_only={sorted(unexpected_control_only)}, "
                f"missing_control_only={sorted(missing_control_only)}, "
                f"missing={sorted(expected - set(assigned) - suppressed)}, "
                f"extra={sorted(control_only_consumed)}, suppressed={sorted(suppressed)}"
            )
        unexpected_suppressed = suppressed - set(_DC_SUPPRESSED)
        missing_suppressed = set(_DC_SUPPRESSED) - suppressed
        if unexpected_suppressed or missing_suppressed:
            raise ValueError(
                "developer_control suppressed field contract failed: "
                f"unexpected={sorted(unexpected_suppressed)}, missing={sorted(missing_suppressed)}"
            )
        self.suppressed_field_ids = frozenset(suppressed)
        self.contextually_repeated_field_ids = _DC_CONTEXTUALLY_REPEATED
        self._field_ids = frozenset(expected)
        self._page_field_ids = frozenset(page_fields)

    def render(self, truth: PageTruthSnapshot) -> None:
        for sink in self._field_sinks:
            sink.render(truth)


__all__ = ("DeveloperControlPage",)
