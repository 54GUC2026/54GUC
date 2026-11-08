"""Named production trace matrix with deterministic delta compression.

The numbered production lane is compact, but numbers alone are not enough to
reconstruct a bug without a decoder.  This module owns the named
multidimensional production trace surface used for first production trials.
It is derived from the same telemetry fact row as telemetry_production and does
not replace telemetry_history until side-by-side evidence proves it is enough.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Mapping
from typing import Any, Final

from arch4.telemetry.production_contract import (
    PRODUCTION_SIGNAL_FIELDS,
    build_production_telemetry_row,
)


TRACE_MATRIX_SCHEMA: Final[str] = "telemetry_trace_matrix.v1"
TRACE_MATRIX_DELTA_SCHEMA: Final[str] = "telemetry_trace_matrix_delta.v1"
TRACE_MATRIX_OWNER: Final[str] = "arch4.telemetry.trace_matrix"
TRACE_MATRIX_BASENAME: Final[str] = "telemetry_trace_matrix"

TRACE_DIMENSION_SIGNALS: Final[dict[str, tuple[str, ...]]] = {
    "sample_integrity": ("S001", "S002", "S003", "S004"),
    "session_identity": ("S006", "S007", "S008"),
    "radio_identity": ("S009", "S010", "S011", "S012", "S013", "S040"),
    "rf_scheduler": ("S016", "S017", "S018", "S033", "S038", "S039", "S041"),
    "traffic_latency": ("S019", "S020", "S021", "S022", "S023", "S024", "S025"),
    "quality_congestion": ("S014", "S015", "S026", "S027", "S028", "S029", "S030", "S031", "S032"),
    "action_decision": ("S034", "S035"),
    "smartness_loop": ("S042",),
    "runtime_health": ("S005", "S036", "S037"),
}


def build_trace_matrix_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build one named trace matrix row from full history or production row."""
    production = _production_row(row)
    signals = _mapping(production.get("signals"))
    dimensions = {
        dimension: _dimension_payload(signals, signal_ids)
        for dimension, signal_ids in TRACE_DIMENSION_SIGNALS.items()
    }
    return {
        "schema": TRACE_MATRIX_SCHEMA,
        "owner": TRACE_MATRIX_OWNER,
        "record_type": str(production.get("record_type") or "tick"),
        "ts": production.get("ts"),
        "iso": str(production.get("iso") or ""),
        "tick": production.get("tick", 0),
        "evidence_scope": dict(production.get("evidence_scope") or {}),
        "evidence_quarantine": dict(
            production.get("evidence_quarantine") or {}
        ),
        "dimensions": dimensions,
    }


