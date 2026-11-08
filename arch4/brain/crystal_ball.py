"""
arch4/brain/crystal_ball.py
═══════════════════════════════════════════════════════════════
ARCH4 — Crystal Ball (Predictive Forecast Engine)

The brain's 24-hour forward-looking model.

Cold start:
    Reads telemetry_production JSONL on startup, builds per-band, per-hour,
    per-day-type (weekday/weekend) forecast slots from multi-day history.

Living forecast:
    Every hour, the brain compares reality vs prediction, updates slots
    with fresh data, and detects pattern drift (e.g. "this weekday
    looks like a weekend").

Query API:
    Supervisor/decision engine asks: "what band should I prefer at hour H?"
    Crystal ball returns ranked bands with confidence scores (0-5).

Non-owner boundary:
    Crystal Ball does not compensate congestion RTT under traffic. Congestion
    RTT is owned by the quiet RTT lane; traffic-busy RTT is excluded there.

Persistence:
    Forecast table is saved into memory.json and restored on load, so
    cold-start JSONL scan is a supplement, not a requirement.

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from arch4.evidence_scope import EvidenceScope, FormulaVector
from arch4.external_memory_evidence_scope import (
    external_projection_scope,
    validated_external_projection_scope,
)
from arch4.historical_artifact_reader import (
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
)
from arch4.telemetry.production_contract import (
    flatten_production_telemetry_row,
)
from arch4.telemetry.history_contract import telemetry_formula_vector

log = logging.getLogger("arch4.brain.crystal_ball")

# ── Constants ────────────────────────────────────────────────────────────────

FORECAST_SCAN_DAYS = 14          # how many days of JSONL to read on cold start
FORECAST_LIVE_ALPHA = 0.08       # EMA blending for live hourly updates
FORECAST_MIN_CONFIDENCE_SAMPLES = {
    0: 0,       # no data
    1: 1,       # 1-2 samples — barely anything
    2: 3,       # 3-9 samples — a couple of observations
    3: 10,      # 10-29 — starting to be reliable
    4: 30,      # 30-99 — solid pattern
    5: 100,     # 100+ — multiple days, high trust
}
FORECAST_MAX_SAMPLES_PER_SECOND = 10
FORECAST_SLOT_MAX_SAMPLE_COUNT = (
    FORECAST_SCAN_DAYS * 60 * 60 * FORECAST_MAX_SAMPLES_PER_SECOND
)
DAY_TYPES = ("weekday", "weekend")
DRIFT_THRESHOLD_CONGESTION = 20.0   # >20 pct deviation = pattern drift
DRIFT_THRESHOLD_THROUGHPUT = 0.40   # >40% relative deviation
HORIZON_CONTEXT_SCHEMA = "crystal_ball_horizon_context.v1"
HORIZON_CONTEXT_OWNER = "arch4.brain.crystal_ball"
DEFAULT_HORIZON_SECONDS = (60, 360, 3600)
CRYSTAL_BALL_FORMULA_EPOCH = "band_hour_cell_ema_v1"


def crystal_ball_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {"crystal_ball": CRYSTAL_BALL_FORMULA_EPOCH}
    )


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class ForecastSlot:
    """Accumulated statistics for one (day_type, hour, band) tuple."""
    avg_congestion: float = 0.0
    avg_dl_mbps: float = 0.0
    avg_ul_mbps: float = 0.0
    avg_rtt_ms: float = 0.0
    avg_sinr_db: float = 0.0
    avg_rsrq_db: float = 0.0
    ca_rate: float = 0.0           # fraction 0.0-1.0
    sample_count: int = 0
    confidence: int = 0            # 0-5
    last_updated_ts: float = 0.0   # epoch of most recent contributing sample
    lower_controller_samples: int = 0
    guc_command_samples: int = 0
    attribution_unavailable_samples: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ForecastSlot":
        s = ForecastSlot()
        for k in (
            "avg_congestion", "avg_dl_mbps", "avg_ul_mbps", "avg_rtt_ms",
            "avg_sinr_db", "avg_rsrq_db", "ca_rate", "last_updated_ts",
        ):
            setattr(s, k, float(d.get(k, 0.0) or 0.0))
        s.sample_count = _normalise_sample_count(d.get("sample_count", 0))
        s.confidence = _normalise_confidence(d.get("confidence", 0), s.sample_count)
        s.lower_controller_samples = max(
            0, int(d.get("lower_controller_samples", 0) or 0)
        )
        s.guc_command_samples = max(0, int(d.get("guc_command_samples", 0) or 0))
        s.attribution_unavailable_samples = max(
            0, int(d.get("attribution_unavailable_samples", 0) or 0)
        )
        return s


@dataclass
class BandRecommendation:
    """A ranked band recommendation from the forecast."""
    band: int
    confidence: int                 # 0-5
    predicted_congestion: float     # 0-100
    predicted_dl_mbps: float
    predicted_rtt_ms: float
    predicted_ca_rate: float
    # Composite score: lower congestion + higher throughput + lower RTT = better.
    # Normalised to 0.0 (worst) – 1.0 (best).
    score: float = 0.0
    lower_controller_selection_rate: float = 0.0
    evidence_scope: Dict[str, Any] | None = None


def _evidence_label(confidence: int) -> str:
    if confidence >= 4:
        return "high"
    if confidence >= 3:
        return "medium"
    if confidence >= 1:
        return "low"
    return "none"


def _recommendation_dict(rec: BandRecommendation | None) -> dict:
    if rec is None:
        return {}
    return {
        "band": int(rec.band),
        "confidence": int(rec.confidence),
        "predicted_congestion": float(rec.predicted_congestion),
        "predicted_dl_mbps": float(rec.predicted_dl_mbps),
        "predicted_rtt_ms": float(rec.predicted_rtt_ms),
        "predicted_ca_rate": float(rec.predicted_ca_rate),
        "score": float(rec.score),
        "lower_controller_selection_rate": float(
            rec.lower_controller_selection_rate
        ),
        "evidence_scope": dict(rec.evidence_scope or {}),
    }


def _lower_controller_selection_rate(slot: ForecastSlot) -> float:
    """Return provenance share without mixing capped evidence-window counts.

    ``sample_count`` is deliberately capped by the forecast evidence window while
    the provenance counters retain their own observation total.  Dividing one by
    the other can therefore produce an impossible rate above 1.0 after a long
    soak.  The provenance family is its own complete denominator.
    """

    provenance_total = (
        int(slot.lower_controller_samples or 0)
        + int(slot.guc_command_samples or 0)
        + int(slot.attribution_unavailable_samples or 0)
    )
    if provenance_total <= 0:
        return 0.0
    return float(slot.lower_controller_samples) / float(provenance_total)


def _slot_prediction_dict(slot: ForecastSlot | None, *, band: int, cell_id: int = 0) -> dict:
    if slot is None or slot.sample_count <= 0:
        return {
            "band": int(band or 0),
            "cell_id": int(cell_id or 0),
            "predicted_congestion": None,
            "predicted_dl_mbps": None,
            "predicted_ul_mbps": None,
            "predicted_rtt_ms": None,
            "predicted_ca_rate": None,
            "score": 0.0,
            "confidence": 0,
            "sample_count": 0,
            "lower_controller_selection_rate": 0.0,
        }
    return {
        "band": int(band or 0),
        "cell_id": int(cell_id or 0),
        "predicted_congestion": float(slot.avg_congestion),
        "predicted_dl_mbps": float(slot.avg_dl_mbps),
        "predicted_ul_mbps": float(slot.avg_ul_mbps),
        "predicted_rtt_ms": float(slot.avg_rtt_ms),
        "predicted_ca_rate": float(slot.ca_rate),
        "score": float(_score_slot(slot)),
        "confidence": int(slot.confidence),
        "sample_count": int(slot.sample_count),
        "lower_controller_selection_rate": _lower_controller_selection_rate(slot),
    }


def _horizon_label(seconds: int) -> str:
    if seconds < 60:
        return f"future_{seconds}s"
    minutes = int(round(seconds / 60.0))
    return f"future_{minutes}m"


def _expected_delta(current: Mapping[str, Any], candidate: BandRecommendation | None) -> dict:
    if candidate is None:
        return {}
    current_congestion = current.get("predicted_congestion")
    current_dl = current.get("predicted_dl_mbps")
    current_rtt = current.get("predicted_rtt_ms")
    current_ca = current.get("predicted_ca_rate")
    current_score = current.get("score")
    if current_congestion is None:
        return {}
    return {
        "congestion_improvement_pct": round(float(current_congestion) - float(candidate.predicted_congestion), 4),
        "dl_gain_mbps": round(float(candidate.predicted_dl_mbps) - float(current_dl or 0.0), 4),
        "rtt_reduction_ms": round(float(current_rtt or 0.0) - float(candidate.predicted_rtt_ms), 4),
        "ca_rate_gain": round(float(candidate.predicted_ca_rate) - float(current_ca or 0.0), 4),
        "score_delta": round(float(candidate.score) - float(current_score or 0.0), 4),
    }


def _capacity_prior_summary(raw: Optional[Mapping[str, Any]]) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    if raw.get("schema") != "measured_capacity_fact.v1":
        return {}
    if raw.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(raw.get("measured", False)):
        return {}
    return {
        "schema": "measured_capacity_fact.v1",
        "owner": "arch4.smart_loop.capacity_probe",
        "serving_band": _safe_int(raw.get("serving_band")),
        "target_bands": _normalise_band_list(raw.get("target_bands")),
        "dl_mbps": _safe_float(raw.get("dl_mbps")),
        "latency_ms": _safe_float(raw.get("latency_ms")),
        "confidence": _safe_float(raw.get("confidence")),
        "sample_ts": _safe_float(raw.get("sample_ts")),
    }


def _passive_capacity_prior_summary(raw: Optional[Mapping[str, Any]]) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    if raw.get("schema") != "passive_dl_peak_fact.v1":
        return {}
    if raw.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(raw.get("observed", False)):
        return {}
    return {
        "schema": "passive_dl_peak_fact.v1",
        "owner": "arch4.smart_loop.capacity_probe",
        "serving_band": _safe_int(raw.get("serving_band")),
        "target_bands": _normalise_band_list(raw.get("target_bands")),
        "peak_dl_mbps_lower_bound": _safe_float(raw.get("peak_dl_mbps_lower_bound")),
        "context_key": str(raw.get("context_key") or ""),
        "sample_ts": _safe_float(raw.get("sample_ts")),
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalise_band_list(value: Any) -> list[int]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for item in value:
        band = _safe_int(item)
        if band > 0 and band not in out:
            out.append(band)
    return out


# ── Accumulator (used during JSONL scan) ─────────────────────────────────────

@dataclass
class _Accumulator:
    """Running sums for building a ForecastSlot from raw samples."""
    sum_congestion: float = 0.0
    sum_dl: float = 0.0
    sum_ul: float = 0.0
    sum_rtt: float = 0.0
    sum_sinr: float = 0.0
    sum_rsrq: float = 0.0
    ca_positive: int = 0
    count: int = 0
    latest_ts: float = 0.0

    def add(self, cong: float, dl: float, ul: float, rtt: float,
            sinr: float, rsrq: float, ca: bool, ts: float) -> None:
        self.sum_congestion += cong
        self.sum_dl += dl
        self.sum_ul += ul
        self.sum_rtt += rtt
        self.sum_sinr += sinr
        self.sum_rsrq += rsrq
        if ca:
            self.ca_positive += 1
        self.count += 1
        if ts > self.latest_ts:
            self.latest_ts = ts

    def to_slot(self) -> ForecastSlot:
        n = max(1, self.count)
        return ForecastSlot(
            avg_congestion=self.sum_congestion / n,
            avg_dl_mbps=self.sum_dl / n,
            avg_ul_mbps=self.sum_ul / n,
            avg_rtt_ms=self.sum_rtt / n,
            avg_sinr_db=self.sum_sinr / n,
            avg_rsrq_db=self.sum_rsrq / n,
            ca_rate=self.ca_positive / n,
            sample_count=self.count,
            confidence=_confidence_from_count(self.count),
            last_updated_ts=self.latest_ts,
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _confidence_from_count(n: int) -> int:
    """Map sample count to confidence level 0-5."""
    if n >= 100:
        return 5
    if n >= 30:
        return 4
    if n >= 10:
        return 3
    if n >= 3:
        return 2
    if n >= 1:
        return 1
    return 0


def _normalise_sample_count(
    value: Any,
    *,
    max_count: int = FORECAST_SLOT_MAX_SAMPLE_COUNT,
) -> int:
    """Keep evidence counts inside the configured scan window."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return min(n, int(max_count))


