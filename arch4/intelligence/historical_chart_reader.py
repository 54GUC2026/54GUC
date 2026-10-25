"""Bounded chart-history reader owned exclusively by Historical Evidence."""

from __future__ import annotations

from collections import deque
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from arch4.evidence_scope import EvidenceScope
from arch4.customer_contracts.historical_evidence import (
    HistoricalEvidenceChartPoint,
    HistoricalEvidenceChartRequest,
    HistoricalEvidenceChartResult,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.historical_artifact_reader import (
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
)
from arch4.historical_continuity_store import (
    CHART_HISTORY_FILENAME,
    committed_generation_status,
)
from arch4.telemetry.production_contract import flatten_production_telemetry_row


_STRONG_IDENTITY_KEYS = ("serial", "imei", "imsi")
_CHART_FIELD_IDS = (
    "traffic.dl_mbps",
    "traffic.ul_mbps",
    "quality.congestion_pct",
    "quality.jitter_trimmed_ms",
    "quality.internet_reachable",
    "rf.sinr_db",
    "rf.rsrp_dbm",
    "rf.rsrq_db",
    "rf.rssi_dbm",
    "nr.sinr_db",
    "nr.rsrp_dbm",
    "nr.rsrq_db",
    "quality.cq_pct",
    "quality.ca_ld_pct",
)


def read_historical_chart(
    *,
    runtime_dir: str | Path,
    memory_dir: str | Path,
    request: HistoricalEvidenceChartRequest,
    target: Mapping[str, Any],
) -> HistoricalEvidenceChartResult:
    """Read only committed/production-minimal evidence for the accepted modem."""

    if not isinstance(request, HistoricalEvidenceChartRequest):
        raise TypeError("historical_evidence_typed_chart_request_required")
    raw_scope = target.get("evidence_scope")
    if not isinstance(raw_scope, Mapping):
        raise RuntimeError("historical_evidence_chart_exact_scope_required")
    scope = EvidenceScope.from_mapping(raw_scope)
    if request.session_scope_key != scope.session_scope_key:
        raise RuntimeError("historical_evidence_chart_session_scope_mismatch")
    strong_identity = {
        key: str(target.get(key) or "").strip()
        for key in _STRONG_IDENTITY_KEYS
        if str(target.get(key) or "").strip()
    }
    if not strong_identity:
        raise RuntimeError("historical_evidence_chart_strong_identity_required")
    exact_identity = {
        "identity_code": scope.identity_code,
        "identity_catalog_version": scope.identity_catalog_version,
        "identity_catalog_checksum": scope.identity_catalog_checksum,
    }
    cutoff = request.created_at - float(request.history_seconds)
    rows: deque[HistoricalEvidenceChartPoint] = deque(maxlen=request.max_rows)
    scanned_rows = 0
    scope_rejected = 0
    identity_rejected = 0
    invalid = 0
    committed_roots: tuple[Path, ...] = ()
    commit_status = committed_generation_status(Path(memory_dir).resolve())
    if commit_status.get("present"):
        committed_roots = (Path(str(commit_status["generation_dir"])).resolve(),)
    reader = HistoricalArtifactReader(
        Path(runtime_dir).resolve(),
        extra_roots=committed_roots,
    )
    newest_records = deque(
        reader.iter_records(
            families=(FAMILY_PRODUCTION,),
            since_ts=cutoff,
            until_ts=request.created_at,
        ),
        maxlen=request.max_rows,
    )
    for record in newest_records:
        scanned_rows += 1
        row = flatten_production_telemetry_row(record.payload)
        if str(row.get("record_type") or "tick") != "tick":
            continue
        timestamp = _optional_float(row.get("ts"))
        if timestamp is None:
            invalid += 1
            continue
        source_session_scope_key = str(row.get("session_scope_key") or "").strip()
        current_session = source_session_scope_key == request.session_scope_key
        same_modem_continuity = bool(
            record.artifact.path.name == CHART_HISTORY_FILENAME
            and _same_exact_modem_history(record.payload, exact_identity)
        )
        if not current_session and not same_modem_continuity:
            scope_rejected += 1
            continue
        if current_session and any(
            str(row.get(key) or "").strip() != expected
            for key, expected in strong_identity.items()
        ):
            identity_rejected += 1
            continue
        point = _chart_point_from_production_row(
            row,
            timestamp=timestamp,
            source_session_scope_key=(
                source_session_scope_key or request.session_scope_key
            ),
            source_file=record.artifact.path.name,
            source_line=record.line_number,
        )
        if point is None:
            invalid += 1
            continue
        rows.append(point)
    invalid += int(reader.status().get("malformed_records", 0) or 0)
    chronological = sorted(
        rows,
        key=lambda point: (
            point.timestamp,
            point.source_file,
            point.source_line,
        ),
    )
    deduplicated: dict[float, HistoricalEvidenceChartPoint] = {}
    for point in chronological:
        deduplicated[point.timestamp] = point
    samples = tuple(deduplicated.values())
    thinned = thin_historical_chart_points(
        samples,
        target_points=request.target_points,
    )
    chunks = tuple(
        tuple(thinned[index : index + request.chunk_size])
        for index in range(0, len(thinned), request.chunk_size)
    )
    return HistoricalEvidenceChartResult(
        request_id=request.request_id,
        session_scope_key=request.session_scope_key,
        chunks=chunks,
        scanned_files=len({record.artifact.path for record in newest_records}),
        scanned_rows=scanned_rows,
        matched_rows=len(samples),
        thinned_rows=len(thinned),
        scope_rejected_rows=scope_rejected,
        identity_rejected_rows=identity_rejected,
        invalid_rows=invalid,
    )


def thin_historical_chart_points(
    points: Sequence[HistoricalEvidenceChartPoint],
    *,
    target_points: int,
) -> tuple[HistoricalEvidenceChartPoint, ...]:
    """Keep recent native detail and apply stable age-tier thinning."""

    chronological = tuple(sorted(points, key=lambda point: point.timestamp))
    if target_points < 1:
        raise ValueError("historical_evidence_chart_target_points_invalid")
    if len(chronological) <= target_points:
        return chronological
    latest = chronological[-1].timestamp
    kept: list[HistoricalEvidenceChartPoint] = []
    last_kept = float("-inf")
    for point in chronological:
        age = max(0.0, latest - point.timestamp)
        if age <= 15 * 60:
            spacing = 0.0
        elif age <= 60 * 60:
            spacing = 8.0
        elif age <= 2 * 60 * 60:
            spacing = 20.0
        elif age <= 6 * 60 * 60:
            spacing = 60.0
        else:
            spacing = 180.0
        if not kept or point.timestamp - last_kept >= spacing:
            kept.append(point)
            last_kept = point.timestamp
    if kept[-1] is not chronological[-1]:
        kept.append(chronological[-1])
    if len(kept) <= target_points:
        return tuple(kept)
    recent_cutoff = latest - 15 * 60
    older = [point for point in kept if point.timestamp < recent_cutoff]
    recent = [point for point in kept if point.timestamp >= recent_cutoff]
    allowed_older = max(0, target_points - len(recent))
    if len(recent) >= target_points:
        return tuple(recent[-target_points:])
    if len(older) <= allowed_older:
        return tuple(older + recent)
    stride = max(1, len(older) // allowed_older)
    thinned_older = older[::stride]
    if thinned_older[-1] is not older[-1]:
        thinned_older.append(older[-1])
    return tuple(thinned_older[-allowed_older:] + recent)


def _chart_point_from_production_row(
    row: Mapping[str, Any],
    *,
    timestamp: float,
    source_session_scope_key: str,
    source_file: str,
    source_line: int,
) -> HistoricalEvidenceChartPoint | None:
    values: dict[str, float | None] = {
        "traffic.dl_mbps": _optional_float(row.get("dl_mbps")),
        "traffic.ul_mbps": _optional_float(row.get("ul_mbps")),
        "quality.congestion_pct": _valid_value(
            row, "congestion_display_pct", "congestion_display_valid"
        ),
        "quality.jitter_trimmed_ms": _valid_value(
            row, "rtt_dl_jitter_trimmed_ms", "rtt_dl_jitter_display_valid"
        ),
        # Production history written before the Core reachability owner did
        # not retain a per-probe current state. Do not invent one while
        # rebuilding an old chart.
        "quality.internet_reachable": None,
        "rf.sinr_db": _rf_value(row.get("sinr_db")),
        "rf.rsrp_dbm": _rf_value(row.get("rsrp_dbm")),
        "rf.rsrq_db": _rf_value(row.get("rsrq_db")),
        "rf.rssi_dbm": _rf_value(row.get("rssi_dbm")),
        "nr.sinr_db": _active_nr_rf_value(row, "nr_sinr_db"),
        "nr.rsrp_dbm": _active_nr_rf_value(row, "nr_rsrp_dbm"),
        "nr.rsrq_db": _active_nr_rf_value(row, "nr_rsrq_db"),
        "quality.cq_pct": _valid_value(
            row, "cq_display_pct", "cq_cell_display_valid"
        ),
        "quality.ca_ld_pct": _valid_value(
            row, "ca_stat_ld_pct", "ca_stat_ld_valid"
        ),
    }
    radio_connected = row.get("radio_connected")
    if radio_connected is not None and type(radio_connected) is not bool:
        return None
    if radio_connected is False:
        for field_id in (
            "rf.sinr_db",
            "rf.rsrp_dbm",
            "rf.rsrq_db",
            "rf.rssi_dbm",
            "nr.sinr_db",
            "nr.rsrp_dbm",
            "nr.rsrq_db",
        ):
            values[field_id] = None
    if set(values) != set(_CHART_FIELD_IDS) or not any(
        value is not None for value in values.values()
    ):
        return None
    pband = (
        str(row.get("serving_band_signature") or "").strip()
        if radio_connected is not False
        else ""
    )
    markers = {"radio.pband": pband} if pband else {}
    nr_band = row.get("nr_band")
    if (radio_connected is not False and row.get("nr_secondary_active") is True
            and row.get("nr_transition_dirty") is not True
            and type(nr_band) is int and nr_band > 0):
        markers["nr.band"] = str(nr_band)
    return HistoricalEvidenceChartPoint(
        timestamp=timestamp,
        values=CoreFrozenPayload.from_mapping(values),
        marker_values=CoreFrozenPayload.from_mapping(
            markers
        ),
        source_session_scope_key=source_session_scope_key,
        source_file=source_file,
        source_line=source_line,
    )


def _same_exact_modem_history(
    payload: Mapping[str, Any],
    exact_identity: Mapping[str, str],
) -> bool:
    try:
        scope = EvidenceScope.from_mapping(dict(payload.get("evidence_scope") or {}))
    except (TypeError, ValueError):
        return False
    return bool(
        scope.identity_code == exact_identity["identity_code"]
        and scope.identity_catalog_version == exact_identity["identity_catalog_version"]
        and scope.identity_catalog_checksum == exact_identity["identity_catalog_checksum"]
    )


def _valid_value(
    row: Mapping[str, Any],
    value_key: str,
    valid_key: str,
) -> float | None:
    if row.get(valid_key) is not True:
        return None
    return _optional_float(row.get(value_key))


def _rf_value(value: Any) -> float | None:
    number = _optional_float(value)
    if number is None or number <= -900.0:
        return None
    return number


def _active_nr_rf_value(row: Mapping[str, Any], key: str) -> float | None:
    if (
        row.get("nr_secondary_active") is not True
        or row.get("nr_transition_dirty") is True
    ):
        return None
    return _rf_value(row.get(key))


def _optional_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


__all__ = (
    "read_historical_chart",
    "thin_historical_chart_points",
)
