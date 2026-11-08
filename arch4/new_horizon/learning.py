"""Build forecasts for an exact context and track adviser feedback."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade

from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from arch4.action_origin import is_brain_decision_origin
from arch4.core_contracts.command_targets import core_command_target_from_payload
from arch4.core_contracts.quality_model import NR_QUALITY_MODEL, LTE_QUALITY_MODEL, require_quality_model
from arch4.new_horizon.contracts import (
    AdvisoryOpportunity,
    DECISION_LEDGER_KIND,
    DECISION_LEDGER_SCHEMA,
    DECISION_TERMINAL_SCHEMA,
    IntelligenceContext,
    PROPOSAL_RESULT_EVIDENCE_SCHEMA,
    PRIOR_DECISION_LEDGER_CONTRACTS,
    RefereeProof,
)
from arch4.core_contracts.intelligence_proposals import (
    GRADE_DIMENSIONS,
    LEARNING_GRADE_DIMENSIONS,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.ca_evidence import forecast_ca_support
from arch4.new_horizon.service_evidence import target_service_evidence
from arch4.core_contracts.command_targets import CorePBandTarget, CoreBandSetTarget
from arch4.new_horizon.cell_presence import serving_cell_key, hourly_cell_outlook
from arch4.new_horizon.evidence_weighting import EvidenceWeights, evidence_weights
from arch4.new_horizon.decision_history import (
    DecisionLifecycleHistoryOwner,
    DecisionLifecycleHistorySnapshot,
    legacy_deadline_terminal_is_superseded,
    forecast_result_attribution,
    validated_contribution,
)
from arch4.new_horizon.learning_context import (
    require_learning_context, build_learning_context, learning_context_key,
)
from arch4.new_horizon.hourly_aq import fixed_hour, nr_evidence_condition
from arch4.new_horizon.proof import RefereeAssessmentEvidence, RefereeAssessmentHistoryOwner, RefereeAssessmentHistorySnapshot, context_continuity_key, context_learning_components, context_learning_key, context_radio_identity, current_context_target, referee_history_vector
from arch4.new_horizon.service_quality import normalized_referee_service_value


ADVISER_CREDIT_SCHEMA = "NewHorizonAdviserCreditSnapshot.v12"
_DECISION_GRADE_DIMENSIONS = frozenset(GRADE_DIMENSIONS)


def empty_adviser_credit() -> dict[str, Any]:
    """Return an empty RAM projection of adviser feedback."""

    return {
        "schema": ADVISER_CREDIT_SCHEMA,
        "rows": {},
        "admitted_feedback_proof_ids": [],
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def adviser_credit_factor(
    checkpoint: Mapping[str, Any],
    *,
    adviser: str,
    dimension: str,
    learning_context_key: str,
) -> float:
    """Return the bounded trust learned for one adviser and dimension."""

    if checkpoint.get("schema") != ADVISER_CREDIT_SCHEMA:
        raise ValueError("new_horizon_adviser_credit_schema_invalid")
    if type(adviser) is not str or not adviser.strip():
        raise ValueError("new_horizon_adviser_credit_name_invalid")
    if dimension not in GRADE_DIMENSIONS:
        raise ValueError("new_horizon_adviser_credit_dimension_invalid")
    if type(learning_context_key) is not str or not learning_context_key:
        raise ValueError("new_horizon_adviser_credit_context_invalid")
    row = dict(
        dict(checkpoint.get("rows") or {}).get(
            adviser + ":" + dimension + ":" + learning_context_key
        )
        or {}
    )
    if int(row.get("count", 0) or 0) <= 0:
        return 1.0
    factor = max(
        0.25,
        min(
            1.0,
            (float(row.get("mean_credit_grade", 0.0)) + 3.0) / 6.0,
        ),
    )
    mass = _number(row.get("effective_count"))
    if mass is None or mass < 0:
        raise ValueError("new_horizon_adviser_credit_effective_count_required")
    return 1.0 - min(1.0, mass) * (1.0 - factor)


_FORECAST_ERROR_SCALES = {"aq": 6.0}


def forecast_accuracy(
    checkpoint: Mapping[str, Any],
    *,
    learning_context_key: str,
) -> tuple[float | None, int]:
    """Return measured forecast accuracy for one learning context."""

    if checkpoint.get("schema") != ADVISER_CREDIT_SCHEMA:
        raise ValueError("new_horizon_adviser_credit_schema_invalid")
    if type(learning_context_key) is not str or not learning_context_key:
        raise ValueError("new_horizon_forecast_accuracy_context_invalid")
    normalized_errors: list[float] = []
    assessment_count = 0
    rows = dict(checkpoint.get("rows") or {})
    for dimension in LEARNING_GRADE_DIMENSIONS:
        row = dict(
            rows.get("crystal_ball_error:" + dimension + ":" + learning_context_key)
            or {}
        )
        count = int(row.get("count", 0) or 0)
        means = row.get("mean_absolute_error")
        if count <= 0 or not isinstance(means, Mapping):
            continue
        mass = _number(row.get("effective_count"))
        if mass is None or mass < 0:
            raise ValueError("new_horizon_forecast_error_effective_count_required")
        assessment_count = max(assessment_count, count)
        for metric, error in means.items():
            parsed = _number(error)
            scale = _FORECAST_ERROR_SCALES.get(str(metric))
            if parsed is not None and scale is not None:
                normalized_errors.append(min(1.0, parsed / scale) * min(1.0, mass))
    if not normalized_errors:
        return None, 0
    return (
        max(0.25, min(1.0, 1.0 - sum(normalized_errors) / len(normalized_errors))),
        assessment_count,
    )


def _forecast_feedback_key(target_signature: str, time_bucket: str,
                           condition: tuple[str, ...] | None, *, quality_model: str) -> str:
    """Error follows the offered target/time evidence, not its starting cell.

    Continuity is already isolated by Crystal Ball's canonical binding. This
    key is only for forecast error; it never merges physical result identities.
    """
    if not target_signature or not time_bucket:
        raise ValueError("new_horizon_forecast_feedback_identity_required")
    key = "forecast-target:" + target_signature + "|" + time_bucket
    return key if require_quality_model(quality_model) == LTE_QUALITY_MODEL else key + "|nr-condition:" + repr(condition)


def _weighted_observed_mean(
    rows: Iterable[Mapping[str, Any]],
    field: str,
    weights: EvidenceWeights,
) -> float | None:
    rows = tuple(rows)
    masses = tuple(float(row["metric_weights"].get(field, 0.0)) for row in rows)
    return EvidenceWeights(masses, math.fsum(masses)).mean(
        _number(dict(row.get("observed_vector") or {}).get(field)) for row in rows)


def _hourly_weights(rows: Iterable[Mapping[str, Any]]) -> EvidenceWeights:
    masses = tuple(float(row["effective_blocks"]) for row in rows)
    mass = math.fsum(masses)
    return EvidenceWeights(tuple(value / mass for value in masses) if mass else (), mass)


@dataclass(frozen=True, slots=True)
class ForecastResultContext:
    """Observed Cell and PCI behind a commandable forecast target."""

    match_tier: str
    expected_eci_points: float | None
    expected_latency_ms: float | None
    expected_jitter_ms: float | None
    expected_cqb: float | None
    expected_internet_availability_pct: float | None
    confidence: float
    evidence_assessments: int
    evidence_refs: tuple[str, ...]
    accuracy_factor: float | None
    accuracy_assessments: int
    evidence_cell_id: int | None = None
    evidence_pci: int | None = None
    evidence_nr_cell_id: int | None = None
    evidence_nr_pci: int | None = None
    result_scope_key: str = ""
    evidence_weight: float = 1.0
    effective_evidence: float = 0.0
    expected_aq: float | None = None
    hourly_evidence: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in ("evidence_weight", "effective_evidence"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("new_horizon_forecast_context_weight_invalid:" + name)

    @property
    def status(self) -> str:
        return "predicted"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "match_tier": self.match_tier,
            "expected_eci_points": self.expected_eci_points,
            "expected_latency_ms": self.expected_latency_ms,
            "expected_jitter_ms": self.expected_jitter_ms,
            "expected_cqb": self.expected_cqb,
            "expected_internet_availability_pct": (
                self.expected_internet_availability_pct
            ),
            "confidence": self.confidence,
            "evidence_assessments": self.evidence_assessments,
            "evidence_refs": list(self.evidence_refs),
            "accuracy_factor": self.accuracy_factor,
            "accuracy_assessments": self.accuracy_assessments,
            "evidence_cell_id": self.evidence_cell_id,
            "evidence_pci": self.evidence_pci,
            "evidence_nr_cell_id": self.evidence_nr_cell_id,
            "evidence_nr_pci": self.evidence_nr_pci,
            "result_scope_key": self.result_scope_key,
            "evidence_weight": self.evidence_weight,
            "effective_evidence": self.effective_evidence,
            "expected_aq": self.expected_aq,
            "hourly_evidence": list(self.hourly_evidence),
        }


@dataclass(frozen=True, slots=True)
class CandidateForecast:
    opportunity_id: str
    target_signature: str
    forecast_id: str
    status: str
    match_tier: str
    expected_eci_points: float | None
    expected_latency_ms: float | None
    expected_jitter_ms: float | None
    expected_cqb: float | None
    expected_internet_availability_pct: float | None
    confidence: float
    # AQ evidence uncertainty only. Public confidence also includes cell coverage.
    uncertainty: float
    evidence_assessments: int
    evidence_refs: tuple[str, ...]
    accuracy_factor: float | None = None
    accuracy_assessments: int = 0
    evidence_cell_id: int | None = None
    evidence_pci: int | None = None
    evidence_nr_cell_id: int | None = None
    evidence_nr_pci: int | None = None
    nr_condition: tuple[str, ...] | None = None
    nr_device: bool = False
    result_contexts: tuple[ForecastResultContext, ...] = ()
    effective_evidence: float = 0.0
    prediction_hour: int | None = None
    prediction_at: float | None = None
    next_hour: CandidateForecast | None = None
    cell_presence: dict[str, Any] | None = None
    service_evidence: dict[str, Any] | None = None

    @property
    def cell_evidence(self) -> dict[str, Any]:
        """Cell outlook first, followed by this target's physical AQ evidence."""
        outlook = hourly_cell_outlook(self.cell_presence)
        if (self.prediction_hour is not None and outlook["hour"] is not None
                and self.prediction_hour != outlook["hour"]):
            raise ValueError("new_horizon_forecast_cell_hour_mismatch")
        supported = set()
        for result in self.result_contexts:
            keys = {serving_cell_key(row["identity"]) for row in result.hourly_evidence}
            if len(keys) > 1:
                raise ValueError("new_horizon_forecast_mixed_result_cells")
            if result.expected_aq is not None:
                supported.update(key for key in keys if key is not None)
        shares = {tuple(row["cell_key"]): row["share"] for row in outlook["cells"]}
        coverage = (None if outlook["state"] == "missing" or not supported
                    else min(1., math.fsum(shares.get(key, 0.) for key in supported)))
        # Saturate each cell separately. Two rare cells cannot pool their
        # residence to pretend either one is commonly reached.
        support = None
        if coverage is not None:
            support = EvidenceWeights(
                tuple(item.evidence_weight for item in self.result_contexts), self.effective_evidence,
            ).mean(
                min(1.0, shares.get(serving_cell_key(item.hourly_evidence[0]["identity"]), 0.0) / 0.30)
                if item.expected_aq is not None and item.hourly_evidence else None
                for item in self.result_contexts
            )
        state = ("no_hourly_cell_evidence" if outlook["state"] == "missing"
                 else "no_band_cell_evidence" if not supported
                 else "observed_cell_support" if coverage
                 else "no_observed_overlap")
        return dict(state=state, outlook=outlook,
                    supported_cells=[list(key) for key in sorted(supported)],
                    observed_support=coverage, decision_support=support)

    @property
    def quality_confidence(self) -> float:
        """Confidence in measured outcomes, separate from hourly cell residence."""
        if self.status != "predicted":
            return 0.0
        return 1.0 - self.uncertainty

    def expected_downside(self, reference_service_value: float | None, *, nr_service_reference: bool = False) -> float | None:
        """Expected loss below the reference, in AQ/3 units; not total variation."""
        if type(nr_service_reference) is not bool:
            raise TypeError("new_horizon_downside_reference_class_bool_required")
        if reference_service_value is None or self.status != "predicted":
            return None
        if not math.isfinite(reference_service_value) or not valid_intelligence_grade(reference_service_value * 3.0,
                dimension="service_outcome" if nr_service_reference else "component"):
            raise ValueError("new_horizon_downside_reference_invalid")
        return EvidenceWeights(
            tuple(item.evidence_weight for item in self.result_contexts), self.effective_evidence,
        ).mean(
            max(0.0, reference_service_value - value)
            if (value := self._context_service(item)) is not None else None
            for item in self.result_contexts
        )

    @property
    def expected_service_value(self) -> float | None:
        """Mean of separate physical outcomes, not a synthetic average cell."""
        if self.status != "predicted":
            return None
        if not self.result_contexts:
            raise ValueError("new_horizon_forecast_result_contexts_required")
        return self._service_summary(self.result_contexts)[0]

    def _service_summary(self, contexts: tuple[ForecastResultContext, ...]) -> tuple[float | None, float]:
        weights = EvidenceWeights(tuple(item.evidence_weight for item in contexts), self.effective_evidence)
        mean = weights.mean(self._context_service(item) for item in contexts)
        if mean is None:
            return None, 0.0
        variance = weights.mean(
            (value - mean) ** 2 if (value := self._context_service(item)) is not None else None
            for item in contexts
        )
        return mean, min(1.0, math.sqrt(variance or 0.0))

    @staticmethod
    def _context_service(item: ForecastResultContext) -> float | None:
        return None if item.expected_aq is None else item.expected_aq / 3.0

    @property
    def service_spread(self) -> float:
        if self.status != "predicted":
            return 0.0
        return self._service_summary(self.result_contexts)[1]

    def __post_init__(self) -> None:
        if self.prediction_hour is not None and (
            type(self.prediction_hour) is not int or not 0 <= self.prediction_hour < 24
        ):
            raise ValueError("new_horizon_forecast_hour_invalid")
        if self.prediction_at is not None and (
            type(self.prediction_at) not in {int, float}
            or not math.isfinite(self.prediction_at)
        ):
            raise ValueError("new_horizon_forecast_time_invalid")
        if self.next_hour is not None and (
            not isinstance(self.next_hour, CandidateForecast)
            or self.next_hour.next_hour is not None
            or self.prediction_at is None
            or self.next_hour.prediction_at != self.prediction_at + 3600.0
            or self.next_hour.opportunity_id != self.opportunity_id
            or self.next_hour.target_signature != self.target_signature
        ):
            raise ValueError("new_horizon_next_hour_forecast_invalid")
        if (
            type(self.effective_evidence) not in {int, float}
            or not math.isfinite(self.effective_evidence)
            or self.effective_evidence < 0
        ):
            raise ValueError("new_horizon_forecast_effective_evidence_invalid")
        if self.status not in {"predicted", "unknown"}:
            raise ValueError("new_horizon_candidate_forecast_status_invalid")
        if self.match_tier not in {
            "exact",
            "related_context",
            "unavailable",
        }:
            raise ValueError("new_horizon_candidate_forecast_tier_invalid")
        if (self.status == "predicted") is (self.match_tier == "unavailable"):
            raise ValueError("new_horizon_candidate_forecast_tier_mismatch")
        if not self.opportunity_id or not self.target_signature:
            raise ValueError("new_horizon_candidate_forecast_identity_required")
        if not self.forecast_id:
            raise ValueError("new_horizon_candidate_forecast_id_required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("new_horizon_candidate_forecast_confidence_invalid")
        if not 0.0 <= self.uncertainty <= 1.0:
            raise ValueError("new_horizon_candidate_forecast_uncertainty_invalid")
        if self.evidence_assessments < 0:
            raise ValueError("new_horizon_candidate_forecast_count_invalid")
        if self.accuracy_factor is not None and not (
            type(self.accuracy_factor) in {int, float}
            and math.isfinite(float(self.accuracy_factor))
            and 0.0 <= float(self.accuracy_factor) <= 1.0
        ):
            raise ValueError("new_horizon_candidate_forecast_accuracy_invalid")
        if type(self.accuracy_assessments) is not int or self.accuracy_assessments < 0:
            raise ValueError("new_horizon_candidate_forecast_accuracy_count_invalid")
        if (self.accuracy_factor is None) != (self.accuracy_assessments == 0):
            raise ValueError("new_horizon_candidate_forecast_accuracy_state_invalid")
        for name in (
            "expected_eci_points",
            "expected_latency_ms",
            "expected_jitter_ms",
            "expected_cqb",
            "expected_internet_availability_pct",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or type(value) not in {int, float}
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(
                    "new_horizon_candidate_forecast_metric_invalid:" + name
                )
        availability = self.expected_internet_availability_pct
        if availability is not None and float(availability) > 100.0:
            raise ValueError("new_horizon_candidate_forecast_availability_invalid")
        if self.expected_cqb is not None and float(self.expected_cqb) > 1.0:
            raise ValueError("new_horizon_candidate_forecast_cqb_invalid")
        for family, cell_id, pci in (
            ("lte", self.evidence_cell_id, self.evidence_pci),
            ("nr", self.evidence_nr_cell_id, self.evidence_nr_pci),
        ):
            if (cell_id is None) != (pci is None):
                raise ValueError(
                    "new_horizon_candidate_forecast_identity_pair_invalid:" + family
                )
            if cell_id is not None and (
                type(cell_id) is not int
                or cell_id <= 0
                or type(pci) is not int
                or pci < 0
            ):
                raise ValueError(
                    "new_horizon_candidate_forecast_identity_invalid:" + family
                )
        if any(
            not isinstance(item, ForecastResultContext) for item in self.result_contexts
        ):
            raise TypeError("new_horizon_candidate_forecast_result_context_invalid")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "target_signature": self.target_signature,
            "forecast_id": self.forecast_id,
            "nr_condition": None if self.nr_condition is None else list(self.nr_condition),
            "nr_device": self.nr_device,
            "expected_aq": None if self.expected_service_value is None else self.expected_service_value * 3.0,
            "status": self.status,
            "match_tier": self.match_tier,
            "expected_eci_points": self.expected_eci_points,
            "expected_latency_ms": self.expected_latency_ms,
            "expected_jitter_ms": self.expected_jitter_ms,
            "expected_cqb": self.expected_cqb,
            "expected_internet_availability_pct": (
                self.expected_internet_availability_pct
            ),
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "evidence_assessments": self.evidence_assessments,
            "evidence_refs": list(self.evidence_refs),
            "accuracy_factor": self.accuracy_factor,
            "accuracy_assessments": self.accuracy_assessments,
            "evidence_cell_id": self.evidence_cell_id,
            "evidence_pci": self.evidence_pci,
            "evidence_nr_cell_id": self.evidence_nr_cell_id,
            "evidence_nr_pci": self.evidence_nr_pci,
            "result_contexts": [item.to_mapping() for item in self.result_contexts],
            "effective_evidence": self.effective_evidence,
            "prediction_hour": self.prediction_hour,
            "prediction_at": self.prediction_at,
            "next_hour": self.next_hour.to_mapping() if self.next_hour is not None else None,
            "cell_presence": self.cell_presence,
            "cell_evidence": self.cell_evidence,
            "quality_confidence": self.quality_confidence,
            "service_evidence": self.service_evidence,
        }


def _forecast_identity_pair(
    identity: object,
    *,
    nr_target: bool,
) -> tuple[int, int] | None:
    if not isinstance(identity, Mapping):
        return None
    cell_key, pci_key = ("nr_cell_id", "nr_pci") if nr_target else ("cell_id", "pci")
    cell_id = identity.get(cell_key)
    pci = identity.get(pci_key)
    if type(cell_id) is int and cell_id > 0 and type(pci) is int and pci >= 0:
        return cell_id, pci
    return None


def _forecast_identity_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], frozenset[tuple[int, int]]]:
    """Index Referee identities by result context and time tier."""

    collected: dict[tuple[str, str, str], set[tuple[int, int]]] = {}
    for row in rows:
        target_signature = str(row.get("target_signature") or "")
        learning = row.get("learning_context")
        identity = row.get("result_identity")
        if not target_signature or not isinstance(learning, Mapping):
            continue
        pair = _forecast_identity_pair(
            identity,
            nr_target=target_signature.startswith("NR=n"),
        )
        if pair is None or not isinstance(identity, Mapping):
            continue
        scope_key = str(identity.get("scope_key") or row.get("result_scope_key") or "")
        time_bucket = str(learning.get("time_bucket") or "")
        if scope_key and time_bucket:
            collected.setdefault((scope_key, time_bucket, target_signature), set()).add(
                pair
            )
    return {key: frozenset(values) for key, values in collected.items()}


