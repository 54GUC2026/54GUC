"""Chart history, request processing, and atomic output without a GUI toolkit."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import Enum
from math import isclose, isfinite, nan
from threading import Condition, Event, RLock, Thread
from time import monotonic, time
from types import MappingProxyType
from typing import Mapping

from arch4.core_contracts.action_reachability import ReachabilityOutageInterval
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority

from .front_register import (
    FrontRegisterSnapshot,
    PresentationState,
    accepted_modem_source_generation,
)
from .registry import chart_marker_fields, fields_for_chart
from .types import ChartPane

CHART_WINDOW_OPTIONS = (
    ("5m", 300),
    ("15m", 900),
    ("1h", 3_600),
    ("2h", 7_200),
    ("6h", 21_600),
    ("12h", 43_200),
    ("24h", 86_400),
)
SUPPORTED_CHART_WINDOW_SECONDS = frozenset(
    seconds for _label, seconds in CHART_WINDOW_OPTIONS
)
DEFAULT_CHART_WINDOW_SECONDS = 300
DEFAULT_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS = 2.0
MIN_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS = 0.5
MAX_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS = 10.0
_CURRENT_INTERNET_FIELD_ID = "quality.internet_reachable"
_INTERNET_OUTAGE_EVENT_FIELD_ID = "quality.internet_outage_event"
_CONNECTIVITY_TRANSITION_FIELD_ID = "event.connectivity_transition"
_CONNECTIVITY_TRANSITION_SCHEMA = "FoundationConnectivityTransition.v2"
_CONNECTIVITY_TRANSITION_OWNER = "54guc.foundation.connectivity_transition"
_MEASUREMENT_CONTINUITY_SERIES_ID = "__measurement_available"
RF_CHART_FIELDS = frozenset({"rf.sinr_db", "rf.rsrp_dbm", "rf.rsrq_db", "rf.rssi_dbm"})


def mobile_data_is_off(fields: Mapping) -> bool:
    """Hide RF only for a valid, explicit data-switch OFF observation."""
    field = fields.get("device.mobile_data_enabled")
    return field is not None and field.state is PresentationState.VALID and field.value is False


_MEASUREMENT_SERIES_IDS = frozenset(
    spec.field_id for pane in (ChartPane.UPPER, ChartPane.LOWER)
    for spec in fields_for_chart(pane)
    if spec.field_id != _CURRENT_INTERNET_FIELD_ID
)
def chart_front_cursor(frame: FrontRegisterSnapshot) -> tuple[int, int]:
    """Return the two owners whose revisions can add truthful chart evidence."""

    if not isinstance(frame, FrontRegisterSnapshot):
        raise TypeError("chart front cursor requires a front register")
    internet = frame.fields.get(_CURRENT_INTERNET_FIELD_ID)
    reachability_sequence = (
        int(internet.source_sequence) if internet is not None else 0
    )
    return (
        accepted_modem_source_generation(frame),
        max(0, reachability_sequence),
    )


def compare_chart_front_cursors(
    candidate: tuple[int, int],
    accepted: tuple[int, int],
) -> int:
    """Compare component-monotonic modem and reachability revisions."""

    if candidate == accepted:
        return 0
    if all(new >= old for new, old in zip(candidate, accepted)):
        return 1
    if all(new <= old for new, old in zip(candidate, accepted)):
        return -1
    raise RuntimeError("chart front owner cursor is incomparable")


class ChartSampleSource(str, Enum):
    LIVE_FRONT_REGISTER = "live_front_register"
    PRODUCTION_TELEMETRY_HISTORY = "production_telemetry_history"


@dataclass(frozen=True, slots=True)
class ChartSampleProvenance:
    source: ChartSampleSource
    owner: str
    historical: bool
    session_scope_key: str = ""
    source_epoch: str = ""
    source_session_scope_key: str = ""
    source_sequence: int = 0
    source_file: str = ""
    source_line: int = 0

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("chart sample provenance owner must not be empty")
        if self.historical is not (
            self.source is ChartSampleSource.PRODUCTION_TELEMETRY_HISTORY
        ):
            raise ValueError("chart sample historical provenance mismatch")
        if self.historical and not self.session_scope_key:
            raise ValueError("historical chart samples require an exact session scope")
        if self.source is ChartSampleSource.LIVE_FRONT_REGISTER and (
            not self.session_scope_key
            or not self.source_epoch
            or self.source_sequence <= 0
        ):
            raise ValueError(
                "live chart samples require exact runtime generation provenance"
            )
        if self.historical and not self.source_session_scope_key:
            object.__setattr__(self, "source_session_scope_key", self.session_scope_key)
        if self.source_sequence < 0 or self.source_line < 0:
            raise ValueError("chart sample provenance counters must not be negative")


@dataclass(frozen=True, slots=True)
class ChartSample:
    timestamp: float
    values: Mapping[str, float | None]
    provenance: ChartSampleProvenance
    marker_values: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.timestamp <= 0 or not _is_finite(self.timestamp):
            raise ValueError("chart sample timestamp must be positive and finite")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(
            self,
            "marker_values",
            MappingProxyType(
                {
                    str(key): str(value)
                    for key, value in self.marker_values.items()
                    if str(value or "").strip()
                }
            ),
        )


MAX_HISTORICAL_SEED_CHUNK_SAMPLES = 500


@dataclass(frozen=True, slots=True)
class ChartHistorySeedTransaction:
    """A bounded history chunk used only for presentation."""

    transaction_id: str
    session_scope_key: str
    requested_at: float
    chunk_index: int
    chunk_count: int
    samples: tuple[ChartSample, ...]

    def __post_init__(self) -> None:
        if not self.transaction_id or not self.session_scope_key:
            raise ValueError("historical chart seed identity must not be empty")
        if self.requested_at <= 0:
            raise ValueError("historical chart seed request timestamp must be positive")
        if self.chunk_count < 1 or not 0 <= self.chunk_index < self.chunk_count:
            raise ValueError("historical chart seed chunk position is invalid")
        samples = tuple(self.samples)
        if len(samples) > MAX_HISTORICAL_SEED_CHUNK_SAMPLES:
            raise ValueError("historical chart seed chunk exceeds admission bound")
        if any(
            not sample.provenance.historical
            or sample.provenance.session_scope_key != self.session_scope_key
            or sample.timestamp > self.requested_at
            for sample in samples
        ):
            raise ValueError("historical chart seed sample provenance mismatch")
        if any(
            right.timestamp < left.timestamp
            for left, right in zip(samples, samples[1:])
        ):
            raise ValueError("historical chart seed chunk must be chronological")
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True, slots=True)
class ChartMarker:
    timestamp: float
    field_id: str
    previous: str
    current: str
    label: str
    lane: int = 0

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("chart marker timestamp must be positive")
        if self.field_id not in {spec.field_id for spec in chart_marker_fields()}:
            raise ValueError(f"undeclared chart marker field: {self.field_id}")
        if not self.label:
            raise ValueError("chart marker label must not be empty")
        if not 0 <= self.lane <= 4:
            raise ValueError("chart marker lane must be between zero and four")


@dataclass(frozen=True, slots=True)
class ChartOutageAnnotation:
    """One Core-measured completed outage plus NGUI-only label placement."""

    started_at: float
    restored_at: float
    duration_s: float
    lane: int = 0

    def __post_init__(self) -> None:
        for field_name in ("started_at", "restored_at", "duration_s"):
            value = float(getattr(self, field_name))
            if not _is_finite(value) or value < 0.0:
                raise ValueError(f"chart outage {field_name} must be finite and non-negative")
            object.__setattr__(self, field_name, value)
        if self.started_at <= 0.0 or self.restored_at < self.started_at:
            raise ValueError("chart outage interval is invalid")
        if not 0 <= self.lane <= 7:
            raise ValueError("chart outage lane must be between zero and seven")

    @property
    def label(self) -> str:
        return f"{self.duration_s:.1f} s"


@dataclass(frozen=True, slots=True)
class _MeasurementOutage:
    """One owner-declared interval in which live measurements did not exist."""

    started_at: float
    restored_at: float

    def __post_init__(self) -> None:
        started = float(self.started_at)
        restored = float(self.restored_at)
        if (
            not _is_finite(started)
            or not _is_finite(restored)
            or started <= 0.0
            or restored < started
        ):
            raise ValueError("chart measurement outage interval is invalid")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "restored_at", restored)


@dataclass(frozen=True, slots=True)
class ChartCQPaint:
    line_width: float
    line_opacity: int
    star_points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if self.line_width <= 0:
            raise ValueError("CQ line width must be positive")
        if not 0 <= self.line_opacity <= 255:
            raise ValueError("CQ line opacity must be between 0 and 255")
        points = tuple((float(x), float(y)) for x, y in self.star_points)
        if any(not _is_finite(value) for point in points for value in point):
            raise ValueError("CQ star points must be finite")
        object.__setattr__(self, "star_points", points)


@dataclass(frozen=True, slots=True)
class ChartWindowRequest:
    sequence: int
    window_seconds: int
    requested_at: float
    session_scope_key: str
    source_epoch: str
    source_sequence: int = 0
    source_append_ts: float = 0.0

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("chart request sequence must be positive")
        if self.window_seconds not in SUPPORTED_CHART_WINDOW_SECONDS:
            raise ValueError("chart window is not a supported operator window")
        if self.requested_at <= 0:
            raise ValueError("chart request timestamp must be positive")
        if (
            not str(self.session_scope_key or "").strip()
            or not str(self.source_epoch or "").strip()
        ):
            raise ValueError("chart request runtime namespace is required")
        if self.source_sequence < 0 or self.source_append_ts < 0:
            raise ValueError("chart request source identity must not be negative")


@dataclass(frozen=True, slots=True)
class ChartRenderSnapshot:
    request_sequence: int
    window_seconds: int
    published_at: float
    session_scope_key: str
    source_epoch: str
    timestamps: tuple[float, ...]
    upper: Mapping[str, tuple[float, ...]]
    lower: Mapping[str, tuple[float, ...]]
    markers: tuple[ChartMarker, ...]
    measurement_connect: Mapping[str, tuple[int, ...]]
    cq_paint: ChartCQPaint
    x_range: tuple[float, float]
    upper_y_range: tuple[float, float]
    lower_y_range: tuple[float, float]
    source_sequence: int = 0
    source_append_ts: float = 0.0
    outages: tuple[ChartOutageAnnotation, ...] = ()

    def __post_init__(self) -> None:
        if self.request_sequence < 1:
            raise ValueError("chart snapshot sequence must be positive")
        if self.window_seconds not in SUPPORTED_CHART_WINDOW_SECONDS:
            raise ValueError("chart snapshot has unsupported operator window")
        if self.published_at <= 0:
            raise ValueError("chart snapshot publication timestamp must be positive")
        if (
            not str(self.session_scope_key or "").strip()
            or not str(self.source_epoch or "").strip()
        ):
            raise ValueError("chart snapshot runtime namespace is required")
        if self.source_sequence < 0 or self.source_append_ts < 0:
            raise ValueError("chart snapshot source identity must not be negative")
        timestamps = tuple(float(timestamp) for timestamp in self.timestamps)
        if any(not _is_finite(timestamp) for timestamp in timestamps):
            raise ValueError("chart snapshot timestamps must be finite")
        if any(right < left for left, right in zip(timestamps, timestamps[1:])):
            raise ValueError("chart snapshot timestamps must be monotonic")
        window_start = self.published_at - self.window_seconds
        if any(timestamp < window_start or timestamp > self.published_at for timestamp in timestamps):
            raise ValueError("chart snapshot timestamp outside requested window")
        upper = {field_id: tuple(values) for field_id, values in self.upper.items()}
        lower = {field_id: tuple(values) for field_id, values in self.lower.items()}
        measurement_connect = {
            field_id: tuple(values)
            for field_id, values in self.measurement_connect.items()
        }
        markers = tuple(self.markers)
        outages = tuple(self.outages)
        if any(
            marker.timestamp < window_start or marker.timestamp > self.published_at
            for marker in markers
        ):
            raise ValueError("chart marker outside requested window")
        if any(not isinstance(item, ChartOutageAnnotation) for item in outages):
            raise TypeError("chart outage annotations must be typed")
        if any(
            item.restored_at < window_start or item.restored_at > self.published_at
            for item in outages
        ):
            raise ValueError("chart outage annotation outside requested window")
        expected_upper = {spec.field_id for spec in fields_for_chart(ChartPane.UPPER)}
        expected_lower = {spec.field_id for spec in fields_for_chart(ChartPane.LOWER)}
        if set(upper) != expected_upper:
            raise ValueError("atomic chart snapshot upper-series contract mismatch")
        if set(lower) != expected_lower:
            raise ValueError("atomic chart snapshot lower-series contract mismatch")
        size = len(self.timestamps)
        if any(len(values) != size for values in (*upper.values(), *lower.values())):
            raise ValueError("atomic chart snapshot series lengths differ")
        if set(measurement_connect) != _MEASUREMENT_SERIES_IDS:
            raise ValueError("atomic chart snapshot measurement connection contract mismatch")
        for field_id, connections in measurement_connect.items():
            _validate_connection_series(
                field_id=field_id,
                values=({**upper, **lower})[field_id],
                connections=connections,
                expected_size=size,
                family="measurement",
            )
        timestamp_set = set(timestamps)
        if any(x not in timestamp_set for x, _y in self.cq_paint.star_points):
            raise ValueError("CQ star timestamp missing from atomic chart series")
        for name, axis_range in (
            ("x", self.x_range),
            ("upper y", self.upper_y_range),
            ("lower y", self.lower_y_range),
        ):
            if len(axis_range) != 2 or any(not _is_finite(value) for value in axis_range):
                raise ValueError(f"chart snapshot {name} range must contain two finite values")
            if axis_range[0] >= axis_range[1]:
                raise ValueError(f"chart snapshot {name} range must increase")
        if self.x_range != (window_start, self.published_at):
            raise ValueError("chart snapshot x range differs from requested window")
        object.__setattr__(self, "timestamps", timestamps)
        object.__setattr__(self, "upper", MappingProxyType(upper))
        object.__setattr__(self, "lower", MappingProxyType(lower))
        object.__setattr__(self, "markers", markers)
        object.__setattr__(self, "outages", outages)
        object.__setattr__(self, "measurement_connect", MappingProxyType(measurement_connect))
        object.__setattr__(self, "x_range", tuple(float(value) for value in self.x_range))
        object.__setattr__(
            self,
            "upper_y_range",
            tuple(float(value) for value in self.upper_y_range),
        )
        object.__setattr__(
            self,
            "lower_y_range",
            tuple(float(value) for value in self.lower_y_range),
        )


class NGUIChartHistory:
    """Store bounded chart history without deriving visible values."""

    def __init__(
        self,
        *,
        max_samples: int = 200_000,
        max_markers: int = 20_000,
        native_sample_interval_s: float = DEFAULT_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        if max_samples < 1 or max_markers < 1:
            raise ValueError("chart history limits must be positive")
        self._lock = RLock()
        self._max_samples = max_samples
        self._max_markers = max_markers
        self._native_sample_interval_s = _chart_native_sample_interval(
            native_sample_interval_s
        )
        self._series = tuple(
            spec.field_id
            for pane in ChartPane
            for spec in fields_for_chart(pane)
        )
        self._markers_declared = tuple(spec.field_id for spec in chart_marker_fields())
        self._timestamps: list[float] = []
        self._samples: list[ChartSample] = []
        self._markers: list[ChartMarker] = []
        self._outages: list[ChartOutageAnnotation] = []
        self._measurement_outages: list[_MeasurementOutage] = []
        self._mobile_off_intervals: list[_MeasurementOutage] = []
        self._mobile_off_started: float | None = None
        self._identity: dict[str, str] = {}
        self._connectivity_transition_sequence = 0
        self._connectivity_transition_signature: tuple[object, ...] | None = None
        self._active_acquisition_outage_started_at: float | None = None
        self._session_scope_key = ""
        self._source_epoch = ""
        self._seed_next_chunk: dict[str, int] = {}
        self._historical_seed_count = 0
        self._last_seed_transaction_id = ""

    def configure_native_sample_interval(self, seconds: float) -> None:
        """Set the runtime sample interval used by long-window buckets."""

        interval = _chart_native_sample_interval(seconds)
        with self._lock:
            self._native_sample_interval_s = interval

    def reset_runtime_state(self) -> tuple[Mapping, Mapping]:
        with self._lock:
            prior = self._reset_observation_unlocked()
            self._timestamps = []
            self._samples = []
            self._markers = []
            self._outages = []
            self._measurement_outages = []
            self._mobile_off_intervals = []
            self._mobile_off_started = None
            self._identity = {}
            self._connectivity_transition_sequence = 0
            self._connectivity_transition_signature = None
            self._active_acquisition_outage_started_at = None
            self._session_scope_key = ""
            self._source_epoch = ""
            self._seed_next_chunk = {}
            self._historical_seed_count = 0
            self._last_seed_transaction_id = ""
            result = self._reset_observation_unlocked()
            return MappingProxyType(prior), MappingProxyType(result)

    def _reset_observation_unlocked(self) -> dict:
        return {
            "timestamps": tuple(self._timestamps),
            "samples": tuple(self._samples),
            "markers": tuple(self._markers),
            "outages": tuple(self._outages),
            "measurement_outages": tuple(self._measurement_outages),
            "mobile_off_intervals": tuple(self._mobile_off_intervals),
            "mobile_off_started": self._mobile_off_started,
            "identity": dict(sorted(self._identity.items())),
            "session_scope_key": self._session_scope_key,
            "source_epoch": self._source_epoch,
            "seed_next_chunk": dict(sorted(self._seed_next_chunk.items())),
            "historical_seed_count": self._historical_seed_count,
            "last_seed_transaction_id": self._last_seed_transaction_id,
        }

    def bind_runtime_namespace(
        self,
        session_scope_key: str,
        source_epoch: str,
    ) -> bool:
        """Bind the live runtime and clear history from another runtime."""

        scope_key = str(session_scope_key or "").strip()
        epoch = str(source_epoch or "").strip()
        if not scope_key:
            raise ValueError("chart history session scope must not be empty")
        if not epoch:
            raise ValueError("chart history source epoch must not be empty")
        with self._lock:
            if (scope_key, epoch) == (
                self._session_scope_key,
                self._source_epoch,
            ):
                return False
            self._session_scope_key = scope_key
            self._source_epoch = epoch
            self._timestamps.clear()
            self._samples.clear()
            self._markers.clear()
            self._outages.clear()
            self._measurement_outages.clear()
            self._mobile_off_intervals.clear()
            self._mobile_off_started = None
            self._identity.clear()
            self._connectivity_transition_sequence = 0
            self._connectivity_transition_signature = None
            self._active_acquisition_outage_started_at = None
            self._seed_next_chunk.clear()
            self._historical_seed_count = 0
            self._last_seed_transaction_id = ""
            return True

    def append_front_register(self, frame: FrontRegisterSnapshot) -> None:
        transition = _chart_connectivity_transition(frame)
        values: dict[str, float | None] = {}
        for field_id in self._series:
            field = frame.fields[field_id]
            if field.state in {PresentationState.VALID, PresentationState.HELD}:
                if field_id == "quality.internet_reachable":
                    if type(field.value) is not bool:
                        raise TypeError(
                            "chart current Internet state requires a Boolean"
                        )
                    values[field_id] = 100.0 if field.value else 0.0
                    continue
                try:
                    values[field_id] = float(field.value)
                except (TypeError, ValueError):
                    values[field_id] = None
            else:
                values[field_id] = None
        marker_values = {
            field_id: frame.fields[field_id].text
            for field_id in self._markers_declared
            if frame.fields[field_id].state
            in {PresentationState.VALID, PresentationState.HELD}
        }
        with self._lock:
            if (
                frame.runtime_session_scope_key != self._session_scope_key
                or frame.runtime_source_epoch != self._source_epoch
            ):
                raise RuntimeError(
                    "chart front register runtime namespace is not active"
                )
            if self._timestamps and frame.published_at < self._timestamps[-1]:
                raise ValueError("chart presentation timestamps must be monotonic")
            self._apply_connectivity_transition_locked(transition)
            if mobile_data_is_off(frame.fields):
                if self._mobile_off_started is None:
                    self._mobile_off_started = frame.published_at
            elif self._mobile_off_started is not None:
                self._mobile_off_intervals.append(_MeasurementOutage(
                    self._mobile_off_started, frame.published_at))
                self._mobile_off_intervals = self._mobile_off_intervals[-self._max_markers:]
                self._mobile_off_started = None
            if self._active_acquisition_outage_started_at is not None:
                values = _without_measurements(values)
            self._timestamps.append(frame.published_at)
            self._samples.append(
                ChartSample(
                    frame.published_at,
                    values,
                    ChartSampleProvenance(
                        source=ChartSampleSource.LIVE_FRONT_REGISTER,
                        owner="arch4.ngui.front_register",
                        historical=False,
                        session_scope_key=self._session_scope_key,
                        source_epoch=frame.runtime_source_epoch,
                        source_sequence=accepted_modem_source_generation(frame),
                    ),
                    marker_values,
                )
            )
            overflow = len(self._samples) - self._max_samples
            if overflow > 0:
                del self._timestamps[:overflow]
                del self._samples[:overflow]
            self._append_identity_markers(frame)
            self._append_network_outage(frame)

    def _apply_connectivity_transition_locked(
        self,
        transition: tuple[int, str, float, float, float] | None,
    ) -> None:
        if transition is None:
            return
        sequence, state, outage_started_at, observed_at, outage_duration_s = transition
        signature = transition
        if sequence < self._connectivity_transition_sequence:
            raise RuntimeError("chart_connectivity_transition_sequence_regressed")
        if sequence == self._connectivity_transition_sequence:
            if signature != self._connectivity_transition_signature:
                raise RuntimeError("chart_connectivity_transition_sequence_reused")
            return

        if state in {"reconnecting", "degraded"}:
            if outage_duration_s != 0.0:
                raise ValueError("chart_active_connectivity_outage_duration_invalid")
            active_start = self._active_acquisition_outage_started_at
            if active_start is not None and not isclose(
                active_start,
                outage_started_at,
                abs_tol=1e-6,
            ):
                raise RuntimeError("chart_active_connectivity_outage_replaced")
            self._active_acquisition_outage_started_at = outage_started_at
            self._mask_live_measurement_interval_locked(
                started_at=outage_started_at,
                restored_at=None,
            )
        else:
            if outage_duration_s > 0.0:
                expected_duration = observed_at - outage_started_at
                if not isclose(
                    outage_duration_s,
                    expected_duration,
                    abs_tol=0.05,
                ):
                    raise ValueError("chart_connectivity_outage_duration_mismatch")
                self._mask_live_measurement_interval_locked(
                    started_at=outage_started_at,
                    restored_at=observed_at,
                )
                self._append_measurement_outage_locked(
                    started_at=outage_started_at,
                    restored_at=observed_at,
                )
            elif self._active_acquisition_outage_started_at is not None:
                raise RuntimeError("chart_connectivity_recovery_interval_missing")
            self._active_acquisition_outage_started_at = None

        self._connectivity_transition_sequence = sequence
        self._connectivity_transition_signature = signature

    def _append_measurement_outage_locked(
        self,
        *,
        started_at: float,
        restored_at: float,
    ) -> None:
        interval = _MeasurementOutage(started_at, restored_at)
        if interval in self._measurement_outages:
            return
        if (
            self._measurement_outages
            and interval.restored_at < self._measurement_outages[-1].restored_at
        ):
            raise RuntimeError("chart_measurement_outage_regressed")
        self._measurement_outages.append(interval)
        overflow = len(self._measurement_outages) - self._max_markers
        if overflow > 0:
            del self._measurement_outages[:overflow]

    def _mask_live_measurement_interval_locked(
        self,
        *,
        started_at: float,
        restored_at: float | None,
    ) -> None:
        masked: list[ChartSample] = []
        for sample in self._samples:
            inside = sample.timestamp >= started_at and (
                restored_at is None or sample.timestamp < restored_at
            )
            if inside:
                sample = replace(
                    sample,
                    values=_without_measurements(sample.values),
                )
            masked.append(sample)
        self._samples = masked

    def admit_historical_seed(self, transaction: ChartHistorySeedTransaction) -> int:
        """Merge a history chunk without advancing live chart freshness."""

        if not isinstance(transaction, ChartHistorySeedTransaction):
            raise TypeError("chart history seed must use the typed transaction")
        with self._lock:
            if transaction.session_scope_key != self._session_scope_key:
                raise ValueError("historical chart seed scope differs from active scope")
            expected = self._seed_next_chunk.get(transaction.transaction_id, 0)
            if transaction.chunk_index != expected:
                raise ValueError("historical chart seed chunk is out of order")
            existing_timestamps = set(self._timestamps)
            admitted: list[ChartSample] = []
            for sample in transaction.samples:
                if sample.timestamp in existing_timestamps:
                    continue
                if (any(interval.started_at <= sample.timestamp < interval.restored_at
                        for interval in self._measurement_outages)
                        or (self._active_acquisition_outage_started_at is not None
                            and sample.timestamp >= self._active_acquisition_outage_started_at)):
                    sample = replace(sample, values=_without_measurements(sample.values))
                admitted.append(sample)
                existing_timestamps.add(sample.timestamp)
            if admitted:
                self._samples.extend(admitted)
                self._samples.sort(
                    key=lambda sample: (
                        sample.timestamp,
                        0 if sample.provenance.historical else 1,
                    )
                )
                overflow = len(self._samples) - self._max_samples
                if overflow > 0:
                    del self._samples[:overflow]
                self._timestamps = [sample.timestamp for sample in self._samples]
                self._rebuild_identity_markers_locked()
                self._historical_seed_count += len(admitted)
            next_chunk = transaction.chunk_index + 1
            if next_chunk >= transaction.chunk_count:
                self._seed_next_chunk.pop(transaction.transaction_id, None)
                self._last_seed_transaction_id = transaction.transaction_id
            else:
                self._seed_next_chunk[transaction.transaction_id] = next_chunk
            return len(admitted)

    def seed_status(self) -> Mapping[str, object]:
        with self._lock:
            return MappingProxyType(
                {
                    "session_scope_key": self._session_scope_key,
                    "historical_seed_count": self._historical_seed_count,
                    "last_seed_transaction_id": self._last_seed_transaction_id,
                    "native_sample_interval_s": self._native_sample_interval_s,
                }
            )

    def build_snapshot(self, request: ChartWindowRequest) -> ChartRenderSnapshot:
        cutoff = request.requested_at - request.window_seconds
        with self._lock:
            start = bisect_left(self._timestamps, cutoff)
            end = bisect_right(self._timestamps, request.requested_at)
            samples = tuple(self._samples[start:end])
            marker_timestamps = [marker.timestamp for marker in self._markers]
            marker_start = bisect_left(marker_timestamps, cutoff)
            marker_end = bisect_right(marker_timestamps, request.requested_at)
            markers = tuple(self._markers[marker_start:marker_end])
            outages = tuple(
                item
                for item in self._outages
                if cutoff <= item.restored_at <= request.requested_at
            )
            measurement_outages = tuple(
                item
                for item in self._measurement_outages
                if item.restored_at >= cutoff
                and item.started_at <= request.requested_at
            )
            if (
                self._active_acquisition_outage_started_at is not None
                and self._active_acquisition_outage_started_at
                <= request.requested_at
            ):
                measurement_outages = (
                    *measurement_outages,
                    _MeasurementOutage(
                        started_at=self._active_acquisition_outage_started_at,
                        restored_at=request.requested_at,
                    ),
                )
            native_sample_interval_s = self._native_sample_interval_s
            mobile_off_intervals = tuple(self._mobile_off_intervals)
            if self._mobile_off_started is not None and self._mobile_off_started <= request.requested_at:
                mobile_off_intervals += (_MeasurementOutage(
                    self._mobile_off_started, request.requested_at + 0.000001),)
        timestamps = tuple(sample.timestamp for sample in samples)
        upper = _series_values(samples, ChartPane.UPPER)
        lower = _series_values(samples, ChartPane.LOWER)
        for field_id in RF_CHART_FIELDS.intersection(lower):
            lower[field_id] = tuple(
                nan if any(gap.started_at <= stamp < gap.restored_at for gap in mobile_off_intervals) else value
                for stamp, value in zip(timestamps, lower[field_id]))
        upper[_MEASUREMENT_CONTINUITY_SERIES_ID] = tuple(
            1.0
            if any(_is_finite(sample.values.get(field_id)) for field_id in _MEASUREMENT_SERIES_IDS)
            else 0.0
            for sample in samples
        )
        timestamps, upper, lower = _display_series(
            request.window_seconds,
            timestamps,
            upper,
            lower,
            native_sample_interval_s=native_sample_interval_s,
            measurement_outages=measurement_outages,
            radio_outages=mobile_off_intervals,
        )
        measurements_available = upper.pop(_MEASUREMENT_CONTINUITY_SERIES_ID)
        displayed = {
            field_id: (
                tuple(
                    value if availability >= 1.0 else nan
                    for value, availability in zip(values, measurements_available)
                )
                if field_id in _MEASUREMENT_SERIES_IDS
                else values
            )
            for field_id, values in {**upper, **lower}.items()
        }
        upper = {field_id: displayed[field_id] for field_id in upper}
        lower = {field_id: displayed[field_id] for field_id in lower}
        markers = _layout_markers(markers, request.window_seconds)
        return ChartRenderSnapshot(
            request_sequence=request.sequence,
            window_seconds=request.window_seconds,
            published_at=request.requested_at,
            session_scope_key=request.session_scope_key,
            source_epoch=request.source_epoch,
            timestamps=timestamps,
            upper=MappingProxyType(upper),
            lower=MappingProxyType(lower),
            markers=markers,
            measurement_connect=MappingProxyType(
                {
                    field_id: _measurement_connections(
                        timestamps,
                        displayed[field_id],
                        measurement_outages + (mobile_off_intervals if field_id in RF_CHART_FIELDS else ()),
                    )
                    for field_id in _MEASUREMENT_SERIES_IDS
                }
            ),
            cq_paint=_cq_paint(
                request.window_seconds,
                timestamps,
                lower["quality.cq_pct"],
            ),
            x_range=(cutoff, request.requested_at),
            upper_y_range=_upper_y_range(upper),
            lower_y_range=_lower_y_range(lower),
            source_sequence=request.source_sequence,
            source_append_ts=request.source_append_ts,
            outages=_layout_outages(outages, request.window_seconds),
        )

    def _append_identity_markers(self, frame: FrontRegisterSnapshot) -> None:
        for field_id in self._markers_declared:
            field = frame.fields[field_id]
            if field.state not in {PresentationState.VALID, PresentationState.HELD}:
                continue
            current = field.text
            previous = self._identity.get(field_id)
            self._identity[field_id] = current
            if previous is not None and previous != current:
                self._markers.append(
                    ChartMarker(
                        frame.published_at,
                        field_id,
                        previous,
                        current,
                        _marker_label(field_id, previous, current),
                    )
                )
        overflow = len(self._markers) - self._max_markers
        if overflow > 0:
            del self._markers[:overflow]

    def _rebuild_identity_markers_locked(self) -> None:
        self._markers.clear()
        self._identity.clear()
        for sample in self._samples:
            for field_id in self._markers_declared:
                current = str(sample.marker_values.get(field_id) or "").strip()
                if not current:
                    continue
                previous = self._identity.get(field_id)
                self._identity[field_id] = current
                if previous is not None and previous != current:
                    self._markers.append(
                        ChartMarker(
                            sample.timestamp,
                            field_id,
                            previous,
                            current,
                            _marker_label(field_id, previous, current),
                        )
                    )
        overflow = len(self._markers) - self._max_markers
        if overflow > 0:
            del self._markers[:overflow]

    def _append_network_outage(self, frame: FrontRegisterSnapshot) -> None:
        field = frame.fields.get(_INTERNET_OUTAGE_EVENT_FIELD_ID)
        if field is None or field.state not in {
            PresentationState.VALID,
            PresentationState.HELD,
        }:
            return
        raw = field.value
        if not isinstance(raw, Mapping):
            raise TypeError("chart Core outage event mapping required")
        if not raw:
            return
        interval = ReachabilityOutageInterval.from_mapping(raw)
        key = (interval.started_at, interval.restored_at)
        if any((item.started_at, item.restored_at) == key for item in self._outages):
            return
        if self._outages and interval.restored_at < self._outages[-1].restored_at:
            raise ValueError("chart Core outage event regressed")
        self._outages.append(
            ChartOutageAnnotation(
                started_at=interval.started_at,
                restored_at=interval.restored_at,
                duration_s=interval.duration_s,
            )
        )
        overflow = len(self._outages) - self._max_markers
        if overflow > 0:
            del self._outages[:overflow]


def _series_values(
    samples: tuple[ChartSample, ...],
    pane: ChartPane,
) -> dict[str, tuple[float, ...]]:
    return {
        spec.field_id: tuple(
            _render_number(spec.field_id, sample.values[spec.field_id])
            for sample in samples
        )
        for spec in fields_for_chart(pane)
    }


def _render_number(field_id: str, value: float | None) -> float:
    if value is None:
        return nan
    rendered = float(value)
    if field_id == "rf.rssi_dbm" and rendered >= 0.0:
        return nan
    if field_id == "quality.ca_ld_pct":
        return max(0.0, min(100.0, rendered))
    return rendered


def _display_series(
    window_seconds: int,
    timestamps: tuple[float, ...],
    upper: Mapping[str, tuple[float, ...]],
    lower: Mapping[str, tuple[float, ...]],
    *,
    native_sample_interval_s: float,
    measurement_outages: tuple[_MeasurementOutage, ...] = (),
    radio_outages: tuple[_MeasurementOutage, ...] = (),
) -> tuple[tuple[float, ...], dict[str, tuple[float, ...]], dict[str, tuple[float, ...]]]:
    """Apply the proven long-window point density before rendering."""
    if not timestamps:
        return timestamps, dict(upper), dict(lower)
    _validate_series_lengths(timestamps, upper, lower)
    factor = max(1, int(window_seconds / 3600))
    all_series = {**upper, **lower}
    shown_timestamps = timestamps
    shown = dict(all_series)
    if factor > 1:
        bucket_seconds = factor * _chart_native_sample_interval(
            native_sample_interval_s
        )
        buckets = _absolute_time_buckets(
            timestamps,
            bucket_seconds,
            split_at_outages=measurement_outages + radio_outages,
        )
        shown_timestamps, shown = _aggregate_buckets(timestamps, all_series, buckets)
        # A hard publication stop must survive averaging even when the short
        # contact loss was below the operator-message threshold. Clear it
        # before smoothing, so buffered CQ/congestion cannot cross the gap.
        availability = shown.get(_MEASUREMENT_CONTINUITY_SERIES_ID)
        if availability is not None:
            for field_id in _MEASUREMENT_SERIES_IDS.intersection(shown):
                shown[field_id] = tuple(value if available >= 1.0 else nan
                                       for value, available in zip(shown[field_id], availability))
        smooth_window = min(15, max(3, factor * 2 + 1))
        for field_id in (
            "quality.congestion_pct",
            "quality.jitter_trimmed_ms",
            "rf.sinr_db",
            "rf.rsrp_dbm",
            "rf.rsrq_db",
            "rf.rssi_dbm",
            "quality.cq_pct",
        ):
            if field_id in shown and len(shown[field_id]) >= smooth_window:
                reset_outages = measurement_outages if field_id in _MEASUREMENT_SERIES_IDS else ()
                if field_id in RF_CHART_FIELDS:
                    reset_outages += radio_outages
                shown[field_id] = _trailing_segment_mean(
                    shown_timestamps,
                    shown[field_id],
                    smooth_window,
                    reset_outages=reset_outages,
                )
    return (
        shown_timestamps,
        {field_id: shown[field_id] for field_id in upper},
        {field_id: shown[field_id] for field_id in lower},
    )


def _chart_native_sample_interval(seconds: float) -> float:
    try:
        interval = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("chart native sample interval must be numeric") from exc
    if not isfinite(interval) or interval <= 0.0:
        raise ValueError("chart native sample interval must be positive and finite")
    return max(
        MIN_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS,
        min(MAX_CHART_NATIVE_SAMPLE_INTERVAL_SECONDS, interval),
    )


def _validate_series_lengths(
    timestamps: tuple[float, ...],
    upper: Mapping[str, tuple[float, ...]],
    lower: Mapping[str, tuple[float, ...]],
) -> None:
    size = len(timestamps)
    if any(len(values) != size for values in (*upper.values(), *lower.values())):
        raise ValueError("chart display series lengths differ")


def _absolute_time_buckets(
    timestamps: tuple[float, ...],
    bucket_seconds: float,
    *,
    split_at_outages: tuple[_MeasurementOutage | ChartOutageAnnotation, ...] = (),
) -> tuple[tuple[int, ...], ...]:
    buckets: list[list[int]] = []
    previous_bucket: int | None = None
    for index, timestamp in enumerate(timestamps):
        bucket = int(timestamp / bucket_seconds)
        crossed_outage = bool(
            index > 0
            and _segment_crosses_outage(
                timestamps[index - 1],
                timestamp,
                split_at_outages,
            )
        )
        if bucket != previous_bucket or crossed_outage:
            buckets.append([])
            previous_bucket = bucket
        buckets[-1].append(index)
    return tuple(tuple(bucket) for bucket in buckets)


def _aggregate_buckets(
    timestamps: tuple[float, ...],
    series: Mapping[str, tuple[float, ...]],
    buckets: tuple[tuple[int, ...], ...],
) -> tuple[tuple[float, ...], dict[str, tuple[float, ...]]]:
    shown_timestamps = tuple(
        sum(timestamps[index] for index in indexes) / len(indexes)
        for indexes in buckets
    )
    shown: dict[str, tuple[float, ...]] = {}
    for field_id, values in series.items():
        if field_id in {"traffic.dl_mbps", "traffic.ul_mbps"}:
            aggregate = _finite_max
        elif field_id in {_CURRENT_INTERNET_FIELD_ID, _MEASUREMENT_CONTINUITY_SERIES_ID}:
            # A Boolean outage is evidence, not a percentage. One proven NO in
            # a long-window bucket must remain visible instead of being averaged
            # into a near-100 value.
            aggregate = _finite_min
        else:
            aggregate = _finite_mean
        shown[field_id] = tuple(
            aggregate(values[index] for index in indexes)
            for indexes in buckets
        )
    return shown_timestamps, shown


def _finite_mean(values) -> float:
    finite = [float(value) for value in values if _is_finite(value)]
    return sum(finite) / len(finite) if finite else nan


def _finite_max(values) -> float:
    finite = [float(value) for value in values if _is_finite(value)]
    return max(finite) if finite else nan


def _finite_min(values) -> float:
    finite = [float(value) for value in values if _is_finite(value)]
    return min(finite) if finite else nan


def _trailing_segment_mean(
    timestamps: tuple[float, ...],
    values: tuple[float, ...],
    width: int,
    *,
    reset_outages: tuple[_MeasurementOutage | ChartOutageAnnotation, ...] = (),
) -> tuple[float, ...]:
    if len(timestamps) != len(values):
        raise ValueError("chart smoothing timestamp and value lengths differ")
    result: list[float] = []
    segment_start = 0
    for index in range(len(values)):
        if index > 0 and (
            not _is_finite(values[index - 1])
            or _segment_crosses_outage(
                timestamps[index - 1],
                timestamps[index],
                reset_outages,
            )
        ):
            segment_start = index
        if not _is_finite(values[index]):
            result.append(nan)
            segment_start = index + 1
            continue
        start = max(segment_start, index - width + 1)
        result.append(_finite_mean(values[start : index + 1]))
    return tuple(result)


def _is_finite(value) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _without_measurements(
    values: Mapping[str, float | None],
) -> dict[str, float | None]:
    return {
        field_id: (None if field_id in _MEASUREMENT_SERIES_IDS else value)
        for field_id, value in values.items()
    }


def _chart_connectivity_transition(
    frame: FrontRegisterSnapshot,
) -> tuple[int, str, float, float, float] | None:
    field = frame.fields.get(_CONNECTIVITY_TRANSITION_FIELD_ID)
    if field is None or field.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return None
    raw = field.value
    if not isinstance(raw, Mapping):
        raise TypeError("chart_connectivity_transition_mapping_required")
    if not raw:
        return None
    if raw.get("schema") != _CONNECTIVITY_TRANSITION_SCHEMA:
        raise ValueError("chart_connectivity_transition_schema_invalid")
    if raw.get("owner") != _CONNECTIVITY_TRANSITION_OWNER:
        raise ValueError("chart_connectivity_transition_owner_invalid")
    sequence = raw.get("sequence")
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("chart_connectivity_transition_sequence_invalid")
    state = str(raw.get("new_state") or "").strip().lower()
    if state not in {"ok", "reconnecting", "degraded"}:
        raise ValueError("chart_connectivity_transition_state_invalid")
    try:
        outage_started_at = float(raw.get("outage_started_at"))
        observed_at = float(raw.get("observed_at"))
        outage_duration_s = float(raw.get("outage_duration_s"))
    except (TypeError, ValueError) as exc:
        raise ValueError("chart_connectivity_transition_time_invalid") from exc
    if (
        not all(
            isfinite(value)
            for value in (outage_started_at, observed_at, outage_duration_s)
        )
        or outage_started_at <= 0.0
        or observed_at < outage_started_at
        or outage_duration_s < 0.0
    ):
        raise ValueError("chart_connectivity_transition_time_invalid")
    return (
        sequence,
        state,
        outage_started_at,
        observed_at,
        outage_duration_s,
    )


def _segment_crosses_outage(
    left_ts: float,
    right_ts: float,
    outages: tuple[_MeasurementOutage | ChartOutageAnnotation, ...],
) -> bool:
    return any(
        left_ts < outage.restored_at and right_ts > outage.started_at
        for outage in outages
    )


def _measurement_connections(
    timestamps: tuple[float, ...],
    values: tuple[float, ...],
    outages: tuple[_MeasurementOutage | ChartOutageAnnotation, ...],
) -> tuple[int, ...]:
    if len(timestamps) != len(values):
        raise ValueError("chart connection series length differs")
    return tuple(
        1
        if (
            index > 0
            and _is_finite(values[index - 1])
            and _is_finite(value)
            and not _segment_crosses_outage(
                timestamps[index - 1],
                timestamps[index],
                outages,
            )
        )
        else 0
        for index, value in enumerate(values)
    )


def _validate_connection_series(
    *,
    field_id: str,
    values: tuple[float, ...],
    connections: tuple[int, ...],
    expected_size: int,
    family: str,
) -> None:
    if len(connections) != expected_size:
        raise ValueError(
            f"atomic chart snapshot {family} connection length differs"
        )
    if any(value not in {0, 1} for value in connections):
        raise ValueError(
            f"atomic chart snapshot {family} connections must be binary"
        )
    if connections and connections[0] != 0:
        raise ValueError(
            f"atomic chart snapshot {family} cannot connect before its first point"
        )
    if any(
        connected
        and (
            index == 0
            or not _is_finite(values[index - 1])
            or not _is_finite(values[index])
        )
        for index, connected in enumerate(connections)
    ):
        raise ValueError(
            f"atomic chart snapshot {family} connection crosses a gap: {field_id}"
        )


def _upper_y_range(upper: Mapping[str, tuple[float, ...]]) -> tuple[float, float]:
    values = tuple(
        float(value)
        for field_id in ("traffic.dl_mbps", "traffic.ul_mbps")
        for value in upper[field_id]
        if _is_finite(value)
    )
    if not values:
        return 0.0, 1.0
    maximum = max(max(values), 0.1)
    return 0.0, max(1.0, maximum * 1.2)


def _lower_y_range(lower: Mapping[str, tuple[float, ...]]) -> tuple[float, float]:
    values = tuple(
        float(value)
        for field_id in (
            "rf.sinr_db",
            "rf.rsrq_db",
            "rf.rsrp_dbm",
            "rf.rssi_dbm",
        )
        for value in lower[field_id]
        if _is_finite(value)
    )
    if not values:
        return -125.0, 25.0
    minimum = min(values)
    maximum = max(25.0, max(values))
    padding = 5.0 if isclose(minimum, maximum, abs_tol=0.001) else max(
        2.0,
        (maximum - minimum) * 0.10,
    )
    return minimum - padding, maximum


def _cq_paint(
    window_seconds: int,
    timestamps: tuple[float, ...],
    values: tuple[float, ...],
) -> ChartCQPaint:
    if window_seconds >= 6 * 3600:
        opacity, width = 200, 1.50
    elif window_seconds >= 2 * 3600:
        opacity, width = 215, 1.65
    elif window_seconds >= 3600:
        opacity, width = 230, 1.80
    else:
        opacity, width = 190, 1.50
    stars = ()
    if window_seconds < 3600:
        stars = tuple(
            (float(timestamp), float(value))
            for timestamp, value in zip(timestamps, values)
            if _is_finite(value)
        )
    return ChartCQPaint(width, opacity, stars)


def _marker_label(field_id: str, previous: str, current: str) -> str:
    if field_id not in {"radio.pband", "nr.band"}:
        raise ValueError(f"illegal non-band chart marker field: {field_id}")
    if field_id == "nr.band":
        previous = "n" + previous.lower().removeprefix("n")
        current = "n" + current.lower().removeprefix("n")
    return f"{previous}->{current}"


def _layout_markers(
    markers: tuple[ChartMarker, ...],
    window_seconds: int,
) -> tuple[ChartMarker, ...]:
    # A PBAND label occupies roughly five percent of the visible plot width.
    # Keep changes inside that footprint on separate vertical lanes.
    cluster_gap = max(8.0, window_seconds * 0.05)
    previous_timestamp: float | None = None
    lane = 0
    laid_out: list[ChartMarker] = []
    for marker in markers:
        if previous_timestamp is not None and marker.timestamp - previous_timestamp <= cluster_gap:
            lane += 1
        else:
            lane = 0
        previous_timestamp = marker.timestamp
        laid_out.append(replace(marker, lane=min(lane, 4)))
    return tuple(laid_out)


def _layout_outages(
    outages: tuple[ChartOutageAnnotation, ...],
    window_seconds: int,
) -> tuple[ChartOutageAnnotation, ...]:
    """Stagger nearby duration labels without changing Core outage facts."""

    label_gap = max(8.0, float(window_seconds) * 0.04)
    lane_last_at = [float("-inf")] * 8
    laid_out: list[ChartOutageAnnotation] = []
    for outage in outages:
        label_at = (outage.started_at + outage.restored_at) / 2.0
        lane = next(
            (
                index
                for index, previous_at in enumerate(lane_last_at)
                if label_at - previous_at > label_gap
            ),
            min(range(len(lane_last_at)), key=lane_last_at.__getitem__),
        )
        lane_last_at[lane] = label_at
        laid_out.append(replace(outage, lane=lane))
    return tuple(laid_out)


class NGUIChartRegister:
    """Hold the latest chart output for one runtime namespace."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._expected_sequence = 0
        self._session_scope_key = ""
        self._source_epoch = ""
        self._snapshot: ChartRenderSnapshot | None = None

    def bind_runtime_namespace(
        self,
        session_scope_key: str,
        source_epoch: str,
    ) -> bool:
        scope = str(session_scope_key or "").strip()
        epoch = str(source_epoch or "").strip()
        if not scope or not epoch:
            raise ValueError("chart output runtime namespace is required")
        with self._lock:
            if (scope, epoch) == (self._session_scope_key, self._source_epoch):
                return False
            self._session_scope_key = scope
            self._source_epoch = epoch
            self._snapshot = None
            return True

    def expect(self, request: ChartWindowRequest) -> None:
        if not isinstance(request, ChartWindowRequest):
            raise TypeError("chart output requires typed window request")
        with self._lock:
            if (
                request.session_scope_key != self._session_scope_key
                or request.source_epoch != self._source_epoch
            ):
                raise RuntimeError("chart request runtime namespace is not active")
            if request.sequence <= self._expected_sequence:
                raise ValueError("chart request sequence must increase")
            self._expected_sequence = request.sequence

    def publish(self, snapshot: ChartRenderSnapshot) -> bool:
        with self._lock:
            if (
                snapshot.session_scope_key != self._session_scope_key
                or snapshot.source_epoch != self._source_epoch
                or snapshot.request_sequence != self._expected_sequence
            ):
                return False
            self._snapshot = snapshot
            return True

    def snapshot(self) -> ChartRenderSnapshot | None:
        with self._lock:
            return self._snapshot

    def expected_sequence(self) -> int:
        with self._lock:
            return self._expected_sequence

    def runtime_namespace(self) -> tuple[str, str]:
        with self._lock:
            return self._session_scope_key, self._source_epoch

    def reset_runtime_state(self) -> tuple[Mapping, Mapping]:
        with self._lock:
            prior = self._reset_observation_unlocked()
            self._expected_sequence = 0
            self._session_scope_key = ""
            self._source_epoch = ""
            self._snapshot = None
            result = self._reset_observation_unlocked()
            return MappingProxyType(prior), MappingProxyType(result)

    def _reset_observation_unlocked(self) -> dict:
        return {
            "expected_sequence": self._expected_sequence,
            "session_scope_key": self._session_scope_key,
            "source_epoch": self._source_epoch,
            "snapshot": self._snapshot,
        }