def _normalise_confidence(value: Any, sample_count: int) -> int:
    try:
        confidence = int(value or 0)
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(5, confidence))
    return min(confidence, _confidence_from_count(sample_count))


def _day_type_from_ts(ts: float) -> str:
    """Return 'weekday' or 'weekend' for a unix timestamp."""
    # Monday=0 .. Sunday=6
    weekday = datetime.fromtimestamp(ts).weekday()
    return "weekend" if weekday >= 5 else "weekday"


def _slot_key(day_type: str, hour: int, band: int, cell_id: int = 0) -> str:
    """Canonical key for the forecast table.  cell_id=0 is the band-level aggregate."""
    return f"{day_type}|{hour:02d}|{band}|{cell_id}"


def _parse_slot_key(key: str) -> Tuple[str, int, int, int]:
    """Reverse of _slot_key.  Returns (day_type, hour, band, cell_id)."""
    parts = key.split("|")
    return parts[0], int(parts[1]), int(parts[2]), int(parts[3]) if len(parts) > 3 else 0


def _band_key(day_type: str, hour: int, band: int) -> str:
    """Key prefix for band-level queries (matches all PCIs)."""
    return f"{day_type}|{hour:02d}|{band}|"


def _score_slot(slot: ForecastSlot) -> float:
    """Composite score: lower is better for congestion/RTT, higher for throughput.

    Returns 0.0 (worst) to 1.0 (best).  Each axis normalised independently
    then blended.  Weights: congestion 40%, throughput 30%, RTT 30%.
    """
    # Congestion: 0% → 1.0, 100% → 0.0
    cong_score = max(0.0, 1.0 - slot.avg_congestion / 100.0)

    # Throughput: 0 Mbps → 0.0, 100 Mbps → 1.0 (capped)
    tp_score = min(1.0, slot.avg_dl_mbps / 100.0)

    # RTT: 20ms → 1.0, 200ms+ → 0.0 (linear)
    rtt_score = max(0.0, min(1.0, (200.0 - slot.avg_rtt_ms) / 180.0))

    return 0.40 * cong_score + 0.30 * tp_score + 0.30 * rtt_score


# ══════════════════════════════════════════════════════════════════════════════
#  CrystalBall
# ══════════════════════════════════════════════════════════════════════════════

