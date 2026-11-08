"""Neutral contracts for optional Intelligence historical evidence."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import math
from typing import Any, Protocol, runtime_checkable

from arch4.core_contracts.payload import CoreFrozenPayload


HISTORICAL_EVIDENCE_REQUEST_SCHEMA = "HistoricalEvidenceImportRequest.v1"
HISTORICAL_EVIDENCE_STATUS_SCHEMA = "HistoricalEvidenceStatus.v1"
HISTORICAL_EVIDENCE_GENERATION_SCHEMA = "HistoricalEvidenceGeneration.v1"
HISTORICAL_EVIDENCE_CHART_REQUEST_SCHEMA = "HistoricalEvidenceChartRequest.v1"
HISTORICAL_EVIDENCE_CHART_RESULT_SCHEMA = "HistoricalEvidenceChartResult.v1"
HISTORICAL_EVIDENCE_CONTRACT_OWNER = "54guc.intelligence.historical_evidence"
MAX_HISTORICAL_EVIDENCE_CHART_CHUNK_POINTS = 500
MAX_HISTORICAL_EVIDENCE_CHART_POINTS = 3_600


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceImportRequest:
    request_id: str
    created_at: float
    confirmed_login_scope: CoreFrozenPayload
    confirmed_handoff_receipt: CoreFrozenPayload
    preferred_sources: tuple[str, ...] = ()
    force: bool = False
    max_age_days: int = 14
    min_telemetry_rows: int = 100
    schema: str = HISTORICAL_EVIDENCE_REQUEST_SCHEMA
    owner: str = HISTORICAL_EVIDENCE_CONTRACT_OWNER

    def __post_init__(self) -> None:
        if self.schema != HISTORICAL_EVIDENCE_REQUEST_SCHEMA:
            raise ValueError("historical_evidence_request_schema_invalid")
        if self.owner != HISTORICAL_EVIDENCE_CONTRACT_OWNER:
            raise ValueError("historical_evidence_request_owner_invalid")
        request_id = str(self.request_id or "").strip()
        if not request_id:
            raise ValueError("historical_evidence_request_id_required")
        created_at = float(self.created_at)
        if not math.isfinite(created_at) or created_at <= 0.0:
            raise ValueError("historical_evidence_request_timestamp_invalid")
        if not isinstance(self.confirmed_login_scope, CoreFrozenPayload):
            raise TypeError("historical_evidence_request_login_scope_required")
        if not isinstance(self.confirmed_handoff_receipt, CoreFrozenPayload):
            raise TypeError("historical_evidence_request_handoff_receipt_required")
        sources = tuple(
            str(source or "").strip()
            for source in self.preferred_sources
            if str(source or "").strip()
        )
        if len(set(sources)) != len(sources):
            raise ValueError("historical_evidence_request_source_duplicate")
        if type(self.force) is not bool:
            raise TypeError("historical_evidence_request_force_bool_required")
        max_age_days = int(self.max_age_days)
        min_rows = int(self.min_telemetry_rows)
        if not 1 <= max_age_days <= 14:
            raise ValueError("historical_evidence_request_age_window_invalid")
        if min_rows <= 0:
            raise ValueError("historical_evidence_request_min_rows_invalid")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "preferred_sources", sources)
        object.__setattr__(self, "max_age_days", max_age_days)
        object.__setattr__(self, "min_telemetry_rows", min_rows)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "confirmed_login_scope": self.confirmed_login_scope.to_payload(),
            "confirmed_handoff_receipt": (
                self.confirmed_handoff_receipt.to_payload()
            ),
            "preferred_sources": list(self.preferred_sources),
            "force": self.force,
            "max_age_days": self.max_age_days,
            "min_telemetry_rows": self.min_telemetry_rows,
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceStatus:
    sequence: int
    published_at: float
    status: str
    phase: str
    progress_pct: float
    reason: str
    transaction_id: str
    attempt: int
    detail: CoreFrozenPayload
    schema: str = HISTORICAL_EVIDENCE_STATUS_SCHEMA
    owner: str = HISTORICAL_EVIDENCE_CONTRACT_OWNER

    def __post_init__(self) -> None:
        if self.schema != HISTORICAL_EVIDENCE_STATUS_SCHEMA:
            raise ValueError("historical_evidence_status_schema_invalid")
        if self.owner != HISTORICAL_EVIDENCE_CONTRACT_OWNER:
            raise ValueError("historical_evidence_status_owner_invalid")
        if int(self.sequence) <= 0 or float(self.published_at) <= 0.0:
            raise ValueError("historical_evidence_status_cursor_invalid")
        status = str(self.status or "").strip()
        phase = str(self.phase or "").strip()
        if not status or not phase:
            raise ValueError("historical_evidence_status_state_required")
        progress = float(self.progress_pct)
        if not math.isfinite(progress) or not 0.0 <= progress <= 100.0:
            raise ValueError("historical_evidence_status_progress_invalid")
        if not isinstance(self.detail, CoreFrozenPayload):
            raise TypeError("historical_evidence_status_detail_required")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "published_at", float(self.published_at))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "progress_pct", progress)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "transaction_id", str(self.transaction_id or ""))
        object.__setattr__(self, "attempt", max(0, int(self.attempt)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": self.sequence,
            "published_at": self.published_at,
            "status": self.status,
            "phase": self.phase,
            "progress_pct": self.progress_pct,
            "reason": self.reason,
            "transaction_id": self.transaction_id,
            "attempt": self.attempt,
            "detail": self.detail.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceGeneration:
    transaction_id: str
    committed_at: float
    commit: CoreFrozenPayload
    prior: CoreFrozenPayload
    chart: CoreFrozenPayload
    schema: str = HISTORICAL_EVIDENCE_GENERATION_SCHEMA
    owner: str = HISTORICAL_EVIDENCE_CONTRACT_OWNER

    def __post_init__(self) -> None:
        if self.schema != HISTORICAL_EVIDENCE_GENERATION_SCHEMA:
            raise ValueError("historical_evidence_generation_schema_invalid")
        if self.owner != HISTORICAL_EVIDENCE_CONTRACT_OWNER:
            raise ValueError("historical_evidence_generation_owner_invalid")
        if not str(self.transaction_id or "").strip():
            raise ValueError("historical_evidence_generation_transaction_required")
        if float(self.committed_at) <= 0.0:
            raise ValueError("historical_evidence_generation_timestamp_invalid")
        for field_name in ("commit", "prior", "chart"):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"historical_evidence_generation_{field_name}_required"
                )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "transaction_id": self.transaction_id,
            "committed_at": float(self.committed_at),
            "commit": self.commit.to_payload(),
            "prior": self.prior.to_payload(),
            "chart": self.chart.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceChartRequest:
    request_id: str
    created_at: float
    session_scope_key: str
    history_seconds: int = 24 * 60 * 60
    max_rows: int = 50_000
    target_points: int = 1_800
    chunk_size: int = 250
    schema: str = HISTORICAL_EVIDENCE_CHART_REQUEST_SCHEMA
    owner: str = HISTORICAL_EVIDENCE_CONTRACT_OWNER

    def __post_init__(self) -> None:
        if self.schema != HISTORICAL_EVIDENCE_CHART_REQUEST_SCHEMA:
            raise ValueError("historical_evidence_chart_request_schema_invalid")
        if self.owner != HISTORICAL_EVIDENCE_CONTRACT_OWNER:
            raise ValueError("historical_evidence_chart_request_owner_invalid")
        request_id = str(self.request_id or "").strip()
        scope_key = str(self.session_scope_key or "").strip()
        created_at = float(self.created_at)
        if not request_id or not scope_key:
            raise ValueError("historical_evidence_chart_request_identity_required")
        if not math.isfinite(created_at) or created_at <= 0.0:
            raise ValueError("historical_evidence_chart_request_timestamp_invalid")
        history_seconds = int(self.history_seconds)
        max_rows = int(self.max_rows)
        target_points = int(self.target_points)
        chunk_size = int(self.chunk_size)
        if (
            history_seconds < 60
            or max_rows < 1
            or not 1 <= target_points <= MAX_HISTORICAL_EVIDENCE_CHART_POINTS
        ):
            raise ValueError("historical_evidence_chart_request_bounds_invalid")
        if not 1 <= chunk_size <= MAX_HISTORICAL_EVIDENCE_CHART_CHUNK_POINTS:
            raise ValueError("historical_evidence_chart_request_chunk_bound_invalid")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "session_scope_key", scope_key)
        object.__setattr__(self, "history_seconds", history_seconds)
        object.__setattr__(self, "max_rows", max_rows)
        object.__setattr__(self, "target_points", target_points)
        object.__setattr__(self, "chunk_size", chunk_size)


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceChartPoint:
    timestamp: float
    values: CoreFrozenPayload
    marker_values: CoreFrozenPayload
    source_session_scope_key: str
    source_file: str
    source_line: int

    def __post_init__(self) -> None:
        timestamp = float(self.timestamp)
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            raise ValueError("historical_evidence_chart_point_timestamp_invalid")
        if int(self.source_line) < 0:
            raise ValueError("historical_evidence_chart_point_source_line_invalid")
        if not isinstance(self.values, CoreFrozenPayload) or not isinstance(
            self.marker_values, CoreFrozenPayload
        ):
            raise TypeError("historical_evidence_chart_point_payload_required")
        values: dict[str, float | None] = {}
        for key, value in self.values.to_mapping().items():
            source_id = str(key or "").strip()
            if not source_id:
                raise ValueError("historical_evidence_chart_point_source_id_invalid")
            if value is None:
                values[source_id] = None
                continue
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("historical_evidence_chart_point_value_invalid")
            values[source_id] = number
        object.__setattr__(self, "timestamp", timestamp)
        markers = self.marker_values.to_mapping()
        if any(
            not str(key or "").strip() or not str(value or "").strip()
            for key, value in markers.items()
        ):
            raise ValueError("historical_evidence_chart_point_marker_invalid")
        object.__setattr__(
            self,
            "source_session_scope_key",
            str(self.source_session_scope_key or "").strip(),
        )
        object.__setattr__(self, "source_file", str(self.source_file or "").strip())
        object.__setattr__(self, "source_line", int(self.source_line))


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceChartResult:
    request_id: str
    session_scope_key: str
    chunks: tuple[tuple[HistoricalEvidenceChartPoint, ...], ...]
    scanned_files: int
    scanned_rows: int
    matched_rows: int
    thinned_rows: int
    scope_rejected_rows: int
    identity_rejected_rows: int
    invalid_rows: int
    schema: str = HISTORICAL_EVIDENCE_CHART_RESULT_SCHEMA
    owner: str = HISTORICAL_EVIDENCE_CONTRACT_OWNER

    def __post_init__(self) -> None:
        if self.schema != HISTORICAL_EVIDENCE_CHART_RESULT_SCHEMA:
            raise ValueError("historical_evidence_chart_result_schema_invalid")
        if self.owner != HISTORICAL_EVIDENCE_CONTRACT_OWNER:
            raise ValueError("historical_evidence_chart_result_owner_invalid")
        if not str(self.request_id or "").strip() or not str(
            self.session_scope_key or ""
        ).strip():
            raise ValueError("historical_evidence_chart_result_identity_required")
        chunks = tuple(tuple(chunk) for chunk in self.chunks)
        if any(
            len(chunk) > MAX_HISTORICAL_EVIDENCE_CHART_CHUNK_POINTS
            or any(not isinstance(point, HistoricalEvidenceChartPoint) for point in chunk)
            for chunk in chunks
        ):
            raise ValueError("historical_evidence_chart_result_chunk_invalid")
        if sum(len(chunk) for chunk in chunks) > MAX_HISTORICAL_EVIDENCE_CHART_POINTS:
            raise ValueError("historical_evidence_chart_result_point_bound_invalid")
        counters = (
            "scanned_files",
            "scanned_rows",
            "matched_rows",
            "thinned_rows",
            "scope_rejected_rows",
            "identity_rejected_rows",
            "invalid_rows",
        )
        if any(int(getattr(self, name)) < 0 for name in counters):
            raise ValueError("historical_evidence_chart_result_counter_invalid")
        object.__setattr__(self, "chunks", chunks)
        for name in counters:
            object.__setattr__(self, name, int(getattr(self, name)))

    @property
    def admitted_rows(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)


@runtime_checkable
class IntelligenceHistoricalChartPort(Protocol):
    """Optional chart boundary; absence means Intelligence is unattached."""

    def request_chart(
        self,
        request: HistoricalEvidenceChartRequest,
    ) -> Future[HistoricalEvidenceChartResult]: ...


__all__ = (
    "HISTORICAL_EVIDENCE_CONTRACT_OWNER",
    "HISTORICAL_EVIDENCE_CHART_REQUEST_SCHEMA",
    "HISTORICAL_EVIDENCE_CHART_RESULT_SCHEMA",
    "HISTORICAL_EVIDENCE_GENERATION_SCHEMA",
    "HISTORICAL_EVIDENCE_REQUEST_SCHEMA",
    "HISTORICAL_EVIDENCE_STATUS_SCHEMA",
    "MAX_HISTORICAL_EVIDENCE_CHART_CHUNK_POINTS",
    "MAX_HISTORICAL_EVIDENCE_CHART_POINTS",
    "HistoricalEvidenceChartPoint",
    "HistoricalEvidenceChartRequest",
    "HistoricalEvidenceChartResult",
    "HistoricalEvidenceGeneration",
    "HistoricalEvidenceImportRequest",
    "HistoricalEvidenceStatus",
    "IntelligenceHistoricalChartPort",
)