def build_trace_matrix_delta(
    row: Mapping[str, Any],
    *,
    previous_dimensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a delta row and the new state for deterministic reconstruction."""
    matrix = build_trace_matrix_row(row)
    current = _mapping(matrix.get("dimensions"))
    previous = _mapping(previous_dimensions)
    first = not previous
    changed = current if first else _changed_dimensions(current, previous)
    return {
        "schema": TRACE_MATRIX_DELTA_SCHEMA,
        "owner": TRACE_MATRIX_OWNER,
        "record_type": matrix["record_type"],
        "ts": matrix["ts"],
        "iso": matrix["iso"],
        "tick": matrix["tick"],
        "evidence_scope": dict(matrix.get("evidence_scope") or {}),
        "evidence_quarantine": dict(
            matrix.get("evidence_quarantine") or {}
        ),
        "full": first,
        "dimensions": changed,
        "unchanged_dimensions": sorted(set(current) - set(changed)),
        "_state": current,
    }


def public_trace_matrix_delta(delta: Mapping[str, Any]) -> dict[str, Any]:
    """Return the persisted delta envelope without the private state cache."""
    out = dict(delta)
    out.pop("_state", None)
    return out


def dedupe_trace_matrix_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build persisted delta rows for a sequence of full rows."""
    previous: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        delta = build_trace_matrix_delta(row, previous_dimensions=previous)
        previous = dict(delta.get("_state") or {})
        out.append(public_trace_matrix_delta(delta))
    return out


def reconstruct_trace_matrix_rows(deltas: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct matrix rows from persisted deltas."""
    state: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for raw in deltas:
        delta = _mapping(raw)
        if delta.get("schema") != TRACE_MATRIX_DELTA_SCHEMA:
            raise ValueError("trace_matrix_delta_bad_schema")
        if delta.get("owner") != TRACE_MATRIX_OWNER:
            raise ValueError("trace_matrix_delta_bad_owner")
        changed = _mapping(delta.get("dimensions"))
        if bool(delta.get("full", False)):
            state = {}
        state = _merge_dimensions(state, changed)
        out.append(
            {
                "schema": TRACE_MATRIX_SCHEMA,
                "owner": TRACE_MATRIX_OWNER,
                "record_type": str(delta.get("record_type") or "tick"),
                "ts": delta.get("ts"),
                "iso": str(delta.get("iso") or ""),
                "tick": delta.get("tick", 0),
                "evidence_scope": dict(delta.get("evidence_scope") or {}),
                "evidence_quarantine": dict(
                    delta.get("evidence_quarantine") or {}
                ),
                "dimensions": state,
            }
        )
    return out


def estimate_trace_matrix_size(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Measure raw matrix, delta, and gzip sizes with time-normalized rates."""
    materialized = [build_trace_matrix_row(row) for row in rows]
    deltas = dedupe_trace_matrix_rows(materialized)
    raw_blob = _jsonl_bytes(materialized)
    delta_blob = _jsonl_bytes(deltas)
    gzip_blob = gzip.compress(delta_blob)
    duration_s = _duration_seconds(materialized)
    per_second = len(gzip_blob) / duration_s if duration_s > 0 else 0.0
    return {
        "schema": "telemetry_trace_matrix_size_estimate.v1",
        "owner": TRACE_MATRIX_OWNER,
        "rows": len(materialized),
        "duration_s": round(duration_s, 3),
        "raw_matrix_bytes": len(raw_blob),
        "delta_bytes": len(delta_blob),
        "delta_gzip_bytes": len(gzip_blob),
        "estimated_bytes_per_hour": int(per_second * 3600),
        "estimated_bytes_per_day": int(per_second * 86400),
        "estimated_bytes_per_14_days": int(per_second * 86400 * 14),
    }


def _production_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("schema_version") == "arch_production_signal_v1" and isinstance(row.get("signals"), Mapping):
        return dict(row)
    return build_production_telemetry_row(row)


def _dimension_payload(signals: Mapping[str, Any], signal_ids: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for signal_id in signal_ids:
        fields = PRODUCTION_SIGNAL_FIELDS.get(signal_id, ())
        payload = _mapping(signals.get(signal_id))
        for field in fields:
            if field in payload:
                out[field] = payload[field]
    return out


def _changed_dimensions(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dimension, payload in current.items():
        payload_map = _mapping(payload)
        previous_map = _mapping(previous.get(dimension))
        changed = {
            key: value
            for key, value in payload_map.items()
            if previous_map.get(key) != value
        }
        deleted = sorted(set(previous_map) - set(payload_map))
        if deleted:
            changed["__deleted__"] = deleted
        if changed:
            out[dimension] = changed
    return out


def _merge_dimensions(state: Mapping[str, Any], changed: Mapping[str, Any]) -> dict[str, Any]:
    out = {dimension: dict(_mapping(payload)) for dimension, payload in state.items()}
    for dimension, payload in changed.items():
        merged = dict(out.get(dimension, {}))
        payload_map = _mapping(payload)
        for key in payload_map.get("__deleted__", ()):
            merged.pop(str(key), None)
        for key, value in payload_map.items():
            if key == "__deleted__":
                continue
            merged[key] = value
        out[dimension] = merged
    return out


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "\n".join(json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows).encode("utf-8")


def _duration_seconds(rows: list[Mapping[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    first = _float(rows[0].get("ts"))
    last = _float(rows[-1].get("ts"))
    return max(0.0, last - first)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "TRACE_DIMENSION_SIGNALS",
    "TRACE_MATRIX_BASENAME",
    "TRACE_MATRIX_DELTA_SCHEMA",
    "TRACE_MATRIX_OWNER",
    "TRACE_MATRIX_SCHEMA",
    "build_trace_matrix_delta",
    "build_trace_matrix_row",
    "dedupe_trace_matrix_rows",
    "estimate_trace_matrix_size",
    "public_trace_matrix_delta",
    "reconstruct_trace_matrix_rows",
]