def _forecast_row_identity(
    row: Mapping[str, Any],
    *,
    target_signature: str,
    identity_index: Mapping[tuple[str, str, str], frozenset[tuple[int, int]]],
) -> tuple[int, int] | None:
    identity = row.get("result_identity")
    pair = _forecast_identity_pair(
        identity,
        nr_target=target_signature.startswith("NR=n"),
    )
    if pair is not None:
        return pair
    learning = row.get("learning_context")
    result_scope_key = str(
        row.get("result_scope_key")
        or (identity.get("scope_key") if isinstance(identity, Mapping) else "")
        or ""
    )
    time_bucket = str(
        learning.get("time_bucket") if isinstance(learning, Mapping) else ""
    )
    resolved = (
        identity_index.get(
            (result_scope_key, time_bucket, target_signature),
            frozenset(),
        )
        if result_scope_key and time_bucket
        else frozenset()
    )
    return next(iter(resolved)) if len(resolved) == 1 else None


def _forecast_result_contexts(
    rows: Iterable[Mapping[str, Any]],
    *,
    target_signature: str,
    requested_pool: tuple[int, ...],
    identity_index: Mapping[tuple[str, str, str], frozenset[tuple[int, int]]],
    match_tier: str,
    tier_factor: float,
    credit_factor: float,
    accuracy_factor: float | None,
    accuracy_assessments: int,
    now: float,
) -> tuple[ForecastResultContext, ...]:
    """Keep each proven physical result behind one commandable target."""

    rows = tuple(rows)
    all_weights = _hourly_weights(rows)
    mass_by_id = dict(
        zip(
            (str(row["assessment_id"]) for row in rows),
            all_weights.weights,
            strict=True,
        )
    )
    grouped: dict[tuple[tuple[Any, ...], tuple[int, int] | None], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (
                RefereeAssessmentHistorySnapshot.physical_aq_context_key(row),
                _forecast_row_identity(
                    row,
                    target_signature=target_signature,
                    identity_index=identity_index,
                ),
            ),
            [],
        ).append(row)
    nr_target = target_signature.startswith("NR=n")
    contexts: list[ForecastResultContext] = []
    for (physical_key, identity), context_rows in sorted(
        grouped.items(),
        key=lambda item: repr(item[0]),
    ):
        source_scopes = {str(row["result_scope_key"]) for row in context_rows}
        scope = (next(iter(source_scopes)) if len(source_scopes) == 1 else
                 "aq-physical:" + hashlib.sha256(json.dumps(
                     physical_key, separators=(",", ":"), ensure_ascii=True,
                 ).encode("utf-8")).hexdigest())
        count = len(context_rows)
        weights = _hourly_weights(context_rows)
        confidence = (
            min(0.9, weights.effective_count / 8.0)
            * credit_factor
            * tier_factor
            * (accuracy_factor if accuracy_factor is not None else 1.0)
        )
        cell_id, pci = identity if identity is not None else (None, None)
        contexts.append(
            ForecastResultContext(
                match_tier=match_tier,
                expected_eci_points=_weighted_observed_mean(
                    context_rows, "eci_points", weights
                ),
                expected_latency_ms=_weighted_observed_mean(
                    context_rows, "latency_ms", weights
                ),
                expected_jitter_ms=_weighted_observed_mean(
                    context_rows, "jitter_ms", weights
                ),
                expected_cqb=_weighted_observed_mean(context_rows, "cqb_cell", weights),
                expected_internet_availability_pct=_weighted_observed_mean(
                    context_rows, "internet_availability_pct", weights
                ),
                confidence=confidence,
                evidence_assessments=count,
                evidence_refs=tuple(str(row["assessment_id"]) for row in context_rows),
                accuracy_factor=accuracy_factor,
                accuracy_assessments=accuracy_assessments,
                evidence_cell_id=None if nr_target else cell_id,
                evidence_pci=None if nr_target else pci,
                evidence_nr_cell_id=cell_id if nr_target else None,
                evidence_nr_pci=pci if nr_target else None,
                result_scope_key=scope,
                evidence_weight=math.fsum(
                    mass_by_id[str(row["assessment_id"])] for row in context_rows
                ),
                effective_evidence=weights.effective_count,
                expected_aq=weights.mean(float(row["aq"]) for row in context_rows),
                hourly_evidence=tuple({
                    "hour": row["local_hour"], "aq": row["aq"],
                    "coverage_s": row["coverage_s"], "effective_blocks": row["effective_blocks"],
                    "first_at": row["first_at"], "last_at": row["last_at"],
                    "hour_refs": row["hour_refs"], "identity": row["result_identity"],
                    "ca_evidence": forecast_ca_support(row["observed_vector"], requested_pool),
                    "prediction_exclusions": row.get("prediction_exclusions", []),
                } for row in context_rows),
            )
        )
    return tuple(contexts)


