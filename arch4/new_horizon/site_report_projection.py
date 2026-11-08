"""Build New Horizon history data for the Foundation Site Report."""

from __future__ import annotations
from arch4.intelligence_presentation_contract import saved_radio_label

from collections import defaultdict
import math
from pathlib import Path
from time import time
from typing import Any, Mapping

from arch4.core_contracts.site_report_history import (
    SITE_REPORT_HISTORY_MAX_CHANGES,
    SITE_REPORT_HISTORY_MAX_POINTS,
    SITE_REPORT_HISTORY_SCHEMA,
    SiteReportHistoryChange,
    SiteReportIntelligenceImpact,
    SiteReportHistoryPoint,
    SiteReportHistoryProjection,
)
from arch4.new_horizon.history import export_history_records, export_history_checkpoints
from arch4.new_horizon.hourly_aq import require_aq_hour, hourly_observed_vector
from arch4.new_horizon.decision_history import (
    DecisionLifecycleHistoryOwner,
    DECISION_HISTORY_KINDS,
)
from arch4.new_horizon.intelligence_impact import (
    INTELLIGENCE_IMPACT_WINDOW_S,
    build_intelligence_impact,
    project_change_grade,
)
from arch4.new_horizon.proof import (
    READABLE_REFEREE_PROOF_KINDS,
    RefereeAssessmentHistoryOwner,
    referee_history_vector,
    AQ_HOURLY_CHECKPOINT, AQ_HOURLY_CHECKPOINT_SCHEMA,
)
from arch4.intelligence_presentation_contract import (
    project_intelligence_display_grade,
)


_MAX_PROOF_ROWS = 65536
_ACTOR_LABELS = {
    "operator_ngui": "User",
    "operator_sms": "User",
    "smart_loop": "Intelligence",
    "brain": "Intelligence",
    "explore": "Intelligence Explore",
    "lower_controller": "Modem",
    "modem_live": "Modem",
    "schedule": "Schedule",
    "watchdog": "Watchdog",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any, *, zero: bool = False) -> int | None:
    minimum = 0 if zero else 1
    return value if type(value) is int and value >= minimum else None


def _pool(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({item for item in value if type(item) is int and item > 0}))


def _weighted_average(
    rows: list[Mapping[str, Any]],
    value_name: str,
    count_name: str,
) -> float | None:
    weighted = 0.0
    weight = 0.0
    for row in rows:
        value = _number(row.get(value_name))
        count = _number(row.get(count_name))
        if value is None:
            continue
        current_weight = count if count is not None and count > 0.0 else 1.0
        weighted += value * current_weight
        weight += current_weight
    return weighted / weight if weight > 0.0 else None


def _proof_payloads(path: Path, *, since: float) -> tuple[dict[str, Any], ...]:
    records = tuple(row for kind in READABLE_REFEREE_PROOF_KINDS
                    for row in export_history_records(path,kind=kind,since=since,limit=_MAX_PROOF_ROWS))
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        scope_key = str(row.get("scope_key") or "")
        if scope_key:
            grouped[scope_key].append(row)
    accepted: list[dict[str, Any]] = []
    hourly_scopes = set()
    for record in export_history_checkpoints(path, name=AQ_HOURLY_CHECKPOINT):
        payload = record["payload"]
        if payload.get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA:
            raise ValueError("site_report_hourly_aq_schema_invalid")
        scope = record["scope_key"]
        hourly_scopes.add(scope)
        for raw in payload["hours"]:
            hour = require_aq_hour(raw)
            if hour["continuity_key"] != scope:
                raise ValueError("site_report_hourly_aq_scope_invalid")
            if hour["last_at"] < since:
                continue
            accepted.append({
                "assessment_id": "aq-hour:" + hour["hour_id"],
                "assessment_mode": "context_quality", "dimension": "service_outcome",
                "final_assessment": True, "status": "graded", "grade": hour["aq"],
                "opened_at": hour["first_at"], "closed_at": hour["last_at"],
                "continuity_key": scope, "_record_scope": scope,
                "result_identity": hour["identity"], "observed_vector": hourly_observed_vector(hour),
                "coverage_s": hour["coverage_s"],
            })
    for scope_key, rows in grouped.items():
        snapshot = RefereeAssessmentHistoryOwner.snapshot_from_records(
            scope_key,
            rows,
        )
        for assessment in snapshot.assessments:
            for payload in assessment.payloads:
                if scope_key in hourly_scopes and payload["subject_kind"] == "active_context_window":
                    continue
                current = dict(payload)
                current["_record_scope"] = scope_key
                accepted.append(current)
    return tuple(accepted)


