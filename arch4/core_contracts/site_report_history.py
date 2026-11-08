"""Neutral immutable optional-history contract for the Foundation Site Report."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade

from dataclasses import dataclass
import math


SITE_REPORT_HISTORY_SCHEMA = "SiteReportHistoryProjection.v3"
SITE_REPORT_HISTORY_MAX_HOURS = 336.0
SITE_REPORT_HISTORY_MAX_POINTS = 168
SITE_REPORT_HISTORY_MAX_CONTEXTS = 128
SITE_REPORT_HISTORY_MAX_CHANGES = 128


def _finite_optional(value: float | None, reason: str) -> None:
    if value is not None and (
        type(value) not in {int, float} or not math.isfinite(float(value))
    ):
        raise ValueError(reason)


def _optional_int(value: int | None, reason: str, *, zero: bool = False) -> None:
    if value is None:
        return
    minimum = 0 if zero else 1
    if type(value) is not int or value < minimum:
        raise ValueError(reason)


@dataclass(frozen=True, slots=True)
class SiteReportHistoryPoint:
    """One bounded historical display bucket from completed proof evidence."""

    started_at: float
    ended_at: float
    sample_count: int
    network_type: str
    serving_band: int | None
    ca_pool: tuple[int, ...]
    cell_id: int | None
    pci: int | None
    earfcn: int | None
    nr_band: int | None
    nr_cell_id: int | None
    nr_pci: int | None
    nr_arfcn: int | None
    capacity_points: float | None
    latency_ms: float | None
    jitter_ms: float | None
    communication_quality: float | None
    congestion_pct: float | None
    internet_availability_pct: float | None
    service_grade: float | None

    def __post_init__(self) -> None:
        if (
            type(self.started_at) not in {int, float}
            or type(self.ended_at) not in {int, float}
            or not math.isfinite(float(self.started_at))
            or not math.isfinite(float(self.ended_at))
            or self.started_at <= 0.0
            or self.ended_at < self.started_at
        ):
            raise ValueError("site_report_history_point_time_invalid")
        if type(self.sample_count) is not int or self.sample_count <= 0:
            raise ValueError("site_report_history_point_sample_count_invalid")
        if type(self.network_type) is not str:
            raise TypeError("site_report_history_point_network_type_invalid")
        for name in ("serving_band", "cell_id", "nr_band", "nr_cell_id"):
            _optional_int(
                getattr(self, name),
                "site_report_history_point_identity_invalid:" + name,
            )
        for name in ("pci", "earfcn", "nr_pci", "nr_arfcn"):
            _optional_int(
                getattr(self, name),
                "site_report_history_point_identity_invalid:" + name,
                zero=True,
            )
        if (
            type(self.ca_pool) is not tuple
            or any(type(item) is not int or item <= 0 for item in self.ca_pool)
            or tuple(sorted(set(self.ca_pool))) != self.ca_pool
        ):
            raise ValueError("site_report_history_point_ca_pool_invalid")
        for name in (
            "capacity_points",
            "latency_ms",
            "jitter_ms",
            "communication_quality",
            "congestion_pct",
            "internet_availability_pct",
        ):
            _finite_optional(
                getattr(self, name),
                "site_report_history_point_metric_invalid:" + name,
            )
        if self.communication_quality is not None and not (
            0.0 <= float(self.communication_quality) <= 1.0
        ):
            raise ValueError("site_report_history_point_cq_range_invalid")
        for name in ("congestion_pct", "internet_availability_pct"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= float(value) <= 100.0:
                raise ValueError("site_report_history_point_pct_range_invalid:" + name)
        if self.service_grade is not None and (
            isinstance(self.service_grade, bool)
            or type(self.service_grade) not in {int, float}
            or not math.isfinite(float(self.service_grade))
            or not valid_intelligence_grade(self.service_grade,dimension="service_outcome")
        ):
            raise ValueError("site_report_history_point_grade_invalid")


@dataclass(frozen=True, slots=True)
class SiteReportHistoryChange:
    """One completed radio-change assessment for customer explanation."""

    observed_at: float
    actor: str
    before: str
    after: str
    outcome: str
    service_grade: float | None

    def __post_init__(self) -> None:
        if (
            type(self.observed_at) not in {int, float}
            or not math.isfinite(float(self.observed_at))
            or self.observed_at <= 0.0
        ):
            raise ValueError("site_report_history_change_time_invalid")
        for name in ("actor", "before", "after", "outcome"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or value != value.strip():
                raise ValueError("site_report_history_change_text_invalid:" + name)
        if self.service_grade is not None and (
            isinstance(self.service_grade, bool)
            or type(self.service_grade) not in {int, float}
            or not math.isfinite(float(self.service_grade))
            or not valid_intelligence_grade(self.service_grade, dimension="service_change")
        ):
            raise ValueError("site_report_history_change_grade_invalid")


@dataclass(frozen=True, slots=True)
class SiteReportIntelligenceImpact:
    """Neutral customer facts already reduced by the Intelligence owner."""

    improved: int
    neutral: int
    worse: int
    not_proven: int
    forecast_correct: int
    forecast_assessed: int
    forecast_accuracy_pct: float | None
    latest_kind: str
    latest_completed_at: float
    latest_before: str
    latest_after: str
    latest_capacity_before: float | None
    latest_capacity_after: float | None
    latest_latency_before: float | None
    latest_latency_after: float | None
    latest_jitter_before: float | None
    latest_jitter_after: float | None
    latest_cq_before: float | None
    latest_cq_after: float | None
    latest_service_impact: float | None
    positive_contexts: tuple[str, ...]
    negative_contexts: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("improved", "neutral", "worse", "not_proven"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("site_report_impact_count_invalid:" + name)
        if (
            type(self.forecast_correct) is not int
            or type(self.forecast_assessed) is not int
            or not 0 <= self.forecast_correct <= self.forecast_assessed
        ):
            raise ValueError("site_report_impact_forecast_count_invalid")
        if (self.forecast_assessed == 0) != (self.forecast_accuracy_pct is None):
            raise ValueError("site_report_impact_forecast_state_invalid")
        if self.forecast_accuracy_pct is not None and not (
            type(self.forecast_accuracy_pct) in {int, float}
            and math.isfinite(float(self.forecast_accuracy_pct))
            and 0.0 <= float(self.forecast_accuracy_pct) <= 100.0
        ):
            raise ValueError("site_report_impact_forecast_accuracy_invalid")
        latest = bool(self.latest_kind)
        if latest != bool(self.latest_completed_at):
            raise ValueError("site_report_impact_latest_state_invalid")
        if latest:
            if self.latest_kind not in {"brain_action", "validated_hold"}:
                raise ValueError("site_report_impact_latest_kind_invalid")
            if not self.latest_before or not self.latest_after:
                raise ValueError("site_report_impact_latest_context_invalid")
        elif self.latest_before or self.latest_after or any(
            getattr(self, name) is not None
            for name in (
                "latest_capacity_before",
                "latest_capacity_after",
                "latest_latency_before",
                "latest_latency_after",
                "latest_jitter_before",
                "latest_jitter_after",
                "latest_cq_before",
                "latest_cq_after",
                "latest_service_impact",
            )
        ):
            raise ValueError("site_report_impact_empty_latest_invalid")
        for name in (
            "latest_capacity_before",
            "latest_capacity_after",
            "latest_latency_before",
            "latest_latency_after",
            "latest_jitter_before",
            "latest_jitter_after",
            "latest_cq_before",
            "latest_cq_after",
            "latest_service_impact",
        ):
            _finite_optional(
                getattr(self, name),
                "site_report_impact_metric_invalid:" + name,
            )
        if self.latest_service_impact is not None and not valid_intelligence_grade(
            self.latest_service_impact, dimension="service_change"
        ):
            raise ValueError("site_report_impact_grade_invalid")
        for name in ("positive_contexts", "negative_contexts"):
            rows = getattr(self, name)
            if (
                type(rows) is not tuple
                or len(rows) > 3
                or any(type(row) is not str or not row.strip() for row in rows)
            ):
                raise ValueError("site_report_impact_contexts_invalid:" + name)

    @classmethod
    def empty(cls) -> "SiteReportIntelligenceImpact":
        return cls(
            improved=0,
            neutral=0,
            worse=0,
            not_proven=0,
            forecast_correct=0,
            forecast_assessed=0,
            forecast_accuracy_pct=None,
            latest_kind="",
            latest_completed_at=0.0,
            latest_before="",
            latest_after="",
            latest_capacity_before=None,
            latest_capacity_after=None,
            latest_latency_before=None,
            latest_latency_after=None,
            latest_jitter_before=None,
            latest_jitter_after=None,
            latest_cq_before=None,
            latest_cq_after=None,
            latest_service_impact=None,
            positive_contexts=(),
            negative_contexts=(),
        )


@dataclass(frozen=True, slots=True)
class SiteReportHistoryProjection:
    """Frozen historical truth supplied to the one Foundation report owner."""

    schema: str
    status: str
    reason: str
    requested_hours: float
    generated_at: float
    continuity_key: str
    source_started_at: float
    source_ended_at: float
    points: tuple[SiteReportHistoryPoint, ...]
    changes: tuple[SiteReportHistoryChange, ...]
    intelligence_impact: SiteReportIntelligenceImpact

    def __post_init__(self) -> None:
        if self.schema != SITE_REPORT_HISTORY_SCHEMA:
            raise ValueError("site_report_history_schema_invalid")
        if self.status not in {"available", "collecting", "unavailable"}:
            raise ValueError("site_report_history_status_invalid")
        if type(self.reason) is not str or type(self.continuity_key) is not str:
            raise TypeError("site_report_history_text_invalid")
        if (
            type(self.requested_hours) not in {int, float}
            or not math.isfinite(float(self.requested_hours))
            or not 0.0 < float(self.requested_hours) <= SITE_REPORT_HISTORY_MAX_HOURS
        ):
            raise ValueError("site_report_history_window_invalid")
        if (
            type(self.generated_at) not in {int, float}
            or not math.isfinite(float(self.generated_at))
            or self.generated_at <= 0.0
        ):
            raise ValueError("site_report_history_generated_at_invalid")
        if len(self.points) > SITE_REPORT_HISTORY_MAX_POINTS:
            raise ValueError("site_report_history_point_limit_exceeded")
        if len(self.changes) > SITE_REPORT_HISTORY_MAX_CHANGES:
            raise ValueError("site_report_history_change_limit_exceeded")
        if not isinstance(
            self.intelligence_impact,
            SiteReportIntelligenceImpact,
        ):
            raise TypeError("site_report_intelligence_impact_required")
        cutoff = self.generated_at - self.requested_hours * 3600.0
        if any(
            point.started_at < cutoff
            or point.ended_at > self.generated_at
            for point in self.points
        ):
            raise ValueError("site_report_history_point_outside_window")
        if any(
            change.observed_at < cutoff or change.observed_at > self.generated_at
            for change in self.changes
        ):
            raise ValueError("site_report_history_change_outside_window")
        if self.status == "available":
            if not self.points or not self.continuity_key:
                raise ValueError("site_report_history_available_evidence_required")
            if self.source_started_at != self.points[0].started_at:
                raise ValueError("site_report_history_start_mismatch")
            if self.source_ended_at != self.points[-1].ended_at:
                raise ValueError("site_report_history_end_mismatch")
        elif (
            self.points
            or self.changes
            or self.continuity_key
            or self.source_started_at != 0.0
            or self.source_ended_at != 0.0
            or not self.reason
        ):
            raise ValueError("site_report_history_unavailable_payload_invalid")

    @classmethod
    def unavailable(
        cls,
        *,
        requested_hours: float,
        generated_at: float,
        reason: str,
        collecting: bool = False,
    ) -> "SiteReportHistoryProjection":
        return cls(
            schema=SITE_REPORT_HISTORY_SCHEMA,
            status="collecting" if collecting else "unavailable",
            reason=str(reason or "history_unavailable").strip(),
            requested_hours=float(requested_hours),
            generated_at=float(generated_at),
            continuity_key="",
            source_started_at=0.0,
            source_ended_at=0.0,
            points=(),
            changes=(),
            intelligence_impact=SiteReportIntelligenceImpact.empty(),
        )


__all__ = (
    "SITE_REPORT_HISTORY_MAX_CHANGES",
    "SITE_REPORT_HISTORY_MAX_HOURS",
    "SITE_REPORT_HISTORY_MAX_POINTS",
    "SITE_REPORT_HISTORY_SCHEMA",
    "SiteReportHistoryChange",
    "SiteReportHistoryPoint",
    "SiteReportIntelligenceImpact",
    "SiteReportHistoryProjection",
)