def _cell_weighted_contexts(
    contexts: tuple[ForecastResultContext, ...], pattern: Mapping[str, Any],
) -> tuple[tuple[ForecastResultContext, ...], float]:
    """Use global presence, not the number of AQ rows for a particular band.

Unknown cells keep their missing share: this lowers confidence, not their AQ.
Several PCI/channel outcomes for one cell divide that cell's share once.
"""
    shares = {tuple(row["cell_key"]): row["share"] for row in pattern["cells"]}
    if not shares or not contexts:
        # Absence is unproven, not full coverage of this hour's cells.
        return contexts, 0.0
    cells = [serving_cell_key(item.hourly_evidence[0]["identity"]) for item in contexts]
    totals = {cell: math.fsum(item.evidence_weight for item, key in zip(contexts, cells, strict=True)
                             if key == cell) for cell in cells}
    coverage = min(1.0, math.fsum(shares.get(cell, 0.0) for cell in set(cells)))
    if not coverage:
        # AQ exists, but no observed current-hour cell pattern supports it.
        return contexts, 0.0
    return tuple(replace(item, evidence_weight=shares.get(cell, 0.0) * item.evidence_weight / totals[cell])
                 for item, cell in zip(contexts, cells, strict=True)), coverage


def _candidate_forecast(
    *,
    opportunity: AdvisoryOpportunity,
    target_signature: str,
    requested_pool: tuple[int, ...],
    continuity_key: str,
    current_identity: Mapping[str, Any],
    current_learning: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    identity_index: Mapping[tuple[str, str, str], frozenset[tuple[int, int]]],
    match_tier: str,
    tier_factor: float,
    credit_factor: float,
    accuracy_factor: float | None,
    accuracy_assessments: int,
    now: float,
    prediction_at: float | None = None,
    cell_presence: Mapping[str, Any] | None = None,
) -> CandidateForecast:
    expected_hour = int(require_learning_context(current_learning)["time_bucket"])
    if any(row["local_hour"] != expected_hour for row in rows):
        raise ValueError("new_horizon_forecast_mixed_clock_hours")
    condition = nr_evidence_condition(current_identity)
    if any(nr_evidence_condition(row["result_identity"]) != condition for row in rows):
        raise ValueError("new_horizon_forecast_mixed_nr_conditions")
    refs = tuple(str(row["assessment_id"]) for row in rows)
    result_contexts = _forecast_result_contexts(
        rows,
        target_signature=target_signature,
        requested_pool=requested_pool,
        identity_index=identity_index,
        match_tier=match_tier,
        tier_factor=tier_factor,
        credit_factor=credit_factor,
        accuracy_factor=accuracy_factor,
        accuracy_assessments=accuracy_assessments,
        now=now,
    )
    pattern_coverage = 1.0
    if cell_presence is not None:
        result_contexts, pattern_coverage = _cell_weighted_contexts(result_contexts, cell_presence)
    sole_context = result_contexts[0] if len(result_contexts) == 1 else None
    forecast_id = (
        "crystal-ball:"
        + hashlib.sha256(
            json.dumps(
                {
                    "continuity": continuity_key,
                    "opportunity": opportunity.opportunity_id,
                    "target": target_signature,
                    "context": current_learning,
                    "match_tier": match_tier,
                    "evidence": refs,
                    "age_minute": int(now // 60),
                    "prediction_at": prediction_at,
                    "cell_presence": cell_presence,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
    )
    count = len(rows)
    weights = _hourly_weights(rows)
    quality_confidence = (
        min(0.9, weights.effective_count / 8.0)
        * credit_factor
        * tier_factor
        * (accuracy_factor if accuracy_factor is not None else 1.0)
    )
    outcome_weights = EvidenceWeights(tuple(item.evidence_weight for item in result_contexts),
                                      weights.effective_count)
    return CandidateForecast(
        opportunity_id=opportunity.opportunity_id,
        target_signature=target_signature,
        forecast_id=forecast_id,
        nr_condition=nr_evidence_condition(current_identity),
        status="predicted" if count else "unknown",
        match_tier=match_tier,
        expected_eci_points=outcome_weights.mean(item.expected_eci_points for item in result_contexts),
        expected_latency_ms=outcome_weights.mean(item.expected_latency_ms for item in result_contexts),
        expected_jitter_ms=outcome_weights.mean(item.expected_jitter_ms for item in result_contexts),
        expected_cqb=outcome_weights.mean(item.expected_cqb for item in result_contexts),
        expected_internet_availability_pct=outcome_weights.mean(
            item.expected_internet_availability_pct for item in result_contexts),
        confidence=quality_confidence * pattern_coverage,
        uncertainty=1.0 - quality_confidence,
        evidence_assessments=count,
        evidence_refs=refs,
        accuracy_factor=accuracy_factor,
        accuracy_assessments=accuracy_assessments,
        evidence_cell_id=(
            sole_context.evidence_cell_id if sole_context is not None else None
        ),
        evidence_pci=sole_context.evidence_pci if sole_context is not None else None,
        evidence_nr_cell_id=(
            sole_context.evidence_nr_cell_id if sole_context is not None else None
        ),
        evidence_nr_pci=(
            sole_context.evidence_nr_pci if sole_context is not None else None
        ),
        result_contexts=result_contexts,
        effective_evidence=weights.effective_count,
        prediction_hour=int(current_learning["time_bucket"]),
        prediction_at=prediction_at if prediction_at is not None else fixed_hour(now, timezone="local")[0],
        cell_presence=None if cell_presence is None else {**cell_presence, "candidate_covered_share": pattern_coverage},
    )


class CrystalBall:
    """Make forecasts only. Brain still makes the final decision."""

    def __init__(
        self,
        history: AdvisoryHistory,
        *,
        assessment_history: RefereeAssessmentHistoryOwner,
        decision_history: DecisionLifecycleHistoryOwner,
    ) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_crystal_ball_history_required")
        self._history = history
        if not isinstance(assessment_history, RefereeAssessmentHistoryOwner):
            raise TypeError("new_horizon_crystal_ball_referee_history_owner_required")
        if assessment_history.history is not history:
            raise ValueError("new_horizon_crystal_ball_referee_history_store_mismatch")
        self._assessment_history = assessment_history
        if not isinstance(decision_history, DecisionLifecycleHistoryOwner):
            raise TypeError("new_horizon_crystal_ball_decision_history_owner_required")
        if decision_history.history is not history:
            raise ValueError("new_horizon_crystal_ball_decision_history_store_mismatch")
        self._decision_history = decision_history
        self._continuity_key = ""
        self._proof_rows: dict[str, dict[str, Any]] = {}
        self._decision_rows: dict[str, dict[str, Any]] = {}
        self._prior_decision_rows: dict[str, dict[str, Any]] = {}
        self._external_learning_rows: dict[str, dict[str, Any]] = {}
        self._completion_proofs: dict[str, dict[str, Any]] = {}
        self._adviser_credit: dict[str, Any] = empty_adviser_credit()
        self._assessment_snapshot: RefereeAssessmentHistorySnapshot | None = None
        self._decision_snapshot: DecisionLifecycleHistorySnapshot | None = None
        self._source_population_id = ""
        self._evaluation_at = 0.0

    @property
    def source_population_id(self) -> str:
        """Return the canonical Referee population used for forecasts."""

        return self._source_population_id

    @property
    def source_assessment_count(self) -> int:
        """Return the canonical assessment count used for forecasts."""

        snapshot = self._assessment_snapshot
        return snapshot.source_assessment_count if snapshot is not None else 0

    @property
    def adviser_credit(self) -> Mapping[str, Any]:
        """Expose the RAM-only adviser projection to the decision coordinator."""

        return self._adviser_credit

    def _binding_state(self) -> tuple[Any, ...]:
        return (
            self._continuity_key,
            self._proof_rows,
            self._decision_rows,
            self._prior_decision_rows,
            self._external_learning_rows,
            self._completion_proofs,
            self._adviser_credit,
            self._assessment_snapshot,
            self._decision_snapshot,
            self._source_population_id,
            self._evaluation_at,
        )

    def _restore_binding_state(self, state: tuple[Any, ...]) -> None:
        (
            self._continuity_key,
            self._proof_rows,
            self._decision_rows,
            self._prior_decision_rows,
            self._external_learning_rows,
            self._completion_proofs,
            self._adviser_credit,
            self._assessment_snapshot,
            self._decision_snapshot,
            self._source_population_id,
            self._evaluation_at,
        ) = state

    @staticmethod
    def _decision_row(
        row: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        payload = row.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != DECISION_LEDGER_SCHEMA
        ):
            return None
        proposal_id = str(payload.get("proposal_id") or "")
        forecast = payload.get("forecast")
        if not proposal_id or not isinstance(forecast, Mapping):
            return None
        source_signature = str(payload.get("source_signature") or "")
        if not source_signature:
            raise ValueError("new_horizon_decision_source_signature_required")
        predicted = payload["predicted_grades"]
        return proposal_id, {
            "proposal_id": proposal_id,
            "decided_at": float(row["observed_at"]),
            "opportunity_id": str(forecast.get("opportunity_id") or ""),
            "source_signature": source_signature,
            "target_signature": str(forecast.get("target_signature") or ""),
            "explanation": str(payload.get("explanation") or ""),
            "forecast": dict(forecast),
            "predicted_grades": (
                dict(predicted) if isinstance(predicted, Mapping) else None
            ),
            "contributions": [dict(item) for item in payload.get("contributions", ())],
            "learned_grades": {},
            "baseline_identity": {},
            "result_identity": {},
            "dispatch_state": "reserved",
            "referee_state": "pending",
            "terminal_reason": "",
        }

    @staticmethod
    def _prior_decision_row(
        row: Mapping[str, Any],
        *,
        expected_schema: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """Read facts from older data without accepting its grades."""

        payload = row.get("payload")
        if not isinstance(payload, Mapping) or payload.get("schema") != expected_schema:
            return None
        proposal_id = str(payload.get("proposal_id") or "")
        forecast = payload.get("forecast")
        if not proposal_id or not isinstance(forecast, Mapping):
            return None
        expected_latency = _number(forecast.get("expected_latency_ms"))
        if expected_latency is None:
            expected_latency = _number(forecast.get("expected_loaded_latency_ms"))
        compatible_forecast = {
            "status": str(forecast.get("status") or "unknown"),
            "match_tier": str(forecast.get("match_tier") or "unavailable"),
            "opportunity_id": str(forecast.get("opportunity_id") or ""),
            "target_signature": str(forecast.get("target_signature") or ""),
            "expected_eci_points": _number(forecast.get("expected_eci_points")),
            # Physical latency still has the same meaning. Older CQ forecasts
            # used another formula and cannot be converted safely.
            "expected_latency_ms": expected_latency,
            "expected_jitter_ms": _number(forecast.get("expected_jitter_ms")),
            "expected_cqb": None,
            "expected_internet_availability_pct": _number(
                forecast.get("expected_internet_availability_pct")
            ),
            "confidence": _number(forecast.get("confidence")) or 0.0,
            "evidence_assessments": max(
                0,
                int(forecast.get("evidence_assessments", 0) or 0),
            ),
        }
        # Explicit v11 boundary: physical AQ forecasts keep their meaning;
        # the old contribution grades were IQ and must never be relabelled.
        predicted_aq = None
        if expected_schema == "NewHorizonDecisionContribution.v11":
            compatible_forecast.update(forecast)
            contexts = forecast.get("result_contexts")
            if isinstance(contexts, list) and contexts and all(
                isinstance(item, Mapping) and _number(item.get("expected_aq")) is not None
                and -3.0 <= item["expected_aq"] <= 3.0
                and _number(item.get("evidence_weight")) is not None
                and item["evidence_weight"] >= 0
                for item in contexts
            ):
                weights = tuple(float(item["evidence_weight"]) for item in contexts)
                predicted_aq = EvidenceWeights(weights, sum(weights)).mean(item["expected_aq"] for item in contexts)
            compatible_forecast["expected_aq"] = predicted_aq
        predicted = None if predicted_aq is None else {
            "basis": "prediction", "components": [{
                "dimension": "service_outcome", "grade": predicted_aq,
                "confidence": compatible_forecast.get("confidence", 0.0),
                "reason": "retained_absolute_forecast",
            }],
        }
        return proposal_id, {
            "proposal_id": proposal_id,
            "decided_at": float(row["observed_at"]),
            "opportunity_id": compatible_forecast["opportunity_id"],
            "source_signature": str(payload.get("source_signature") or ""),
            "target_signature": compatible_forecast["target_signature"],
            "explanation": str(payload.get("explanation") or "Reason not retained."),
            "forecast": compatible_forecast,
            "predicted_grades": predicted,
            "contributions": [],
            "learned_grades": {},
            "baseline_identity": {},
            "result_identity": {},
            "dispatch_state": "historical",
            "referee_state": "prior_formula",
            "terminal_reason": "prior_formula_display_only",
        }

    @staticmethod
    def _drop_row(row: dict[str, Any], reason: str) -> None:
        row["referee_state"] = "dropped"
        row["terminal_reason"] = reason or "referee_assessment_invalid"
        row["learned_grades"] = {}

    def _admit_historical_assessment(
        self,
        assessment: RefereeAssessmentEvidence,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(assessment, RefereeAssessmentEvidence):
            raise TypeError("new_horizon_crystal_ball_referee_assessment_required")
        rows = assessment.payloads
        first = rows[0]
        assessment_mode = (
            "context_quality" if first["subject_kind"] == "active_context_window"
            else "transition"
        )
        baseline_identity = (
            dict(first["baseline_identity"])
            if isinstance(first.get("baseline_identity"), Mapping)
            else {}
        )
        brain = is_brain_decision_origin(first["origin"])
        decision_id = str(first["decision_id"]).removeprefix("core-command:")
        decision_row = self._decision_rows.get(decision_id)
        if decision_row is None:
            decision_row = self._prior_decision_rows.get(decision_id)
        invalid = next(
            (row for row in rows if row.get("status") == "invalid"),
            None,
        )
        complete_brain = bool(
            brain
            and tuple(
                str(row["dimension"])
                for row in rows
                if row.get("dimension") in LEARNING_GRADE_DIMENSIONS
            )
            == LEARNING_GRADE_DIMENSIONS
            and all(
                row.get("status") == "graded"
                and row.get("final_assessment") is True
                and type(row.get("grade")) in {int, float}
                and not isinstance(row.get("grade"), bool)
                for row in rows
                if row.get("dimension") in LEARNING_GRADE_DIMENSIONS
            )
        )
        service_proof = next(
            (
                row
                for row in rows
                if row.get("dimension") == "service_outcome"
                and row.get("status") == "graded"
                and row.get("final_assessment") is True
            ),
            None,
        )
        if decision_row is not None:
            if invalid is not None:
                reasons = invalid.get("reasons")
                self._drop_row(
                    decision_row,
                    str(reasons[0])
                    if isinstance(reasons, list) and reasons
                    else "referee_assessment_invalid",
                )
            elif complete_brain and decision_row["referee_state"] != "dropped":
                if service_proof is None:
                    raise ValueError(
                        "new_horizon_complete_brain_service_proof_required"
                    )
                decision_row["dispatch_state"] = "submitted"
                decision_row["learned_grades"] = {
                    str(row["dimension"]): float(row["grade"])
                    for row in rows
                    if row.get("dimension") in LEARNING_GRADE_DIMENSIONS
                }
                decision_row["result_identity"] = dict(rows[0]["result_identity"])
                decision_row["baseline_identity"] = dict(baseline_identity)
                decision_row["assessment_id"] = str(service_proof["assessment_id"])
                decision_row["assessment_mode"] = assessment_mode
                decision_row["completed_at"] = float(service_proof["closed_at"])
                decision_row["baseline_vector"] = referee_history_vector(service_proof, "baseline_vector")
                decision_row["observed_vector"] = referee_history_vector(service_proof, "observed_vector")
                decision_row["origin"] = str(first["origin"])
                decision_row["result_attribution"] = forecast_result_attribution(
                    decision_row["forecast"], decision_row["result_identity"])
                decision_row["referee_state"] = "complete"
                decision_row["terminal_reason"] = ""
                self._completion_proofs[decision_id] = dict(service_proof)
        if brain and not complete_brain:
            return ()
        if brain and (
            decision_row is None or decision_row["referee_state"] != "complete"
        ):
            return ()
        trainable = tuple(
            row
            for row in rows
            if row.get("status") == "graded"
            and row.get("final_assessment") is True
            and type(row.get("grade")) in {int, float}
            and not isinstance(row.get("grade"), bool)
            and (brain or row.get("dimension") == "service_outcome")
        )
        if (
            not brain
            and first.get("subject_kind") != "active_context_window"
            and trainable
        ):
            if service_proof is None:
                raise ValueError("new_horizon_external_service_proof_required")
            assessment_id = str(first["assessment_id"])
            self._external_learning_rows[assessment_id] = {
                "proposal_id": "assessment:" + assessment_id,
                "decided_at": float(first["opened_at"]),
                "opportunity_id": "",
                "source_signature": "",
                "target_signature": str(first["target_signature"]),
                "origin": str(first["origin"]),
                "explanation": "",
                "forecast": {},
                "predicted_grades": None,
                "learned_grades": {
                    str(row["dimension"]): float(row["grade"]) for row in trainable
                },
                "baseline_identity": dict(baseline_identity),
                "result_identity": dict(first["result_identity"]),
                "assessment_id": assessment_id,
                "assessment_mode": assessment_mode,
                "completed_at": float(service_proof["closed_at"]),
                "baseline_vector": referee_history_vector(service_proof, "baseline_vector"),
                "observed_vector": referee_history_vector(service_proof, "observed_vector"),
                "referee_state": "complete",
                "terminal_reason": "",
            }
        return trainable

    def _admit_proposal_result_payload(
        self,
        payload: Mapping[str, Any],
    ) -> None:
        if payload.get("schema") != PROPOSAL_RESULT_EVIDENCE_SCHEMA:
            return
        proposal_id = str(payload.get("proposal_id") or "")
        row = self._decision_rows.get(proposal_id)
        if row is None:
            row = self._prior_decision_rows.get(proposal_id)
        if row is None:
            raise ValueError("new_horizon_proposal_result_ledger_missing")
        submitted = payload.get("submitted")
        if submitted is False:
            row["dispatch_state"] = "not_submitted"
            self._drop_row(
                row,
                str(payload.get("reason") or "proposal_not_submitted"),
            )
        elif submitted is True:
            row["dispatch_state"] = "submitted"
        else:
            raise ValueError("new_horizon_proposal_result_state_invalid")

    def _admit_terminal_record(self, record: Mapping[str, Any]) -> None:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("new_horizon_decision_terminal_payload_invalid")
        if payload.get("schema") != DECISION_TERMINAL_SCHEMA:
            raise ValueError("new_horizon_decision_terminal_schema_invalid")
        proposal_id = str(payload.get("proposal_id") or "")
        reason = str(payload.get("reason") or "")
        row = self._decision_rows.get(proposal_id)
        if row is None:
            # A terminal result stays factual after a formula change. Older
            # decision rows are display-only, but their terminal receipt must
            # still close the matching row.
            row = self._prior_decision_rows.get(proposal_id)
        if row is None:
            raise ValueError("new_horizon_decision_terminal_ledger_missing")
        if row["referee_state"] == "complete":
            if legacy_deadline_terminal_is_superseded(
                terminal_record=record,
                proof_payload=self._completion_proofs.get(proposal_id) or {},
            ):
                return
            raise ValueError("new_horizon_completed_decision_terminal_conflict")
        self._drop_row(row, reason or "decision_lifecycle_terminated")

    def bind_context(
        self,
        context: IntelligenceContext,
        assessment_history: RefereeAssessmentHistorySnapshot,
        decision_history: DecisionLifecycleHistorySnapshot,
    ) -> str:
        continuity = context_continuity_key(context)
        if (
            not isinstance(assessment_history, RefereeAssessmentHistorySnapshot)
            or assessment_history.continuity_key != continuity
        ):
            raise ValueError("new_horizon_crystal_ball_referee_history_scope_mismatch")
        if (
            not isinstance(decision_history, DecisionLifecycleHistorySnapshot)
            or decision_history.continuity_key != continuity
        ):
            raise ValueError("new_horizon_crystal_ball_decision_history_scope_mismatch")
        if (
            continuity == self._continuity_key
            and assessment_history is self._assessment_snapshot
            and decision_history is self._decision_snapshot
            and int(context.accepted_at // 60) == int(self._evaluation_at // 60)
            and assessment_history.evidence_time_equivalent(self._evaluation_at, context.accepted_at)
            and decision_history.evidence_time_equivalent(self._evaluation_at, context.accepted_at)
        ):
            return continuity
        previous = self._binding_state()
        try:
            self._proof_rows = {row["assessment_id"]: row for row in
                                assessment_history.aq_advice(now=context.accepted_at)}
            self._decision_rows = {}
            self._prior_decision_rows = {}
            self._external_learning_rows = {}
            self._completion_proofs = {}
            self._adviser_credit = empty_adviser_credit()
            self._evaluation_at = context.accepted_at
            for row in decision_history.current_decisions:
                parsed = self._decision_row(row)
                if parsed is not None:
                    proposal_id, decision_row = parsed
                    self._decision_rows[proposal_id] = decision_row
            for prior_kind, prior_schema in PRIOR_DECISION_LEDGER_CONTRACTS:
                for row in decision_history.records_for(prior_kind):
                    parsed = self._prior_decision_row(
                        row,
                        expected_schema=prior_schema,
                    )
                    if (
                        parsed is not None
                        and parsed[0] not in self._decision_rows
                        and parsed[0] not in self._prior_decision_rows
                    ):
                        self._prior_decision_rows[parsed[0]] = parsed[1]
            for row in decision_history.proposal_results:
                payload = row.get("payload")
                if isinstance(payload, Mapping):
                    self._admit_proposal_result_payload(payload)
            for assessment in assessment_history.assessments:
                trainable = self._admit_historical_assessment(assessment)
                by_id = {proof.proof_id: proof for proof in assessment.proofs}
                for payload in trainable:
                    proof = by_id[str(payload["proof_id"])]
                    self._apply_feedback(
                        self._adviser_credit,
                        proof,
                        proof_payload=payload,
                    )
            for row in decision_history.terminals:
                self._admit_terminal_record(row)
            self._continuity_key = continuity
            self._assessment_snapshot = assessment_history
            self._decision_snapshot = decision_history
            self._source_population_id = assessment_history.population_id
        except Exception:
            self._restore_binding_state(previous)
            raise
        return continuity

    def admit_proof(self, _proof: RefereeProof) -> bool:
        raise RuntimeError("new_horizon_crystal_ball_assessment_admission_required")

    def completed_learning_cycle_ring(self) -> tuple[dict[str, Any], ...]:
        """Return the newest eight completed learning proofs.

        Pending, rejected, timed-out and dropped attempts remain in diagnostic
        history. They cannot consume a customer proof row.
        """

        rows = sorted(
            (
                *(
                    row
                    for row in self._decision_rows.values()
                    if row["referee_state"] == "complete"
                ),
                *(
                    row
                    for proposal_id, row in self._prior_decision_rows.items()
                    if proposal_id not in self._decision_rows
                    and row["referee_state"] == "complete"
                ),
                *self._external_learning_rows.values(),
            ),
            key=lambda row: float(row["decided_at"]),
            reverse=True,
        )[:8]
        return tuple(
            {
                **row,
                "forecast": dict(row["forecast"]),
                "predicted_grades": (
                    dict(row["predicted_grades"])
                    if isinstance(row["predicted_grades"], Mapping)
                    else None
                ),
                "learned_grades": dict(row["learned_grades"]),
                "baseline_identity": dict(row["baseline_identity"]),
                "result_identity": dict(row["result_identity"]),
                "assessment_id": str(row.get("assessment_id") or ""),
                "completed_at": row.get("completed_at"),
                "baseline_vector": dict(row.get("baseline_vector") or {}),
                "observed_vector": dict(row.get("observed_vector") or {}),
            }
            for row in rows
        )

    @staticmethod
    def _target_signature(
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
    ) -> str:
        if opportunity.action_target is not None:
            return opportunity.action_target.target_signature
        current = current_context_target(context)
        return (
            current.target_signature
            if current is not None
            else "do_nothing:current_target_unavailable"
        )

    def forecasts(
        self,
        context: IntelligenceContext,
        opportunities: Iterable[AdvisoryOpportunity],
    ) -> dict[str, CandidateForecast]:
        if context_continuity_key(context) != self._continuity_key:
            raise RuntimeError("new_horizon_crystal_ball_context_not_bound")
        current_learning = context_learning_components(context)
        current_identity = context_radio_identity(context)
        condition = nr_evidence_condition(current_identity)
        quality_model = require_quality_model(context.confirmed_continuity.to_mapping()["quality_model"])
        results: dict[str, CandidateForecast] = {}
        identity_index = _forecast_identity_index(
            self._assessment_snapshot.condition_aq_advice(self._proof_rows.values(),
                current_identity=current_identity, now=context.accepted_at))
        starts_at = fixed_hour(context.accepted_at, timezone="local")[0]
        patterns = {stamp: self._assessment_snapshot.cell_presence(
            hour=fixed_hour(context.accepted_at + stamp - starts_at, timezone="local")[3],
            now=context.accepted_at, current_identity=current_identity)
            for stamp in (starts_at, starts_at + 3600.0)}
        availability = {stamp: self._assessment_snapshot.service_evidence(
            hour=fixed_hour(context.accepted_at + stamp - starts_at, timezone="local")[3],
            now=context.accepted_at, current_identity=current_identity)
            for stamp in (starts_at, starts_at + 3600.0)}
        for opportunity in opportunities:
            signature = self._target_signature(context, opportunity)
            target = opportunity.action_target or current_context_target(context)
            command = None if target is None else core_command_target_from_payload(target.target.to_mapping())
            pool = (command.ca_pool if isinstance(command, CorePBandTarget)
                    else command.bands if isinstance(command, CoreBandSetTarget) else ())
            hourly = []
            for prediction_at in (starts_at, starts_at + 3600.0):
                learning = build_learning_context(
                    exact_scope_key=current_learning["exact_scope_key"],
                    observed_at=context.accepted_at + prediction_at - starts_at)
                credit_factor = adviser_credit_factor(self._adviser_credit,
                    adviser="crystal_ball", dimension="service_outcome",
                    learning_context_key=learning_context_key(learning))
                accuracy_factor, accuracy_assessments = forecast_accuracy(
                    self._adviser_credit,
                    learning_context_key=_forecast_feedback_key(signature, learning["time_bucket"], condition,
                                                               quality_model=quality_model))
                rows, match_tier, tier_factor = RefereeAssessmentHistorySnapshot.matching_aq_advice(
                    self._proof_rows.values(),
                    target=command,
                    current_learning=learning, current_identity=context_radio_identity(context),
                    purpose="current_connection" if opportunity.action_target is None else "band_request",
                    now=context.accepted_at)
                hourly.append(_candidate_forecast(
                    opportunity=opportunity, target_signature=signature,
                    requested_pool=pool,
                    continuity_key=self._continuity_key, current_learning=learning,
                    current_identity=current_identity,
                    rows=rows, identity_index=identity_index, match_tier=match_tier,
                    tier_factor=tier_factor, credit_factor=credit_factor,
                    accuracy_factor=accuracy_factor, accuracy_assessments=accuracy_assessments,
                    now=context.accepted_at, prediction_at=prediction_at,
                    cell_presence=patterns[prediction_at]))
                pband = (command.pband if isinstance(command, CorePBandTarget)
                         else context_radio_identity(context).get("pband", 0))
                working = target_service_evidence(availability[prediction_at], pband=pband, pool=pool,
                    identity=context_radio_identity(context) if opportunity.opportunity_id == "do_nothing" else None)
                hourly[-1] = replace(hourly[-1], service_evidence=working,
                                     nr_device=quality_model == NR_QUALITY_MODEL)
            results[opportunity.opportunity_id] = replace(hourly[0], next_hour=hourly[1])
        return results

    def _apply_feedback(
        self,
        checkpoint: dict[str, Any],
        proof: RefereeProof,
        *,
        proof_payload: Mapping[str, Any],
    ) -> None:
        decision_id = proof.decision_id.removeprefix("core-command:")
        ledger = self._decision_rows.get(decision_id) or self._prior_decision_rows.get(decision_id)
        if ledger is None:
            return
        contributions = ledger.get("contributions")
        if not isinstance(contributions, list):
            raise ValueError("new_horizon_decision_contributions_invalid")
        if checkpoint.get("schema") != ADVISER_CREDIT_SCHEMA:
            raise ValueError("new_horizon_adviser_credit_schema_invalid")
        rows = dict(checkpoint.get("rows") or {})
        admitted = checkpoint.get("admitted_feedback_proof_ids") or []
        if not isinstance(admitted, list) or any(
            type(item) is not str or not item for item in admitted
        ):
            raise ValueError("new_horizon_adviser_feedback_ids_invalid")
        if proof.proof_id in admitted:
            return
        attribution = forecast_result_attribution(
            ledger.get("forecast"), proof_payload.get("result_identity"))
        if not attribution["eligible"]:
            checkpoint["admitted_feedback_proof_ids"] = sorted({*admitted, proof.proof_id})
            return
        weight = evidence_weights(
            (proof.closed_at,), now=self._evaluation_at
        ).effective_count
        if weight == 0.0:
            return
        if (
            isinstance(proof.grade, bool)
            or type(proof.grade) not in {int, float}
            or not math.isfinite(float(proof.grade))
        ):
            raise ValueError("new_horizon_adviser_feedback_grade_required")
        for raw_contribution in contributions:
            contribution = validated_contribution(raw_contribution)
            if (
                contribution["dimension"] != proof.dimension
                or contribution["used"] is not True
            ):
                continue
            adviser = contribution["adviser"]
            predicted_grade = (
                attribution["expected_aq"] if adviser in {"bandit", "crystal_ball"}
                else contribution["predicted_grade"]
            )
            key = adviser + ":" + proof.dimension + ":" + proof.learning_context_key
            current = dict(rows.get(key) or {})
            count = int(current.get("count", 0) or 0) + 1
            mass = float(current.get("effective_count", 0.0)) + weight
            # Accuracy of absolute prediction, not whether the change gained.
            # A correct forecast of AQ -2 deserves full prediction credit.
            credit_grade = 3.0 - abs(predicted_grade - proof.grade)
            old_mean = float(current.get("mean_credit_grade", 0.0) or 0.0)
            rows[key] = {
                "count": count,
                "effective_count": mass,
                "mean_credit_grade": old_mean
                + (float(credit_grade) - old_mean) * weight / mass,
                "last_stance": contribution["stance"],
                "last_predicted_grade": predicted_grade,
                "last_actual_grade": proof.grade,
                "last_assessment_id": proof.assessment_id,
                "last_proof_id": proof.proof_id,
                "result_attribution": dict(attribution),
            }
        forecast = ledger.get("forecast")
        observed = proof_payload.get("observed_vector")
        if isinstance(forecast, Mapping) and isinstance(observed, Mapping):
            errors: dict[str, float] = {}
            predicted = attribution["expected_aq"]
            if proof.dimension == "service_outcome" and predicted is not None:
                errors["aq"] = abs(predicted - proof.grade)
            if errors:
                # Referee retains the assessed change's frozen source context.
                # Use that canonical context, not today's cell or a new ledger
                # requirement that would reject otherwise valid retained rows.
                learning = require_learning_context(proof_payload["learning_context"])
                error_key = (
                    "crystal_ball_error:"
                    + proof.dimension
                    + ":"
                    + _forecast_feedback_key(
                        str(forecast["target_signature"]), learning["time_bucket"],
                        nr_evidence_condition(proof_payload["result_identity"]),
                        quality_model=referee_history_vector(proof_payload, "observed_vector")["quality_model"],
                    )
                )
                current = dict(rows.get(error_key) or {})
                count = int(current.get("count", 0) or 0) + 1
                mass = float(current.get("effective_count", 0.0)) + weight
                means = dict(current.get("mean_absolute_error") or {})
                metric_mass = dict(current.get("metric_effective_counts") or {})
                for metric, error in errors.items():
                    old = float(means.get(metric, 0.0) or 0.0)
                    metric_mass[metric] = float(metric_mass.get(metric, 0.0)) + weight
                    means[metric] = old + (error - old) * weight / metric_mass[metric]
                rows[error_key] = {
                    "count": count,
                    "effective_count": mass,
                    "metric_effective_counts": metric_mass,
                    "mean_absolute_error": means,
                    "last_assessment_id": proof.assessment_id,
                }
        checkpoint["rows"] = rows
        checkpoint["admitted_feedback_proof_ids"] = sorted({*admitted, proof.proof_id})


__all__ = (
    "ADVISER_CREDIT_SCHEMA",
    "CandidateForecast",
    "CrystalBall",
    "adviser_credit_factor",
    "empty_adviser_credit",
    "forecast_accuracy",
)