def _impact_metric(vector: object, name: str) -> float | None:
    return _number(vector.get(name)) if isinstance(vector, Mapping) else None


def _impact_projection(
    path: Path,
    *,
    continuity_key: str,
    generated_at: float,
) -> SiteReportIntelligenceImpact:
    since = generated_at - INTELLIGENCE_IMPACT_WINDOW_S - 720.0
    proof_snapshot = RefereeAssessmentHistoryOwner.snapshot_from_records(
        continuity_key,
        (record for kind in READABLE_REFEREE_PROOF_KINDS for record in export_history_records(
            path,
            kind=kind,
            scope_key=continuity_key,
            since=since,
            limit=_MAX_PROOF_ROWS,
        )),
    )
    decision_snapshot = DecisionLifecycleHistoryOwner.snapshot_from_records(
        continuity_key,
        (
            record
            for kind in DECISION_HISTORY_KINDS
            for record in export_history_records(
                path,
                kind=kind,
                scope_key=continuity_key,
                since=since,
                limit=8192,
            )
        ),
    )
    impact = build_intelligence_impact(
        continuity_key=continuity_key,
        proof_records=proof_snapshot.records,
        decision_history=decision_snapshot,
        now=generated_at,
    )
    counts = impact["counts"]
    forecast = impact["forecast_direction"]
    recent_outcomes = impact["recent_outcomes"]
    latest = recent_outcomes[-1] if recent_outcomes else None
    baseline = latest["baseline_vector"] if latest is not None else {}
    observed = latest["observed_vector"] if latest is not None else {}
    return SiteReportIntelligenceImpact(
        improved=int(counts["improved"]),
        neutral=int(counts["neutral"]),
        worse=int(counts["worse"]),
        not_proven=int(counts["not_proven"]),
        forecast_correct=int(forecast["correct"]),
        forecast_assessed=int(forecast["assessed"]),
        forecast_accuracy_pct=forecast["accuracy_pct"],
        latest_kind=str(latest["attribution"]) if latest is not None else "",
        latest_completed_at=(
            float(latest["completed_at"]) if latest is not None else 0.0
        ),
        latest_before=saved_radio_label(latest["before_text"], latest["before_nr"]) if latest is not None else "",
        latest_after=saved_radio_label(latest["after_text"], latest["after_nr"]) if latest is not None else "",
        latest_capacity_before=_impact_metric(baseline, "eci_points"),
        latest_capacity_after=_impact_metric(observed, "eci_points"),
        latest_latency_before=_impact_metric(baseline, "latency_ms"),
        latest_latency_after=_impact_metric(observed, "latency_ms"),
        latest_jitter_before=_impact_metric(baseline, "jitter_ms"),
        latest_jitter_after=_impact_metric(observed, "jitter_ms"),
        latest_cq_before=_impact_metric(baseline, "cqb_cell"),
        latest_cq_after=_impact_metric(observed, "cqb_cell"),
        latest_service_impact=(
            float(latest["impact_grade"]) if latest is not None else None
        ),
        positive_contexts=tuple(
            saved_radio_label(row["context"], row["nr"]) for row in impact["positive_contexts"]
        ),
        negative_contexts=tuple(
            saved_radio_label(row["context"], row["nr"]) for row in impact["negative_contexts"]
        ),
    )


