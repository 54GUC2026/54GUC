"""Optional adapter for Intelligence chart history."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from arch4.customer_contracts.historical_evidence import (
    HISTORICAL_EVIDENCE_CONTRACT_OWNER,
    HistoricalEvidenceChartRequest,
    HistoricalEvidenceChartResult,
)

from .chart_model import (
    ChartHistorySeedTransaction,
    ChartSample,
    ChartSampleProvenance,
    ChartSampleSource,
    NGUIChartProcessor,
)
OWNER = "arch4.ngui.historical_evidence_chart_adapter"
REQUEST_SCHEMA = "ngui.historical_evidence_chart_request.v1"
RESULT_SCHEMA = "ngui.historical_evidence_chart_result.v1"
PROVENANCE = "intelligence_historical_evidence_typed_chunks"


@dataclass(frozen=True, slots=True)
class NGUIHistoricalChartRequest:
    transaction_id: str
    requested_at: float
    session_scope_key: str

    def __post_init__(self) -> None:
        if (
            not str(self.transaction_id or "").strip()
            or float(self.requested_at) <= 0.0
            or not str(self.session_scope_key or "").strip()
        ):
            raise ValueError("historical chart adapter request identity invalid")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "NGUIHistoricalChartRequest":
        if str(payload.get("schema") or "") != REQUEST_SCHEMA:
            raise ValueError("historical chart adapter request schema mismatch")
        return cls(
            transaction_id=str(payload.get("transaction_id") or "").strip(),
            requested_at=float(payload.get("requested_at") or 0.0),
            session_scope_key=str(payload.get("session_scope_key") or "").strip(),
        )

    def intelligence_request(self) -> HistoricalEvidenceChartRequest:
        return HistoricalEvidenceChartRequest(
            request_id=self.transaction_id,
            created_at=self.requested_at,
            session_scope_key=self.session_scope_key,
        )


HistoricalChartRequester = Callable[
    [HistoricalEvidenceChartRequest],
    Future[HistoricalEvidenceChartResult],
]


class HistoricalChartProgress(Protocol):
    def __call__(
        self,
        progress_pct: float,
        updated_at: float,
        message: str = "",
    ) -> None: ...


class NGUIHistoricalChartAdapter:
    """Accept typed history chunks without opening history files."""

    def __init__(
        self,
        *,
        charts: NGUIChartProcessor,
        request_chart: HistoricalChartRequester,
        timeout_s: float = 60.0,
    ) -> None:
        if not callable(request_chart):
            raise TypeError("historical_evidence_chart_requester_required")
        if float(timeout_s) <= 0.0:
            raise ValueError("historical_evidence_chart_timeout_invalid")
        self._charts = charts
        self._request_chart = request_chart
        self._timeout_s = float(timeout_s)

    def handle(
        self,
        request: NGUIHistoricalChartRequest,
        progress: HistoricalChartProgress,
    ) -> Mapping[str, object]:
        if not isinstance(request, NGUIHistoricalChartRequest):
            raise TypeError("ngui_historical_chart_request_required")
        typed = request
        progress(5.0, monotonic(), "Requesting committed historical evidence")
        result = self._request_chart(typed.intelligence_request()).result(
            timeout=self._timeout_s
        )
        if not isinstance(result, HistoricalEvidenceChartResult):
            raise TypeError("historical_evidence_typed_chart_result_required")
        if (
            result.request_id != typed.transaction_id
            or result.session_scope_key != typed.session_scope_key
        ):
            raise RuntimeError("historical_evidence_chart_result_identity_mismatch")
        admitted = 0
        chunk_count = len(result.chunks)
        for index, chunk in enumerate(result.chunks):
            samples = tuple(
                ChartSample(
                    timestamp=point.timestamp,
                    values=point.values.to_mapping(),
                    marker_values=point.marker_values.to_mapping(),
                    provenance=ChartSampleProvenance(
                        source=ChartSampleSource.PRODUCTION_TELEMETRY_HISTORY,
                        owner=HISTORICAL_EVIDENCE_CONTRACT_OWNER,
                        historical=True,
                        session_scope_key=typed.session_scope_key,
                        source_session_scope_key=(
                            point.source_session_scope_key
                            or typed.session_scope_key
                        ),
                        source_file=point.source_file,
                        source_line=point.source_line,
                    ),
                )
                for point in chunk
            )
            admitted += self._charts.admit_historical_seed(
                ChartHistorySeedTransaction(
                    transaction_id=typed.transaction_id,
                    session_scope_key=typed.session_scope_key,
                    requested_at=typed.requested_at,
                    chunk_index=index,
                    chunk_count=chunk_count,
                    samples=samples,
                )
            )
            progress(
                20.0 + (75.0 * float(index + 1) / float(chunk_count)),
                monotonic(),
                f"Admitted history chunk {index + 1}/{chunk_count}",
            )
        if not result.chunks:
            progress(95.0, monotonic(), "No matching production chart history")
        return MappingProxyType(
            {
                "schema": RESULT_SCHEMA,
                "owner": OWNER,
                "provenance": PROVENANCE,
                "historical": True,
                "learning_allowed": False,
                "live_freshness_allowed": False,
                "transaction_id": typed.transaction_id,
                "session_scope_key": typed.session_scope_key,
                "scanned_files": result.scanned_files,
                "scanned_rows": result.scanned_rows,
                "matched_rows": result.matched_rows,
                "thinned_rows": result.thinned_rows,
                "admitted_rows": admitted,
                "scope_rejected_rows": result.scope_rejected_rows,
                "identity_rejected_rows": result.identity_rejected_rows,
                "invalid_rows": result.invalid_rows,
            }
        )


def historical_chart_seed_payload(
    *,
    transaction_id: str,
    requested_at: float,
    session_scope_key: str,
    identity: Mapping[str, object],
) -> Mapping[str, object]:
    if not any(
        str(identity.get(key) or "").strip()
        for key in ("serial", "imei", "imsi")
    ):
        raise ValueError("historical chart adapter requires strong modem identity")
    typed = NGUIHistoricalChartRequest(
        transaction_id=str(transaction_id or "").strip(),
        requested_at=float(requested_at),
        session_scope_key=str(session_scope_key or "").strip(),
    )
    return MappingProxyType(
        {
            "schema": REQUEST_SCHEMA,
            "transaction_id": typed.transaction_id,
            "requested_at": typed.requested_at,
            "session_scope_key": typed.session_scope_key,
        }
    )


__all__ = (
    "NGUIHistoricalChartAdapter",
    "NGUIHistoricalChartRequest",
    "historical_chart_seed_payload",
)