class NGUIChartWorker:
    """Own the current chart request and one newer replacement."""

    def __init__(
        self,
        history: NGUIChartHistory,
        output: NGUIChartRegister,
        *,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("ngui_chart_worker_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("ngui_chart_worker_requires_fault_supervisor")
        self._history = history
        self._output = output
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._worker_id = "ngui_chart_worker"
        self._worker_health.register(
            self._worker_id,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=True,
            retryable=True,
        )
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._close_event = Event()
        self._closed = False
        self._active: tuple[
            ChartWindowRequest,
            Future[ChartRenderSnapshot],
        ] | None = None
        self._pending: tuple[
            ChartWindowRequest,
            Future[ChartRenderSnapshot],
        ] | None = None
        self._coalesced = 0
        self._retry_streak = 0
        self._thread = Thread(
            target=self._run,
            name="ngui-chart",
            daemon=True,
        )
        self._worker_health.mark_started(self._worker_id)
        self._thread.start()

    def request(self, request: ChartWindowRequest) -> Future[ChartRenderSnapshot]:
        if not isinstance(request, ChartWindowRequest):
            raise TypeError("chart worker requires typed window request")
        public: Future[ChartRenderSnapshot] = Future()
        with self._changed:
            if self._closed:
                raise RuntimeError("chart-worker admission is closed")
            for admitted in (self._pending, self._active):
                if admitted is None:
                    continue
                previous, future = admitted
                if (
                    previous.window_seconds == request.window_seconds
                    and previous.session_scope_key == request.session_scope_key
                    and previous.source_epoch == request.source_epoch
                    and previous.sequence == self._output.expected_sequence()
                    and not future.done()
                ):
                    self._coalesced += 1
                    return future
            self._retry_streak = 0
            # Keep the request under the same worker lock. Nothing between
            # this check and storage may fail.
            self._output.expect(request)
            if self._pending is not None:
                _old_request, old_public = self._pending
                old_public.cancel()
                self._coalesced += 1
            self._pending = (request, public)
            self._changed.notify()
        return public

    def bind_runtime_namespace(
        self,
        session_scope_key: str,
        source_epoch: str,
    ) -> None:
        scope = str(session_scope_key or "").strip()
        epoch = str(source_epoch or "").strip()
        with self._changed:
            if self._pending is not None:
                request, public = self._pending
                if (
                    request.session_scope_key != scope
                    or request.source_epoch != epoch
                ):
                    public.cancel()
                    self._pending = None
                    self._coalesced += 1
            self._changed.notify_all()

    def fence_and_drain_runtime_state(
        self,
        *,
        timeout_s: float = 2.0,
    ) -> tuple[Mapping, Mapping]:
        deadline = monotonic() + max(0.0, float(timeout_s))
        with self._changed:
            prior = self._reset_observation_unlocked()
            if self._pending is not None:
                _request, public = self._pending
                public.cancel()
                self._pending = None
                self._coalesced += 1
            self._changed.notify_all()
            while self._active is not None:
                remaining = deadline - monotonic()
                if remaining <= 0.0:
                    raise TimeoutError("chart runtime reset drain timed out")
                self._changed.wait(timeout=remaining)
            self._coalesced = 0
            self._retry_streak = 0
            result = self._reset_observation_unlocked()
            return MappingProxyType(prior), MappingProxyType(result)

    def _reset_observation_unlocked(self) -> dict:
        return {
            "active_request": (
                self._active[0] if self._active is not None else None
            ),
            "pending_request": (
                self._pending[0] if self._pending is not None else None
            ),
            "coalesced": self._coalesced,
            "retry_streak": self._retry_streak,
            "closed": self._closed,
        }

    def close(self, *, timeout_s: float = 2.0) -> Mapping[str, object]:
        budget_s = max(0.0, float(timeout_s))
        with self._changed:
            self._closed = True
            self._close_event.set()
            if self._pending is not None:
                _request, public = self._pending
                public.cancel()
                self._pending = None
            self._changed.notify_all()
        self._thread.join(timeout=budget_s)
        with self._lock:
            inflight = int(self._active is not None)
            terminated = not self._thread.is_alive()
        return MappingProxyType(
            {
                "owner": "ngui_chart_worker",
                "admission_closed": True,
                "inflight": inflight,
                "pending": False,
                "coalesced": self._coalesced,
                "terminated": terminated,
                "status": "complete" if terminated else "degraded",
            }
        )

    def status(self) -> Mapping[str, int | bool]:
        with self._lock:
            return MappingProxyType(
                {
                    "active": self._active is not None,
                    "pending": self._pending is not None,
                    "coalesced": self._coalesced,
                    "closed": self._closed,
                }
            )

    def _run(self) -> None:
        try:
            while True:
                with self._changed:
                    while self._pending is None and not self._closed:
                        self._changed.wait()
                    if self._closed:
                        return
                    request, public = self._pending
                    self._pending = None
                    self._active = (request, public)
                failure: Exception | None = None
                retry_delay_s = 0.0
                snapshot: ChartRenderSnapshot | None = None
                try:
                    snapshot = self._history.build_snapshot(request)
                except Exception as exc:
                    failure = exc
                    with self._lock:
                        self._retry_streak += 1
                        retry_delay_s = min(
                            1.0,
                            0.05 * (2 ** min(self._retry_streak - 1, 5)),
                        )
                    self._safe_diagnostic_call(
                        lambda exc=exc: self._worker_health.mark_failure(
                            self._worker_id,
                            exc,
                            next_retry_at=time() + retry_delay_s,
                        )
                    )
                    self._safe_diagnostic_call(
                        lambda exc=exc: self._fault_supervisor.record(
                            boundary=FaultBoundaryClass.GUI,
                            owner=self._worker_id,
                            operation="build_snapshot",
                            generation=request.sequence,
                            severity=FaultSeverity.ERROR,
                            retryable=True,
                            degraded_behavior="retain_last_chart_snapshot",
                            error=exc,
                            suppression_budget=3,
                        )
                    )
                if failure is not None and retry_delay_s > 0.0:
                    self._close_event.wait(retry_delay_s)
                with self._changed:
                    if self._closed:
                        self._active = None
                        self._changed.notify_all()
                        if not public.done():
                            public.cancel()
                        return
                    if failure is not None:
                        self._active = None
                        self._changed.notify_all()
                        still_expected = (
                            self._output.expected_sequence() == request.sequence
                        )
                        if still_expected and self._pending is None:
                            self._pending = (request, public)
                        elif not public.done():
                            public.cancel()
                        self._changed.notify()
                        continue
                    published = snapshot is not None and self._output.publish(snapshot)
                    if published and not public.done():
                        public.set_result(snapshot)
                    elif not public.done():
                        public.cancel()
                    self._active = None
                    self._changed.notify_all()
                if published:
                    with self._lock:
                        self._retry_streak = 0
                    self._safe_diagnostic_call(
                        lambda: self._worker_health.mark_success(
                            self._worker_id
                        )
                    )
        finally:
            with self._changed:
                active = self._active
                self._active = None
                pending = self._pending
                self._pending = None
            for item in (active, pending):
                if item is not None and not item[1].done():
                    item[1].cancel()
            self._safe_diagnostic_call(
                lambda: self._worker_health.mark_stopped(
                    self._worker_id,
                    reason="chart_worker_closed",
                )
            )

    @staticmethod
    def _safe_diagnostic_call(fn) -> None:
        try:
            fn()
        except BaseException:
            return


class NGUIChartProcessor:
    """Own chart state used by boundary adapters outside the GUI toolkit."""

    def __init__(
        self,
        history: NGUIChartHistory | None = None,
        *,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        self.history = history or NGUIChartHistory()
        self.output = NGUIChartRegister()
        self._worker = NGUIChartWorker(
            self.history,
            self.output,
            worker_health=worker_health,
            fault_supervisor=fault_supervisor,
        )
        self._lock = RLock()
        self._request_sequence = 0
        self._history_source_sequence = 0
        self._history_source_cursor = (0, 0)
        self._history_append_ts = 0.0
        self._session_scope_key = ""
        self._source_epoch = ""
        self._render_active = False

    def bind_runtime_namespace(
        self,
        session_scope_key: str,
        source_epoch: str,
    ) -> bool:
        """Bind chart history, work, and output to one runtime."""

        scope = str(session_scope_key or "").strip()
        epoch = str(source_epoch or "").strip()
        if not scope or not epoch:
            raise ValueError("chart runtime namespace is required")
        with self._lock:
            changed = (scope, epoch) != (
                self._session_scope_key,
                self._source_epoch,
            )
            self.history.bind_runtime_namespace(scope, epoch)
            self.output.bind_runtime_namespace(scope, epoch)
            self._worker.bind_runtime_namespace(scope, epoch)
            if changed:
                self._history_source_sequence = 0
                self._history_source_cursor = (0, 0)
                self._history_append_ts = 0.0
                self._session_scope_key = scope
                self._source_epoch = epoch
            return changed

    def configure_native_sample_interval(self, seconds: float) -> None:
        """Configure the chart grid from the active sample interval."""

        self.history.configure_native_sample_interval(seconds)

    def admit_historical_seed(self, transaction: ChartHistorySeedTransaction) -> int:
        """Accept presentation history without changing live freshness."""

        return self.history.admit_historical_seed(transaction)

    def append(self, frame: FrontRegisterSnapshot) -> bool:
        source_cursor = chart_front_cursor(frame)
        with self._lock:
            if compare_chart_front_cursors(
                source_cursor,
                self._history_source_cursor,
            ) <= 0:
                return False
            if (
                frame.runtime_session_scope_key != self._session_scope_key
                or frame.runtime_source_epoch != self._source_epoch
            ):
                raise RuntimeError(
                    "chart front register runtime namespace is not active"
                )
            self.history.append_front_register(frame)
            self._history_source_sequence = max(
                self._history_source_sequence + 1,
                *source_cursor,
            )
            self._history_source_cursor = source_cursor
            self._history_append_ts = float(frame.published_at)
            return True

    def set_render_active(
        self,
        *,
        active: bool,
        window_seconds: int,
        requested_at: float,
    ) -> Future[ChartRenderSnapshot] | None:
        """Own graph-page demand and render once when it becomes active."""

        with self._lock:
            self._render_active = bool(active)
            if not self._render_active:
                return None
            if not self._session_scope_key or not self._source_epoch:
                return None
            return self._request_snapshot_unlocked(
                window_seconds=window_seconds,
                requested_at=requested_at,
            )

    def refresh_if_active(
        self,
        *,
        window_seconds: int,
        requested_at: float,
    ) -> Future[ChartRenderSnapshot] | None:
        """Refresh expensive graph output only while Graphs is selected."""

        with self._lock:
            if not self._render_active:
                return None
            if not self._session_scope_key or not self._source_epoch:
                raise RuntimeError("active chart render has no runtime namespace")
            return self._request_snapshot_unlocked(
                window_seconds=window_seconds,
                requested_at=requested_at,
            )

    def _request_snapshot_unlocked(
        self,
        *,
        window_seconds: int,
        requested_at: float,
    ) -> Future[ChartRenderSnapshot]:
        self._request_sequence += 1
        request = ChartWindowRequest(
            self._request_sequence,
            window_seconds,
            requested_at,
            session_scope_key=self._session_scope_key,
            source_epoch=self._source_epoch,
            source_sequence=self._history_source_sequence,
            source_append_ts=self._history_append_ts,
        )
        return self._worker.request(request)

    def status(self) -> Mapping[str, object]:
        """Return status for the latest complete chart snapshot."""

        snapshot = self.output.snapshot()
        request_sequence = int(getattr(snapshot, "request_sequence", 0) or 0)
        with self._lock:
            worker_status = dict(self._worker.status())
            return MappingProxyType(
                {
                    "history_source_sequence": self._history_source_sequence,
                    "history_source_cursor": self._history_source_cursor,
                    "history_append_ts": self._history_append_ts,
                    "output_request_sequence": request_sequence,
                    "output_source_sequence": int(
                        getattr(snapshot, "source_sequence", 0) or 0
                    ),
                    "output_append_ts": float(
                        getattr(snapshot, "source_append_ts", 0.0) or 0.0
                    ),
                    "output_sample_count": len(snapshot.timestamps)
                    if snapshot is not None
                    else 0,
                    "session_scope_key": self._session_scope_key,
                    "source_epoch": self._source_epoch,
                    "render_active": self._render_active,
                    "worker_active": bool(worker_status.get("active", False)),
                    "worker_pending": bool(worker_status.get("pending", False)),
                    "coalesced": int(worker_status.get("coalesced", 0) or 0),
                    "closed": bool(worker_status.get("closed", False)),
                    **dict(self.history.seed_status()),
                }
            )

    def reset_runtime_state(
        self,
        *,
        timeout_s: float = 2.0,
    ) -> tuple[Mapping, Mapping]:
        with self._lock:
            prior = self._reset_observation_unlocked()
            self.output.reset_runtime_state()
            self._worker.fence_and_drain_runtime_state(
                timeout_s=timeout_s
            )
            self.history.reset_runtime_state()
            self._request_sequence = 0
            self._history_source_sequence = 0
            self._history_source_cursor = (0, 0)
            self._history_append_ts = 0.0
            self._session_scope_key = ""
            self._source_epoch = ""
            self._render_active = False
            result = self._reset_observation_unlocked()
            return MappingProxyType(prior), MappingProxyType(result)

    def _reset_observation_unlocked(self) -> dict:
        with self.history._lock:
            history = self.history._reset_observation_unlocked()
        with self.output._lock:
            output = self.output._reset_observation_unlocked()
        with self._worker._lock:
            worker = self._worker._reset_observation_unlocked()
        return {
            "request_sequence": self._request_sequence,
            "history_source_sequence": self._history_source_sequence,
            "history_source_cursor": self._history_source_cursor,
            "history_append_ts": self._history_append_ts,
            "session_scope_key": self._session_scope_key,
            "source_epoch": self._source_epoch,
            "render_active": self._render_active,
            "history": history,
            "output": output,
            "worker": worker,
        }

    def close(self) -> Mapping[str, object]:
        return self._worker.close()