def _completed_service_rows(
    rows: tuple[dict[str, Any], ...],
    *,
    assessment_mode: str,
    cutoff: float,
    generated_at: float,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for payload in rows:
        closed_at = _number(payload.get("closed_at"))
        if (
            closed_at is None
            or not cutoff <= closed_at <= generated_at
            or payload.get("assessment_mode") != assessment_mode
            or payload.get("dimension") != "service_outcome"
            or payload.get("final_assessment") is not True
            or payload.get("status") not in {"graded", "invalid"}
        ):
            continue
        assessment_id = str(payload.get("assessment_id") or "")
        if assessment_id:
            selected[assessment_id] = payload
    return sorted(
        selected.values(),
        key=lambda item: float(item["closed_at"]),
    )


def _context_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "not retained"
    identity = dict(value)
    network_type = str(identity.get("network_type") or "").strip()
    if network_type == "NR SA (5G)":
        band = _integer(identity.get("nr_band"))
        cell = _integer(identity.get("nr_cell_id"))
        pci = _integer(identity.get("nr_pci"), zero=True)
        if band is None:
            return "not retained"
        return (
            f"n{band} · Cell {cell or '---'} · PCI {pci if pci is not None else '---'}"
        )
    pband = _integer(identity.get("pband"))
    pool = _pool(identity.get("ca_pool"))
    cell = _integer(identity.get("cell_id"))
    pci = _integer(identity.get("pci"), zero=True)
    if pband is None:
        return "not retained"
    pool_text = "+".join(f"B{band}" for band in pool) or "---"
    return (
        f"B{pband} / {pool_text} · Cell {cell or '---'} · "
        f"PCI {pci if pci is not None else '---'}"
    )


def _points(
    rows: list[dict[str, Any]],
    *,
    cutoff: float,
    generated_at: float,
    requested_hours: float,
) -> tuple[SiteReportHistoryPoint, ...]:
    if not rows:
        return ()
    bucket_seconds = max(
        600.0,
        requested_hours * 3600.0 / SITE_REPORT_HISTORY_MAX_POINTS,
    )
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        closed_at = float(row["closed_at"])
        index = min(
            SITE_REPORT_HISTORY_MAX_POINTS - 1,
            max(0, int((closed_at - cutoff) / bucket_seconds)),
        )
        buckets[index].append(row)
    points: list[SiteReportHistoryPoint] = []
    for index in sorted(buckets):
        bucket = buckets[index]
        latest = bucket[-1]
        identity = dict(latest.get("result_identity") or {})
        vectors = [
            dict(row.get("observed_vector") or {})
            for row in bucket
            if isinstance(row.get("observed_vector"), Mapping)
        ]
        opened_times = [
            value
            for row in bucket
            if (value := _number(row.get("opened_at"))) is not None
        ]
        closed_times = [float(row["closed_at"]) for row in bucket]
        grades = [
            float(row["grade"])
            for row in bucket
            if type(row.get("grade")) in {int, float}
            and not isinstance(row.get("grade"), bool)
        ]
        points.append(
            SiteReportHistoryPoint(
                started_at=max(cutoff, min(opened_times, default=min(closed_times))),
                ended_at=min(generated_at, max(closed_times)),
                sample_count=max(
                    1,
                    sum(
                        max(1, int(_number(vector.get("sample_count")) or 1))
                        for vector in vectors
                    ),
                ),
                network_type=str(identity.get("network_type") or ""),
                serving_band=_integer(identity.get("pband")),
                ca_pool=_pool(identity.get("ca_pool")),
                cell_id=_integer(identity.get("cell_id")),
                pci=_integer(identity.get("pci"), zero=True),
                earfcn=_integer(identity.get("earfcn"), zero=True),
                nr_band=_integer(identity.get("nr_band")),
                nr_cell_id=_integer(identity.get("nr_cell_id")),
                nr_pci=_integer(identity.get("nr_pci"), zero=True),
                nr_arfcn=_integer(identity.get("nr_arfcn"), zero=True),
                capacity_points=_weighted_average(
                    vectors, "eci_points", "sample_count"
                ),
                latency_ms=_weighted_average(
                    vectors, "latency_ms", "latency_rtt_sample_count"
                ),
                jitter_ms=_weighted_average(
                    vectors, "jitter_ms", "jitter_sample_count"
                ),
                communication_quality=_weighted_average(
                    vectors, "cqb_cell", "sample_count"
                ),
                congestion_pct=_weighted_average(
                    vectors, "congestion_pct", "sample_count"
                ),
                internet_availability_pct=_weighted_average(
                    vectors,
                    "internet_availability_pct",
                    "internet_probe_count",
                ),
                service_grade=(round(sum(grades) / len(grades), 2) if grades else None),
            )
        )
    return tuple(points[-SITE_REPORT_HISTORY_MAX_POINTS:])


def _changes(
    rows: list[dict[str, Any]],
) -> tuple[SiteReportHistoryChange, ...]:
    projected: list[SiteReportHistoryChange] = []
    for row in rows[-SITE_REPORT_HISTORY_MAX_CHANGES:]:
        grade = project_change_grade(
            referee_history_vector(row, "baseline_vector"), referee_history_vector(row, "observed_vector"),
            completed_transition=(row.get("assessment_mode") == "transition"
                                  and row.get("status") == "graded" and row.get("final_assessment") is True),
        )
        displayed_grade = project_intelligence_display_grade(grade, dimension="service_change")
        if displayed_grade is None:
            outcome = "Not graded"
        elif displayed_grade > 0:
            outcome = "Improved"
        elif displayed_grade < 0:
            outcome = "Worsened"
        else:
            outcome = "No material change"
        origin = str(row.get("origin") or "unknown").strip().lower()
        actor = _ACTOR_LABELS.get(
            origin,
            origin.replace("_", " ").strip().title(),
        )
        projected.append(
            SiteReportHistoryChange(
                observed_at=float(row["closed_at"]),
                actor=actor or "Unknown",
                before=_context_text(row.get("baseline_identity")),
                after=_context_text(row.get("result_identity")),
                outcome=outcome,
                service_grade=grade,
            )
        )
    return tuple(projected)


def build_site_report_history_projection(
    history_path: str | Path,
    *,
    hours: float,
    now: float | None = None,
) -> SiteReportHistoryProjection:
    """Return a bounded read-only view of accepted proof history."""

    generated_at = float(time() if now is None else now)
    requested_hours = float(hours)
    source = Path(history_path).expanduser().resolve()
    if not source.is_file():
        return SiteReportHistoryProjection.unavailable(
            requested_hours=requested_hours,
            generated_at=generated_at,
            reason="intelligence_history_not_found",
        )
    cutoff = generated_at - requested_hours * 3600.0
    records = _proof_payloads(source, since=cutoff)
    context_rows = _completed_service_rows(
        records,
        assessment_mode="context_quality",
        cutoff=cutoff,
        generated_at=generated_at,
    )
    if not context_rows:
        return SiteReportHistoryProjection.unavailable(
            requested_hours=requested_hours,
            generated_at=generated_at,
            reason="completed_history_is_still_collecting",
            collecting=True,
        )
    latest = context_rows[-1]
    continuity_key = str(
        latest.get("continuity_key") or latest.get("_record_scope") or ""
    )
    context_rows = [
        row
        for row in context_rows
        if str(row.get("continuity_key") or row.get("_record_scope") or "")
        == continuity_key
    ]
    points = _points(
        context_rows,
        cutoff=cutoff,
        generated_at=generated_at,
        requested_hours=requested_hours,
    )
    transition_rows = _completed_service_rows(
        records,
        assessment_mode="transition",
        cutoff=cutoff,
        generated_at=generated_at,
    )
    transition_rows = [
        row
        for row in transition_rows
        if str(row.get("continuity_key") or row.get("_record_scope") or "")
        == continuity_key
    ]
    return SiteReportHistoryProjection(
        schema=SITE_REPORT_HISTORY_SCHEMA,
        status="available",
        reason="",
        requested_hours=requested_hours,
        generated_at=generated_at,
        continuity_key=continuity_key,
        source_started_at=points[0].started_at,
        source_ended_at=points[-1].ended_at,
        points=points,
        changes=_changes(transition_rows),
        intelligence_impact=_impact_projection(
            source,
            continuity_key=continuity_key,
            generated_at=generated_at,
        ),
    )


__all__ = ("build_site_report_history_projection",)