class CrystalBall:
    """Predictive 24-hour forecast engine for the brain.

    Usage::

        cb = CrystalBall()
        cb.build_from_jsonl(output_dir)          # cold start
        ...
        recs = cb.get_recommendation(hour=18, day_type="weekday",
                                     deployed_bands=[1, 3, 7, 20])
        # recs is a list of BandRecommendation sorted by score (best first)
    """

    def __init__(self) -> None:
        # Primary forecast table: key → ForecastSlot
        self._table: Dict[str, ForecastSlot] = {}
        # Build metadata
        self._build_ts: float = 0.0         # when the JSONL scan completed
        self._build_files: int = 0          # how many JSONL files were read
        self._build_records: int = 0        # total records processed
        self._last_hourly_update_ts: float = 0.0
        self._last_hourly_update_hour: int = -1
        # Drift detection state
        self._drift_detected: bool = False
        self._drift_reason: str = ""
        self._drift_since_ts: float = 0.0
        # Which day_type the brain currently trusts for predictions.
        # Normally follows calendar; drift detection can override.
        self._effective_day_type: str = ""
        self._delayed_reward_projection_keys: set[str] = set()
        self._source_evidence_scope: Dict[str, Any] = {}
        self._evidence_scope: Dict[str, Any] = {}
        self._evidence_quarantine: List[Dict[str, Any]] = []
        # Same-modem continuity is a low-confidence advisory projection.  It
        # never enters the live forecast table and therefore cannot overwrite
        # fresh, current-session Crystal Ball evidence.
        self._external_band_hour_prior: Dict[str, float] = {}
        self._external_band_hour_counts: Dict[str, int] = {}
        self._external_prior_scope: Dict[str, Any] = {}
        self._external_prior_sources: List[Dict[str, Any]] = []
        self._external_prior_applied_ts: float = 0.0

    def apply_external_memory_prior(
        self,
        prior: Mapping[str, Any],
        *,
        target_identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Admit a weighted historical ranking without claiming live truth."""

        row = dict(prior or {})
        if (
            row.get("schema") != "external_memory_learning_prior.v1"
            or row.get("owner") != "arch4.external_memory_learning_projection"
            or row.get("present") is not True
        ):
            return {"accepted": False, "reason": "external learning prior invalid"}
        try:
            source_scope = validated_external_projection_scope(
                dict(row.get("evidence_scope") or {})
            )
            target_scope = external_projection_scope(target_identity, evidence_count=1)
        except (TypeError, ValueError) as exc:
            return {"accepted": False, "reason": f"external prior scope invalid: {exc}"}
        if not source_scope.exact_generation_match(target_scope):
            return {"accepted": False, "reason": "external prior target generation mismatch"}
        quality: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        for key, value in dict(row.get("band_hour_quality") or {}).items():
            try:
                band_text, hour_text = str(key).split(":", 1)
                band = int(band_text)
                hour = int(hour_text)
                score = max(0.0, min(1.0, float(value)))
                count = max(1, int(dict(row.get("band_hour_counts") or {}).get(key, 1)))
            except (TypeError, ValueError):
                continue
            if band <= 0 or not 0 <= hour <= 23:
                continue
            normalized = f"{band}:{hour}"
            quality[normalized] = score
            counts[normalized] = min(200, count)
        if not quality:
            return {"accepted": False, "reason": "external prior has no band-hour rankings"}
        confidence = min(
            0.35,
            max(0.05, float(row.get("profile_confidence", 0.0) or 0.0) * 0.35),
        )
        projected_scope = EvidenceScope(
            identity_code=source_scope.identity_code,
            identity_status=source_scope.identity_status,
            identity_catalog_version=source_scope.identity_catalog_version,
            identity_catalog_checksum=source_scope.identity_catalog_checksum,
            session_scope_key=source_scope.session_scope_key,
            source_epoch=source_scope.source_epoch,
            source_sequence=source_scope.source_sequence,
            observed_at=source_scope.observed_at,
            evidence_age_s=source_scope.evidence_age_s,
            confidence=confidence,
            evidence_count=max(1, int(row.get("sample_count", 0) or 0)),
            formula_vector=crystal_ball_formula_vector(),
            portability=source_scope.portability,
            access_type_id=source_scope.access_type_id,
            model_family_id=source_scope.model_family_id,
            provider_plmn=source_scope.provider_plmn,
            sim_scope_hash=source_scope.sim_scope_hash,
            site_scope_id=source_scope.site_scope_id,
        )
        self._external_band_hour_prior = quality
        self._external_band_hour_counts = counts
        self._external_prior_scope = projected_scope.as_dict()
        self._external_prior_sources = [
            dict(source)
            for source in row.get("sources", [])
            if isinstance(source, Mapping)
        ][:12]
        self._external_prior_applied_ts = time.time()
        return {
            "accepted": True,
            "advisory_only": True,
            "live_truth_override": False,
            "band_hour_count": len(quality),
            "sample_count": int(row.get("sample_count", 0) or 0),
        }

    def _query_evidence_scope(self) -> Dict[str, Any]:
        return dict(self._evidence_scope or self._external_prior_scope)

    def _quarantine_evidence(self, *, reason: str, row: Mapping[str, Any]) -> None:
        self._evidence_quarantine.append(
            {
                "schema": "EvidenceQuarantine.v1",
                "owner": "arch4.brain.crystal_ball",
                "reason": str(reason or "evidence_scope_invalid"),
                "quarantined_at": time.time(),
                "row": dict(row or {}),
            }
        )
        if len(self._evidence_quarantine) > 128:
            self._evidence_quarantine = self._evidence_quarantine[-128:]

    def _bind_source_scope(self, source_scope: EvidenceScope) -> None:
        try:
            current = EvidenceScope.from_mapping(self._source_evidence_scope)
        except (TypeError, ValueError):
            current = None
        if current is not None and not current.same_physical_namespace(source_scope):
            self._quarantine_evidence(
                reason="crystal_ball_runtime_scope_changed",
                row={
                    "source_evidence_scope": current.as_dict(),
                    "evidence_scope": dict(self._evidence_scope),
                    "table": {key: slot.to_dict() for key, slot in self._table.items()},
                },
            )
            self._table = {}
            self._delayed_reward_projection_keys = set()
            self._evidence_scope = {}
        self._source_evidence_scope = source_scope.as_dict()

    def _refresh_evidence_scope(self, *, evidence_count: int) -> None:
        try:
            source = EvidenceScope.from_mapping(self._source_evidence_scope)
        except (TypeError, ValueError):
            self._evidence_scope = {}
            return
        total_count = max(
            int(evidence_count or 0),
            sum(int(slot.sample_count or 0) for slot in self._table.values()),
        )
        if total_count <= 0:
            self._evidence_scope = {}
            return
        self._evidence_scope = EvidenceScope(
            identity_code=source.identity_code,
            identity_status=source.identity_status,
            identity_catalog_version=source.identity_catalog_version,
            identity_catalog_checksum=source.identity_catalog_checksum,
            session_scope_key=source.session_scope_key,
            source_epoch=source.source_epoch,
            source_sequence=source.source_sequence,
            observed_at=source.observed_at,
            evidence_age_s=source.evidence_age_s,
            confidence=min(1.0, float(total_count) / 100.0),
            evidence_count=total_count,
            formula_vector=crystal_ball_formula_vector(),
            access_type_id=source.access_type_id,
            provider_plmn=source.provider_plmn,
            sim_scope_hash=source.sim_scope_hash,
            site_scope_id=source.site_scope_id,
            cell_scope_key="",
            serving_band=0,
        ).as_dict()
    # ── Cold start: JSONL scan ────────────────────────────────────────────

    def build_from_jsonl(self, output_dir: str | Path, *, history_logger=None) -> int:
        """Scan telemetry_production JSONL files and build the forecast table.

        If *history_logger* is provided, its file-gate context manager
        is used to close today's JSONL before reading and reopen after —
        preventing the WSL/NTFS infinite-EOF hang when iterating a file
        that is open for append by the same process.

        Returns the number of records processed.  Safe to call with an
        empty or nonexistent directory — returns 0.
        """
        output_path = Path(output_dir)
        if not output_path.exists():
            log.info("CrystalBall: output_dir %s not found, skipping JSONL scan", output_path)
            return 0

        if history_logger is not None:
            with history_logger.history_file_gate():
                return self._build_from_jsonl_inner(output_path)
        return self._build_from_jsonl_inner(output_path)

    def _build_from_jsonl_inner(self, output_path: Path) -> int:
        """Inner scan — runs while the history file gate is held."""
        reader = HistoricalArtifactReader(
            output_path,
            retention_days=FORECAST_SCAN_DAYS,
        )
        artifacts = reader.discover(families=(FAMILY_PRODUCTION,))
        if not artifacts:
            log.info("CrystalBall: no telemetry_production JSONL files found in %s", output_path)
            return 0

        log.info(
            "CrystalBall: scanning %d current/archive JSONL artifacts from %s",
            len(artifacts),
            output_path,
        )

        accumulators: Dict[str, _Accumulator] = defaultdict(_Accumulator)
        regrades: List[dict] = []
        total_records = 0
        skipped = 0

        cutoff = time.time() - float(FORECAST_SCAN_DAYS * 86400)
        for historical_record in reader.iter_records(
            families=(FAMILY_PRODUCTION,),
            since_ts=cutoff,
        ):
            rec = flatten_production_telemetry_row(historical_record.payload)
            processed = self._ingest_record(
                rec,
                accumulators,
                regrades,
            )
            if processed:
                total_records += 1
            else:
                skipped += 1
        reader_status = reader.status()
        skipped += int(reader_status.get("malformed_records", 0) or 0)

        # Convert accumulators to forecast slots
        for key, acc in accumulators.items():
            if acc.count > 0:
                self._table[key] = acc.to_slot()

        # Apply regrade penalties: reduce confidence on slots that drifted.
        # If the slot doesn't exist (brain switched away, no tick data),
        # create a stub slot so the penalty survives cold rebuild.
        regrades_applied = 0
        stubs_created = 0
        for rg in regrades:
            rg_day = str(rg.get("day_type", "") or "")
            rg_hour = rg.get("hour")
            rg_band = rg.get("serving_band", rg.get("primary_band"))
            # cell_id is the stable identifier; fall back to pci for old JSONL
            rg_cell_id = int(rg.get("cell_id", 0) or 0)
            if rg_cell_id == 0:
                rg_cell_id = int(rg.get("pci", 0) or 0)
            rg_penalty = rg.get("confidence_penalty")
            if rg_hour is None or rg_band is None:
                continue
            rg_hour = int(rg_hour)
            rg_band = int(rg_band)
            penalty = int(rg_penalty) if rg_penalty else 1
            if rg_hour < 0 or rg_band <= 0 or not rg_day:
                continue
            key = _slot_key(rg_day, rg_hour, rg_band, rg_cell_id)
            slot = self._table.get(key)
            if slot is None:
                # Stub slot: the brain switched away from this band/cell,
                # so no tick data exists.  Create a minimal slot with
                # confidence=0 so the regrade penalty is recorded and
                # future tick data merges into this slot properly.
                slot = _Accumulator().to_slot()
                slot.confidence = 0
                self._table[key] = slot
                stubs_created += 1
                regrades_applied += 1
            elif slot.confidence > 0:
                slot.confidence = max(0, slot.confidence - penalty)
                regrades_applied += 1
        if regrades_applied:
            log.info(
                "CrystalBall: applied %d regrade penalties (%d records, %d stubs)",
                regrades_applied, len(regrades), stubs_created,
            )

        self._build_ts = time.time()
        self._build_files = len(artifacts)
        self._build_records = total_records
        self._effective_day_type = _day_type_from_ts(time.time())
        self._refresh_evidence_scope(evidence_count=total_records)

        log.info(
            "CrystalBall: built forecast from %d records (%d skipped) across "
            "%d files — %d slots covering %d bands",
            total_records, skipped, len(artifacts),
            len(self._table), len(self.get_known_bands()),
        )
        return total_records

    def _ingest_record(
        self, rec: dict, accumulators: Dict[str, _Accumulator],
        regrades: Optional[List[dict]] = None,
    ) -> bool:
        """Parse one JSONL record into the accumulator.  Returns True if used."""
        try:
            evidence = EvidenceScope.from_mapping(
                dict(rec.get("evidence_scope") or {})
            )
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=f"crystal_ball_source_scope_invalid:{exc}",
                row=rec,
            )
            return False
        record_type = str(rec.get("record_type", "tick") or "tick")
        if (
            record_type == "tick"
            and evidence.formula_vector != telemetry_formula_vector()
        ) or (
            record_type == "action_regrade"
            and evidence.formula_vector != crystal_ball_formula_vector()
        ):
            self._quarantine_evidence(
                reason="crystal_ball_source_formula_vector_mismatch",
                row=rec,
            )
            return False
        try:
            current = EvidenceScope.from_mapping(self._source_evidence_scope)
        except (TypeError, ValueError):
            current = None
        if current is not None and not current.same_physical_namespace(evidence):
            if evidence.observed_at < current.observed_at:
                self._quarantine_evidence(
                    reason="crystal_ball_source_scope_foreign_older",
                    row=rec,
                )
                return False
            self._quarantine_evidence(
                reason="crystal_ball_source_scope_replaced_by_newer_session",
                row={"evidence_scope": current.as_dict()},
            )
            accumulators.clear()
            if regrades is not None:
                regrades.clear()
        self._source_evidence_scope = evidence.as_dict()
        # ── Action regrade: collect for post-build confidence reduction ──
        if record_type == "action_regrade":
            if regrades is not None:
                regrades.append(rec)
            return True

        # Only tick records with connected radio
        if record_type != "tick":
            return False
        if not rec.get("radio_connected", False):
            return False

        ts = float(rec.get("ts", 0.0) or 0.0)
        if ts <= 0.0:
            return False

        band = int(rec.get("serving_band", rec.get("primary_band", 0)) or 0)
        if band <= 0:
            return False

        # cell_id is the stable identifier; fall back to pci for old JSONL
        cell_id = int(rec.get("cell_id", 0) or 0)
        if cell_id == 0:
            cell_id = int(rec.get("pci", 0) or 0)
        hour = datetime.fromtimestamp(ts).hour
        day_type = _day_type_from_ts(ts)
        key = _slot_key(day_type, hour, band, cell_id)

        # Extract fields (graceful fallback for older JSONL without congestion_pct)
        congestion = float(rec.get("congestion_pct", 0.0) or 0.0)
        dl = float(rec.get("dl_mbps", 0.0) or 0.0)
        ul = float(rec.get("ul_mbps", 0.0) or 0.0)
        rtt = float(rec.get("rtt_ms", 0.0) or 0.0)
        sinr = float(rec.get("sinr_db", 0.0) or 0.0)
        rsrq = float(rec.get("rsrq_db", 0.0) or 0.0)
        ca = bool(rec.get("ca_active", False))

        # Sanity clamp — reject obvious garbage
        if rtt < 0.0 or rtt > 30000.0:
            rtt = 0.0
        if dl < 0.0:
            dl = 0.0

        accumulators[key].add(congestion, dl, ul, rtt, sinr, rsrq, ca, ts)
        return True

    # ── Living forecast: hourly update ────────────────────────────────────

    def update_from_live_hour(
        self,
        hour: int,
        day_type: str,
        band_cell_stats: Dict[Tuple[int, int], Dict[str, Any]],
        *,
        evidence_scope: Mapping[str, Any],
    ) -> None:
        """Blend live observed stats into the forecast table.

        Called by the engine once per hour with the current hour's
        observed per-band-per-cell statistics.

        Args:
            hour: 0-23
            day_type: 'weekday' or 'weekend'
            band_cell_stats: {(band, cell_id): {'congestion': float, 'dl_mbps': float,
                             'ul_mbps': float, 'rtt_ms': float, 'sinr_db': float,
                             'rsrq_db': float, 'ca_rate': float, 'count': int}}
        """
        try:
            source_scope = EvidenceScope.from_mapping(dict(evidence_scope or {}))
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=f"crystal_ball_live_scope_invalid:{exc}",
                row={"band_cell_stats": dict(band_cell_stats or {})},
            )
            return
        self._bind_source_scope(source_scope)
        now = time.time()
        alpha = FORECAST_LIVE_ALPHA

        for (band, cell_id), stats in band_cell_stats.items():
            key = _slot_key(day_type, hour, band, cell_id)
            count = int(stats.get("count", 0) or 0)
            if count <= 0:
                continue

            existing = self._table.get(key)
            if existing is None or existing.sample_count == 0:
                # First data for this slot — seed directly
                self._table[key] = ForecastSlot(
                    avg_congestion=float(stats.get("congestion", 0.0) or 0.0),
                    avg_dl_mbps=float(stats.get("dl_mbps", 0.0) or 0.0),
                    avg_ul_mbps=float(stats.get("ul_mbps", 0.0) or 0.0),
                    avg_rtt_ms=float(stats.get("rtt_ms", 0.0) or 0.0),
                    avg_sinr_db=float(stats.get("sinr_db", 0.0) or 0.0),
                    avg_rsrq_db=float(stats.get("rsrq_db", 0.0) or 0.0),
                    ca_rate=float(stats.get("ca_rate", 0.0) or 0.0),
                    sample_count=_normalise_sample_count(count),
                    confidence=_confidence_from_count(_normalise_sample_count(count)),
                    last_updated_ts=now,
                    lower_controller_samples=int(
                        stats.get("lower_controller_samples", 0) or 0
                    ),
                    guc_command_samples=int(
                        stats.get("guc_command_samples", 0) or 0
                    ),
                    attribution_unavailable_samples=int(
                        stats.get("attribution_unavailable_samples", 0) or 0
                    ),
                )
            else:
                # EMA blend into existing forecast
                s = existing
                s.avg_congestion = (1 - alpha) * s.avg_congestion + alpha * float(stats.get("congestion", 0.0) or 0.0)
                s.avg_dl_mbps = (1 - alpha) * s.avg_dl_mbps + alpha * float(stats.get("dl_mbps", 0.0) or 0.0)
                s.avg_ul_mbps = (1 - alpha) * s.avg_ul_mbps + alpha * float(stats.get("ul_mbps", 0.0) or 0.0)
                s.avg_rtt_ms = (1 - alpha) * s.avg_rtt_ms + alpha * float(stats.get("rtt_ms", 0.0) or 0.0)
                s.avg_sinr_db = (1 - alpha) * s.avg_sinr_db + alpha * float(stats.get("sinr_db", 0.0) or 0.0)
                s.avg_rsrq_db = (1 - alpha) * s.avg_rsrq_db + alpha * float(stats.get("rsrq_db", 0.0) or 0.0)
                s.ca_rate = (1 - alpha) * s.ca_rate + alpha * float(stats.get("ca_rate", 0.0) or 0.0)
                s.sample_count = _normalise_sample_count(s.sample_count + count)
                s.confidence = _confidence_from_count(s.sample_count)
                s.last_updated_ts = now
                s.lower_controller_samples += int(
                    stats.get("lower_controller_samples", 0) or 0
                )
                s.guc_command_samples += int(
                    stats.get("guc_command_samples", 0) or 0
                )
                s.attribution_unavailable_samples += int(
                    stats.get("attribution_unavailable_samples", 0) or 0
                )

        self._last_hourly_update_ts = now
        self._last_hourly_update_hour = hour
        self._refresh_evidence_scope(
            evidence_count=sum(
                int(stats.get("count", 0) or 0)
                for stats in band_cell_stats.values()
            )
        )

    # ── Live regrade ───────────────────────────────────────────────────

    def apply_live_regrade(
        self,
        *,
        hour: int,
        day_type: str,
        band: int,
        cell_id: int = 0,
        confidence_penalty: int = 1,
    ) -> bool:
        """Reduce confidence on a forecast slot immediately.

        Called by the engine when drift is detected so the penalty is
        reflected in the live crystal ball table (and persisted to
        Memory on the next checkpoint) rather than waiting for a cold
        rebuild to ingest the JSONL regrade record.

        Returns True if a slot was penalised, False otherwise.
        """
        if hour < 0 or band <= 0 or not day_type:
            return False
        key = _slot_key(day_type, hour, band, cell_id)
        slot = self._table.get(key)
        if slot is not None and slot.confidence > 0:
            slot.confidence = max(0, slot.confidence - confidence_penalty)
            log.debug(
                "CrystalBall: live regrade %s → confidence=%d",
                key, slot.confidence,
            )
            return True
        return False

    # ── Drift detection ───────────────────────────────────────────────────

    def check_drift(
        self,
        hour: int,
        day_type: str,
        live_band_cell_stats: Dict[Tuple[int, int], Dict[str, Any]],
    ) -> Optional[str]:
        """Compare live observations against forecast and detect drift.

        Returns a drift reason string if significant drift detected,
        or None if predictions match reality.

        Also updates internal drift state and may switch the effective
        day_type used for recommendations (e.g. if a weekday looks like
        a weekend).
        """
        alt_day_type = "weekend" if day_type == "weekday" else "weekday"
        now = time.time()

        # Aggregate live stats to band level for drift comparison
        band_agg: Dict[int, Dict[str, float]] = {}
        for (band, _cell_id), stats in live_band_cell_stats.items():
            count = int(stats.get("count", 0) or 0)
            if count <= 0:
                continue
            if band not in band_agg:
                band_agg[band] = {"sum_cong": 0.0, "total": 0}
            band_agg[band]["sum_cong"] += float(stats.get("congestion", 0.0) or 0.0) * count
            band_agg[band]["total"] += count

        # Compare aggregated live vs forecast
        current_dev = 0.0
        alt_dev = 0.0
        compared = 0

        for band, agg in band_agg.items():
            if agg["total"] < 5:
                continue
            live_cong = agg["sum_cong"] / max(1, agg["total"])

            # Current day_type forecast (band-level aggregate)
            cur_slot = self._aggregate_band_slot(day_type, hour, band)
            if cur_slot and cur_slot.confidence >= 2:
                current_dev += abs(live_cong - cur_slot.avg_congestion)
                compared += 1

            # Alternative day_type forecast
            alt_slot = self._aggregate_band_slot(alt_day_type, hour, band)
            if alt_slot and alt_slot.confidence >= 2:
                alt_dev += abs(live_cong - alt_slot.avg_congestion)

        if compared == 0:
            return None

        avg_dev = current_dev / compared

        if avg_dev > DRIFT_THRESHOLD_CONGESTION:
            # Check if alternative day_type fits better
            avg_alt_dev = alt_dev / max(1, compared)
            if avg_alt_dev < avg_dev * 0.6:
                reason = (
                    f"drift: {day_type} prediction off by {avg_dev:.0f}%% avg, "
                    f"{alt_day_type} model fits better ({avg_alt_dev:.0f}%%)"
                )
                self._drift_detected = True
                self._drift_reason = reason
                self._drift_since_ts = now
                self._effective_day_type = alt_day_type
                log.info("CrystalBall: %s", reason)
                return reason
            else:
                reason = (
                    f"drift: {day_type} prediction off by {avg_dev:.0f}%% avg, "
                    f"no better model available"
                )
                self._drift_detected = True
                self._drift_reason = reason
                self._drift_since_ts = now
                log.info("CrystalBall: %s", reason)
                return reason

        # No drift — reset if previously drifting
        if self._drift_detected:
            self._drift_detected = False
            self._drift_reason = ""
            self._effective_day_type = day_type
            log.info("CrystalBall: drift cleared, back to %s model", day_type)

        return None

    # ── Live regrade ─────────────────────────────────────────────────

    # ── Internal: band-level aggregation ────────────────────────────────

    def _aggregate_band_slot(
        self, day_type: str, hour: int, band: int,
    ) -> Optional[ForecastSlot]:
        """Aggregate all PCI slots for a given (day_type, hour, band).

        Weighted average by sample count — PCIs with more data contribute
        more to the band-level view.  Returns None if no data.
        """
        prefix = _band_key(day_type, hour, band)
        matching = [
            (k, s) for k, s in self._table.items()
            if k.startswith(prefix) and s.sample_count > 0
        ]
        if not matching:
            return None

        total_n = sum(s.sample_count for _, s in matching)
        if total_n == 0:
            return None

        agg = ForecastSlot()
        for _k, s in matching:
            w = s.sample_count / total_n
            agg.avg_congestion += w * s.avg_congestion
            agg.avg_dl_mbps += w * s.avg_dl_mbps
            agg.avg_ul_mbps += w * s.avg_ul_mbps
            agg.avg_rtt_ms += w * s.avg_rtt_ms
            agg.avg_sinr_db += w * s.avg_sinr_db
            agg.avg_rsrq_db += w * s.avg_rsrq_db
            agg.ca_rate += w * s.ca_rate
            agg.lower_controller_samples += int(s.lower_controller_samples)
            agg.guc_command_samples += int(s.guc_command_samples)
            agg.attribution_unavailable_samples += int(
                s.attribution_unavailable_samples
            )
            if s.last_updated_ts > agg.last_updated_ts:
                agg.last_updated_ts = s.last_updated_ts
        agg.sample_count = total_n
        agg.confidence = _confidence_from_count(total_n)
        return agg

    # ── Query API ─────────────────────────────────────────────────────────

    def get_recommendation(
        self,
        hour: int,
        day_type: str,
        deployed_bands: List[int],
        *,
        blacklisted_bands: Optional[set] = None,
    ) -> List[BandRecommendation]:
        """Return bands ranked by predicted quality for the given hour.

        Aggregates across all PCIs per band.  Uses the effective day_type
        (may differ from calendar if drift detected).

        Returns list sorted best-first.  Empty if no forecast data.
        """
        effective = self._effective_day_type if self._drift_detected else day_type
        bl = blacklisted_bands or set()
        recs: List[BandRecommendation] = []

        for band in deployed_bands:
            if band in bl:
                continue
            slot = self._aggregate_band_slot(effective, hour, band)
            if slot is None or slot.sample_count == 0:
                external_score = self._external_band_hour_prior.get(f"{band}:{hour}")
                if external_score is not None:
                    recs.append(BandRecommendation(
                        band=band,
                        # Historical continuity may rank a candidate, but its
                        # confidence is deliberately below autonomous-action
                        # thresholds until current-session evidence exists.
                        confidence=1,
                        predicted_congestion=100.0 * (1.0 - external_score),
                        predicted_dl_mbps=0.0,
                        predicted_rtt_ms=0.0,
                        predicted_ca_rate=0.0,
                        score=external_score,
                        evidence_scope=dict(self._external_prior_scope),
                    ))
                    continue
                recs.append(BandRecommendation(
                    band=band,
                    confidence=0,
                    predicted_congestion=50.0,
                    predicted_dl_mbps=0.0,
                    predicted_rtt_ms=0.0,
                    predicted_ca_rate=0.0,
                    score=0.0,
                    evidence_scope=self._query_evidence_scope(),
                ))
                continue

            score = _score_slot(slot)
            recs.append(BandRecommendation(
                band=band,
                confidence=slot.confidence,
                predicted_congestion=slot.avg_congestion,
                predicted_dl_mbps=slot.avg_dl_mbps,
                predicted_rtt_ms=slot.avg_rtt_ms,
                predicted_ca_rate=slot.ca_rate,
                score=score,
                lower_controller_selection_rate=_lower_controller_selection_rate(slot),
                evidence_scope=self._query_evidence_scope(),
            ))

        recs.sort(key=lambda r: (-r.confidence, -r.score))
        return recs

    def get_best_band(
        self,
        hour: int,
        day_type: str,
        deployed_bands: List[int],
        *,
        min_confidence: int = 2,
        blacklisted_bands: Optional[set] = None,
    ) -> Optional[BandRecommendation]:
        """Convenience: return the single best band if confidence >= threshold.

        Returns None if no band meets the confidence floor.
        """
        recs = self.get_recommendation(
            hour, day_type, deployed_bands,
            blacklisted_bands=blacklisted_bands,
        )
        for r in recs:
            if r.confidence >= min_confidence:
                return r
        return None

    def build_prediction_advisory(
        self,
        *,
        hour: int,
        day_type: str,
        current_band: int,
        deployed_bands: List[int],
        current_cell_id: int = 0,
        congestion_threshold: float = 40.0,
        min_confidence: int = 3,
        min_margin: float = 10.0,
        blacklisted_bands: Optional[set] = None,
    ) -> dict:
        """Return Crystal Ball's local advisory judgment for Brain.

        This is intentionally not an action command.  Crystal Ball may present
        a ranked historical prediction, or withhold it with a concrete reason.
        Brain remains the only consumer that can combine this with live RF,
        policy, calibration, safety, and write authority.
        """
        effective = self._effective_day_type if self._drift_detected else day_type
        base: dict[str, Any] = {
            "schema": "crystal_ball_prediction_advisory_v1",
            "authority": "crystal_ball",
            "status": "withheld",
            "action": "none",
            "reason": "",
            "advisory_only": True,
            "direct_modem_actuation": False,
            "brain_authority_required": True,
            "hour": int(hour),
            "day_type": str(day_type or ""),
            "effective_day_type": str(effective or day_type or ""),
            "current": {
                "band": int(current_band or 0),
                "cell_id": int(current_cell_id or 0),
                "predicted_congestion": None,
                "confidence": 0,
                "sample_count": 0,
            },
            "recommendation": {},
            "candidates": [],
            "confidence": 0.0,
            "evidence_strength": "none",
            "provenance": "local_predictive_history",
            "evidence_scope": self._query_evidence_scope(),
        }
        try:
            EvidenceScope.from_mapping(self._query_evidence_scope())
        except (TypeError, ValueError):
            base["reason"] = "evidence_scope_unavailable"
            return base
        if hour < 0 or hour > 23:
            base["reason"] = "invalid_hour"
            return base
        if not effective:
            base["reason"] = "missing_day_type"
            return base
        if current_band <= 0:
            base["reason"] = "missing_current_band"
            return base
        if not deployed_bands:
            base["reason"] = "no_deployed_bands"
            return base

        current_slot = None
        if current_cell_id > 0:
            current_slot = self.get_cell_forecast(hour, day_type, current_band, current_cell_id)
        if current_slot is None:
            current_slot = self._aggregate_band_slot(effective, hour, current_band)
        if current_slot is None or current_slot.sample_count <= 0:
            base["reason"] = "no_current_band_history"
            return base
        base["current"] = {
            "band": int(current_band),
            "cell_id": int(current_cell_id or 0),
            "predicted_congestion": float(current_slot.avg_congestion),
            "confidence": int(current_slot.confidence),
            "sample_count": int(current_slot.sample_count),
        }
        if current_slot.confidence < max(1, min_confidence - 1):
            base["reason"] = "current_band_confidence_low"
            base["confidence"] = round(max(0.0, current_slot.confidence / 5.0), 3)
            base["evidence_strength"] = _evidence_label(current_slot.confidence)
            return base
        if current_slot.avg_congestion < congestion_threshold:
            base["reason"] = "current_band_not_predicted_congested"
            base["confidence"] = round(max(0.0, current_slot.confidence / 5.0), 3)
            base["evidence_strength"] = _evidence_label(current_slot.confidence)
            return base

        candidates = self.get_recommendation(
            hour,
            day_type,
            deployed_bands,
            blacklisted_bands=blacklisted_bands,
        )
        base["candidates"] = [_recommendation_dict(rec) for rec in candidates[:5]]
        best = None
        for rec in candidates:
            if rec.band == current_band:
                continue
            if rec.confidence < min_confidence:
                continue
            if rec.predicted_congestion >= current_slot.avg_congestion - min_margin:
                continue
            best = rec
            break
        if best is None:
            base["reason"] = "no_better_band_with_sufficient_margin"
            best_candidate = next((rec for rec in candidates if rec.band != current_band), None)
            candidate_conf = best_candidate.confidence if best_candidate else 0
            confidence = min(current_slot.confidence, candidate_conf) / 5.0 if best_candidate else current_slot.confidence / 5.0
            base["confidence"] = round(max(0.0, min(1.0, confidence)), 3)
            base["evidence_strength"] = _evidence_label(min(current_slot.confidence, candidate_conf))
            return base

        evidence_confidence = min(int(current_slot.confidence), int(best.confidence))
        congestion_margin = float(current_slot.avg_congestion - best.predicted_congestion)
        confidence_score = (evidence_confidence / 5.0) * max(0.0, min(1.0, congestion_margin / 40.0))
        base.update(
            {
                "status": "present",
                "action": "recommend_band",
                "reason": "current_band_predicted_congested_with_better_candidate",
                "recommendation": _recommendation_dict(best),
                "confidence": round(max(0.0, min(1.0, confidence_score)), 3),
                "evidence_strength": _evidence_label(evidence_confidence),
            }
        )
        return base

    def build_horizon_context(
        self,
        *,
        current_band: int,
        deployed_bands: List[int],
        current_cell_id: int = 0,
        measured_capacity_fact: Optional[Mapping[str, Any]] = None,
        passive_dl_peak_fact: Optional[Mapping[str, Any]] = None,
        now_ts: float | None = None,
        horizons_s: Tuple[int, ...] = DEFAULT_HORIZON_SECONDS,
        blacklisted_bands: Optional[set] = None,
    ) -> dict:
        """Publish owner-stamped near-future prediction facts for self-learning.

        This is not an action recommendation.  It is a forecast surface that
        lets self-learning compare current-anchor expectation with likely
        alternatives over short horizons before self-learning generates its own
        candidates and TSAR applies the normal action gates.
        """
        now = float(now_ts or time.time())
        deployed: list[int] = []
        for raw in deployed_bands or []:
            try:
                band = int(raw)
            except (TypeError, ValueError):
                continue
            if band > 0 and band not in deployed:
                deployed.append(band)
        base: dict[str, Any] = {
            "schema": HORIZON_CONTEXT_SCHEMA,
            "owner": HORIZON_CONTEXT_OWNER,
            "advisory_only": True,
            "direct_modem_actuation": False,
            "candidate_generation_owner": "arch4.smart_loop.closed_loop",
            "generated_ts": now,
            "current_band": int(current_band or 0),
            "current_cell_id": int(current_cell_id or 0),
            "deployed_bands": deployed,
            "measured_capacity_fact": _capacity_prior_summary(measured_capacity_fact),
            "passive_dl_peak_fact": _passive_capacity_prior_summary(passive_dl_peak_fact),
            "horizons": {},
            "status": "present" if self.is_ready and deployed and int(current_band or 0) > 0 else "withheld",
            "reason": "",
        }
        if not self.is_ready:
            base["reason"] = "crystal_ball_history_empty"
            return base
        if int(current_band or 0) <= 0:
            base["reason"] = "missing_current_band"
            return base
        if not deployed:
            base["reason"] = "no_deployed_bands"
            return base

        blocked = blacklisted_bands or set()
        for raw_seconds in horizons_s:
            seconds = max(1, int(raw_seconds or 0))
            horizon_ts = now + seconds
            hour = int(datetime.fromtimestamp(horizon_ts).hour)
            day_type = _day_type_from_ts(horizon_ts)
            effective = self._effective_day_type if self._drift_detected else day_type
            current_slot = None
            if current_cell_id > 0:
                current_slot = self.get_cell_forecast(hour, day_type, int(current_band), int(current_cell_id))
            if current_slot is None:
                current_slot = self._aggregate_band_slot(effective, hour, int(current_band))
            current_prediction = _slot_prediction_dict(
                current_slot,
                band=int(current_band),
                cell_id=int(current_cell_id or 0),
            )
            ranked = self.get_recommendation(
                hour,
                day_type,
                deployed,
                blacklisted_bands=blocked,
            )
            best = ranked[0] if ranked else None
            best_alternate = next((rec for rec in ranked if rec.band != int(current_band)), None)
            evidence_confidence = min(
                int(current_prediction.get("confidence") or 0),
                int(best_alternate.confidence if best_alternate else best.confidence if best else 0),
            )
            base["horizons"][_horizon_label(seconds)] = {
                "horizon_s": seconds,
                "hour": hour,
                "day_type": day_type,
                "effective_day_type": effective,
                "current": current_prediction,
                "ranked_candidates": [_recommendation_dict(rec) for rec in ranked[:5]],
                "best_candidate": _recommendation_dict(best),
                "best_alternate": _recommendation_dict(best_alternate),
                "expected_delta": _expected_delta(current_prediction, best_alternate),
                "capacity_prior": _capacity_prior_summary(measured_capacity_fact),
                "passive_capacity_prior": _passive_capacity_prior_summary(passive_dl_peak_fact),
                "confidence": round(max(0.0, min(1.0, evidence_confidence / 5.0)), 3),
                "evidence_strength": _evidence_label(evidence_confidence),
                "sample_count": int(current_prediction.get("sample_count") or 0),
            }
        if not base["horizons"]:
            base["status"] = "withheld"
            base["reason"] = "no_horizons_built"
        return base

    def get_forecast_slot(
        self, day_type: str, hour: int, band: int, cell_id: int = 0,
    ) -> Optional[ForecastSlot]:
        """Slot access for diagnostics/GUI.

        If cell_id=0, returns band-level aggregate.  Otherwise returns
        the specific cell slot.
        """
        if cell_id == 0:
            return self._aggregate_band_slot(day_type, hour, band)
        return self._table.get(_slot_key(day_type, hour, band, cell_id))

    def get_cell_forecast(
        self, hour: int, day_type: str, band: int, cell_id: int,
    ) -> Optional[ForecastSlot]:
        """Cell-specific forecast: exact (day_type, hour, band, cell_id) slot.

        Use this for trigger decisions where the modem's current cell matters.
        Returns None if no data for this specific cell.
        """
        effective = self._effective_day_type if self._drift_detected else day_type
        return self._table.get(_slot_key(effective, hour, band, cell_id))

    def get_known_bands(self) -> set:
        """Return set of all bands that have any forecast data."""
        bands = set()
        for key in self._table:
            _, _, band, _cell_id = _parse_slot_key(key)
            bands.add(band)
        for key in self._external_band_hour_prior:
            try:
                bands.add(int(key.split(":", 1)[0]))
            except (TypeError, ValueError):
                continue
        return bands

    def get_24h_summary(
        self, day_type: str, band: int,
    ) -> Dict[int, ForecastSlot]:
        """Return all 24 hourly slots for a given day_type + band.

        Band-level aggregate across PCIs.  Only includes hours with data.
        For GUI / site report.
        """
        result: Dict[int, ForecastSlot] = {}
        for h in range(24):
            slot = self._aggregate_band_slot(day_type, h, band)
            if slot and slot.sample_count > 0:
                result[h] = slot
        return result

    def get_congestion_forecast(
        self, hour: int, day_type: str, band: int,
        cell_id: int = 0,
    ) -> Optional[float]:
        """Return predicted congestion % for a specific slot, or None if no data.

        If cell_id=0, returns band-level aggregate.  If cell_id specified, returns
        cell-specific prediction (higher precision for trigger decisions).
        """
        if cell_id > 0:
            effective = self._effective_day_type if self._drift_detected else day_type
            slot = self._table.get(_slot_key(effective, hour, band, cell_id))
        else:
            slot = self._aggregate_band_slot(day_type, hour, band)
        if slot and slot.confidence >= 1:
            return slot.avg_congestion
        return None

    # ── Diagnostic / status ───────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True if the crystal ball has any forecast data (from JSONL or persistence)."""
        return bool(self._table or self._external_band_hour_prior)

    @property
    def effective_day_type(self) -> str:
        return self._effective_day_type or _day_type_from_ts(time.time())

    def get_status(self) -> dict:
        """Diagnostic status for GUI / logging."""
        return {
            "ready": self.is_ready,
            "slots": len(self._table),
            "known_bands": sorted(self.get_known_bands()),
            "build_ts": self._build_ts,
            "build_files": self._build_files,
            "build_records": self._build_records,
            "last_hourly_update_ts": self._last_hourly_update_ts,
            "last_hourly_update_hour": self._last_hourly_update_hour,
            "effective_day_type": self.effective_day_type,
            "drift_detected": self._drift_detected,
            "drift_reason": self._drift_reason,
            "delayed_reward_projection_keys": sorted(self._delayed_reward_projection_keys),
            "evidence_scope": dict(self._evidence_scope),
            "evidence_quarantine_count": len(self._evidence_quarantine),
            "external_prior": {
                "present": bool(self._external_band_hour_prior),
                "band_hour_count": len(self._external_band_hour_prior),
                "source_count": len(self._external_prior_sources),
                "applied_at_ts": self._external_prior_applied_ts,
                "advisory_only": True,
                "live_truth_override": False,
                "evidence_scope": dict(self._external_prior_scope),
            },
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise for memory.json persistence."""
        return {
            "table": {k: v.to_dict() for k, v in self._table.items()},
            "build_ts": self._build_ts,
            "build_files": self._build_files,
            "build_records": self._build_records,
            "effective_day_type": self._effective_day_type,
            "drift_detected": self._drift_detected,
            "drift_reason": self._drift_reason,
            "delayed_reward_projection_keys": sorted(self._delayed_reward_projection_keys),
            "source_evidence_scope": dict(self._source_evidence_scope),
            "evidence_scope": dict(self._evidence_scope),
            "evidence_quarantine": list(self._evidence_quarantine[-128:]),
        }

    def from_dict(self, data: dict) -> None:
        """Restore from memory.json persistence."""
        if not isinstance(data, dict):
            return
        try:
            source_scope = EvidenceScope.from_mapping(
                dict(data.get("source_evidence_scope") or {})
            )
            output_scope = EvidenceScope.from_mapping(
                dict(data.get("evidence_scope") or {})
            )
            if not source_scope.same_physical_namespace(output_scope):
                raise ValueError("crystal_ball_persisted_scope_mismatch")
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=f"crystal_ball_persisted_scope_invalid:{exc}",
                row={"kind": "crystal_ball_snapshot", "state": dict(data)},
            )
            return
        self._source_evidence_scope = source_scope.as_dict()
        self._evidence_scope = output_scope.as_dict()
        self._evidence_quarantine = [
            dict(row)
            for row in data.get("evidence_quarantine", [])[-128:]
            if isinstance(row, Mapping)
        ] + self._evidence_quarantine
        self._evidence_quarantine = self._evidence_quarantine[-128:]
        raw_table = data.get("table")
        if isinstance(raw_table, dict):
            self._table = {}
            for k, v in raw_table.items():
                if isinstance(v, dict):
                    self._table[k] = ForecastSlot.from_dict(v)
        self._build_ts = float(data.get("build_ts", 0.0) or 0.0)
        self._build_files = int(data.get("build_files", 0) or 0)
        self._build_records = int(data.get("build_records", 0) or 0)
        self._effective_day_type = str(data.get("effective_day_type", "") or "")
        self._drift_detected = bool(data.get("drift_detected", False))
        self._drift_reason = str(data.get("drift_reason", "") or "")
        self._delayed_reward_projection_keys = {
            str(key)
            for key in data.get("delayed_reward_projection_keys", [])
            if str(key or "")
        }
        if self._table:
            log.info(
                "CrystalBall: restored %d slots from persistence (%d bands)",
                len(self._table), len(self.get_known_bands()),
            )

    # ── Merge: combine persisted + fresh JSONL ────────────────────────────

    def merge_persisted_and_fresh(self, output_dir: str | Path, *, history_logger=None) -> int:
        """Load from persistence first, then overlay with fresh JSONL data.

        If *history_logger* is provided it is forwarded to
        :meth:`build_from_jsonl` which uses it to gate today's JSONL
        file (close before read, reopen after).

        Fresh JSONL is the truth owner for any slot it covers. Persistence
        only keeps old slots that have no fresh scan coverage. This keeps the
        cold-start merge idempotent across restarts; otherwise the same JSONL
        evidence is repeatedly blended into already-merged memory.

        Returns total records processed from JSONL.
        """
        persisted_table = {
            key: ForecastSlot.from_dict(slot.to_dict())
            for key, slot in self._table.items()
        }
        persisted_source_scope = dict(self._source_evidence_scope)
        persisted_output_scope = dict(self._evidence_scope)
        persisted_quarantine = list(self._evidence_quarantine)

        fresh = CrystalBall()
        records = fresh.build_from_jsonl(output_dir, history_logger=history_logger)
        self._table = dict(fresh._table)
        self._build_ts = fresh._build_ts
        self._build_files = fresh._build_files
        self._build_records = fresh._build_records
        self._effective_day_type = fresh._effective_day_type
        self._drift_detected = fresh._drift_detected
        self._drift_reason = fresh._drift_reason
        self._source_evidence_scope = dict(fresh._source_evidence_scope)
        self._evidence_scope = dict(fresh._evidence_scope)
        self._evidence_quarantine = (
            persisted_quarantine + list(fresh._evidence_quarantine)
        )[-128:]

        if records <= 0:
            self._table = persisted_table
            self._source_evidence_scope = persisted_source_scope
            self._evidence_scope = persisted_output_scope
            return records

        if not persisted_table:
            return records  # nothing to merge, JSONL is authoritative

        try:
            persisted_scope = EvidenceScope.from_mapping(persisted_source_scope)
            fresh_scope = EvidenceScope.from_mapping(self._source_evidence_scope)
            scopes_match = persisted_scope.same_physical_namespace(fresh_scope)
        except (TypeError, ValueError):
            scopes_match = False
        if not scopes_match:
            self._quarantine_evidence(
                reason="crystal_ball_persisted_foreign_to_fresh",
                row={
                    "source_evidence_scope": persisted_source_scope,
                    "evidence_scope": persisted_output_scope,
                    "table": {
                        key: slot.to_dict() for key, slot in persisted_table.items()
                    },
                },
            )
            return records

        # Fresh JSONL owns overlapping slots. Persisted-only slots are kept
        # with one confidence step of decay, because the active scan window did
        # not see them.
        for key, old_slot in persisted_table.items():
            old_slot.sample_count = _normalise_sample_count(old_slot.sample_count)
            old_slot.confidence = _normalise_confidence(old_slot.confidence, old_slot.sample_count)
            if old_slot.sample_count == 0:
                continue
            fresh_slot = self._table.get(key)
            if fresh_slot is None or fresh_slot.sample_count == 0:
                old_slot.confidence = max(0, old_slot.confidence - 1)
                self._table[key] = old_slot

        self._refresh_evidence_scope(evidence_count=records)

        log.info(
            "CrystalBall: merged %d persisted slots with %d fresh JSONL records -> %d total slots",
            len(persisted_table), records, len(self._table),
        )
        return records
