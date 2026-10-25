"""
arch4/intelligence/historical_site_report.py
═══════════════════════════════════════════════════════════════
Detached Intelligence Historical Site Report Renderer

Reads telemetry_production JSONL + events JSONL from guc_output/,
aggregates 30 days of data, and produces a self-contained HTML
report with inline SVG charts.

This is an unattached consumer for a future Intelligence report extension.  It
is not the Foundation Site Report implementation and has no production caller.
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import math
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from arch4.coerce import safe_bool
from arch4.atomic_io import atomic_write_text
from arch4.historical_artifact_reader import (
    FAMILY_EVENT,
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
)
from arch4.telemetry.production_contract import (
    flatten_production_telemetry_row,
)

log = logging.getLogger("arch4.intelligence.historical_site_report")

# ─── constants ────────────────────────────────────────────────
REPORT_WINDOW_DAYS = 30
REPORT_RETENTION_DAYS = 14

# SVG dimensions
SVG_HEATMAP_CELL = 18
SVG_HEATMAP_GAP = 2
SVG_CHART_W = 720
SVG_CHART_H = 200
SVG_BAR_H = 24


# ═══════════════════════════════════════════════════════════════
#  1. Canonical Artifact Consumer
# ═══════════════════════════════════════════════════════════════


def load_telemetry(
    output_dir: Path,
    days: int = REPORT_WINDOW_DAYS,
    *,
    cutoff_ts: float = 0.0,
) -> list[dict[str, Any]]:
    """Load all telemetry tick rows within the reporting window.

    If *cutoff_ts* is positive, only ticks with ``ts >= cutoff_ts`` are kept.
    The *days* parameter controls which daily JSONL files are opened
    (coarse filter); *cutoff_ts* is the precise filter.
    """
    if cutoff_ts <= 0:
        cutoff_ts = time.time() - days * 86400
    rows: list[dict[str, Any]] = []
    reader = HistoricalArtifactReader(output_dir, retention_days=max(1, days))
    for record in reader.iter_records(
        families=(FAMILY_PRODUCTION,),
        since_ts=cutoff_ts,
    ):
        row = flatten_production_telemetry_row(record.payload)
        if row.get("record_type") == "tick":
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("ts", 0)))
    return rows


def load_events(
    output_dir: Path,
    days: int = REPORT_WINDOW_DAYS,
    *,
    cutoff_ts: float = 0.0,
) -> list[dict[str, Any]]:
    """Load all event rows within the reporting window."""
    if cutoff_ts <= 0:
        cutoff_ts = time.time() - days * 86400
    rows = [
        record.payload
        for record in HistoricalArtifactReader(
            output_dir,
            retention_days=max(1, days),
        ).iter_records(
            families=(FAMILY_EVENT,),
            since_ts=cutoff_ts,
        )
    ]
    rows.sort(key=lambda r: float(r.get("ts", 0)))
    return rows


# ═══════════════════════════════════════════════════════════════
#  2. Aggregation Engine
# ═══════════════════════════════════════════════════════════════

@dataclass
class HourBucket:
    """Accumulator for one hour slot."""
    samples: int = 0
    observed_seconds: float = 0.0
    sum_dl_mbps: float = 0.0
    dl_samples: int = 0
    sum_ul_mbps: float = 0.0
    ul_samples: int = 0
    sum_sinr: float = 0.0
    sinr_samples: int = 0
    sum_rtt_ms: float = 0.0
    rtt_samples: int = 0
    sum_congestion_pct: float = 0.0
    congestion_samples: int = 0
    sum_rsrq: float = 0.0
    rsrq_samples: int = 0
    states: dict[str, int] = field(default_factory=dict)
    bands: dict[int, int] = field(default_factory=dict)
    ca_samples: int = 0
    action_count: int = 0
    band_switch_count: int = 0
    cell_switch_count: int = 0

    @property
    def avg_dl(self) -> float:
        denominator = self.dl_samples or self.samples
        return self.sum_dl_mbps / denominator if denominator else 0.0

    @property
    def avg_ul(self) -> float:
        denominator = self.ul_samples or self.samples
        return self.sum_ul_mbps / denominator if denominator else 0.0

    @property
    def avg_sinr(self) -> float:
        denominator = self.sinr_samples or self.samples
        return self.sum_sinr / denominator if denominator else 0.0

    @property
    def avg_rtt(self) -> float:
        denominator = self.rtt_samples or self.samples
        return self.sum_rtt_ms / denominator if denominator else 0.0

    @property
    def avg_congestion(self) -> float:
        return self.sum_congestion_pct / self.congestion_samples if self.congestion_samples else 0.0

    @property
    def avg_rsrq(self) -> float:
        denominator = self.rsrq_samples or self.samples
        return self.sum_rsrq / denominator if denominator else 0.0

    @property
    def ca_pct(self) -> float:
        return 100.0 * self.ca_samples / self.samples if self.samples else 0.0


@dataclass
class DayBucket:
    """Accumulator for one calendar day."""
    hours: dict[int, HourBucket] = field(default_factory=lambda: {h: HourBucket() for h in range(24)})
    _total: HourBucket = field(default_factory=HourBucket)

    @property
    def total(self) -> HourBucket:
        return self._total


@dataclass
class BandStats:
    """Per-band accumulated statistics."""
    samples: int = 0
    observed_seconds: float = 0.0
    sum_dl_mbps: float = 0.0
    dl_samples: int = 0
    sum_sinr: float = 0.0
    sinr_samples: int = 0
    sum_congestion: float = 0.0
    congestion_samples: int = 0
    ca_samples: int = 0
    switch_in_count: int = 0  # how many times we switched TO this band

    @property
    def avg_dl(self) -> float:
        denominator = self.dl_samples or self.samples
        return self.sum_dl_mbps / denominator if denominator else 0.0

    @property
    def avg_sinr(self) -> float:
        denominator = self.sinr_samples or self.samples
        return self.sum_sinr / denominator if denominator else 0.0

    @property
    def avg_congestion(self) -> float:
        return self.sum_congestion / self.congestion_samples if self.congestion_samples else 0.0

    @property
    def time_hours(self) -> float:
        return self.observed_seconds / 3600.0


@dataclass
class ActionRecord:
    """One action from events."""
    ts: float = 0.0
    iso: str = ""
    lever: str = ""
    reason: str = ""
    params: Any = None
    success: bool = True


@dataclass
class IncidentRecord:
    """One state transition into a fault/emergency state."""
    ts_start: float = 0.0
    ts_end: float = 0.0
    iso_start: str = ""
    state: str = ""
    reason: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ts_end - self.ts_start)


@dataclass
class SiteAggregation:
    """Complete aggregated report data."""
    date_start: str = ""
    date_end: str = ""
    total_ticks: int = 0
    runtime_hours: float = 0.0
    evidence_scope: str = ""
    scope_rejected_ticks: int = 0
    scope_rejected_events: int = 0
    days: dict[str, DayBucket] = field(default_factory=dict)
    bands: dict[int, BandStats] = field(default_factory=dict)
    state_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    state_seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    invalid_metric_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    actions: list[ActionRecord] = field(default_factory=list)
    incidents: list[IncidentRecord] = field(default_factory=list)
    congestion_source_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rsrq_source_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    congestion_bucket_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    host_profile_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    ca_pool_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    active_band_signature_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    ca_state_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    radio_state_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rat_state_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rat_nr_band_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    event_kind_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    brain_verdict_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    brain_blocker_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    learning_gate_block_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mutation_window_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mutation_phase_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mutation_workflow_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    action_outcome_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    action_suppression_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    post_action_verification_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    lock_confirmation_reason_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    read_miss_reason_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bouncer_outcome_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    referee_outcome_ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    modem_read_missed_tick_count: int = 0
    modem_read_missed_event_count: int = 0
    modem_read_missed_max_consecutive: int = 0
    rat_missing_scoped_tick_count: int = 0
    rat_nr_active_tick_count: int = 0
    rat_transition_tick_count: int = 0
    band_switch_count: int = 0
    cell_switch_count: int = 0
    # Global averages
    avg_dl_mbps: float = 0.0
    avg_ul_mbps: float = 0.0
    avg_congestion_pct: float = 0.0
    avg_sinr: float = 0.0
    avg_rtt_ms: float = 0.0


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or val == "":
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


_INCIDENT_STATES = {"FAULT", "EMERGENCY", "CRITICAL"}
_EMPTY_LABELS = {"", "none", "null", "unknown", "n/a", "na", "[]", "{}"}


def _clean_label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        parts = [_clean_label(v) for v in value]
        return "+".join(p for p in parts if p)
    if isinstance(value, dict):
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    label = str(value).strip()
    if label.lower() in _EMPTY_LABELS:
        return ""
    return label


def _first_label(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        label = _clean_label(row.get(key))
        if label:
            return label
    return ""


def _host_profile_label(row: dict[str, Any]) -> str:
    model = _first_label(row, "model", "modem_model", "device_model")
    firmware = _first_label(row, "firmware", "fw_version", "firmware_version", "sw_version")
    backend = _first_label(row, "backend_family", "backend", "protocol")
    provider = _first_label(row, "provider_plmn", "provider_name", "operator", "network_operator")
    parts = [p for p in (model, firmware, backend, provider) if p]
    return " / ".join(parts)


def _scoped_rat_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("rat_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def _rat_snapshot_group(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    group = snapshot.get(key)
    return group if isinstance(group, dict) else {}


def _nr_band_label(value: Any) -> str:
    band = _safe_int(value)
    return f"n{band}" if band > 0 else ""


def _event_text(ev: dict[str, Any], *keys: str) -> str:
    for key in keys:
        label = _clean_label(ev.get(key))
        if label:
            return label
    return ""


_OPERATOR_REASON_LABELS = {
    "live_register_stale": "modem refresh pending",
    "refresh_missed_refreshing": "modem refresh in progress",
    "planned_radio_quiet": "radio settling",
    "mutating_action_lock": "radio action in progress",
    "radio_mutation_workflow": "radio action in progress",
    "runtime_recovery": "runtime recovery in progress",
    "failed_action_missing_modem_after": "waiting for modem readback",
}


def _nested_text(ev: dict[str, Any], path: tuple[str, ...]) -> str:
    cur: Any = ev
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return _clean_label(cur)


def _counter_reason(label: str, fallback: str) -> str:
    cleaned = _clean_label(label)
    return cleaned if cleaned else fallback


def _identity_containers(row: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    containers = [row]
    for key in (
        "runtime_identity",
        "compatibility_identity",
        "modem_identity",
        "identity",
        "evidence_scope",
    ):
        value = row.get(key)
        if isinstance(value, dict):
            containers.append(value)
    return tuple(containers)


def _identity_value(row: dict[str, Any], *keys: str) -> str:
    for container in _identity_containers(row):
        for key in keys:
            value = _clean_label(container.get(key))
            if value:
                return value
    return ""


def _evidence_identity(row: dict[str, Any]) -> str:
    identity_code = _identity_value(row, "identity_code", "modem_identity_code")
    if identity_code:
        exact_fields = (
            ("identity_code", identity_code),
            ("access_type_id", _identity_value(row, "access_type_id")),
            ("catalog_version", _identity_value(row, "catalog_version", "identity_catalog_version")),
            ("catalog_checksum", _identity_value(row, "catalog_checksum", "identity_catalog_checksum")),
            ("profile_id", _identity_value(row, "profile_id", "resolved_profile_id")),
            ("provider_plmn", _identity_value(row, "provider_plmn", "plmn")),
        )
        return "identity:" + "|".join(
            f"{key}={value}" for key, value in exact_fields if value
        )

    # Explicit legacy import boundary: old evidence predating compact identity
    # may still be reported, but it never merges with compact-identity rows.
    legacy_fields = (
        ("model", _identity_value(row, "model", "modem_model", "device_model")),
        ("firmware", _identity_value(row, "firmware", "fw_version", "firmware_version")),
        ("software", _identity_value(row, "software_version", "sw_version")),
        ("access", _identity_value(row, "access_type_id", "access_type", "backend_family", "backend")),
        ("provider_plmn", _identity_value(row, "provider_plmn", "plmn")),
    )
    parts = [f"{key}={value}" for key, value in legacy_fields if value]
    return "legacy_identity:" + "|".join(parts) if parts else ""


def _scope_report_evidence(
    ticks: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, int, int]:
    ordered_ticks = sorted(
        (row for row in ticks if _safe_float(row.get("ts"), 0.0) > 0.0),
        key=lambda row: _safe_float(row.get("ts"), 0.0),
    )
    ordered_events = sorted(
        (row for row in events if _safe_float(row.get("ts"), 0.0) > 0.0),
        key=lambda row: _safe_float(row.get("ts"), 0.0),
    )
    selected_scope = next(
        (
            _evidence_identity(row)
            for row in reversed(ordered_ticks)
            if _evidence_identity(row)
        ),
        "",
    ) or next(
        (
            _evidence_identity(row)
            for row in reversed(ordered_events)
            if _evidence_identity(row)
        ),
        "",
    )
    if not selected_scope:
        return ordered_ticks, ordered_events, "", 0, 0
    scoped_ticks = [
        row for row in ordered_ticks if _evidence_identity(row) == selected_scope
    ]
    scoped_events = [
        row for row in ordered_events if _evidence_identity(row) == selected_scope
    ]
    return (
        scoped_ticks,
        scoped_events,
        selected_scope,
        len(ordered_ticks) - len(scoped_ticks),
        len(ordered_events) - len(scoped_events),
    )


def _sample_durations(ticks: list[dict[str, Any]]) -> list[float]:
    if not ticks:
        return []
    timestamps = [_safe_float(row.get("ts"), 0.0) for row in ticks]
    positive = [
        later - earlier
        for earlier, later in zip(timestamps, timestamps[1:])
        if 0.0 < later - earlier <= 300.0
    ]
    if not positive:
        return [0.0] * len(ticks)
    ordered = sorted(positive)
    # Lower median resists one outage-sized gap in a short evidence window.
    expected = ordered[(len(ordered) - 1) // 2]
    cap = max(expected * 3.0, expected)
    durations = [
        min(cap, max(0.0, later - earlier))
        for earlier, later in zip(timestamps, timestamps[1:])
    ]
    durations.append(expected)
    return durations


def _valid_metric(
    row: dict[str, Any],
    key: str,
    *,
    valid_keys: tuple[str, ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if key not in row or row.get(key) in (None, ""):
        return None
    for valid_key in valid_keys:
        if valid_key in row and not safe_bool(row.get(valid_key), False):
            return None
    try:
        value = float(row[key])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


_ACTION_CORRELATION_KEYS = (
    "root_id",
    "action_root_id",
    "transaction_root_id",
    "transaction_id",
    "command_id",
    "proof_id",
    "decision_id",
    "decision_trace_id",
    "gui_action_id",
    "objective_id",
    "workflow_id",
)


def _action_correlation_ids(event: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    containers: list[dict[str, Any]] = [event]
    for key in (
        "action_transaction_key",
        "action_transaction",
        "receipt",
        "details",
        "proof_identity",
    ):
        value = event.get(key)
        if isinstance(value, dict):
            containers.append(value)
        elif key == "action_transaction_key":
            label = _clean_label(value)
            if label:
                identifiers.add(label)
    for container in containers:
        for key in _ACTION_CORRELATION_KEYS:
            value = _clean_label(container.get(key))
            if value:
                identifiers.add(value)
    return identifiers


def _terminal_action_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    action_kinds = {
        "action": 3,
        "action_executed": 3,
        "manual_action_executed": 3,
        "action_completed": 4,
        "action_failed": 4,
        "action_issued": 1,
        "command_accepted": 1,
    }
    groups: dict[int, dict[str, Any]] = {}
    identifier_group: dict[str, int] = {}
    next_group = 0
    for index, event in enumerate(events):
        kind = str(event.get("kind") or event.get("event") or "")
        rank = action_kinds.get(kind)
        if kind == "controller_action":
            rank = 4 if safe_bool(event.get("action_executed", False), False) else 1
        if rank is None:
            continue
        identifiers = _action_correlation_ids(event)
        matched = {
            identifier_group[value]
            for value in identifiers
            if value in identifier_group
        }
        if matched:
            group_id = min(matched)
        else:
            group_id = next_group
            next_group += 1
            groups[group_id] = {
                "identifiers": set(),
                "candidate": (-1, -1, event),
            }
        for merged_id in sorted(matched - {group_id}):
            merged = groups.pop(merged_id)
            groups[group_id]["identifiers"].update(merged["identifiers"])
            if merged["candidate"][:2] >= groups[group_id]["candidate"][:2]:
                groups[group_id]["candidate"] = merged["candidate"]
        groups[group_id]["identifiers"].update(identifiers)
        for value in groups[group_id]["identifiers"]:
            identifier_group[value] = group_id
        candidate = (rank, index, event)
        if candidate[:2] >= groups[group_id]["candidate"][:2]:
            groups[group_id]["candidate"] = candidate
    return [
        event
        for rank, _index, event in sorted(
            (group["candidate"] for group in groups.values()),
            key=lambda item: item[1],
        )
        if rank >= 3
    ]


def aggregate(ticks: list[dict[str, Any]], events: list[dict[str, Any]]) -> SiteAggregation:
    """Build aggregation from raw tick + event data."""
    agg = SiteAggregation()
    if not ticks and not events:
        return agg

    ticks, events, selected_scope, rejected_ticks, rejected_events = _scope_report_evidence(
        ticks,
        events,
    )
    agg.evidence_scope = selected_scope
    agg.scope_rejected_ticks = rejected_ticks
    agg.scope_rejected_events = rejected_events
    agg.total_ticks = len(ticks)
    durations = _sample_durations(ticks)
    agg.runtime_hours = sum(durations) / 3600.0

    first_ts = float(ticks[0].get("ts", 0)) if ticks else 0.0
    last_ts = float(ticks[-1].get("ts", 0)) if ticks else 0.0
    if ticks:
        agg.date_start = time.strftime("%Y-%m-%d", time.localtime(first_ts))
        agg.date_end = time.strftime("%Y-%m-%d", time.localtime(last_ts))

    sum_dl = 0.0
    sum_ul = 0.0
    sum_sinr = 0.0
    sum_rtt = 0.0
    sum_cong = 0.0
    dl_n = 0
    ul_n = 0
    sinr_n = 0
    rtt_n = 0
    cong_n = 0
    prev_band = 0
    prev_cell_key: tuple[Any, ...] | None = None

    for row, observed_seconds in zip(ticks, durations):
        ts = float(row.get("ts", 0))
        lt = time.localtime(ts)
        day_key = time.strftime("%Y-%m-%d", lt)
        hour = lt.tm_hour
        state = str(row.get("state", ""))

        # Day/hour bucket
        if day_key not in agg.days:
            agg.days[day_key] = DayBucket()
        day = agg.days[day_key]
        hb = day.hours[hour]
        tb = day.total

        dl = _valid_metric(row, "dl_mbps", minimum=0.0)
        ul = _valid_metric(row, "ul_mbps", minimum=0.0)
        sinr = _valid_metric(row, "sinr_db", minimum=-50.0, maximum=80.0)
        rtt = _valid_metric(row, "rtt_ms", minimum=0.001, maximum=600_000.0)
        rsrq = _valid_metric(row, "rsrq_db", minimum=-50.0, maximum=10.0)
        congestion = _valid_metric(
            row,
            "congestion_pct",
            valid_keys=("congestion_pct_valid",),
            minimum=0.0,
            maximum=100.0,
        )
        ca_active = safe_bool(row.get("ca_active", False), False)
        band = _safe_int(row.get("serving_band") or row.get("primary_band"))
        congestion_source = str(row.get("congestion_source", "") or "")
        rsrq_source = str(row.get("rsrq_source", "") or "")
        congestion_bucket = _first_label(row, "congestion_bucket", "congestion_state_bucket")
        ca_pool = _first_label(
            row,
            "configured_band_pool_signature",
            "selected_band_set_signature",
            "band_set_signature",
            "ca_pool_signature",
        )
        active_band_signature = _first_label(
            row,
            "active_band_set_signature",
            "active_band_signature",
            "ca_signature",
            "ca_band_signature",
        )
        ca_state = _first_label(row, "ca_state_text", "ca_state", "ca_status")
        radio_state = _first_label(row, "radio_state_text", "radio_state", "connection_status")
        host_profile = _host_profile_label(row)
        rat_snapshot = _scoped_rat_snapshot(row)
        learning_gate = row.get("learning_gate") if isinstance(row.get("learning_gate"), dict) else {}
        mutation_window = (
            row.get("radio_mutation_window")
            if isinstance(row.get("radio_mutation_window"), dict)
            else {}
        )

        for bucket in (hb, tb):
            bucket.samples += 1
            bucket.observed_seconds += observed_seconds
            if dl is not None:
                bucket.sum_dl_mbps += dl
                bucket.dl_samples += 1
            if ul is not None:
                bucket.sum_ul_mbps += ul
                bucket.ul_samples += 1
            if sinr is not None:
                bucket.sum_sinr += sinr
                bucket.sinr_samples += 1
            if rtt is not None:
                bucket.sum_rtt_ms += rtt
                bucket.rtt_samples += 1
            if rsrq is not None:
                bucket.sum_rsrq += rsrq
                bucket.rsrq_samples += 1
            if congestion is not None:
                bucket.sum_congestion_pct += congestion
                bucket.congestion_samples += 1
            if ca_active:
                bucket.ca_samples += 1
            bucket.states[state] = bucket.states.get(state, 0) + 1
            if band > 0:
                bucket.bands[band] = bucket.bands.get(band, 0) + 1
        for metric_name, metric in (
            ("dl_mbps", dl),
            ("ul_mbps", ul),
            ("sinr_db", sinr),
            ("rtt_ms", rtt),
            ("rsrq_db", rsrq),
            ("congestion_pct", congestion),
        ):
            if metric is None:
                agg.invalid_metric_ticks[metric_name] += 1

        # Global sums
        if dl is not None:
            sum_dl += dl
            dl_n += 1
        if ul is not None:
            sum_ul += ul
            ul_n += 1
        if sinr is not None:
            sum_sinr += sinr
            sinr_n += 1
        if rtt is not None:
            sum_rtt += rtt
            rtt_n += 1
        if congestion is not None:
            sum_cong += congestion
            cong_n += 1

        # State distribution
        agg.state_ticks[state] += 1
        agg.state_seconds[state] += observed_seconds

        # Band stats
        if band > 0:
            if band not in agg.bands:
                agg.bands[band] = BandStats()
            bs = agg.bands[band]
            bs.samples += 1
            bs.observed_seconds += observed_seconds
            if dl is not None:
                bs.sum_dl_mbps += dl
                bs.dl_samples += 1
            if sinr is not None:
                bs.sum_sinr += sinr
                bs.sinr_samples += 1
            if congestion is not None:
                bs.sum_congestion += congestion
                bs.congestion_samples += 1
            if ca_active:
                bs.ca_samples += 1
            if band != prev_band and prev_band > 0:
                bs.switch_in_count += 1
                agg.band_switch_count += 1
                hb.band_switch_count += 1
                tb.band_switch_count += 1
            prev_band = band

        cell_key = (
            band if band > 0 else "",
            _first_label(row, "pci", "cell_pci"),
            _first_label(row, "earfcn", "dl_earfcn"),
            _first_label(row, "cell_id", "cellid"),
        )
        if any(cell_key):
            if prev_cell_key is not None and cell_key != prev_cell_key:
                agg.cell_switch_count += 1
                hb.cell_switch_count += 1
                tb.cell_switch_count += 1
            prev_cell_key = cell_key

        # Current runtime provenance and behavior dimensions.
        if congestion_source:
            agg.congestion_source_ticks[congestion_source] += 1
        if rsrq_source:
            agg.rsrq_source_ticks[rsrq_source] += 1
        if congestion_bucket:
            agg.congestion_bucket_ticks[congestion_bucket] += 1
        if host_profile:
            agg.host_profile_ticks[host_profile] += 1
        if ca_pool:
            agg.ca_pool_ticks[ca_pool] += 1
        if active_band_signature:
            agg.active_band_signature_ticks[active_band_signature] += 1
        if ca_state:
            agg.ca_state_ticks[ca_state] += 1
        if radio_state:
            agg.radio_state_ticks[radio_state] += 1
        if rat_snapshot:
            rat_state = _clean_label(_rat_snapshot_group(rat_snapshot, "rat_state").get("state"))
            nr_radio = _rat_snapshot_group(rat_snapshot, "nr_radio")
            transition = _rat_snapshot_group(rat_snapshot, "rat_transition")
            if rat_state:
                agg.rat_state_ticks[rat_state] += 1
            nr_band = _nr_band_label(nr_radio.get("band"))
            if nr_band:
                agg.rat_nr_band_ticks[nr_band] += 1
            if safe_bool(nr_radio.get("active", False), False):
                agg.rat_nr_active_tick_count += 1
            if safe_bool(transition.get("active", False), False):
                agg.rat_transition_tick_count += 1
        else:
            agg.rat_missing_scoped_tick_count += 1
        if learning_gate:
            if not safe_bool(learning_gate.get("learning_allowed", True), True):
                reason = _counter_reason(
                    str(
                        learning_gate.get("block_reason")
                        or learning_gate.get("learning_block_reason")
                        or ""
                    ),
                    "learning_gate_blocked",
                )
                agg.learning_gate_block_ticks[reason] += 1
        if mutation_window and safe_bool(mutation_window.get("active", False), False):
            reason = _counter_reason(
                str(mutation_window.get("reason") or mutation_window.get("state") or ""),
                "radio_mutation_window",
            )
            agg.mutation_window_ticks[reason] += 1
        if safe_bool(row.get("modem_read_missed", False), False):
            agg.modem_read_missed_tick_count += 1
            reason = _first_label(row, "modem_read_missed_reason", "modem_health_state")
            agg.read_miss_reason_ticks[_counter_reason(reason, "tick_row")] += 1

    agg.avg_dl_mbps = sum_dl / dl_n if dl_n else 0.0
    agg.avg_ul_mbps = sum_ul / ul_n if ul_n else 0.0
    agg.avg_sinr = sum_sinr / sinr_n if sinr_n else 0.0
    agg.avg_rtt_ms = sum_rtt / rtt_n if rtt_n else 0.0
    agg.avg_congestion_pct = sum_cong / cong_n if cong_n else 0.0

    # Actions from events.  One transaction contributes at most one terminal
    # outcome; issued/accepted stages are lifecycle evidence, not extra actions.
    terminal_action_ids = {id(event) for event in _terminal_action_events(events)}
    for ev in events:
        kind = str(ev.get("kind", "") or ev.get("event", ""))
        if kind:
            agg.event_kind_ticks[kind] += 1
        if id(ev) in terminal_action_ids:
            action_ok = safe_bool(
                ev.get("success", kind not in {"action_failed"}),
                kind not in {"action_failed"},
            )
            agg.actions.append(ActionRecord(
                ts=float(ev.get("ts", 0)),
                iso=str(ev.get("iso", "")),
                lever=str(ev.get("lever", "")),
                reason=str(ev.get("reason", "")),
                params=ev.get("params"),
                success=action_ok,
            ))
            agg.action_outcome_ticks["success" if action_ok else "failed"] += 1
            action_ts = _safe_float(ev.get("ts"), 0.0)
            if action_ts > 0.0:
                action_lt = time.localtime(action_ts)
                action_day = agg.days.get(time.strftime("%Y-%m-%d", action_lt))
                if action_day is not None:
                    action_day.hours[action_lt.tm_hour].action_count += 1
                    action_day.total.action_count += 1

        if kind.startswith("action_suppressed"):
            suffix = kind.removeprefix("action_suppressed").strip("_")
            reason = _event_text(ev, "reason", "bouncer_code", "modem_health_state") or suffix
            agg.action_suppression_ticks[_counter_reason(reason, "suppressed")] += 1

        if kind in {"tick_learning_gate_blocked", "post_action_learning_gate_blocked"}:
            reason = _event_text(ev, "block_reason", "learning_block_reason", "reason")
            agg.learning_gate_block_ticks[_counter_reason(reason, kind)] += 1

        if kind == "radio_mutation_phase":
            phase = _event_text(ev, "phase")
            workflow_id = _event_text(ev, "workflow_id")
            agg.mutation_phase_ticks[_counter_reason(phase, "unknown_phase")] += 1
            if workflow_id:
                agg.mutation_workflow_ticks[workflow_id] += 1

        if kind == "mutating_action_verification":
            note = _event_text(ev, "note", "verification_note")
            success = safe_bool(ev.get("success", False), False)
            agg.post_action_verification_ticks[_counter_reason(note, "verified" if success else "failed")] += 1

        if kind == "radio_lock_confirmation":
            success = safe_bool(ev.get("success", False), False)
            agg.action_outcome_ticks["radio_lock_success" if success else "radio_lock_failed"] += 1
            confirmation = ev.get("confirmation") if isinstance(ev.get("confirmation"), dict) else {}
            reason = _clean_label(confirmation.get("reason")) if confirmation else ""
            agg.lock_confirmation_reason_ticks[_counter_reason(reason, "radio_lock_confirmation")] += 1

        if kind == "modem_read_missed":
            agg.modem_read_missed_event_count += 1
            consecutive = _safe_int(ev.get("consecutive_failures"), 0)
            agg.modem_read_missed_max_consecutive = max(
                agg.modem_read_missed_max_consecutive,
                consecutive,
            )
            if safe_bool(ev.get("intentional_switch_grace", False), False):
                miss_reason = "intentional_switch_grace"
            elif safe_bool(ev.get("degraded", False), False):
                miss_reason = "modem_degraded"
            elif safe_bool(ev.get("reconnect_attempted", False), False):
                miss_reason = "reconnect_attempted"
            else:
                miss_reason = "transient_read_miss"
            agg.read_miss_reason_ticks[miss_reason] += 1

        bouncer_outcome = ev.get("bouncer_outcome") if isinstance(ev.get("bouncer_outcome"), dict) else {}
        referee_outcome = ev.get("referee_outcome") if isinstance(ev.get("referee_outcome"), dict) else {}
        if bouncer_outcome:
            code = _clean_label(bouncer_outcome.get("code")) or _clean_label(bouncer_outcome.get("level"))
            if code:
                agg.bouncer_outcome_ticks[code] += 1
        elif kind == "action_suppressed_bouncer_intent":
            code = _event_text(ev, "bouncer_code", "bouncer_level")
            if code:
                agg.bouncer_outcome_ticks[code] += 1
        if referee_outcome:
            code = _clean_label(referee_outcome.get("code")) or _clean_label(referee_outcome.get("level"))
            if code:
                agg.referee_outcome_ticks[code] += 1

        verdict = (
            _event_text(ev, "decision_verdict", "verdict", "final", "decision_trace_final")
            or _nested_text(ev, ("decision_trace", "decision_verdict"))
            or _nested_text(ev, ("decision_trace", "final"))
            or _nested_text(ev, ("brain_tsar", "decision_verdict"))
            or _nested_text(ev, ("brain_vectors", "brain_tsar", "decision_verdict"))
        )
        if verdict:
            agg.brain_verdict_ticks[verdict] += 1

        blocker = (
            _event_text(ev, "primary_blocker_gate", "blocker", "blocked_by", "gate", "reason")
            or _nested_text(ev, ("decision_trace", "primary_blocker_gate"))
            or _nested_text(ev, ("brain_tsar", "decision_reason"))
            or _nested_text(ev, ("brain_vectors", "brain_tsar", "decision_reason"))
        )
        if blocker and kind in (
            "decision_trace",
            "brain_posture_deferred",
            "radio_stability_hold_deferred",
            "exploration_suppressed",
            "cell_opt_suppressed",
            "tsar_vector_candidate_selected",
        ):
            agg.brain_blocker_ticks[blocker] += 1

    # Incidents from events (state transitions into fault/emergency states)
    open_incident: Optional[IncidentRecord] = None
    for ev in events:
        kind = str(ev.get("kind", "") or ev.get("event", ""))
        if kind != "state_transition":
            continue
        new_state = str(ev.get("new_state", "")).upper()
        ts = float(ev.get("ts", 0))
        iso = str(ev.get("iso", ""))
        reason = str(ev.get("reason", ""))

        if new_state in _INCIDENT_STATES:
            if open_incident is None:
                open_incident = IncidentRecord(
                    ts_start=ts, iso_start=iso, state=new_state, reason=reason,
                )
        else:
            if open_incident is not None:
                open_incident.ts_end = ts
                agg.incidents.append(open_incident)
                open_incident = None

    if open_incident is not None:
        open_incident.ts_end = last_ts
        agg.incidents.append(open_incident)

    return agg


# ═══════════════════════════════════════════════════════════════
#  3. SVG Chart Generators
# ═══════════════════════════════════════════════════════════════

def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _color_for_congestion(pct: float) -> str:
    """Green → yellow → red gradient for congestion percentage."""
    if pct <= 0:
        return "#e8e8e8"
    if pct < 30:
        r = int(80 + pct * 3)
        return f"#{r:02x}cc44"
    if pct < 60:
        t = (pct - 30) / 30.0
        r = int(180 + t * 75)
        g = int(200 - t * 100)
        return f"#{r:02x}{g:02x}33"
    r = min(255, int(220 + (pct - 60) * 0.8))
    g = max(40, int(100 - (pct - 60) * 1.2))
    return f"#{r:02x}{g:02x}33"


def _band_color(band: int) -> str:
    """Consistent color per band number."""
    palette = [
        "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
        "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
        "#9c755f", "#bab0ac", "#6b9ac4", "#d4a373",
    ]
    return palette[band % len(palette)]


def svg_congestion_heatmap(agg: SiteAggregation) -> str:
    """24h × days heatmap of average congestion per hour."""
    sorted_days = sorted(agg.days.keys())
    if not sorted_days:
        return ""

    cell = SVG_HEATMAP_CELL
    gap = SVG_HEATMAP_GAP
    step = cell + gap
    label_w = 80
    header_h = 30
    n_days = len(sorted_days)
    w = label_w + 24 * step + 10
    h = header_h + n_days * step + 40  # extra for legend

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}" style="font-family:monospace;font-size:10px;">']

    # Hour labels
    for hr in range(24):
        x = label_w + hr * step + cell // 2
        parts.append(f'<text x="{x}" y="{header_h - 6}" text-anchor="middle" '
                     f'fill="#666" font-size="9">{hr:02d}</text>')

    # Day rows
    for i, day_key in enumerate(sorted_days):
        y = header_h + i * step
        # Day label — short date
        parts.append(f'<text x="{label_w - 6}" y="{y + cell - 3}" text-anchor="end" '
                     f'fill="#666" font-size="9">{day_key[5:]}</text>')
        day = agg.days[day_key]
        for hr in range(24):
            x = label_w + hr * step
            hb = day.hours[hr]
            if hb.samples == 0:
                # No data for this hour — faint outline only
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                    f'rx="2" fill="none" stroke="#333" stroke-width="0.5">'
                    f'<title>{day_key} {hr:02d}:00 — no data</title></rect>'
                )
                continue
            cong = hb.avg_congestion
            color = _color_for_congestion(cong)
            title = f"{day_key} {hr:02d}:00 — {cong:.0f}% ({hb.samples} ticks)"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                f'rx="2" fill="{color}">'
                f'<title>{_svg_escape(title)}</title></rect>'
            )

    # Legend
    legend_y = header_h + n_days * step + 8
    legend_stops = [0, 20, 40, 60, 80, 100]
    parts.append(f'<text x="{label_w}" y="{legend_y + 10}" fill="#666" font-size="9">Congestion:</text>')
    for j, pct in enumerate(legend_stops):
        lx = label_w + 70 + j * 36
        parts.append(
            f'<rect x="{lx}" y="{legend_y}" width="28" height="14" rx="2" '
            f'fill="{_color_for_congestion(pct)}"/>'
            f'<text x="{lx + 14}" y="{legend_y + 11}" text-anchor="middle" '
            f'fill="#333" font-size="8">{pct}%</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def svg_daily_timeline(agg: SiteAggregation) -> str:
    """Multi-line timeline: DL, congestion, RTT, SINR.

    Uses hourly resolution when data spans ≤2 days, daily otherwise.
    """
    sorted_days = sorted(agg.days.keys())
    if not sorted_days:
        return ""

    # Decide granularity: hourly when ≤2 days of data
    use_hourly = len(sorted_days) <= 2

    if use_hourly:
        # Build hourly data points from populated hours only
        labels: list[str] = []
        dl_vals: list[float] = []
        cong_vals: list[float] = []
        rtt_vals: list[float] = []
        sinr_vals: list[float] = []
        for day_key in sorted_days:
            day = agg.days[day_key]
            for hr in range(24):
                hb = day.hours[hr]
                if hb.samples == 0:
                    continue
                labels.append(f"{hr:02d}:00")
                dl_vals.append(hb.avg_dl)
                cong_vals.append(hb.avg_congestion)
                rtt_vals.append(hb.avg_rtt)
                sinr_vals.append(hb.avg_sinr)
    else:
        labels = [d[5:] for d in sorted_days]
        dl_vals = [agg.days[d].total.avg_dl for d in sorted_days]
        cong_vals = [agg.days[d].total.avg_congestion for d in sorted_days]
        rtt_vals = [agg.days[d].total.avg_rtt for d in sorted_days]
        sinr_vals = [agg.days[d].total.avg_sinr for d in sorted_days]

    n = len(labels)
    if n == 0:
        return ""

    w = SVG_CHART_W
    h = SVG_CHART_H
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 40
    chart_w = w - pad_l - pad_r
    chart_h = h - pad_t - pad_b

    def _line(vals: list[float], color: str, max_val: float, label: str) -> str:
        if max_val <= 0:
            max_val = 1.0
        points = []
        for i, v in enumerate(vals):
            x = pad_l + (i / max(1, n - 1)) * chart_w if n > 1 else pad_l + chart_w / 2
            y = pad_t + chart_h - (min(v, max_val) / max_val) * chart_h
            points.append(f"{x:.1f},{y:.1f}")
        pts_str = " ".join(points)
        return (f'<polyline points="{pts_str}" fill="none" stroke="{color}" '
                f'stroke-width="1.8" stroke-linejoin="round"/>')

    max_dl = max(dl_vals) * 1.1 if dl_vals else 1.0
    max_rtt = max(rtt_vals) * 1.1 if rtt_vals else 1.0
    max_sinr = max(max(sinr_vals) * 1.1, 1.0) if sinr_vals else 1.0

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}" style="font-family:monospace;font-size:10px;">']

    # Grid lines
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + chart_h * (1.0 - frac)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.0f}" x2="{w - pad_r}" y2="{gy:.0f}" '
                     f'stroke="#eee" stroke-width="1"/>')

    # Lines
    parts.append(_line(dl_vals, "#4e79a7", max_dl, "DL"))
    parts.append(_line(cong_vals, "#e15759", 100.0, "Cong"))
    parts.append(_line(rtt_vals, "#f28e2b", max_rtt, "RTT"))
    parts.append(_line(sinr_vals, "#59a14f", max_sinr, "SINR"))

    # X-axis labels (sparse)
    step = max(1, n // 8)
    for i in range(0, n, step):
        x = pad_l + (i / max(1, n - 1)) * chart_w if n > 1 else pad_l + chart_w / 2
        parts.append(f'<text x="{x:.0f}" y="{h - 8}" text-anchor="middle" '
                     f'fill="#666" font-size="8">{labels[i]}</text>')

    # Legend
    legend_items = [("DL Mbps", "#4e79a7"), ("Cong %", "#e15759"),
                    ("RTT ms", "#f28e2b"), ("SINR dB", "#59a14f")]
    for j, (lbl, clr) in enumerate(legend_items):
        lx = pad_l + j * 110
        parts.append(f'<line x1="{lx}" y1="{pad_t - 8}" x2="{lx + 20}" y2="{pad_t - 8}" '
                     f'stroke="{clr}" stroke-width="2"/>')
        parts.append(f'<text x="{lx + 24}" y="{pad_t - 4}" fill="#666" font-size="9">{lbl}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def svg_band_bars(agg: SiteAggregation) -> str:
    """Horizontal bar chart of time spent per band."""
    if not agg.bands:
        return ""

    sorted_bands = sorted(agg.bands.keys(), key=lambda b: agg.bands[b].samples, reverse=True)
    max_samples = max(bs.samples for bs in agg.bands.values())
    if max_samples <= 0:
        return ""

    bar_h = SVG_BAR_H
    gap = 4
    label_w = 70
    value_w = 190
    pad_r = 20
    w = SVG_CHART_W
    chart_w = w - label_w - value_w - pad_r
    h = len(sorted_bands) * (bar_h + gap) + 10

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}" style="font-family:monospace;font-size:10px;">']

    for i, band in enumerate(sorted_bands):
        bs = agg.bands[band]
        y = i * (bar_h + gap)
        bar_w = max(2, (bs.samples / max_samples) * chart_w)
        color = _band_color(band)
        hours = bs.time_hours
        avg_dl = bs.avg_dl

        parts.append(f'<text x="{label_w - 6}" y="{y + bar_h - 6}" text-anchor="end" '
                     f'fill="#333" font-size="11">B{band}</text>')
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.0f}" height="{bar_h}" '
            f'rx="3" fill="{color}" opacity="0.85">'
            f'<title>Band {band}: {hours:.1f}h, avg DL {avg_dl:.1f} Mbps, '
            f'{bs.switch_in_count} switches</title></rect>'
        )
        # Inline text uses a reserved value column so long bars cannot clip it.
        txt_x = label_w + chart_w + 8
        parts.append(f'<text x="{txt_x:.0f}" y="{y + bar_h - 6}" fill="#666" '
                     f'font-size="9">{hours:.1f}h · {avg_dl:.1f} Mbps · '
                     f'{bs.switch_in_count} sw</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def svg_state_pie(agg: SiteAggregation) -> str:
    """Simple donut chart of state distribution."""
    if not agg.state_ticks:
        return ""

    total = sum(agg.state_ticks.values())
    if total <= 0:
        return ""

    size = 200
    cx, cy, r = size // 2, size // 2, 70
    ri = 40  # inner radius for donut

    state_colors = {
        "MONITORING": "#59a14f",
        "OPTIMIZING": "#4e79a7",
        "RECOVERING": "#f28e2b",
        "EXPLORING": "#76b7b2",
        "EMERGENCY": "#e15759",
        "FAULT": "#b07aa1",
        "CRITICAL": "#ff9da7",
        "STARTUP": "#edc948",
        "IDLE": "#bab0ac",
    }

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size + 140} {size}" '
             f'width="{size + 140}" height="{size}" style="font-family:monospace;font-size:10px;">']

    sorted_states = sorted(agg.state_ticks.keys(), key=lambda s: agg.state_ticks[s], reverse=True)
    angle = -math.pi / 2  # start at top

    for state in sorted_states:
        count = agg.state_ticks[state]
        frac = count / total
        sweep = frac * 2 * math.pi
        if sweep < 0.001:
            continue

        color = state_colors.get(state, "#999")
        large = 1 if sweep > math.pi else 0

        x1o = cx + r * math.cos(angle)
        y1o = cy + r * math.sin(angle)
        x2o = cx + r * math.cos(angle + sweep)
        y2o = cy + r * math.sin(angle + sweep)
        x1i = cx + ri * math.cos(angle + sweep)
        y1i = cy + ri * math.sin(angle + sweep)
        x2i = cx + ri * math.cos(angle)
        y2i = cy + ri * math.sin(angle)

        path = (f'M {x1o:.1f} {y1o:.1f} '
                f'A {r} {r} 0 {large} 1 {x2o:.1f} {y2o:.1f} '
                f'L {x1i:.1f} {y1i:.1f} '
                f'A {ri} {ri} 0 {large} 0 {x2i:.1f} {y2i:.1f} Z')
        title = (
            f"{state}: {frac * 100:.1f}% "
            f"({agg.state_seconds.get(state, 0.0) / 3600.0:.1f}h)"
        )
        parts.append(f'<path d="{path}" fill="{color}" stroke="white" stroke-width="1">'
                     f'<title>{_svg_escape(title)}</title></path>')
        angle += sweep

    # Legend
    lx = size + 10
    for j, state in enumerate(sorted_states[:8]):
        ly = 20 + j * 22
        color = state_colors.get(state, "#999")
        pct = agg.state_ticks[state] / total * 100
        parts.append(f'<rect x="{lx}" y="{ly}" width="12" height="12" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{lx + 16}" y="{ly + 10}" fill="#333" font-size="10">'
                     f'{state} {pct:.0f}%</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def svg_count_bars(
    counts: dict[str, int],
    *,
    limit: int = 8,
    color: str = "#4e79a7",
    label_map: dict[str, str] | None = None,
) -> str:
    """Horizontal bars for compact categorical report summaries."""
    filtered = [(k, v) for k, v in counts.items() if k and v > 0]
    if not filtered:
        return ""
    filtered.sort(key=lambda item: (-item[1], item[0]))
    rows = filtered[:limit]
    total = sum(v for _, v in filtered) or 1
    max_count = max(v for _, v in rows)
    if max_count <= 0:
        return ""

    w = SVG_CHART_W
    label_w = 230
    value_w = 110
    pad_r = 20
    chart_w = max(120, w - label_w - value_w - pad_r)
    row_h = 24
    gap = 5
    h = len(rows) * (row_h + gap) + 8

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}" style="font-family:monospace;font-size:10px;">']
    for i, (label, count) in enumerate(rows):
        y = i * (row_h + gap)
        bar_w = max(2.0, count / max_count * chart_w)
        pct = count / total * 100.0
        display_label = str((label_map or {}).get(label, label))
        shown_label = display_label if len(display_label) <= 36 else f"{display_label[:33]}..."
        value_x = label_w + chart_w + 8
        parts.append(f'<text x="{label_w - 8}" y="{y + row_h - 7}" text-anchor="end" '
                     f'fill="#333" font-size="10">{_svg_escape(shown_label)}</text>')
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.0f}" height="{row_h}" '
            f'rx="3" fill="{color}" opacity="0.86">'
            f'<title>{_svg_escape(display_label)}: {pct:.1f}% ({count} ticks)</title></rect>'
        )
        parts.append(f'<text x="{value_x:.0f}" y="{y + row_h - 7}" fill="#666" '
                     f'font-size="9">{pct:.1f}% · {count}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def svg_ca_anchor_timeline(agg: SiteAggregation) -> str:
    """Visualize CA, RF/load pressure, anchor churn, and action pressure over time."""
    sorted_days = sorted(agg.days.keys())
    if not sorted_days:
        return ""

    labels: list[str] = []
    ca_vals: list[float] = []
    congestion_vals: list[float] = []
    sinr_quality_vals: list[float] = []
    rtt_pressure_vals: list[float] = []
    switch_vals: list[float] = []
    action_vals: list[float] = []

    def _sinr_quality(sinr_db: float) -> float:
        # Normalized operator signal quality: -5 dB = 0, 20 dB = 100.
        return max(0.0, min(100.0, (float(sinr_db) + 5.0) / 25.0 * 100.0))

    def _collect(label: str, bucket: HourBucket) -> None:
        labels.append(label)
        ca_vals.append(bucket.ca_pct)
        congestion_vals.append(max(0.0, min(100.0, bucket.avg_congestion)))
        sinr_quality_vals.append(_sinr_quality(bucket.avg_sinr))
        rtt_pressure_vals.append(max(0.0, float(bucket.avg_rtt)))
        switch_vals.append(float(bucket.band_switch_count + bucket.cell_switch_count))
        action_vals.append(float(bucket.action_count))

    use_hourly = len(sorted_days) <= 2
    if use_hourly:
        for day_key in sorted_days:
            day = agg.days[day_key]
            for hr in range(24):
                hb = day.hours[hr]
                if hb.samples == 0:
                    continue
                _collect(f"{hr:02d}:00", hb)
    else:
        for day_key in sorted_days:
            tb = agg.days[day_key].total
            if tb.samples == 0:
                continue
            _collect(day_key[5:], tb)

    n = len(labels)
    if n == 0:
        return ""

    w = SVG_CHART_W
    h = SVG_CHART_H + 38
    pad_l, pad_r, pad_t, pad_b = 50, 20, 48, 40
    chart_w = w - pad_l - pad_r
    chart_h = h - pad_t - pad_b
    max_switch = max(max(switch_vals), 1.0)
    max_action = max(max(action_vals), 1.0)
    max_rtt = max(max(rtt_pressure_vals), 1.0)

    def _line(vals: list[float], color: str, max_val: float) -> str:
        points = []
        for i, val in enumerate(vals):
            x = pad_l + (i / max(1, n - 1)) * chart_w if n > 1 else pad_l + chart_w / 2
            y = pad_t + chart_h - (min(max(val, 0.0), max_val) / max_val) * chart_h
            points.append(f"{x:.1f},{y:.1f}")
        return (f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round"/>')

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}" style="font-family:monospace;font-size:10px;">']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + chart_h * (1.0 - frac)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.0f}" x2="{w - pad_r}" y2="{gy:.0f}" '
                     f'stroke="#eee" stroke-width="1"/>')

    parts.append(_line(ca_vals, "#59a14f", 100.0))
    parts.append(_line(congestion_vals, "#e15759", 100.0))
    parts.append(_line(sinr_quality_vals, "#76b7b2", 100.0))
    parts.append(_line(rtt_pressure_vals, "#f28e2b", max_rtt))
    parts.append(_line(switch_vals, "#9c755f", max_switch))
    parts.append(_line(action_vals, "#4e79a7", max_action))

    legend_items = [
        ("CA %", "#59a14f"),
        ("Cong %", "#e15759"),
        ("SINR quality", "#76b7b2"),
        (f"RTT pressure, max {max_rtt:.0f}ms", "#f28e2b"),
        (f"Band+cell switches, max {max_switch:.0f}", "#9c755f"),
        (f"Actions, max {max_action:.0f}", "#4e79a7"),
    ]
    for j, (lbl, clr) in enumerate(legend_items):
        lx = pad_l + (j % 3) * 215
        ly = 14 + (j // 3) * 16
        parts.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 20}" y2="{ly}" '
                     f'stroke="{clr}" stroke-width="2"/>')
        parts.append(f'<text x="{lx + 24}" y="{ly + 4}" fill="#666" font-size="9">{_svg_escape(lbl)}</text>')

    step = max(1, n // 8)
    for i in range(0, n, step):
        x = pad_l + (i / max(1, n - 1)) * chart_w if n > 1 else pad_l + chart_w / 2
        parts.append(f'<text x="{x:.0f}" y="{h - 8}" text-anchor="middle" '
                     f'fill="#666" font-size="8">{_svg_escape(labels[i])}</text>')
    parts.append(f'<text x="{pad_l - 8}" y="{pad_t + 4}" text-anchor="end" fill="#666" font-size="8">100</text>')
    parts.append(f'<text x="{pad_l - 8}" y="{pad_t + chart_h}" text-anchor="end" fill="#666" font-size="8">0</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
#  4. HTML Report Builder
# ═══════════════════════════════════════════════════════════════

def _fmt(val: float, decimals: int = 1) -> str:
    return f"{val:.{decimals}f}"


def _duration_str(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _count_breakdown_table(title: str, label_header: str, counts: dict[str, int], *, limit: int = 10) -> str:
    total = sum(counts.values()) or 1
    rows = ""
    for source, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        pct = count / total * 100.0
        rows += f"<tr><td>{_svg_escape(source)}</td><td>{pct:.1f}%</td><td>{count}</td></tr>\n"
    if not rows:
        return ""
    return (
        f"<h3 style=\"font-size:0.95em;margin:12px 0 8px 0;\">{_svg_escape(title)}</h3>\n"
        "<table>\n"
        f"<tr><th>{_svg_escape(label_header)}</th><th>Share</th><th>Ticks</th></tr>\n"
        f"{rows}</table>"
    )


def _source_breakdown_table(title: str, counts: dict[str, int]) -> str:
    return _count_breakdown_table(title, "Source", counts)


def _top_count_label(counts: dict[str, int]) -> str:
    if not counts:
        return "n/a"
    label, _count = max(counts.items(), key=lambda item: item[1])
    return label or "n/a"


def build_html(agg: SiteAggregation) -> str:
    """Render a self-contained HTML report."""

    heatmap_svg = svg_congestion_heatmap(agg)
    timeline_svg = svg_daily_timeline(agg)
    band_svg = svg_band_bars(agg)
    state_svg = svg_state_pie(agg)
    ca_anchor_svg = svg_ca_anchor_timeline(agg)
    ca_pool_svg = svg_count_bars(agg.ca_pool_ticks, color="#4e79a7")
    active_signature_svg = svg_count_bars(agg.active_band_signature_ticks, color="#76b7b2")
    ca_state_dist_svg = svg_count_bars(agg.ca_state_ticks, color="#59a14f")
    radio_state_dist_svg = svg_count_bars(agg.radio_state_ticks, color="#f28e2b")
    rat_state_dist_svg = svg_count_bars(agg.rat_state_ticks, color="#6b9ac4")
    rat_nr_band_svg = svg_count_bars(agg.rat_nr_band_ticks, color="#59a14f")
    congestion_bucket_svg = svg_count_bars(agg.congestion_bucket_ticks, color="#e15759")
    brain_verdict_svg = svg_count_bars(agg.brain_verdict_ticks, color="#b07aa1")
    event_kind_svg = svg_count_bars(agg.event_kind_ticks, limit=10, color="#9c755f")
    learning_gate_svg = svg_count_bars(agg.learning_gate_block_ticks, color="#b07aa1", label_map=_OPERATOR_REASON_LABELS)
    mutation_phase_svg = svg_count_bars(agg.mutation_phase_ticks, color="#9c755f")
    mutation_workflow_svg = svg_count_bars(agg.mutation_workflow_ticks, color="#6b9ac4")
    mutation_window_svg = svg_count_bars(agg.mutation_window_ticks, color="#f28e2b", label_map=_OPERATOR_REASON_LABELS)
    action_outcome_svg = svg_count_bars(agg.action_outcome_ticks, color="#4e79a7")
    action_suppression_svg = svg_count_bars(agg.action_suppression_ticks, color="#e15759")
    post_action_verification_svg = svg_count_bars(agg.post_action_verification_ticks, color="#76b7b2")
    lock_confirmation_svg = svg_count_bars(agg.lock_confirmation_reason_ticks, color="#edc948")
    read_miss_svg = svg_count_bars(agg.read_miss_reason_ticks, color="#f28e2b")
    bouncer_outcome_svg = svg_count_bars(agg.bouncer_outcome_ticks, color="#6b9ac4")
    referee_outcome_svg = svg_count_bars(agg.referee_outcome_ticks, color="#59a14f")

    total_ca_samples = sum(day.total.ca_samples for day in agg.days.values())
    ca_pct = 100.0 * total_ca_samples / agg.total_ticks if agg.total_ticks else 0.0
    learning_block_total = sum(agg.learning_gate_block_ticks.values())
    action_suppression_total = sum(agg.action_suppression_ticks.values())
    read_miss_total = agg.modem_read_missed_tick_count + agg.modem_read_missed_event_count
    rat_scoped_tick_count = max(0, agg.total_ticks - agg.rat_missing_scoped_tick_count)
    rat_scoped_pct = 100.0 * rat_scoped_tick_count / agg.total_ticks if agg.total_ticks else 0.0
    evidence_scope_note = (
        f"Evidence scope: {_svg_escape(agg.evidence_scope)}; "
        f"excluded {agg.scope_rejected_ticks} tick(s) and "
        f"{agg.scope_rejected_events} event(s) from other/unscoped sessions."
        if agg.evidence_scope
        else "Evidence scope: legacy rows did not publish a session/identity scope."
    )
    rat_nr_active_pct = 100.0 * agg.rat_nr_active_tick_count / agg.total_ticks if agg.total_ticks else 0.0
    rat_transition_pct = 100.0 * agg.rat_transition_tick_count / agg.total_ticks if agg.total_ticks else 0.0

    provenance_sections = "".join(
        section
        for section in (
            _source_breakdown_table("Congestion Source", agg.congestion_source_ticks),
            _source_breakdown_table("RSRQ Source", agg.rsrq_source_ticks),
            _count_breakdown_table("Congestion Bucket", "Bucket", agg.congestion_bucket_ticks),
            _count_breakdown_table("Brain Verdict", "Verdict", agg.brain_verdict_ticks),
            _count_breakdown_table("Brain Hold / Blocker", "Reason", agg.brain_blocker_ticks),
        )
        if section
    )

    # Band table
    band_rows = ""
    total_band_samples = sum(bs.samples for bs in agg.bands.values()) or 1
    total_band_seconds = sum(bs.observed_seconds for bs in agg.bands.values())
    for band in sorted(agg.bands.keys(), key=lambda b: agg.bands[b].samples, reverse=True):
        bs = agg.bands[band]
        time_pct = (
            bs.observed_seconds / total_band_seconds * 100
            if total_band_seconds > 0.0
            else bs.samples / total_band_samples * 100
        )
        band_ca_pct = 100.0 * bs.ca_samples / bs.samples if bs.samples else 0
        band_rows += (
            f"<tr><td>B{band}</td>"
            f"<td>{bs.time_hours:.1f}h ({time_pct:.0f}%)</td>"
            f"<td>{bs.avg_dl:.1f}</td>"
            f"<td>{bs.avg_sinr:.1f}</td>"
            f"<td>{bs.avg_congestion:.0f}%</td>"
            f"<td>{band_ca_pct:.0f}%</td>"
            f"<td>{bs.switch_in_count}</td></tr>\n"
        )

    # Incident table
    incident_rows = ""
    for inc in agg.incidents:
        incident_rows += (
            f"<tr><td>{_svg_escape(inc.iso_start)}</td>"
            f"<td>{_svg_escape(inc.state)}</td>"
            f"<td>{_duration_str(inc.duration_s)}</td>"
            f"<td>{_svg_escape(inc.reason)}</td></tr>\n"
        )

    # Action summary
    action_lever_counts: dict[str, int] = defaultdict(int)
    for a in agg.actions:
        action_lever_counts[a.lever] += 1
    action_summary_rows = ""
    for lever, count in sorted(action_lever_counts.items(), key=lambda x: -x[1]):
        action_summary_rows += f"<tr><td>{_svg_escape(lever)}</td><td>{count}</td></tr>\n"

    # Recent actions (last 20)
    recent_actions_rows = ""
    for a in agg.actions[-20:]:
        recent_actions_rows += (
            f"<tr><td>{_svg_escape(a.iso)}</td>"
            f"<td>{_svg_escape(a.lever)}</td>"
            f"<td>{_svg_escape(a.reason)}</td>"
            f"<td>{'✓' if a.success else '✗'}</td></tr>\n"
        )

    gen_time = time.strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>54GUC Site Report — {_svg_escape(agg.date_start)} to {_svg_escape(agg.date_end)}</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --muted: #666;
    --accent: #4e79a7; --border: #e0e0e0;
    --section-bg: #fafafa; --table-header: #f0f0f0;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a1a; --fg: #e0e0e0; --muted: #999;
      --accent: #6fa8dc; --border: #333;
      --section-bg: #222; --table-header: #2a2a2a;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    background: var(--bg); color: var(--fg);
    max-width: 840px; margin: 0 auto; padding: 24px 16px;
    line-height: 1.5;
  }}
  h1 {{ font-size: 1.6em; margin-bottom: 4px; color: var(--accent); }}
  h2 {{
    font-size: 1.15em; margin: 28px 0 12px 0; padding-bottom: 4px;
    border-bottom: 2px solid var(--accent); color: var(--fg);
  }}
  .subtitle {{ color: var(--muted); font-size: 0.9em; margin-bottom: 20px; }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px; margin: 16px 0;
  }}
  .kpi {{
    background: var(--section-bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 12px; text-align: center;
  }}
  .kpi .value {{ font-size: 1.5em; font-weight: 700; color: var(--accent); }}
  .kpi .label {{ font-size: 0.8em; color: var(--muted); margin-top: 2px; }}
  .chart-wrap {{
    background: var(--section-bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 12px; margin: 12px 0;
    overflow-x: auto;
  }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 0.85em;
    margin: 8px 0;
  }}
  th, td {{ padding: 6px 10px; border: 1px solid var(--border); text-align: left; }}
  th {{ background: var(--table-header); font-weight: 600; }}
  tr:nth-child(even) {{ background: var(--section-bg); }}
  .empty {{ color: var(--muted); font-style: italic; padding: 12px; }}
  svg {{ max-width: 100%; height: auto; }}
  @media print {{
    body {{ max-width: none; padding: 0; font-size: 10pt; }}
    h2 {{ page-break-after: avoid; }}
    .chart-wrap {{ break-inside: avoid; }}
    table {{ break-inside: avoid; }}
  }}
  .footer {{ margin-top: 40px; padding-top: 12px; border-top: 1px solid var(--border);
             color: var(--muted); font-size: 0.8em; text-align: center; }}
</style>
</head>
<body>

<h1>54GUC Site Report</h1>
<div class="subtitle">{_svg_escape(agg.date_start)} — {_svg_escape(agg.date_end)} · Generated {gen_time}</div>
<div class="subtitle">{evidence_scope_note}</div>

<!-- ══ Executive Summary ══ -->
<h2>1. Executive Summary</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="value">{_fmt(agg.runtime_hours)}</div><div class="label">Runtime (hours)</div></div>
  <div class="kpi"><div class="value">{_fmt(agg.avg_dl_mbps)}</div><div class="label">Avg DL (Mbps)</div></div>
  <div class="kpi"><div class="value">{_fmt(agg.avg_ul_mbps)}</div><div class="label">Avg UL (Mbps)</div></div>
  <div class="kpi"><div class="value">{_fmt(agg.avg_congestion_pct, 0)}%</div><div class="label">Avg Congestion</div></div>
  <div class="kpi"><div class="value">{_fmt(agg.avg_sinr)}</div><div class="label">Avg SINR (dB)</div></div>
  <div class="kpi"><div class="value">{_fmt(agg.avg_rtt_ms, 0)}</div><div class="label">Avg RTT (ms)</div></div>
  <div class="kpi"><div class="value">{_fmt(ca_pct, 0)}%</div><div class="label">CA Active</div></div>
  <div class="kpi"><div class="value">{agg.band_switch_count}</div><div class="label">Band Switches</div></div>
  <div class="kpi"><div class="value">{agg.cell_switch_count}</div><div class="label">Cell Switches</div></div>
  <div class="kpi"><div class="value">{len(agg.actions)}</div><div class="label">Actions Taken</div></div>
  <div class="kpi"><div class="value">{action_suppression_total}</div><div class="label">Actions Suppressed</div></div>
  <div class="kpi"><div class="value">{learning_block_total}</div><div class="label">Learning Blocks</div></div>
  <div class="kpi"><div class="value">{read_miss_total}</div><div class="label">Read Misses</div></div>
  <div class="kpi"><div class="value">{len(agg.incidents)}</div><div class="label">Incidents</div></div>
</div>

{state_svg}

<!-- ══ Host / Runtime Scope ══ -->
<h2>2. Host / Runtime Scope</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  This report is scoped to the observed host profile and runtime evidence in the selected output window.
</p>
<div class="chart-wrap">
{svg_count_bars(agg.host_profile_ticks, limit=4, color="#6b9ac4") if agg.host_profile_ticks else '<div class="empty">No host identity data available.</div>'}
</div>
{_count_breakdown_table("Host Profile", "Profile", agg.host_profile_ticks, limit=4)}

<!-- ══ CA / Anchor Behavior ══ -->
<h2>3. CA / Anchor Behavior</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  Correlates CA activity, congestion, SINR quality, RTT pressure, anchor churn, and 54GUC actions on the same time axis.
</p>
<div class="kpi-grid">
  <div class="kpi"><div class="value">{_fmt(ca_pct, 0)}%</div><div class="label">CA Active</div></div>
  <div class="kpi"><div class="value">{agg.band_switch_count}</div><div class="label">Band Switches</div></div>
  <div class="kpi"><div class="value">{agg.cell_switch_count}</div><div class="label">Cell Switches</div></div>
  <div class="kpi"><div class="value">{_svg_escape(_top_count_label(agg.ca_pool_ticks))}</div><div class="label">Top Configured Pool</div></div>
</div>
<div class="chart-wrap">
{ca_anchor_svg if ca_anchor_svg else '<div class="empty">No CA/anchor timeline data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Configured / Selected Band Pool</h3>
<div class="chart-wrap">
{ca_pool_svg if ca_pool_svg else '<div class="empty">No configured pool data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Active CA Signature</h3>
<div class="chart-wrap">
{active_signature_svg if active_signature_svg else '<div class="empty">No active CA signature data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">CA State</h3>
<div class="chart-wrap">
{ca_state_dist_svg if ca_state_dist_svg else '<div class="empty">No CA state data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Radio State</h3>
<div class="chart-wrap">
{radio_state_dist_svg if radio_state_dist_svg else '<div class="empty">No radio state data available.</div>'}
</div>

<!-- ══ RAT Model ══ -->
<h2>4. RAT Model</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  Scoped LTE/NR evidence from the canonical RAT snapshot. Missing scoped evidence is reported directly, not inferred from flat radio columns.
</p>
<div class="kpi-grid">
  <div class="kpi"><div class="value">{_fmt(rat_scoped_pct, 0)}%</div><div class="label">Scoped RAT Rows</div></div>
  <div class="kpi"><div class="value">{_fmt(rat_nr_active_pct, 0)}%</div><div class="label">NR Active Rows</div></div>
  <div class="kpi"><div class="value">{_fmt(rat_transition_pct, 0)}%</div><div class="label">RAT Transition Rows</div></div>
  <div class="kpi"><div class="value">{agg.rat_missing_scoped_tick_count}</div><div class="label">Missing Scoped Rows</div></div>
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">RAT State</h3>
<div class="chart-wrap">
{rat_state_dist_svg if rat_state_dist_svg else '<div class="empty">No scoped RAT state data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">NR Bands</h3>
<div class="chart-wrap">
{rat_nr_band_svg if rat_nr_band_svg else '<div class="empty">No scoped NR band data available.</div>'}
</div>
{_count_breakdown_table("RAT State Breakdown", "State", agg.rat_state_ticks)}
{_count_breakdown_table("NR Band Breakdown", "Band", agg.rat_nr_band_ticks)}

<!-- ══ Congestion Profile ══ -->
<h2>5. Congestion Profile</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  24-hour heatmap — rows are days, columns are hours.  Hover for details.
</p>
<div class="chart-wrap">
{heatmap_svg if heatmap_svg else '<div class="empty">No congestion data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Congestion Bucket</h3>
<div class="chart-wrap">
{congestion_bucket_svg if congestion_bucket_svg else '<div class="empty">No congestion bucket data available.</div>'}
</div>

{provenance_sections}

<!-- ══ Performance Timeline ══ -->
<h2>6. Performance Timeline</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  Daily averages for DL throughput, congestion, RTT, and SINR.
</p>
<div class="chart-wrap">
{timeline_svg if timeline_svg else '<div class="empty">No timeline data available.</div>'}
</div>

<!-- ══ Band Analysis ══ -->
<h2>7. Band Analysis</h2>
<div class="chart-wrap">
{band_svg if band_svg else '<div class="empty">No band data available.</div>'}
</div>

{f'''<table>
<tr><th>Band</th><th>Time</th><th>Avg DL</th><th>Avg SINR</th><th>Avg Cong</th><th>CA %</th><th>Switches</th></tr>
{band_rows}
</table>''' if band_rows else ''}

<!-- ══ Incident Log ══ -->
<h2>8. Brain / Event Pressure</h2>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Brain Verdict</h3>
<div class="chart-wrap">
{brain_verdict_svg if brain_verdict_svg else '<div class="empty">No brain verdict events available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Event Kind</h3>
<div class="chart-wrap">
{event_kind_svg if event_kind_svg else '<div class="empty">No event-kind data available.</div>'}
</div>

<!-- ══ Runtime Learning / Action Evidence ══ -->
<h2>9. Runtime Learning / Action Evidence</h2>
<p style="color:var(--muted);font-size:0.9em;margin-bottom:8px;">
  Aggregated from canonical runtime events and telemetry owner surfaces. This section explains why learning or actions were blocked during soak.
</p>
<div class="kpi-grid">
  <div class="kpi"><div class="value">{learning_block_total}</div><div class="label">Learning Gate Blocks</div></div>
  <div class="kpi"><div class="value">{action_suppression_total}</div><div class="label">Action Suppressions</div></div>
  <div class="kpi"><div class="value">{read_miss_total}</div><div class="label">Read Miss Ticks/Events</div></div>
  <div class="kpi"><div class="value">{agg.modem_read_missed_max_consecutive}</div><div class="label">Max Consecutive Read Miss</div></div>
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Learning Gate Blocks</h3>
<div class="chart-wrap">
{learning_gate_svg if learning_gate_svg else '<div class="empty">No learning gate blocks recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Action Outcomes</h3>
<div class="chart-wrap">
{action_outcome_svg if action_outcome_svg else '<div class="empty">No action outcome data available.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Action Suppressions</h3>
<div class="chart-wrap">
{action_suppression_svg if action_suppression_svg else '<div class="empty">No action suppression events recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Radio Mutation Phases</h3>
<div class="chart-wrap">
{mutation_phase_svg if mutation_phase_svg else '<div class="empty">No radio mutation phase events recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Radio Mutation Workflows</h3>
<div class="chart-wrap">
{mutation_workflow_svg if mutation_workflow_svg else '<div class="empty">No radio mutation workflow data recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Radio Mutation Windows</h3>
<div class="chart-wrap">
{mutation_window_svg if mutation_window_svg else '<div class="empty">No active mutation-window telemetry recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Post-Action Verification</h3>
<div class="chart-wrap">
{post_action_verification_svg if post_action_verification_svg else '<div class="empty">No post-action verification events recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Lock Confirmation Reasons</h3>
<div class="chart-wrap">
{lock_confirmation_svg if lock_confirmation_svg else '<div class="empty">No lock confirmation reasons recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Modem Read Misses</h3>
<div class="chart-wrap">
{read_miss_svg if read_miss_svg else '<div class="empty">No modem read misses recorded.</div>'}
</div>
<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Bouncer / Referee Outcomes</h3>
<div class="chart-wrap">
{bouncer_outcome_svg if bouncer_outcome_svg else '<div class="empty">No bouncer outcome codes recorded.</div>'}
</div>
<div class="chart-wrap">
{referee_outcome_svg if referee_outcome_svg else '<div class="empty">No referee outcome codes recorded.</div>'}
</div>

<!-- ══ Incident Log ══ -->
<h2>10. Incident Log</h2>
{f'''<table>
<tr><th>Time</th><th>State</th><th>Duration</th><th>Reason</th></tr>
{incident_rows}
</table>''' if incident_rows else '<div class="empty">No incidents recorded in this period.</div>'}

<!-- ══ Actions ══ -->
<h2>11. Actions</h2>
{f'''<h3 style="font-size:0.95em;margin:8px 0;">Summary</h3>
<table>
<tr><th>Lever</th><th>Count</th></tr>
{action_summary_rows}
</table>''' if action_summary_rows else ''}

{f'''<h3 style="font-size:0.95em;margin:12px 0 8px 0;">Recent (last 20)</h3>
<table>
<tr><th>Time</th><th>Lever</th><th>Reason</th><th>OK</th></tr>
{recent_actions_rows}
</table>''' if recent_actions_rows else '<div class="empty">No actions recorded in this period.</div>'}

<div class="footer">
  54GUC Site Report · Generated by 54GUC · {_svg_escape(gen_time)}
</div>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════
#  5. Public API
# ═══════════════════════════════════════════════════════════════

def enforce_site_report_retention(
    output_dir: str | Path,
    *,
    now: float | None = None,
    retention_days: int = REPORT_RETENTION_DAYS,
) -> dict[str, Any]:
    root = Path(output_dir)
    cutoff = float(time.time() if now is None else now) - max(1, int(retention_days)) * 86400.0
    removed: list[str] = []
    for path in sorted(root.glob("site_report_*.html")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(str(path))
        except OSError:
            continue
    return {
        "schema": "SiteReportRetention.v1",
        "owner": "arch4.intelligence.historical_site_report",
        "retention_days": max(1, int(retention_days)),
        "removed_count": len(removed),
        "removed_paths": removed,
    }

def generate_historical_site_report(
    output_dir: str | Path,
    days: int = REPORT_WINDOW_DAYS,
    open_browser: bool = True,
    *,
    hours: Optional[float] = None,
) -> Path:
    """Generate an Intelligence-history report and optionally open it.

    *hours* takes precedence over *days* when set.  Sub-day windows
    (e.g. 1, 2, 6) are supported — the day-file scanner opens enough
    daily files and a precise timestamp cutoff trims the ticks.

    Returns the generated HTML path.  Failures are raised to the typed job,
    which writes the durable operator-visible failure receipt.
    """
    out_path = Path(output_dir)
    if not out_path.is_dir():
        raise RuntimeError("site_report_runtime_dir_missing")

    enforce_site_report_retention(out_path)

    # Resolve the window — hours wins over days.
    if hours is not None and hours > 0:
        window_hours = float(hours)
    else:
        window_hours = float(days) * 24.0

    # How many calendar days of files to scan (at least 1).
    scan_days = max(1, math.ceil(window_hours / 24.0))
    cutoff_ts = time.time() - window_hours * 3600.0

    log.info("Generating site report from %s (last %.1f hours, scanning %d day-files)...",
             out_path, window_hours, scan_days)

    ticks = load_telemetry(out_path, scan_days, cutoff_ts=cutoff_ts)
    events = load_events(out_path, scan_days, cutoff_ts=cutoff_ts)

    if not ticks:
        raise RuntimeError("site_report_telemetry_missing")

    agg = aggregate(ticks, events)
    html = build_html(agg)

    report_name = f"site_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
    report_path = out_path / report_name

    try:
        atomic_write_text(report_path, html, encoding="utf-8", logger=log)
        log.info("Site report written to %s", report_path)
    except OSError as exc:
        raise RuntimeError(f"site_report_write_failed:{exc}") from exc

    if open_browser:
        try:
            webbrowser.open(report_path.as_uri())
        except Exception as exc:
            log.warning("Site report: could not open browser: %s", exc)

    return report_path
