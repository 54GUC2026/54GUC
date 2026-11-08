"""Chart-history adapters and the optional, local-only chart archive."""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
import hashlib
import json
import logging
import sqlite3
import zlib
from pathlib import Path
from arch4.log_processor import MemoryFirstLogProcessor, ROUTINE_LANE

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

LOCAL_CHART_DATABASE = 'local_charts.sqlite3'
_LOCAL_CHART_RETENTION_S = 86400
_log = logging.getLogger(__name__)


def local_chart_router_key(frame) -> str:
    """Never join charts by address, model, user label or SIM alone."""
    for name in ('device.serial', 'device.imei'):
        value = frame.fields.get(name)
        if value is not None and value.state.value in {'valid', 'held'}:
            text = str(value.value or '').strip()
            if text and text not in {'---', '----', 'UNSUP.'}:
                material = frame.runtime_session_scope_key + '|' + name + '=' + text
                return hashlib.sha256(material.encode()).hexdigest()
    return ''


class LocalChartArchive:
    """Optional presentation data. Never a Brain or support-export source.

    Intake copies a small RAM record; the existing memory-first writer pattern
    owns all SQLite/encoding work. No database connection is held open between
    batches, so a deleted archive simply starts empty on the next launch.
    """
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._processor = MemoryFirstLogProcessor(owner='ngui.local_chart_archive',
            critical_sink=lambda batch: None, routine_sink=self._write)

    def admit(self, *, router: str, epoch: str, sequence: int, sample: ChartSample) -> None:
        if not router:
            return
        self._processor.submit(key=f'{router}:{epoch}:{sequence}', lane=ROUTINE_LANE,
            observed_at=sample.timestamp, payload=dict(router=router, epoch=epoch,
                sequence=sequence, values=dict(sample.values), markers=dict(sample.marker_values)))

    def _write(self, batch) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1.)) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
            connection.execute('CREATE TABLE IF NOT EXISTS chart_frames ('
                'router TEXT NOT NULL,epoch TEXT NOT NULL,sequence INTEGER NOT NULL,'
                'at REAL NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(router,epoch,sequence))')
            connection.execute('CREATE INDEX IF NOT EXISTS chart_router_time ON chart_frames(router,at)')
            connection.executemany('INSERT OR IGNORE INTO chart_frames VALUES(?,?,?,?,?)',
                ((r.payload['router'], r.payload['epoch'], r.payload['sequence'], r.observed_at,
                  zlib.compress(json.dumps(dict(values=r.payload['values'], markers=r.payload['markers']),
                                           allow_nan=False, separators=(',', ':')).encode())) for r in batch))
            cutoff = max(r.observed_at for r in batch) - _LOCAL_CHART_RETENTION_S
            connection.execute('DELETE FROM chart_frames WHERE at < ?', (cutoff,))

    def read(self, *, router: str, until: float) -> tuple[dict, ...]:
        return self._read(router=router, until=until)

    def _read(self, *, router: str, until: float) -> tuple[dict, ...]:
        if not router or not self.path.is_file():
            return ()
        try:
            with closing(sqlite3.connect(f'file:{self.path.as_posix()}?mode=ro', uri=True, timeout=1.)) as connection:
                rows = connection.execute('SELECT epoch,sequence,at,payload FROM chart_frames '
                    'WHERE router=? AND at>=? AND at<? ORDER BY at DESC LIMIT 200000',
                    (router, until-_LOCAL_CHART_RETENTION_S, until)).fetchall()
            return tuple(dict(epoch=epoch, sequence=sequence, timestamp=stamp,
                              **json.loads(zlib.decompress(payload))) for epoch, sequence, stamp, payload in reversed(rows))
        except (OSError, sqlite3.Error, ValueError, zlib.error):
            # The optional chart archive cannot prevent normal live operation.
            _log.exception('Local chart archive could not be restored; live charts continue')
            return ()

    def close(self) -> None:
        try:
            self._processor.close(timeout_s=5.)
        except RuntimeError:
            _log.exception('Local chart archive did not finish saving within its close budget')


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
