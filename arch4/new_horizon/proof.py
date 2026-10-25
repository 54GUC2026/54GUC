"""Collect delayed proof and calculate New Horizon Referee grades."""

from __future__ import annotations
from arch4.core_contracts.nr_identity import nr_identity_values_valid

from arch4.new_horizon.ca_evidence import (
    capture_ca_observation, summarize_ca_observations, ca_pool_compatible, observed_ca_bands,
)

from dataclasses import dataclass, field, replace
from copy import deepcopy
import hashlib
import json
import math
from collections import Counter, defaultdict
from statistics import median
from time import time
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from arch4.action_origin import is_brain_decision_origin
from arch4.core_contracts.connected_settlement import CONNECTION_SETTLING_REASON
from arch4.architecture_contracts.runtime_generation import MatrixGenerationIdentity
from arch4.new_horizon.contracts import (
    DECISION_LEDGER_SCHEMA,
    IntelligenceContext,
    RefereeProof,
    RefereeSubject,
    RefereeTarget,
)
from arch4.new_horizon import service_quality as _quality
from arch4.new_horizon.history import AdvisoryHistory, evidence_time_equivalent
from arch4.core_contracts.quality_model import LTE_QUALITY_MODEL, NR_QUALITY_MODEL, UNKNOWN_QUALITY_MODEL, require_quality_model
from arch4.new_horizon.support_targets import referee_target_from_history_record
from arch4.new_horizon.continuity import (
    CONTINUITY_PATHS, LEARNING_SCOPE_FIELDS, learning_device_key, continuity_value, resolve_continuity, continuity_digest,
)
from arch4.new_horizon.cell_presence import daily_cell_coverage, hourly_cell_presence
from arch4.new_horizon.service_evidence import hourly_service_evidence
from arch4.new_horizon.evidence_weighting import evidence_influence
from arch4.new_horizon.hourly_aq import (
    fixed_hour, make_aq_block, merge_aq_block, rekey_aq_hour, require_aq_hour,
    hourly_aq_advice, hourly_observed_vector, immutable_aq_hour, AQ_RETENTION_SECONDS, aq_pool_key,
    AQ_PHYSICAL_FIELDS as _AQ_PHYSICAL_FIELDS,
)
from arch4.new_horizon.decision_history import (
    DecisionLifecycleHistoryOwner,
    DecisionLifecycleHistorySnapshot,
)
from arch4.new_horizon.learning_context import (
    build_learning_context,
    learning_context_key,
    learning_time_bucket_for_hour,
    require_learning_context,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.action_reachability import (
    CoreReachabilityEvidenceView,
    CoreReachabilityIntervalReceipt,
    ReachabilityIntervalCoverage,
    core_reachability_interval_receipt,
)
from arch4.core_contracts.intelligence_proposals import GRADE_DIMENSIONS
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CorePBandTarget,
    core_command_target_from_payload,
    core_command_target_payload,
    core_lte_band_signature,
    core_lte_pband_signature,
)
if TYPE_CHECKING:
    from arch4.controller_frames import RadioTransitionFrame


REFEREE_PROOF_HISTORY_KIND = "referee_proof_v19"
RETIRED_LTE_PROOF_KIND = "referee_proof_v18"
RETIRED_LTE_PROOF_SCHEMA = "NewHorizonRefereeProofEvidence.v18"
READABLE_REFEREE_PROOF_KINDS = (REFEREE_PROOF_HISTORY_KIND, RETIRED_LTE_PROOF_KIND)
AQ_HOURLY_CHECKPOINT = "referee_hourly_aq_v1"
AQ_HOURLY_CHECKPOINT_SCHEMA = "NewHorizonHourlyAQHistory.v1"
AQ_PROVIDER_ALIAS_TOMBSTONE = "referee_hourly_aq_provider_alias_tombstone_v1"
AQ_PROVIDER_ALIAS_TOMBSTONE_SCHEMA = "NewHorizonProviderAliasTombstone.v1"
REFEREE_PROOF_EVIDENCE_SCHEMA = "NewHorizonRefereeProofEvidence.v19"
REFEREE_PARTIAL_HISTORY_KIND = "referee_partial_context_v1"
REFEREE_PARTIAL_EVIDENCE_SCHEMA = "NewHorizonRefereePartialContext.v1"
REFEREE_TIMELINE_DISCONTINUITY_HISTORY_KIND = "referee_timeline_discontinuity_v1"
REFEREE_BASELINE_RESERVATION_KIND = "referee_baseline_reservation_v1"
REFEREE_BASELINE_RESERVATION_SCHEMA = "NewHorizonRefereeBaselineReservation.v1"
PRIOR_REFEREE_PROOF_CONTRACTS = (
    ("referee_proof_v17", "NewHorizonRefereeProofEvidence.v17"),
    ("referee_proof_v16", "NewHorizonRefereeProofEvidence.v16"),
    ("referee_proof_v15", "NewHorizonRefereeProofEvidence.v15"),
    ("referee_proof_v14", "NewHorizonRefereeProofEvidence.v14"),
    ("referee_proof_v13", "NewHorizonRefereeProofEvidence.v13"),
    ("referee_proof_v12", "NewHorizonRefereeProofEvidence.v12"),
    ("referee_proof_v11", "NewHorizonRefereeProofEvidence.v11"),
    ("referee_proof_v10", "NewHorizonRefereeProofEvidence.v10"),
)
FINAL_GRADE_WINDOW_S = 600.0
DECISION_LIFECYCLE_TIMEOUT_S = FINAL_GRADE_WINDOW_S + 120.0

_SERVICE_REWARD_IMPORT_RECALCULATION = "service_reward_import_recalculation_v20"
_SERVICE_REWARD_IMPORT_RECALCULATION_SCHEMA = (
    "NewHorizonServiceRewardImportRecalculation.v8"
)

def _dimension_proof_id(base_id: str, dimension: str) -> str:
    return hashlib.sha256(f"{base_id}|{dimension}".encode("utf-8")).hexdigest()


def normalize_referee_assessment(
    proofs: Iterable[RefereeProof],
) -> tuple[RefereeProof, ...]:
    """Validate and order one Referee assessment."""

    rows = tuple(proofs)
    if not rows or any(not isinstance(row, RefereeProof) for row in rows):
        raise TypeError("new_horizon_referee_assessment_required")
    first = rows[0]
    identity = (
        first.assessment_id,
        first.decision_id,
        first.scope_key,
        first.learning_context_key,
        first.subject_kind,
        first.origin,
        first.target.action_family,
        first.target.target_signature,
    )
    if any(
        (
            row.assessment_id,
            row.decision_id,
            row.scope_key,
            row.learning_context_key,
            row.subject_kind,
            row.origin,
            row.target.action_family,
            row.target.target_signature,
        )
        != identity
        for row in rows
    ):
        raise ValueError("new_horizon_referee_assessment_identity_mismatch")
    by_dimension = {row.dimension: row for row in rows}
    if len(by_dimension) != len(rows):
        raise ValueError("new_horizon_referee_assessment_dimension_duplicate")
    order = {name: index for index, name in enumerate(GRADE_DIMENSIONS)}
    return tuple(sorted(rows, key=lambda row: order[row.dimension]))


def referee_assessment_training_proofs(
    proofs: Iterable[RefereeProof],
) -> tuple[RefereeProof, ...]:
    """Return proof rows allowed to train or rank an assessment."""

    rows = normalize_referee_assessment(proofs)
    graded = tuple(
        row
        for row in rows
        if row.status == "graded"
        and row.final_assessment
        and type(row.grade) in {int, float}
        and not isinstance(row.grade, bool)
    )
    return tuple(row for row in graded if row.dimension == "service_outcome")


def referee_assessment_is_complete(
    proofs: Iterable[RefereeProof],
) -> bool:
    """Return whether one assessment may enter canonical learning history."""

    rows = normalize_referee_assessment(proofs)
    return all(row.status == "graded" and row.final_assessment for row in rows)


@dataclass(frozen=True, slots=True)
class RefereeAssessmentEvidence:
    """One assessment after Referee-owned history validation."""

    assessment_id: str
    records: tuple[dict[str, Any], ...]
    payloads: tuple[dict[str, Any], ...]
    proofs: tuple[RefereeProof, ...]
    closed_at: float


@dataclass(frozen=True, slots=True)
class RefereeAssessmentHistorySnapshot:
    """The only legal interpreted view of saved Referee proof."""

    continuity_key: str
    assessments: tuple[RefereeAssessmentEvidence, ...]
    rejected: tuple[dict[str, str], ...]
    aq_hours: tuple[dict[str, Any], ...] = ()
    partial_visits: tuple[dict[str, Any], ...] = ()
    clock_timezone: str = "local"
    population_id: str = field(init=False)
    cell_days: tuple[dict[str, Any], ...] = field(init=False, repr=False)
    nr_cell_days: Mapping[tuple[str, ...], tuple[dict[str, Any], ...]] = field(init=False, repr=False)
    _aq_advice_cache: tuple[float, tuple[dict[str, Any], ...]] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if any(hour["continuity_key"] != self.continuity_key for hour in self.aq_hours):
            raise ValueError("new_horizon_hourly_history_continuity_mismatch")
        if any(row["continuity_key"] != self.continuity_key for row in self.partial_visits):
            raise ValueError("new_horizon_partial_history_continuity_mismatch")
        object.__setattr__(self, "aq_hours", tuple(immutable_aq_hour(hour) for hour in self.aq_hours))
        intervals = [dict(identity=h["identity"], opened_at=i["start"], closed_at=i["end"],
                          timezone=h["timezone"])
                     for h in self.aq_hours for i in h["intervals"]]
        for payload in self.partial_visits:
            if payload["observed_vector"].get("sample_count", 0) > 0:
                intervals.append(dict(identity=payload["identity"], opened_at=payload["opened_at"],
                                      closed_at=payload["closed_at"], timezone=self.clock_timezone))
        for assessment in self.assessments:
            payload = assessment.payloads[0]
            if (payload["subject_kind"] == "active_context_window"
                    and payload["observed_vector"].get("sample_count", 0) > 0):
                intervals.append(dict(identity=payload["result_identity"],
                    opened_at=assessment.proofs[0].opened_at,
                    closed_at=min(assessment.closed_at, assessment.proofs[0].due_at), timezone=self.clock_timezone))
        object.__setattr__(self, "cell_days", daily_cell_coverage(intervals))
        from arch4.new_horizon.hourly_aq import nr_evidence_condition
        conditions = {nr_evidence_condition(row["identity"]) for row in intervals}
        object.__setattr__(self, "nr_cell_days", {
            key: daily_cell_coverage(row for row in intervals
                                    if nr_evidence_condition(row["identity"]) == key)
            for key in conditions if key is not None
        })
        material = {
            "continuity_key": self.continuity_key,
            "aq_hours": [(row["hour_id"], row["revision"]) for row in self.aq_hours],
            "partial_visits": self.partial_visits,
            "clock_timezone": self.clock_timezone,
            "assessments": [
                {
                    "assessment_id": assessment.assessment_id,
                    "proof_ids": [proof.proof_id for proof in assessment.proofs],
                }
                for assessment in self.assessments
            ],
        }
        object.__setattr__(
            self,
            "population_id",
            hashlib.sha256(
                json.dumps(
                    material,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            record for assessment in self.assessments for record in assessment.records
        )

    def cell_presence(self, *, hour: int, now: float,
                      current_identity: Mapping[str, Any]) -> dict[str, Any]:
        from arch4.new_horizon.hourly_aq import nr_evidence_condition
        condition = nr_evidence_condition(current_identity)
        return hourly_cell_presence(self.nr_cell_days.get(condition, ()), hour=hour, now=now)

    def service_evidence(self, *, hour: int, now: float,
                         current_identity: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        """One availability view, independent of AQ's isolated-day filter."""
        from arch4.new_horizon.hourly_aq import nr_evidence_condition
        condition = nr_evidence_condition(current_identity)
        return tuple(row for row in hourly_service_evidence(self.aq_hours, self.partial_visits,
            now=now, hour=hour, timezone=self.clock_timezone)
            if condition is not None and nr_evidence_condition(row["identity"]) == condition)

    def local_band_use(self, *, now: float) -> dict[int, float]:
        """Retained hours with real band use, not allowed-mask membership.

        This supports companion restoration, not a forecast of next-hour AQ.
        Count a dated hour once per band; multiple cells/pools do not create
        extra hours. Existing ageing applies; no added quality grade.
        """
        hours: dict[tuple[int, float], float] = {}
        for hour in self.aq_hours:
            if hour["last_at"] > now:
                continue
            weight = evidence_influence(hour["mean_evidence_at"], now=now)
            if not weight:
                continue
            observed = hourly_observed_vector(hour)
            availability = observed.get("internet_availability_pct")
            if availability is None or availability <= 0:
                continue
            bands = set(observed_ca_bands(observed))
            primary = hour["identity"]["pband"]
            if type(primary) is int and primary > 0:
                bands.add(primary)
            for band in bands:
                key = (band, hour["hour_start"])
                hours[key] = max(hours.get(key, 0.0), weight)
        return {band: math.fsum(weight for (item, _), weight in hours.items() if item == band)
                for band in sorted({band for band, _ in hours})}

    @property
    def source_assessment_count(self) -> int:
        """Count measured blocks, including those already packed into hours."""
        return sum(len(hour["intervals"]) for hour in self.aq_hours) + sum(
            item.payloads[0]["subject_kind"] != "active_context_window"
            for item in self.assessments)

    def evidence_time_equivalent(self, previous_at: float, now: float) -> bool:
        """Expose time eligibility through this canonical Referee snapshot."""
        return evidence_time_equivalent(previous_at, now, (
            stamp for group in (
                (hour["last_at"] for hour in self.aq_hours),
                (item.closed_at for item in self.assessments),
                (visit["closed_at"] for visit in self.partial_visits),
            ) for stamp in group
        ))

    def aq_advice(self, *, now: float) -> tuple[dict[str, Any], ...]:
        """One shared hourly input for learning, forecasts and Brain evidence."""
        if type(now) not in {int, float} or not math.isfinite(now) or now <= 0:
            raise ValueError("hourly_aq_clock_invalid")
        cached = self._aq_advice_cache
        if cached is not None and cached[0] == now:
            return deepcopy(cached[1])
        result = []
        exclusions = self.normal_prediction_exclusions(now=now)
        for row in hourly_aq_advice(
            (hour for hour in self.aq_hours if hour["hour_id"] not in exclusions), now=now
        ):
            identity = row["identity"]
            learning = build_learning_context(
                exact_scope_key=identity["scope_key"], observed_at=row["last_at"],
            )
            # Slot identity is stored, not derived from the import machine's
            # current date or timezone.
            learning["time_bucket"] = learning_time_bucket_for_hour(row["local_hour"])
            target = CorePBandTarget(pband=identity["pband"], ca_pool=tuple(identity["ca_pool"]))
            result.append({
                **row, "assessment_id": "aq-hour:" + row["evidence_id"],
                "closed_at": row["last_at"], "learning_context": learning,
                "learning_context_key": learning_context_key(learning),
                "target_signature": identity["target_signature"],
                "target": core_command_target_payload(target),
                "action_family": "lte_radio_selection",
                "result_scope_key": identity["scope_key"],
                "result_identity": dict(identity),
                "prediction_exclusions": [detail for detail in exclusions.values()
                    if detail["subject"] == self._normal_prediction_subject(identity)
                    and detail["hour"] == row["local_hour"]],
            })
        detached = deepcopy(tuple(result))
        object.__setattr__(self, "_aq_advice_cache", (now, detached))
        return deepcopy(detached)

    @staticmethod
    def _normal_prediction_subject(identity: Mapping[str, Any]) -> tuple[Any, ...]:
        return (tuple(identity.get(name) for name in _AQ_PHYSICAL_FIELDS),
                aq_pool_key(identity["ca_pool"]))

    def normal_prediction_exclusions(self, *, now: float) -> dict[str, dict[str, Any]]:
        """Exclude a confirmed isolated low day from normal prediction only.

        Raw hourly AQ remains unchanged. Four consistent other days and later
        recovery are required. Another low day breaks the normal cluster and
        restores the earlier observation. No weather cause is inferred.
        """
        groups: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
        seen: dict[str, str] = {}
        for raw in self.aq_hours:
            hour = immutable_aq_hour(raw)
            ref = hour["hour_id"]
            if ref in seen:
                if seen[ref] != hour["revision"]:
                    raise ValueError("hourly_aq_conflicting_hour_revisions")
                continue
            seen[ref] = hour["revision"]
            if hour["last_at"] > now or now-hour["last_at"] >= AQ_RETENTION_SECONDS:
                continue
            identity = hour["identity"]
            if any(identity.get(name) is None for name in _AQ_PHYSICAL_FIELDS[:5]):
                continue
            key = (hour["continuity_key"], self._normal_prediction_subject(identity),
                   hour["local_hour"])
            groups[key].setdefault(hour["hour_label"][:10], {})[ref] = hour
        excluded: dict[str, dict[str, Any]] = {}
        for (_continuity, subject, clock_hour), dates in groups.items():
            if len(dates) < 5:
                continue
            days = []
            for date, saved in dates.items():
                rows = tuple(saved.values())
                duration = math.fsum(r["coverage_s"] for r in rows)
                if duration > 3600.0 + 1e-6:
                    # Overlapping source visits cannot prove a normal day.
                    days = []
                    break
                days.append((math.fsum(r["aq"]*r["coverage_s"] for r in rows)/duration,
                             date, rows))
            if len(days) < 5:
                continue
            days.sort(key=lambda item: (item[0], item[1]))
            low, others = days[0], days[1:]
            if others[-1][0]-others[0][0] > 0.4+1e-9 or others[0][0]-low[0] < 1.0-1e-9:
                continue
            bad_end = max(r["last_at"] for r in low[2])
            if not any(min(r["first_at"] for r in day[2]) > bad_end for day in others):
                continue
            support = sorted(r["hour_id"] for day in others for r in day[2])
            for row in low[2]:
                excluded[row["hour_id"]] = dict(
                    hour_id=row["hour_id"], subject=subject, hour=clock_hour,
                    date=low[1], aq=row["aq"], reason="confirmed_isolated_low_hour",
                    normal_days=len(others), normal_aq_min=others[0][0],
                    normal_aq_max=others[-1][0], supporting_hour_refs=support,
                )
        return excluded

    @staticmethod
    def physical_aq_context_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        """Group one physical outcome without erasing source pool identities."""
        identity = row["result_identity"]
        key = tuple(identity.get(field) for field in _AQ_PHYSICAL_FIELDS)
        if any(identity.get(field) is None for field in _AQ_PHYSICAL_FIELDS[:5]):
            # Missing identity is not proof that two outcomes are one cell.
            key += (row["result_scope_key"], row["assessment_id"])
        return key

    @staticmethod
    def condition_aq_advice(
        rows: Iterable[dict[str, Any]], *, current_identity: Mapping[str, Any], now: float,
    ) -> list[dict[str, Any]]:
        """Comparable measured quality for one observed NR service condition."""
        from arch4.new_horizon.hourly_aq import nr_evidence_condition
        condition = nr_evidence_condition(current_identity)
        if condition is None:
            return []
        return [row for row in rows if row["closed_at"] <= now
                and nr_evidence_condition(row["result_identity"]) == condition]

    @staticmethod
    def target_aq_advice(
        rows: Iterable[dict[str, Any]], *, target: CorePBandTarget | CoreBandSetTarget | None,
        now: float, current_identity: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Retained primary-band observations, before time/pool refinement.

        Coverage is not an exact-pool forecast. A measured primary band does
        not become untested because its pool or clock hour changed.
        """
        if target is None:
            return []
        if not isinstance(target, (CorePBandTarget, CoreBandSetTarget)):
            raise TypeError("new_horizon_aq_command_target_required")
        comparable = RefereeAssessmentHistorySnapshot.condition_aq_advice(
            rows, current_identity=current_identity, now=now)
        return [row for row in comparable if (
            row["result_identity"]["pband"] in target.bands
            if isinstance(target, CoreBandSetTarget) else
            row["result_identity"]["pband"] == target.pband
        )]

    @staticmethod
    def matching_aq_advice(
        rows: Iterable[dict[str, Any]], *, target: CorePBandTarget | CoreBandSetTarget | None,
        current_learning: Mapping[str, Any], current_identity: Mapping[str, Any], now: float,
        purpose: str,
    ) -> tuple[list[dict[str, Any]], str, float]:
        """Select an explicit stay or request population for both advisers.

        Broad pools share a primary-cell subject. Restricted pools do not
        transfer AQ into it. Original pool identities remain in every row.
        """
        if purpose not in {"current_connection", "band_request"}:
            raise ValueError("new_horizon_aq_forecast_purpose_invalid")
        current = require_learning_context(current_learning)
        if not isinstance(current_identity, Mapping):
            raise TypeError("new_horizon_aq_current_radio_identity_required")
        def same_physical_context(row: Mapping[str, Any]) -> bool:
            # Pool membership is the requested action, not the serving cell.
            # Keep a different cell's history as related evidence, even when it
            # has many more hours than this cell.
            fields = ["network_type", "pband", "cell_id", "pci", "earfcn"]
            if current_identity.get("network_type") == "NR NSA (5G)":
                fields += ["nr_band", "nr_arfcn", "nr_pci", "nr_cell_id"]
            return bool(current_identity) and all(
                (current_identity.get(key) is not None or key == "nr_cell_id")
                and row["result_identity"].get(key) == current_identity.get(key)
                for key in fields
            )
        classified = [
            (row, require_learning_context(row["learning_context"]))
            for row in RefereeAssessmentHistorySnapshot.target_aq_advice(
                rows, target=target, now=now, current_identity=current_identity
            )
        ]
        if target is not None:
            requested_pool = target.bands if isinstance(target, CoreBandSetTarget) else target.ca_pool
            pool_key = aq_pool_key(requested_pool)
            classified = [(row, learning) for row, learning in classified
                          if aq_pool_key(
                              row["result_identity"]["ca_pool"]) == pool_key
                          and ca_pool_compatible(row["observed_vector"], requested_pool)]
        exact = [row for row, learning in classified
                 if same_physical_context(row)
                 and learning["time_bucket"] == current["time_bucket"]]
        related = [row for row, learning in classified
                   if not same_physical_context(row)
                   and learning["time_bucket"] == current["time_bucket"]]
        selected = exact if purpose == "current_connection" else exact + related
        if selected:
            tier = "exact" if not related or purpose == "current_connection" else "related_context"
            factor = 1.0 if tier == "exact" else 0.40
            return sorted(selected, key=lambda r:(r["closed_at"], r["assessment_id"])), tier, factor
        return [], "unavailable", 0.0


def _referee_storage_vector(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Permanent v18 LTE storage adapter; never infer a live device's model.

    Old combined/NR-bonus populations are not imported. Preserve archived
    payloads and hourly signatures: only this read projection gains a tag.
    """
    if field not in {"observed_vector", "baseline_vector"}:
        raise ValueError("referee_history_vector_field_invalid")
    vector = dict(payload[field])
    schema = payload["schema"]
    if not vector:
        return vector
    if schema == RETIRED_LTE_PROOF_SCHEMA:
        if vector.get("nr_active") is True or "quality_model" in vector:
            raise ValueError("retired_non_lte_grading_population_not_admissible")
        vector["quality_model"] = LTE_QUALITY_MODEL
    elif schema != REFEREE_PROOF_EVIDENCE_SCHEMA:
        raise ValueError("new_horizon_proof_history_schema_invalid")
    if "quality_model" not in vector:
        raise ValueError("new_horizon_proof_quality_model_required")
    require_quality_model(vector["quality_model"])
    return vector


def referee_history_vector(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Validated saved evidence projected through the one Intelligence formula owner."""
    return _quality.project_service_vector(_referee_storage_vector(payload, field))


def referee_hourly_storage_vector(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep retired LTE block signatures stable at the hourly storage boundary."""
    vector = _referee_storage_vector(payload, "observed_vector")
    if payload["schema"] == RETIRED_LTE_PROOF_SCHEMA:
        vector.pop("quality_model")
    elif "replay_provenance" in payload:
        provenance = payload["replay_provenance"]
        if (vector["quality_model"] != LTE_QUALITY_MODEL
            or provenance.get("source_schema") not in {schema for _, schema in PRIOR_REFEREE_PROOF_CONTRACTS}
            or provenance.get("formula_schema") != REFEREE_PROOF_EVIDENCE_SCHEMA
            or provenance.get("vector_migration") not in {
                "complete_legacy_context_vector_to_v17", "complete_legacy_transition_vector_to_v17"}):
            raise ValueError("new_horizon_retired_hourly_projection_invalid")
        vector.pop("quality_model")
    return vector


def adapt_retired_lte_pending_v13(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Permanent storage adapter for pre-model LTE pending measurements."""
    from copy import deepcopy
    if payload.get("schema") != "NewHorizonPendingProofCheckpoint.v13":
        raise ValueError("new_horizon_pending_proof_checkpoint_schema_invalid")
    result = deepcopy(dict(payload))
    for row in result["rows"]:
        for vector in (row["baseline"], *row["samples"]):
            if not vector:
                continue
            if "quality_model" in vector or vector.get("nr_active") is True:
                raise ValueError("retired_non_lte_pending_population_not_admissible")
            vector["quality_model"] = LTE_QUALITY_MODEL
    result["schema"] = "NewHorizonPendingProofCheckpoint.v14"
    return result


def _referee_proof_from_history_record(
    scope_key: str,
    row: Mapping[str, Any],
) -> RefereeProof:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("new_horizon_proof_history_payload_invalid")
    if (row.get("kind"), payload.get("schema")) not in {
        (REFEREE_PROOF_HISTORY_KIND, REFEREE_PROOF_EVIDENCE_SCHEMA),
        (RETIRED_LTE_PROOF_KIND, RETIRED_LTE_PROOF_SCHEMA),
    }:
        raise ValueError("new_horizon_proof_history_schema_invalid")
    stored_learning_key = str(payload.get("learning_context_key") or "")
    if payload.get("status") == "graded" and payload.get("final_assessment") is True:
        learning_context = payload.get("learning_context")
        if not isinstance(learning_context, Mapping):
            raise ValueError("new_horizon_proof_learning_context_missing")
        learning_context = require_learning_context(learning_context)
        if stored_learning_key != learning_context_key(learning_context):
            raise ValueError("new_horizon_proof_learning_context_key_mismatch")
    dimension = str(payload.get("dimension") or "")
    grade = payload.get("grade")
    if dimension == "service_outcome":
        if payload.get("status") == "graded" and payload.get("final_assessment") is True:
            observed = referee_history_vector(payload, "observed_vector")
            absolute_grade = _quality.context_quality_grades(observed)['performance_overall_outcome'] if isinstance(observed, Mapping) else None
            if absolute_grade is None or _number(grade) is None or abs(float(grade) - absolute_grade) > 0.000001:
                raise ValueError("new_horizon_absolute_grade_not_canonical")
        components = payload.get("component_grades")
        components = dict(components) if isinstance(components, Mapping) else {}
        observed=referee_history_vector(payload, "observed_vector")
        canonical_components = _quality.context_quality_grades(observed) if observed else {}
        canonical_grade = canonical_components.get('performance_overall_outcome')
        saved_base = _quality.weighted_service_reward(components)
        canonical_base = _quality.weighted_service_reward(canonical_components)
        if saved_base is not None and (
            canonical_base is None or abs(saved_base - canonical_base) > 0.000001
        ):
            raise ValueError("new_horizon_service_grade_not_canonical")
        if canonical_grade is not None:
            if (
                type(grade) not in {int, float}
                or isinstance(grade, bool)
                or abs(float(grade) - canonical_grade) > 0.000001
            ):
                raise ValueError("new_horizon_service_grade_not_canonical")
            grade = canonical_grade
    return RefereeProof(
        proof_id=str(payload.get("proof_id") or ""),
        assessment_id=str(payload.get("assessment_id") or ""),
        decision_id=str(payload.get("decision_id") or ""),
        scope_key=scope_key,
        learning_context_key=stored_learning_key,
        subject_kind=str(payload.get("subject_kind") or ""),
        origin=str(payload.get("origin") or ""),
        target=referee_target_from_history_record(row),
        dimension=dimension,
        status=str(payload.get("status") or ""),
        grade=grade,
        final_assessment=payload.get("final_assessment") is True,
        reasons=tuple(payload.get("reasons") or ()),
        opened_at=float(payload.get("opened_at") or 0.0),
        due_at=float(payload.get("due_at") or 0.0),
        closed_at=float(payload.get("closed_at") or 0.0),
    )


class RefereeAssessmentHistoryOwner:
    """Read, validate and group saved proof once for every consumer."""

    def __init__(self, history: AdvisoryHistory, *, timezone: str = "local") -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_referee_history_owner_store_required")
        self._history = history
        self._snapshot: RefereeAssessmentHistorySnapshot | None = None
        self._timezone = timezone
        self._aq_status: dict[str, Any] = {}

    @property
    def history(self) -> AdvisoryHistory:
        """Return the one durable store used by this owner."""

        return self._history

    def _retained_snapshot(
        self,
        snapshot: RefereeAssessmentHistorySnapshot,
        *,
        now: float | None,
    ) -> RefereeAssessmentHistorySnapshot:
        if now is None:
            return snapshot
        if (
            isinstance(now, bool)
            or type(now) not in {int, float}
            or not math.isfinite(float(now))
            or float(now) <= 0.0
        ):
            raise ValueError("new_horizon_referee_history_retention_time_invalid")
        cutoff = float(now) - self._history.retention_seconds
        assessments = tuple(
            assessment
            for assessment in snapshot.assessments
            if assessment.closed_at >= cutoff
        )
        hours = tuple(row for row in snapshot.aq_hours if row["last_at"] >= cutoff)
        visits = tuple(row for row in snapshot.partial_visits if row["closed_at"] >= cutoff)
        if (len(assessments) == len(snapshot.assessments) and len(hours) == len(snapshot.aq_hours)
                and len(visits) == len(snapshot.partial_visits)):
            return snapshot
        return RefereeAssessmentHistorySnapshot(
            continuity_key=snapshot.continuity_key,
            assessments=assessments,
            rejected=snapshot.rejected,
            aq_hours=hours,
            partial_visits=visits,
            clock_timezone=snapshot.clock_timezone,
        )

    def _compact_hourly(
        self, snapshot: RefereeAssessmentHistorySnapshot, *, now: float,
    ) -> RefereeAssessmentHistorySnapshot:
        saved = self._history.load_checkpoint(
            scope_key=snapshot.continuity_key, name=AQ_HOURLY_CHECKPOINT,
        )
        if saved is not None and saved.get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA:
            raise ValueError("new_horizon_hourly_aq_checkpoint_schema_invalid")
        cutoff = now - self._history.retention_seconds
        hours = {}
        for raw in (saved or {}).get("hours", ()):
            hour = require_aq_hour(raw)
            if hour["continuity_key"] != snapshot.continuity_key:
                raise ValueError("new_horizon_hourly_aq_scope_mismatch")
            if hour["last_at"] >= cutoff:
                hours[hour["hour_id"]] = hour
        excluded = {key: stamp for key, stamp in (saved or {}).get("excluded", {}).items()
                    if stamp >= cutoff}
        known = {row["assessment_id"] for hour in hours.values() for row in hour["intervals"]}
        rejected = {row["event_key"]: row for row in snapshot.rejected}
        continuous = []
        for assessment in snapshot.assessments:
            first = assessment.payloads[0]
            if first["subject_kind"] != "active_context_window":
                continue
            continuous.append(assessment)
            if assessment.closed_at > now or assessment.assessment_id in excluded:
                continue
            proof = assessment.proofs[0]
            if not (proof.status == "graded" and proof.final_assessment):
                continue
            aq = None if proof.grade is None else proof.grade / 3.0
            if aq is None:
                continue
            try:
                block = make_aq_block(
                    assessment_id=assessment.assessment_id,
                    continuity_key=snapshot.continuity_key,
                    identity=first["result_identity"],
                    opened_at=proof.opened_at, ended_at=proof.due_at,
                    aq=aq * 3.0, observed_vector=referee_hourly_storage_vector(first),
                )
            except (TypeError, ValueError) as exc:
                key = "hourly:" + assessment.assessment_id
                rejected[key] = {"event_key": key, "reason": str(exc)}
                continue
            start, end, _, _ = fixed_hour(block["opened_at"], timezone=self._timezone)
            if block["ended_at"] > end + 0.000001:
                excluded[assessment.assessment_id] = assessment.closed_at
                continue
            previous = next((hour for hour in hours.values()
                             if hour["hour_start"] == start and hour["identity"] == block["identity"]), None)
            hour = merge_aq_block(previous, block, timezone=self._timezone)
            hours[hour["hour_id"]] = hour
            known.add(assessment.assessment_id)
        # Raw AQ is a bounded display ring, never a second learning population.
        keep = {item.assessment_id for item in sorted(continuous, key=lambda a:a.closed_at)[-8:]}
        retire = tuple(record["event_key"] for item in continuous
                       if item.assessment_id not in keep and
                       (item.assessment_id in known or item.assessment_id in excluded)
                       for record in item.records)
        retired_keys = set(retire)
        # Once an imported AQ block has a canonical hour (or the approved
        # crossing exclusion), its retired formula copies are no longer facts
        # to replay. Retired transition copies remain diagnostic originals.
        converted_lineages = set()
        for item in continuous:
            if item.assessment_id not in known and item.assessment_id not in excluded:
                continue
            provenance = item.payloads[0].get("replay_provenance")
            if isinstance(provenance, Mapping):
                converted_lineages.add((provenance.get("source_schema"),
                                        provenance.get("source_assessment_id")))
        if converted_lineages:
            legacy = self._history.query_scoped_kinds(
                scope_key=snapshot.continuity_key,
                kinds=tuple(kind for kind, _ in PRIOR_REFEREE_PROOF_CONTRACTS),
                limit_per_kind=None, since=cutoff)
            legacy_retire = []
            for rows in legacy.values():
                for row in rows:
                    payload = row["payload"]
                    if payload.get("subject_kind") != "active_context_window":
                        continue
                    provenance = payload.get("replay_provenance") or {}
                    lineage = (provenance.get("source_schema") or payload.get("schema"),
                               provenance.get("source_assessment_id") or payload.get("assessment_id"))
                    if lineage in converted_lineages:
                        legacy_retire.append(row["event_key"])
            retire += tuple(legacy_retire)
        retained = tuple(item for item in snapshot.assessments
                         if not any(r["event_key"] in retired_keys for r in item.records))
        checkpoint = {
            "schema": AQ_HOURLY_CHECKPOINT_SCHEMA,
            "hours": sorted(hours.values(), key=lambda h:(h["hour_start"],h["hour_id"])),
            "excluded": excluded,
        }
        if checkpoint != saved or retire:
            self._history.save_checkpoint(
                scope_key=snapshot.continuity_key, name=AQ_HOURLY_CHECKPOINT,
                updated_at=now, payload=checkpoint, retire_event_keys=retire,
            )
        self._aq_status = {
            "aq_hour_count": len(hours),
            "aq_coverage_s": sum(h["coverage_s"] for h in hours.values()),
            "aq_converted_blocks": sum(len(h["intervals"]) for h in hours.values()),
            "aq_excluded_crossing_count": len(excluded),
            "aq_excluded_crossing_seconds": len(excluded) * FINAL_GRADE_WINDOW_S,
        }
        return RefereeAssessmentHistorySnapshot(
            continuity_key=snapshot.continuity_key, assessments=retained,
            rejected=tuple(rejected.values()), aq_hours=tuple(checkpoint["hours"]),
            partial_visits=snapshot.partial_visits,
            clock_timezone=self._timezone,
        )

    @staticmethod
    def _assessment(
        continuity_key: str,
        records: Iterable[Mapping[str, Any]],
    ) -> RefereeAssessmentEvidence:
        copied_records = tuple(
            {
                **dict(record),
                "payload": dict(record.get("payload") or {}),
            }
            for record in records
        )
        if not copied_records:
            raise ValueError("new_horizon_referee_history_assessment_required")
        if any(
            record.get("scope_key") != continuity_key
            or record.get("kind") not in READABLE_REFEREE_PROOF_KINDS
            for record in copied_records
        ):
            raise ValueError("new_horizon_referee_history_record_scope_invalid")
        payloads = tuple(dict(record["payload"]) for record in copied_records)
        first = payloads[0]
        identity = (
            str(first.get("assessment_id") or ""),
            str(first.get("decision_id") or ""),
            str(first.get("origin") or ""),
            str(first.get("learning_context_key") or ""),
            str(first.get("target_signature") or ""),
        )
        if not all(identity) or any(
            (
                str(payload.get("assessment_id") or ""),
                str(payload.get("decision_id") or ""),
                str(payload.get("origin") or ""),
                str(payload.get("learning_context_key") or ""),
                str(payload.get("target_signature") or ""),
            )
            != identity
            for payload in payloads
        ):
            raise ValueError("new_horizon_referee_history_assessment_mismatch")
        result_identity = first.get("result_identity")
        if not isinstance(result_identity, Mapping) or any(
            not isinstance(payload.get("result_identity"), Mapping)
            or dict(payload["result_identity"]) != dict(result_identity)
            for payload in payloads
        ):
            raise ValueError("new_horizon_referee_history_result_identity_mismatch")
        baseline_identity = first.get("baseline_identity")
        baseline_identity = (
            dict(baseline_identity) if isinstance(baseline_identity, Mapping) else {}
        )
        if any(
            dict(
                payload.get("baseline_identity")
                if isinstance(payload.get("baseline_identity"), Mapping)
                else {}
            )
            != baseline_identity
            for payload in payloads
        ):
            raise ValueError("new_horizon_referee_history_baseline_identity_mismatch")
        proofs = normalize_referee_assessment(
            _referee_proof_from_history_record(continuity_key, record)
            for record in copied_records
        )
        payload_by_dimension = {
            str(payload.get("dimension") or ""): payload for payload in payloads
        }
        record_by_dimension = {
            str(record["payload"].get("dimension") or ""): record
            for record in copied_records
        }
        ordered_payloads = tuple(
            payload_by_dimension[proof.dimension] for proof in proofs
        )
        ordered_records = tuple(
            record_by_dimension[proof.dimension] for proof in proofs
        )
        return RefereeAssessmentEvidence(
            assessment_id=identity[0],
            records=ordered_records,
            payloads=ordered_payloads,
            proofs=proofs,
            closed_at=max(float(proof.closed_at) for proof in proofs),
        )

    @classmethod
    def snapshot_from_records(
        cls,
        continuity_key: str,
        records: Iterable[Mapping[str, Any]],
        *,
        clock_timezone: str = "local",
    ) -> RefereeAssessmentHistorySnapshot:
        """Build the canonical snapshot from an explicit storage boundary."""

        if type(continuity_key) is not str or not continuity_key:
            raise ValueError("new_horizon_referee_history_owner_scope_required")
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        rejected: list[dict[str, str]] = []
        for record in records:
            payload = record.get("payload")
            assessment_id = (
                str(payload.get("assessment_id") or "")
                if isinstance(payload, Mapping)
                else ""
            )
            if not assessment_id:
                rejected.append(
                    {
                        "event_key": str(record.get("event_key") or ""),
                        "reason": "assessment_id_missing",
                    }
                )
                continue
            grouped[assessment_id].append(record)
        assessments: list[RefereeAssessmentEvidence] = []
        for assessment_id, rows in grouped.items():
            try:
                assessments.append(cls._assessment(continuity_key, rows))
            except (TypeError, ValueError) as exc:
                rejected.append(
                    {
                        "event_key": "assessment:" + assessment_id,
                        "reason": str(exc),
                    }
                )
        return RefereeAssessmentHistorySnapshot(
            continuity_key=continuity_key,
            assessments=tuple(sorted(assessments, key=lambda item: item.closed_at)),
            rejected=tuple(rejected),
            clock_timezone=clock_timezone,
        )

    def refresh(
        self,
        continuity_key: str,
        *,
        now: float | None = None,
    ) -> RefereeAssessmentHistorySnapshot:
        if type(continuity_key) is not str or not continuity_key:
            raise ValueError("new_horizon_referee_history_owner_scope_required")
        tombstone = self._history.load_checkpoint(
            scope_key=continuity_key, name=AQ_PROVIDER_ALIAS_TOMBSTONE,
        )
        if tombstone is not None:
            if tombstone.get("schema") != AQ_PROVIDER_ALIAS_TOMBSTONE_SCHEMA:
                raise ValueError("new_horizon_provider_alias_tombstone_schema_invalid")
            raise RuntimeError("new_horizon_provider_alias_history_scope_retired")
        records = self._history.query_scoped_kinds(
            scope_key=continuity_key,
            kinds=(*READABLE_REFEREE_PROOF_KINDS, REFEREE_PARTIAL_HISTORY_KIND),
            limit_per_kind=None,
            since=None if now is None else float(now) - self._history.retention_seconds,
        )
        visits = []
        rejected = []
        for record in records[REFEREE_PARTIAL_HISTORY_KIND]:
            try:
                visits.append(self._partial_visit(record, continuity_key))
            except (TypeError, ValueError, KeyError) as exc:
                rejected.append(dict(event_key=record["event_key"], reason=str(exc)))
        snapshot = self.snapshot_from_records(continuity_key,
                                              (row for kind in READABLE_REFEREE_PROOF_KINDS for row in records[kind]),
                                              clock_timezone=self._timezone)
        snapshot = self._retained_snapshot(
            replace(snapshot, partial_visits=tuple(visits), rejected=snapshot.rejected + tuple(rejected)),
            now=now,
        )
        snapshot = self._compact_hourly(snapshot, now=now if now is not None else time())
        self._snapshot = snapshot
        return snapshot

    @staticmethod
    def _partial_visit(record: Mapping[str, Any], continuity_key: str) -> dict[str, Any]:
        payload = dict(record["payload"])
        if (record["scope_key"] != continuity_key or payload["continuity_key"] != continuity_key
                or payload["schema"] != REFEREE_PARTIAL_EVIDENCE_SCHEMA
                or payload["maturity"] != "partial" or not payload["assessment_id"]
                or not isinstance(payload["observed_vector"], Mapping)):
            raise ValueError("new_horizon_cell_visit_contract_invalid")
        if not 0 < payload["closed_at"] - payload["opened_at"] <= FINAL_GRADE_WINDOW_S:
            raise ValueError("new_horizon_cell_visit_duration_invalid")
        # Validate the physical/time boundary before it can enter the snapshot.
        daily_cell_coverage((dict(identity=payload["identity"], opened_at=payload["opened_at"],
                                 closed_at=payload["closed_at"], timezone="local"),))
        return payload

    def admit_partial(self, *, event_key: str, continuity_key: str) -> None:
        record = self._history.load_evidence(event_key=event_key)
        if record is None:
            raise RuntimeError("new_horizon_cell_visit_evidence_missing")
        visit = self._partial_visit(record, continuity_key)
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("new_horizon_referee_history_snapshot_not_bound")
        if snapshot.continuity_key != continuity_key:
            # A router/provider change can close the previous visit after the
            # new snapshot is bound. It stays in its old History scope, never
            # in the new router's cell pattern; restore reads it if we return.
            return
        visits = {row["assessment_id"]: row for row in snapshot.partial_visits}
        if visit["assessment_id"] in visits and visits[visit["assessment_id"]] != visit:
            raise ValueError("new_horizon_cell_visit_identity_reused")
        visits[visit["assessment_id"]] = visit
        self._snapshot = self._retained_snapshot(
            replace(snapshot, partial_visits=tuple(visits.values())), now=visit["closed_at"])

    def migrate_provider_alias_hourly_aq(
        self,
        *,
        identity_code: str,
        profile_id: str,
        network_type: str,
        old_provider_name: str,
        canonical_provider_name: str,
        provider_plmn: str,
        updated_at: float,
    ) -> dict[str, Any]:
        """Move one stopped store's AQ hours across an approved provider rename."""

        from arch4.operator_profiles import (
            resolve_provider_name,
            resolve_provider_plmn_from_name,
        )

        values = (
            identity_code, profile_id, network_type, old_provider_name,
            canonical_provider_name, provider_plmn,
        )
        if not all(type(value) is str and value.strip() for value in values):
            raise ValueError("new_horizon_provider_alias_migration_identity_required")
        old_plmn = resolve_provider_plmn_from_name(old_provider_name)
        new_plmn = resolve_provider_plmn_from_name(canonical_provider_name)
        if (
            old_plmn != provider_plmn
            or new_plmn != provider_plmn
            or resolve_provider_name(provider_plmn) != canonical_provider_name
            or old_provider_name == canonical_provider_name
        ):
            raise ValueError("new_horizon_provider_alias_migration_not_approved")
        old_scope = continuity_key_from_values(
            identity_code=identity_code, profile_id=profile_id,
            provider_name=old_provider_name, network_type=network_type,
            quality_model=LTE_QUALITY_MODEL,
        )
        new_scope = continuity_key_from_values(
            identity_code=identity_code, profile_id=profile_id,
            provider_name=canonical_provider_name, network_type=network_type,
            quality_model=LTE_QUALITY_MODEL,
        )
        retired = self._history.load_checkpoint(
            scope_key=old_scope, name=AQ_PROVIDER_ALIAS_TOMBSTONE,
        )
        source = self._history.load_checkpoint(
            scope_key=old_scope, name=AQ_HOURLY_CHECKPOINT,
        )
        target = self._history.load_checkpoint(
            scope_key=new_scope, name=AQ_HOURLY_CHECKPOINT,
        )
        if retired is not None:
            if (
                retired.get("schema") != AQ_PROVIDER_ALIAS_TOMBSTONE_SCHEMA
                or retired.get("source_scope_key") != old_scope
                or retired.get("target_scope_key") != new_scope
                or source is not None
                or target is None
            ):
                raise RuntimeError("new_horizon_provider_alias_migration_tombstone_invalid")
            expected_ids = tuple(retired.get("target_hour_ids") or ())
            actual_ids = tuple(row["hour_id"] for row in target.get("hours", ()))
            if expected_ids != actual_ids:
                raise RuntimeError("new_horizon_provider_alias_migration_target_changed")
            return {
                "status": "already_applied", "source_scope": old_scope,
                "target_scope": new_scope, "hour_count": len(actual_ids),
                "coverage_s": sum(require_aq_hour(row)["coverage_s"] for row in target["hours"]),
            }
        if source is None:
            raise ValueError("new_horizon_provider_alias_migration_source_missing")
        if source.get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA:
            raise ValueError("new_horizon_hourly_aq_checkpoint_schema_invalid")
        if target is not None and (
            target.get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA
            or target.get("hours")
            or target.get("excluded")
        ):
            raise RuntimeError("new_horizon_provider_alias_migration_target_occupied")
        moved = []
        source_ids = []
        for raw in source.get("hours", ()):
            hour = require_aq_hour(raw)
            if hour["continuity_key"] != old_scope:
                raise ValueError("new_horizon_provider_alias_migration_source_scope_invalid")
            identity = hour["identity"]
            old_exact = radio_scope_key_from_values(
                identity_code=identity_code, profile_id=profile_id,
                provider_name=old_provider_name, network_type=identity["network_type"],
                serving_band=identity["pband"], configured_band_pool=identity["ca_pool"],
                cell_id=identity["cell_id"], earfcn_dl=identity["earfcn"], pci=identity["pci"],
                nr_band=identity["nr_band"], nr_arfcn=identity["nr_arfcn"],
                nr_pci=identity["nr_pci"], nr_cell_id=identity["nr_cell_id"],
            )
            if identity["scope_key"] != old_exact:
                raise ValueError("new_horizon_provider_alias_migration_exact_scope_invalid")
            new_exact = radio_scope_key_from_values(
                identity_code=identity_code, profile_id=profile_id,
                provider_name=canonical_provider_name, network_type=identity["network_type"],
                serving_band=identity["pband"], configured_band_pool=identity["ca_pool"],
                cell_id=identity["cell_id"], earfcn_dl=identity["earfcn"], pci=identity["pci"],
                nr_band=identity["nr_band"], nr_arfcn=identity["nr_arfcn"],
                nr_pci=identity["nr_pci"], nr_cell_id=identity["nr_cell_id"],
            )
            source_ids.append(hour["hour_id"])
            moved.append(rekey_aq_hour(hour, continuity_key=new_scope, scope_key=new_exact))
        if not moved:
            raise ValueError("new_horizon_provider_alias_migration_hours_required")
        moved.sort(key=lambda row: (row["hour_start"], row["hour_id"]))
        migrated = {
            "schema": AQ_HOURLY_CHECKPOINT_SCHEMA,
            "hours": moved,
            "excluded": dict(source.get("excluded") or {}),
        }
        tombstone = {
            "schema": AQ_PROVIDER_ALIAS_TOMBSTONE_SCHEMA,
            "source_scope_key": old_scope,
            "target_scope_key": new_scope,
            "provider_plmn": provider_plmn,
            "old_provider_name": old_provider_name,
            "canonical_provider_name": canonical_provider_name,
            "source_hour_ids": source_ids,
            "target_hour_ids": [row["hour_id"] for row in moved],
        }
        self._history.replace_checkpoint_scope(
            source_scope_key=old_scope, target_scope_key=new_scope,
            name=AQ_HOURLY_CHECKPOINT, updated_at=updated_at,
            target_payload=migrated, tombstone_name=AQ_PROVIDER_ALIAS_TOMBSTONE,
            tombstone_payload=tombstone,
        )
        self._history.flush()
        return {
            "status": "applied", "source_scope": old_scope,
            "target_scope": new_scope, "hour_count": len(moved),
            "coverage_s": sum(row["coverage_s"] for row in moved),
        }

    def current(
        self,
        continuity_key: str,
        *,
        now: float | None = None,
    ) -> RefereeAssessmentHistorySnapshot:
        snapshot = self._snapshot
        if snapshot is None or snapshot.continuity_key != continuity_key:
            raise RuntimeError("new_horizon_referee_history_snapshot_not_bound")
        retained = self._retained_snapshot(snapshot, now=now)
        if retained is not snapshot:
            self._snapshot = retained
            snapshot = retained
        return snapshot

    def admit_live(
        self,
        proofs: Iterable[RefereeProof],
    ) -> RefereeAssessmentEvidence:
        rows = normalize_referee_assessment(proofs)
        continuity_key = rows[0].scope_key
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("new_horizon_referee_history_snapshot_not_bound")
        if snapshot.continuity_key != continuity_key:
            raise RuntimeError("new_horizon_referee_history_live_scope_mismatch")
        records: list[Mapping[str, Any]] = []
        for proof in rows:
            record = self._history.load_evidence(event_key="proof:" + proof.proof_id)
            if record is None:
                raise RuntimeError("new_horizon_referee_history_live_evidence_missing")
            records.append(record)
        assessment = self._assessment(continuity_key, records)
        retained = {item.assessment_id: item for item in snapshot.assessments}
        retained[assessment.assessment_id] = assessment
        candidate = self._retained_snapshot(
            RefereeAssessmentHistorySnapshot(
                continuity_key=continuity_key,
                assessments=tuple(
                    sorted(retained.values(), key=lambda item: item.closed_at)
                ),
                rejected=snapshot.rejected,
                aq_hours=snapshot.aq_hours,
                partial_visits=snapshot.partial_visits,
                clock_timezone=snapshot.clock_timezone,
            ),
            now=max(proof.closed_at for proof in rows),
        )
        candidate = self._compact_hourly(candidate, now=max(proof.closed_at for proof in rows))
        self._snapshot = candidate
        return assessment

    def status(self) -> dict[str, Any]:
        """Report whether saved Referee proof has one usable interpretation."""

        snapshot = self._snapshot
        return {
            **self._aq_status,
            "owner": "54guc.new_horizon.referee_assessment_history",
            "bound": snapshot is not None,
            "continuity_key": snapshot.continuity_key if snapshot else "",
            "population_id": snapshot.population_id if snapshot else "",
            "assessment_count": snapshot.source_assessment_count if snapshot else 0,
            "record_count": len(snapshot.records) if snapshot else 0,
            "rejected_count": len(snapshot.rejected) if snapshot else 0,
            "recent_rejections": list(snapshot.rejected[-8:]) if snapshot else [],
        }


def _rules_proof(
    pending: "_PendingProof",
    *,
    service: RefereeProof,
    predicted_grades: Mapping[str, float | None] | None,
    internet_availability_pct: float | None,
    closed_at: float,
) -> RefereeProof:
    rules_prediction = (
        predicted_grades.get("rules_safety") if predicted_grades is not None else None
    )
    if type(rules_prediction) is not int:
        rules_status = "invalid"
        rules_grade = None
        rules_reason = "frozen_brain_rules_prediction_unavailable"
    elif rules_prediction <= 0:
        rules_status = "graded"
        rules_grade = -3
        rules_reason = "brain_executed_despite_negative_rules_prediction"
    elif (
        type(internet_availability_pct) in {int, float}
        and float(internet_availability_pct) <= 0.0
    ):
        rules_status = "graded"
        rules_grade = -3
        rules_reason = "decision_caused_complete_internet_outage"
    else:
        rules_status = "graded"
        rules_grade = 1
        rules_reason = "frozen_rules_clear_without_emergency_consequence"
    rules = RefereeProof(
        proof_id=_dimension_proof_id(pending.proof_id, "rules_safety"),
        assessment_id=pending.proof_id,
        decision_id=pending.decision_id,
        scope_key=pending.continuity_key,
        learning_context_key=pending.learning_context_key,
        subject_kind=pending.subject_kind,
        origin=pending.origin,
        target=service.target,
        dimension="rules_safety",
        status=rules_status,
        grade=rules_grade,
        final_assessment=True,
        reasons=(rules_reason,),
        opened_at=service.opened_at,
        due_at=service.due_at,
        closed_at=closed_at,
    )
    return rules


def radio_scope_key_from_values(
    *,
    identity_code: str,
    profile_id: Any,
    provider_name: Any,
    network_type: Any,
    serving_band: Any,
    configured_band_pool: Any,
    cell_id: Any,
    earfcn_dl: Any,
    pci: Any,
    nr_band: Any = None,
    nr_arfcn: Any = None,
    nr_pci: Any = None,
    nr_cell_id: Any = None,
) -> str:
    """Hash already accepted radio facts using the live context contract."""

    if type(identity_code) is not str or not identity_code:
        raise ValueError("new_horizon_scope_identity_code_required")
    named = (
        ("identity.profile_id", profile_id),
        ("identity.provider_name", provider_name),
        ("radio.network_type", network_type),
        ("radio.serving_band", serving_band),
        ("radio.configured_band_pool", configured_band_pool),
        ("rf.cell_id", cell_id),
        ("rf.earfcn_dl", earfcn_dl),
        ("rf.pci", pci),
    )
    values = [identity_code]
    values.extend(
        str(value) if value is not None else f"unavailable:{path}"
        for path, value in named
    )
    network_type_value = network_type
    if network_type_value == "NR NSA (5G)":
        for path, value in (
            ("radio.nr_band", nr_band),
            ("radio.nr_arfcn", nr_arfcn),
            ("radio.nr_pci", nr_pci),
        ):
            values.append(
                str(value) if value is not None else f"unavailable:{path}"
            )
        if nr_cell_id is not None:
            values.append("nr_nci:" + str(nr_cell_id))
    raw = "|".join(values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_scope_key(context: IntelligenceContext) -> str:
    def value(path: str) -> Any:
        fact = context.fact(path)
        return fact.value.require("value") if fact.disposition == "available" else None

    confirmed = resolve_continuity(identity_code=context.identity_code,
        values={**{path: value(path) for path in CONTINUITY_PATHS},
                "quality_model": context.surface("quality").value.require("quality_model")},
        confirmed=context.confirmed_continuity.to_mapping())
    return radio_scope_key_from_values(
        identity_code=learning_device_key({**confirmed,
            **{key: context.confirmed_continuity.get(key) for key in LEARNING_SCOPE_FIELDS
               if key in context.confirmed_continuity.to_mapping()}}),
        profile_id=value("identity.profile_id"),
        provider_name=value("identity.provider_name"),
        network_type=value("radio.network_type"),
        serving_band=value("radio.serving_band"),
        configured_band_pool=value("radio.configured_band_pool"),
        cell_id=value("rf.cell_id"),
        earfcn_dl=value("rf.earfcn_dl"),
        pci=value("rf.pci"),
        nr_band=value("radio.nr_band"),
        nr_arfcn=value("radio.nr_arfcn"),
        nr_pci=value("radio.nr_pci"),
        nr_cell_id=value("radio.nr_cell_id"),
    )


def context_learning_components(
    context: IntelligenceContext,
) -> dict[str, Any]:
    """Return the exact situation identity used for learning."""

    return build_learning_context(
        exact_scope_key=context_scope_key(context),
        observed_at=context.accepted_at,
    )


def _learning_context_key(components: Mapping[str, Any]) -> str:
    return learning_context_key(components)


def context_learning_key(context: IntelligenceContext) -> str:
    return _learning_context_key(context_learning_components(context))


def continuity_key_from_values(
    *, identity_code: str, profile_id: Any, provider_name: Any, network_type: Any,
    quality_model: str,
) -> str:
    """Hash accepted continuity facts; the offline adapter uses this same owner."""

    return continuity_digest(resolve_continuity(identity_code=identity_code,
        values={**dict(zip(CONTINUITY_PATHS, (profile_id, provider_name, network_type))),
                "quality_model": quality_model},
        confirmed={}))


def context_continuity_key(context: IntelligenceContext) -> str:
    """Return identity that remains valid across a radio target change."""

    def value(path: str) -> Any:
        fact = context.fact(path)
        return fact.value.require("value") if fact.disposition == "available" else None

    binding = context.confirmed_continuity.to_mapping()
    return continuity_digest(resolve_continuity(
        identity_code=context.identity_code,
        values={**{path: value(path) for path in CONTINUITY_PATHS},
                "quality_model": context.surface("quality").value.require("quality_model"),
                **{key: binding[key] for key in LEARNING_SCOPE_FIELDS if key in binding}},
        confirmed=binding))


def context_action_lifecycle_key(context: IntelligenceContext) -> str:
    """Return the stable identity of one in-flight physical action.

    Provider and RAT display values are excluded because they may change while
    the radio reconnects. A new runtime namespace, physical device, or selected
    profile means this is a different action.
    """

    profile = context.fact("identity.profile_id")
    if profile.disposition != "available":
        return ""
    profile_id = str(profile.value.get("value") or "").strip()
    if not profile_id:
        return ""
    raw = json.dumps(
        {
            "namespace": context.matrix_identity.namespace.as_dict(),
            "identity_code": context.identity_code,
            "profile_id": profile_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_continuity_observation_state(
    context: IntelligenceContext,
    *,
    expected_key: str,
) -> str:
    """Classify provider and RAT continuity without treating missing data as change."""

    if type(expected_key) is not str or not expected_key:
        raise ValueError("new_horizon_expected_continuity_key_required")
    for path in (
        "identity.profile_id",
        "identity.provider_name",
        "radio.network_type",
    ):
        fact = context.fact(path)
        if fact.disposition != "available":
            return "unavailable"
        value = str(fact.value.get("value") or "").strip()
        if continuity_value(path, value) is None:
            return "unavailable"
    return "matches" if context_continuity_key(context) == expected_key else "changed"


def _namespace_key(context: IntelligenceContext) -> str:
    return _namespace_identity_key(context.matrix_identity)


def _namespace_identity_key(identity: Any) -> str:
    raw = json.dumps(
        identity.namespace.as_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _surface_value(
    context: IntelligenceContext,
    surface_name: str,
    field: str,
) -> float | None:
    surface = context.surface(surface_name)
    if not surface.valid or surface.stale:
        return None
    value = surface.value.get(field)
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    return float(value)


def _qualified_surface_value(
    context: IntelligenceContext,
    surface_name: str,
    field: str,
    *,
    valid_field: str,
) -> float | None:
    surface = context.surface(surface_name)
    if not surface.valid or surface.stale:
        return None
    if surface.value.get(valid_field) is not True:
        return None
    value = surface.value.get(field)
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    return float(value)


def _fact_value(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    return (
        fact.value.require("value")
        if fact.disposition == "available" and fact.retention is None
        else None
    )


def _stable_fact_value(context: IntelligenceContext, path: str) -> Any | None:
    """Read the stable Matrix value even when this sample retained it."""

    fact = context.fact(path)
    return fact.value.require("value") if fact.disposition == "available" else None


def context_discloses_ca(context: IntelligenceContext) -> bool:
    """Use Modem Live's capability, not a vendor name or an allowed band mask."""
    capability = _stable_fact_value(context, "capability.runtime_capabilities")
    return isinstance(capability, Mapping) and capability.get("ca_details") is True


def _target_fact(
    context: IntelligenceContext,
    path: str,
) -> tuple[Any | None, bool]:
    """Return the current Matrix value and whether this sample refreshed it."""

    fact = context.fact(path)
    if fact.disposition != "available":
        return None, False
    return fact.value.require("value"), fact.retention is None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _latency_observation(context: IntelligenceContext) -> dict[str, Any]:
    surface = context.surface("latency")
    if not surface.valid:
        return {}
    raw = surface.value.get("rtt_observation")
    return dict(raw) if isinstance(raw, Mapping) else {}





def referee_sample_metrics(context: IntelligenceContext) -> dict[str, Any]:
    rtt = _latency_observation(context)
    has_traffic = _stable_fact_value(context, "traffic.has_traffic")
    latency_surface = context.surface("latency")
    ca_surface = context.surface("ca")
    primary_band = _stable_fact_value(context, "radio.serving_band")
    ca_observation = (
        capture_ca_observation(ca_surface.value.to_mapping(),
            primary_band=primary_band,
            accepted_at=context.accepted_at)
        if context_discloses_ca(context) and ca_surface.valid and not ca_surface.stale
        and type(primary_band) is int and primary_band > 0
        and _stable_fact_value(context, "radio.network_type") != "NR SA (5G)"
        else None
    )
    latency_value = (
        latency_surface.value
        if latency_surface.valid and not latency_surface.stale
        else None
    )
    metrics = {
        "quality_model": require_quality_model(context.surface("quality").value.require("quality_model")),
        "quality_capacity_fraction": _qualified_surface_value(context, "quality", "quality_capacity_fraction", valid_field="quality_valid"),
        "quality_total_bw_mhz": _qualified_surface_value(context, "quality", "quality_total_bw_mhz", valid_field="quality_valid"),
        "quality_radio_inputs": context.surface("quality").value.get("quality_radio_inputs"),
        "has_traffic": has_traffic,
        "nr_active": (_fact_value(context,'radio.nr5g_active') is True
            and _fact_value(context,'radio.network_type') in {'NR NSA (5G)','NR SA (5G)'}),
        "ca_observation": ca_observation,
        "low_load_latency_valid": (
            latency_value is not None
            and latency_value.get("rtt_low_load_latency_valid") is True
        ),
        "low_load_latency_reason": str(
            latency_value.get("rtt_low_load_latency_reason")
            if latency_value is not None
            else ""
        ),
        "low_load_latency_mbps": _number(
            latency_value.get("rtt_low_load_latency_mbps")
            if latency_value is not None
            else None
        ),
        "dl_bandwidth_mhz": _number(
            _stable_fact_value(context, "slow.dl_bandwidth_mhz")
        ),
        "congestion_pct": _surface_value(
            context,
            "quality",
            "congestion_pct",
        ),
        "rtt_probe_sequence": rtt.get("probe_sequence"),
        "rtt_probe_status": rtt.get("probe_status"),
        "rtt_freshness": rtt.get("freshness"),
        "rtt_value_ms": rtt.get("value_ms"),
        "rtt_observed_at": rtt.get("observed_at"),
        # Signal Processing Matrix calculates jitter. Referee uses its qualified
        # result and does not calculate jitter again from RTT.
        "jitter_ms": _qualified_surface_value(
            context,
            "latency",
            "rtt_dl_jitter_trimmed_ms",
            valid_field="rtt_dl_jitter_valid",
        ),
        "cq_cell": _qualified_surface_value(
            context,
            "quality",
            "cq_cell",
            valid_field="cq_cell_valid",
        ),
        "cqb_cell": _qualified_surface_value(
            context,
            "quality",
            "cqb_cell",
            valid_field="cqb_cell_valid",
        ),
        "bq_cell": _qualified_surface_value(
            context,
            "quality",
            "bq_cell",
            valid_field="bq_cell_valid",
        ),
        "rsrp_dbm": _fact_value(context, "rf.rsrp_dbm"),
        "rsrq_db": _fact_value(context, "rf.rsrq_db"),
        "sinr_db": _fact_value(context, "rf.sinr_db"),
        "ca_active_pct": _surface_value(context, "ca", "ca_active_pct"),
        "ri_stability_pct": _surface_value(
            context,
            "quality",
            "ri_stability_pct",
        ),
        # This is supporting proof only. It does not affect the service grade.
        "max_dl_candidate_mbps": _fact_value(context, "traffic.dl_mbps"),
    }
    metrics["eci_points"] = _quality.capacity_points(metrics)
    return metrics


def _current_probe_attempt_sample(
    context: IntelligenceContext,
    *,
    scope_key: str,
    identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read one current ping attempt without requiring a fresh RTT value."""

    metrics = referee_sample_metrics(context)
    sequence = metrics.get("rtt_probe_sequence")
    if (
        type(sequence) is not int
        or sequence <= 0
        or metrics.get("rtt_probe_status") not in {"ok", "failed"}
    ):
        return None
    return {
        **metrics,
        "accepted_at": context.accepted_at,
        "scope_key": scope_key,
        "identity": dict(identity),
    }


def _append_unique_probe_sample(
    rows: tuple[dict[str, Any], ...],
    sample: dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    if sample is None:
        return rows
    sequence = sample["rtt_probe_sequence"]
    if any(row.get("rtt_probe_sequence") == sequence for row in rows):
        return rows
    return (*rows, sample)


def _qualified_probe_latency(
    probes: tuple[dict[str, Any], ...],
) -> tuple[float | None, int]:
    values = tuple(
        float(row["rtt_value_ms"])
        for row in probes
        if row.get("low_load_latency_valid") is True
        and row.get("rtt_probe_status") == "ok"
        and row.get("rtt_freshness") == "fresh"
        and _number(row.get("rtt_value_ms")) is not None
    )
    return (
        float(median(values)) if values else None,
        len(values),
    )


def referee_service_vector(
    rows: Iterable[dict[str, Any]],
    *,
    reachability_receipt: CoreReachabilityIntervalReceipt | None = None,
) -> dict[str, Any]:
    values = tuple(rows)
    averaged: dict[str, Any] = {}
    for key in (
        "dl_bandwidth_mhz",
        "eci_points",
        "congestion_pct",
        "cq_cell",
        "cqb_cell",
        "bq_cell",
        "rsrp_dbm",
        "rsrq_db",
        "sinr_db",
        "ca_active_pct",
        "ri_stability_pct",
    ):
        samples = [
            float(row[key]) for row in values if _number(row.get(key)) is not None
        ]
        averaged[key] = sum(samples) / len(samples) if samples else None
    traffic_samples = tuple(
        float(row["max_dl_candidate_mbps"])
        for row in values
        if row.get("has_traffic") is True
        and _number(row.get("max_dl_candidate_mbps")) is not None
    )
    averaged["has_traffic"] = any(row.get("has_traffic") is True for row in values)
    averaged["has_traffic_pct"] = (
        100.0 * sum(row.get("has_traffic") is True for row in values) / len(values)
        if values
        else None
    )
    averaged["max_dl_mbps"] = max(traffic_samples, default=None)
    averaged["dl_sample_count"] = len(traffic_samples)
    probes_by_sequence: dict[int, dict[str, Any]] = {}
    for row in values:
        sequence = row.get("rtt_probe_sequence")
        if type(sequence) is int and sequence > 0:
            previous = probes_by_sequence.get(sequence)
            if (
                previous is None
                or previous.get("rtt_freshness") != "fresh"
                or row.get("rtt_freshness") == "fresh"
            ):
                probes_by_sequence[sequence] = row
    probes = tuple(probes_by_sequence[key] for key in sorted(probes_by_sequence))
    latency, latency_count = _qualified_probe_latency(probes)
    averaged["latency_ms"] = latency
    averaged["latency_rtt_sample_count"] = latency_count
    jitter_values = tuple(
        float(row["jitter_ms"])
        for row in probes
        if row.get("rtt_probe_status") == "ok"
        and row.get("rtt_freshness") == "fresh"
        and _number(row.get("jitter_ms")) is not None
    )
    averaged["jitter_ms"] = float(median(jitter_values)) if jitter_values else None
    averaged["jitter_sample_count"] = len(jitter_values)
    if reachability_receipt is not None and not isinstance(
        reachability_receipt,
        CoreReachabilityIntervalReceipt,
    ):
        raise TypeError("referee_core_interval_evidence_required")
    if (
        reachability_receipt is not None
        and reachability_receipt.coverage
        is not ReachabilityIntervalCoverage.UNAVAILABLE
    ):
        averaged["internet_availability_pct"] = reachability_receipt.availability_pct
        averaged["internet_probe_count"] = reachability_receipt.attempt_count
        averaged["internet_excluded_probe_count"] = (
            reachability_receipt.excluded_attempt_count
        )
        averaged["internet_evidence_state"] = reachability_receipt.coverage.value
        averaged["internet_current_state"] = (
            "reachable" if reachability_receipt.latest_reachable else "outage"
        )
    else:
        averaged["internet_availability_pct"] = None
        averaged["internet_probe_count"] = 0
        averaged["internet_excluded_probe_count"] = 0
        averaged["internet_evidence_state"] = (
            "unavailable"
            if reachability_receipt is None
            else reachability_receipt.reason
        )
        averaged["internet_current_state"] = "unknown"
    averaged["sample_count"] = len(values)
    models = {require_quality_model(row["quality_model"]) for row in values}
    if {LTE_QUALITY_MODEL, NR_QUALITY_MODEL} <= models:
        raise ValueError("referee_quality_models_mixed")
    averaged["quality_model"] = next(iter(models)) if len(models) == 1 else UNKNOWN_QUALITY_MODEL
    if averaged["quality_model"] != LTE_QUALITY_MODEL and values:
        for key in ("quality_capacity_fraction", "quality_total_bw_mhz"):
            numbers = [float(row[key]) for row in values if _number(row.get(key)) is not None]
            averaged[key] = sum(numbers)/len(numbers) if numbers else None
    # Mixed or unavailable samples cannot turn retained NR into active service.
    averaged['nr_active']=(all(row.get('nr_active') is True for row in values)
        if values and all(type(row.get('nr_active')) is bool for row in values) else None)
    accepted_times = [
        float(row["accepted_at"])
        for row in values
        if type(row.get("accepted_at")) in {int, float}
    ]
    averaged["observed_at"] = max(accepted_times, default=None)
    averaged.update(summarize_ca_observations(values))
    return _quality.project_service_vector(averaged)


def _context_reachability_evidence(
    context: IntelligenceContext,
) -> CoreReachabilityEvidenceView:
    surface = context.surface("reachability_evidence")
    if not surface.valid or surface.stale:
        raise RuntimeError("referee_core_reachability_evidence_unavailable")
    return CoreReachabilityEvidenceView.from_mapping(surface.value.to_mapping())


def _context_reachability_receipt(
    context: IntelligenceContext,
    *,
    opened_at: float,
    ended_at: float,
    complete: bool,
) -> CoreReachabilityIntervalReceipt:
    return _reachability_receipt_from_evidence(
        _context_reachability_evidence(context),
        opened_at=opened_at,
        ended_at=ended_at,
        complete=complete,
    )


def _reachability_receipt_from_evidence(
    evidence: CoreReachabilityEvidenceView,
    *,
    opened_at: float,
    ended_at: float,
    complete: bool,
) -> CoreReachabilityIntervalReceipt:
    return core_reachability_interval_receipt(
        evidence,
        interval_started_at=opened_at,
        interval_ended_at=ended_at,
        complete=complete,
    )

















def current_context_target(
    context: IntelligenceContext,
) -> RefereeTarget | None:
    """Return the actionable radio target for the current context."""

    network_type = _stable_fact_value(context, "radio.network_type")
    current_band = _stable_fact_value(context, "radio.serving_band")
    current_pool_raw = _stable_fact_value(
        context,
        "radio.configured_band_pool",
    )
    current_pool = (
        tuple(current_pool_raw) if isinstance(current_pool_raw, (list, tuple)) else ()
    )
    if (
        network_type != "NR SA (5G)"
        and type(current_band) is int
        and current_band > 0
        and current_pool
        and all(type(band) is int and band > 0 for band in current_pool)
        and current_band in current_pool
    ):
        target = CorePBandTarget(
            pband=current_band,
            ca_pool=current_pool,
        )
        return RefereeTarget(
            action_family="lte_radio_selection",
            target_signature=core_lte_pband_signature(
                target.pband,
                target.ca_pool,
            ),
            target=CoreFrozenPayload.from_mapping(core_command_target_payload(target)),
        )
    return None


def context_radio_identity(context: IntelligenceContext) -> dict[str, Any]:
    """Capture the accepted radio identity for one Referee stage."""

    target = current_context_target(context)
    if target is None:
        return {}
    network_type = _stable_fact_value(context, "radio.network_type")
    pband = _stable_fact_value(context, "radio.serving_band")
    pool_raw = _stable_fact_value(context, "radio.configured_band_pool")
    pool = tuple(pool_raw) if isinstance(pool_raw, (list, tuple)) else ()
    cell_id = _stable_fact_value(context, "rf.cell_id")
    earfcn = _stable_fact_value(context, "rf.earfcn_dl")
    pci = _stable_fact_value(context, "rf.pci")
    nr_band = _stable_fact_value(context, "radio.nr_band")
    nr_cell_id = _stable_fact_value(context, "radio.nr_cell_id")
    nr_arfcn = _stable_fact_value(context, "radio.nr_arfcn")
    nr_pci = _stable_fact_value(context, "radio.nr_pci")
    lte_available = bool(
        network_type != "NR SA (5G)"
        and type(pband) is int
        and pband > 0
        and pool
        and all(type(band) is int and band > 0 for band in pool)
        and type(cell_id) is int
        and cell_id > 0
        and type(earfcn) is int
        and earfcn >= 0
        and type(pci) is int
        and pci >= 0
    )
    nr_available = bool(
        network_type == "NR NSA (5G)"
        and nr_identity_values_valid(band=nr_band, arfcn=nr_arfcn, pci=nr_pci, nci=nr_cell_id)
    )
    if (
        not (lte_available or nr_available)
        or (network_type == "NR NSA (5G)" and not (lte_available and nr_available))
        or network_type == "NR SA (5G)"
    ):
        return {}
    return {
        "target_signature": target.target_signature,
        "scope_key": context_scope_key(context),
        "network_type": str(network_type or ""),
        "pband": pband if lte_available else None,
        "ca_pool": list(pool) if lte_available else [],
        "cell_id": cell_id if lte_available else None,
        "earfcn": earfcn if lte_available else None,
        "pci": pci if lte_available else None,
        "nr_band": nr_band if nr_available else None,
        "nr_cell_id": nr_cell_id if nr_available else None,
        "nr_arfcn": nr_arfcn if nr_available else None,
        "nr_pci": nr_pci if nr_available else None,
    }


def _proof_context_qualified(context: IntelligenceContext) -> bool:
    quality = context.surface("quality")
    return bool(
        context.complete
        and context.sequence_gap == 0
        and quality.value.get("nr_transition_dirty") is not True
        and all(
            context.surface(name).valid and not context.surface(name).stale
            for name in ("quality", "latency")
        )
    )


def _target_state(
    target: RefereeTarget,
    context: IntelligenceContext,
) -> str:
    command_target = core_command_target_from_payload(target.target.to_mapping())
    current_band, band_fresh = _target_fact(context, "radio.serving_band")
    current_pool_raw, pool_fresh = _target_fact(
        context,
        "radio.configured_band_pool",
    )
    current_pool = (
        tuple(current_pool_raw) if isinstance(current_pool_raw, (list, tuple)) else ()
    )
    if isinstance(command_target, CoreBandSetTarget):
        target_pool = command_target.bands
        if not target_pool or not current_pool:
            return "unavailable"
        if current_pool == target_pool:
            return "matches"
        return "changed" if pool_fresh else "unavailable"
    if isinstance(command_target, CorePBandTarget):
        target_band = command_target.pband
        target_pool = command_target.ca_pool
        if type(target_band) is not int or not target_pool:
            return "unavailable"
        if current_band is None or not current_pool:
            return "unavailable"
        if current_band != target_band:
            return "changed" if band_fresh else "unavailable"
        if current_pool != target_pool:
            return "changed" if pool_fresh else "unavailable"
        return "matches"
    return "matches"


def _exact_result_target(
    target: RefereeTarget,
    context: IntelligenceContext,
) -> RefereeTarget:
    """Bind delayed evidence to the observed PBAND and CA pool."""

    current_band = _fact_value(context, "radio.serving_band")
    current_pool_raw = _fact_value(context, "radio.configured_band_pool")
    current_pool = (
        tuple(current_pool_raw) if isinstance(current_pool_raw, (list, tuple)) else ()
    )
    if (
        type(current_band) is int
        and current_pool
        and all(type(band) is int and band > 0 for band in current_pool)
        and current_band in current_pool
    ):
        return RefereeTarget(
            action_family="lte_radio_selection",
            target_signature=core_lte_pband_signature(
                current_band,
                current_pool,
            ),
            target=CoreFrozenPayload.from_mapping(
                core_command_target_payload(
                    CorePBandTarget(
                        pband=current_band,
                        ca_pool=current_pool,
                    )
                )
            ),
        )
    return target


@dataclass(slots=True)
class _PendingProof:
    proof_id: str
    decision_id: str
    baseline_scope_key: str
    learning_context_key: str
    learning_context: dict[str, Any]
    continuity_key: str
    namespace_key: str
    subject_kind: str
    origin: str
    target: RefereeTarget
    opened_at: float
    windows_s: tuple[float, ...]
    baseline: dict[str, Any]
    baseline_identity: dict[str, Any]
    result_scope_key: str = ""
    samples: tuple[dict[str, Any], ...] = ()
    completed: bool = False
    command_id: str = ""

    @property
    def due_at(self) -> float:
        return self.opened_at + self.windows_s[-1]


@dataclass(slots=True)
class _ActiveContext:
    scope_key: str
    continuity_key: str
    namespace_key: str
    target: RefereeTarget
    opened_at: float
    latest: dict[str, Any]
    latest_identity: dict[str, Any]
    latest_reachability_evidence: CoreReachabilityEvidenceView
    evaluated_at: float
    learning_context: dict[str, Any]
    completed_count: int = 0
    initial_samples: tuple[dict[str, Any], ...] = ()
    identity_observations: tuple[tuple[float, bool], ...] = ()


class ProofReferee:
    """Collect transition proof and recurring context-quality assessments."""

    def __init__(
        self,
        history: AdvisoryHistory,
        *,
        windows_s: tuple[float, ...] = (600.0,),
        assessment_history: RefereeAssessmentHistoryOwner,
        decision_history: DecisionLifecycleHistoryOwner,
    ) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_referee_history_required")
        if (
            not isinstance(windows_s, tuple)
            or len(windows_s) != 1
            or any(type(value) not in {int, float} for value in windows_s)
        ):
            raise TypeError("new_horizon_referee_windows_required")
        windows = tuple(float(value) for value in windows_s)
        if (
            any(value <= 0.0 for value in windows)
            or tuple(sorted(set(windows))) != windows
        ):
            raise ValueError("new_horizon_referee_windows_invalid")
        self._history = history
        if not isinstance(assessment_history, RefereeAssessmentHistoryOwner):
            raise TypeError("new_horizon_referee_history_owner_required")
        self._assessment_history = assessment_history
        if self._assessment_history.history is not history:
            raise ValueError("new_horizon_referee_history_owner_store_mismatch")
        if not isinstance(decision_history, DecisionLifecycleHistoryOwner):
            raise TypeError("new_horizon_referee_decision_history_owner_required")
        if decision_history.history is not history:
            raise ValueError("new_horizon_referee_decision_history_store_mismatch")
        self._decision_history = decision_history
        self._windows_s = windows
        self._pending: dict[str, _PendingProof] = {}
        self._active: _ActiveContext | None = None
        self._recent_context_assessments: dict[str, dict[str, Any]] = {}
        self._restored_context_owner: tuple[str, str] | None = None
        self._last: RefereeProof | None = None

    @property
    def last(self) -> RefereeProof | None:
        return self._last

    @staticmethod
    def _baseline_reservation_event_key(
        subject_key: str,
        *,
        namespace_key: str,
        baseline_scope_key: str,
    ) -> str:
        if type(subject_key) is not str or not subject_key.strip():
            raise ValueError("new_horizon_referee_baseline_subject_required")
        if type(namespace_key) is not str or not namespace_key:
            raise ValueError("new_horizon_referee_baseline_namespace_required")
        if type(baseline_scope_key) is not str or not baseline_scope_key:
            raise ValueError("new_horizon_referee_baseline_scope_required")
        return (
            "referee-baseline:"
            + hashlib.sha256(
                (subject_key + "|" + namespace_key + "|" + baseline_scope_key).encode(
                    "utf-8"
                )
            ).hexdigest()
        )

    def _matching_live_baseline(
        self,
        *,
        namespace_key: str,
        baseline_scope_key: str,
        created_at: float,
    ) -> dict[str, Any] | None:
        assessments = tuple(
            item
            for item in self._recent_context_assessments.values()
            if item.get("namespace_key") == namespace_key
            and item.get("scope_key") == baseline_scope_key
            and item.get("maturity", "completed") == "completed"
            and float(item.get("closed_at") or 0.0) <= created_at
        )
        return (
            dict(
                max(
                    assessments,
                    key=lambda item: float(item["closed_at"]),
                )
            )
            if assessments
            else None
        )

    def reserve_subject_baseline(
        self,
        *,
        subject_key: str,
        context: IntelligenceContext,
        observed_at: float,
    ) -> bool:
        """Save Referee's completed before-window before dispatch."""

        if not isinstance(context, IntelligenceContext):
            raise TypeError("new_horizon_referee_baseline_context_required")
        stamp = float(observed_at)
        if not math.isfinite(stamp) or stamp <= 0.0:
            raise ValueError("new_horizon_referee_baseline_timestamp_invalid")
        namespace_key = _namespace_key(context)
        baseline_scope_key = context_scope_key(context)
        event_key = self._baseline_reservation_event_key(
            subject_key,
            namespace_key=namespace_key,
            baseline_scope_key=baseline_scope_key,
        )
        existing = self._history.load_evidence(event_key=event_key)
        if existing is not None:
            payload = existing.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != REFEREE_BASELINE_RESERVATION_SCHEMA
                or payload.get("subject_key") != subject_key
            ):
                raise ValueError("new_horizon_referee_baseline_reservation_conflict")
            return payload.get("available") is True
        continuity_key = context_continuity_key(context)
        learning_context = context_learning_components(context)
        baseline = self._matching_live_baseline(
            namespace_key=namespace_key,
            baseline_scope_key=baseline_scope_key,
            created_at=stamp,
        )
        payload: dict[str, Any] = {
            "schema": REFEREE_BASELINE_RESERVATION_SCHEMA,
            "subject_key": subject_key,
            "available": baseline is not None,
            "reason": (
                "completed_exact_baseline_frozen"
                if baseline is not None
                else "baseline_completed_assessment_unavailable_at_dispatch"
            ),
            "namespace_key": namespace_key,
            "baseline_scope_key": baseline_scope_key,
            "continuity_key": continuity_key,
            "learning_context_key": _learning_context_key(learning_context),
            "learning_context": learning_context,
            "reserved_at": stamp,
            "baseline": baseline or {},
        }
        recorded = self._history.record(
            event_key=event_key,
            scope_key=continuity_key,
            kind=REFEREE_BASELINE_RESERVATION_KIND,
            observed_at=stamp,
            payload=payload,
        )
        if not recorded:
            raise RuntimeError("new_horizon_referee_baseline_reservation_lost")
        return baseline is not None

    def reserved_subject_contract(
        self,
        *,
        subject_key: str,
        matrix_identity: MatrixGenerationIdentity,
    ) -> dict[str, Any] | None:
        """Return the saved subject contract when STM no longer has it."""

        if type(subject_key) is not str or not subject_key.strip():
            raise ValueError("new_horizon_referee_baseline_subject_required")
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("new_horizon_referee_baseline_identity_required")
        namespace_key = _namespace_identity_key(matrix_identity)
        matches = []
        for row in self._history.query_kind(
            kind=REFEREE_BASELINE_RESERVATION_KIND,
            limit=8192,
        ):
            payload = row.get("payload")
            if (
                isinstance(payload, Mapping)
                and payload.get("schema") == REFEREE_BASELINE_RESERVATION_SCHEMA
                and payload.get("subject_key") == subject_key
                and payload.get("namespace_key") == namespace_key
            ):
                matches.append(payload)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("new_horizon_referee_baseline_reservation_ambiguous")
        payload = matches[0]
        baseline_scope_key = str(payload.get("baseline_scope_key") or "")
        learning_context_key = str(payload.get("learning_context_key") or "")
        learning_context = payload.get("learning_context")
        if (
            not baseline_scope_key
            or not learning_context_key
            or not isinstance(learning_context, Mapping)
        ):
            raise ValueError("new_horizon_referee_baseline_subject_contract_incomplete")
        validated_learning = require_learning_context(learning_context)
        if learning_context_key != _learning_context_key(validated_learning):
            raise ValueError("new_horizon_referee_baseline_learning_context_mismatch")
        return {
            "baseline_scope_key": baseline_scope_key,
            "learning_context_key": learning_context_key,
            "learning_context": validated_learning,
        }

    def _reserved_subject_baseline(
        self,
        subject: RefereeSubject,
    ) -> tuple[dict[str, Any] | None, str]:
        namespace_key = _namespace_identity_key(subject.matrix_identity)
        row = self._history.load_evidence(
            event_key=self._baseline_reservation_event_key(
                subject.key,
                namespace_key=namespace_key,
                baseline_scope_key=subject.baseline_scope_key,
            )
        )
        if row is None:
            return None, "baseline_reservation_missing"
        payload = row.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != REFEREE_BASELINE_RESERVATION_SCHEMA
            or payload.get("subject_key") != subject.key
        ):
            raise ValueError("new_horizon_referee_baseline_reservation_invalid")
        if payload.get("available") is not True:
            return None, str(
                payload.get("reason")
                or "baseline_completed_assessment_unavailable_at_dispatch"
            )
        baseline = payload.get("baseline")
        if not isinstance(baseline, Mapping):
            raise ValueError("new_horizon_referee_reserved_baseline_invalid")
        if (
            payload.get("namespace_key") != namespace_key
            or payload.get("baseline_scope_key") != subject.baseline_scope_key
            or float(payload.get("reserved_at") or 0.0) > subject.created_at
            or str(baseline.get("scope_key") or "") != subject.baseline_scope_key
        ):
            raise ValueError("new_horizon_referee_reserved_baseline_mismatch")
        return dict(baseline), ""

    def admit_subject(
        self,
        subject: RefereeSubject,
    ) -> RefereeProof:
        if not isinstance(subject, RefereeSubject):
            raise TypeError("new_horizon_referee_subject_required")
        namespace_key = _namespace_identity_key(subject.matrix_identity)
        if is_brain_decision_origin(subject.origin):
            baseline, unavailable_reason = self._reserved_subject_baseline(subject)
        else:
            baseline = self._matching_live_baseline(
                namespace_key=namespace_key,
                baseline_scope_key=subject.baseline_scope_key,
                created_at=subject.created_at,
            )
            unavailable_reason = "baseline_completed_assessment_unavailable"
        if baseline is None:
            return self._finish_invalid(
                subject,
                scope_key="unavailable",
                reason=unavailable_reason,
            )
        baseline_vector = baseline.get("observed_vector")
        baseline_identity = baseline.get("identity")
        baseline_scope_key = str(baseline.get("scope_key") or "")
        continuity_key = str(baseline.get("continuity_key") or "")
        if (
            not isinstance(baseline_vector, Mapping)
            or not isinstance(baseline_identity, Mapping)
            or not baseline_scope_key
            or not continuity_key
        ):
            return self._finish_invalid(
                subject,
                scope_key=continuity_key or "unavailable",
                reason="baseline_completed_assessment_incomplete",
            )
        if subject.learning_context is None:
            return self._finish_invalid(
                subject,
                scope_key=continuity_key,
                reason="subject_learning_context_unavailable",
            )
        learning_context = require_learning_context(
            subject.learning_context.to_mapping()
        )
        if subject.learning_context_key != _learning_context_key(learning_context):
            raise ValueError("new_horizon_subject_learning_context_key_mismatch")
        if not subject.proof_ready or (
            subject.subject_kind == "core_command"
            and subject.terminal_status != "settled"
        ):
            return self._finish_invalid(
                subject,
                scope_key=continuity_key,
                reason="subject_terminal_proof_not_ready",
            )
        proof_id = hashlib.sha256(
            f"{subject.key}|{continuity_key}".encode("utf-8")
        ).hexdigest()
        if proof_id in self._pending:
            pending = self._pending[proof_id]
            # The earlier attempt may have installed the pending proof but
            # failed before its checkpoint entered History's critical lane.
            self._save_pending(pending.continuity_key, subject.terminal_at)
            return self._as_pending(pending)
        predecessor = None
        if subject.superseded_proof_id:
            predecessor = self._pending.get(subject.superseded_proof_id)
            if (predecessor is None
                    or predecessor.subject_kind != "external_radio_change"
                    or predecessor.namespace_key != namespace_key
                    or predecessor.continuity_key != continuity_key
                    or predecessor.baseline_scope_key != baseline_scope_key
                    or not 0.0 <= subject.terminal_at - predecessor.opened_at <= 30.0
                    or subject.terminal_at >= predecessor.due_at):
                raise ValueError("external_referee_predecessor_mismatch")
        pending = _PendingProof(
            proof_id=proof_id,
            decision_id=subject.key,
            baseline_scope_key=baseline_scope_key,
            learning_context_key=subject.learning_context_key,
            learning_context=learning_context,
            continuity_key=continuity_key,
            namespace_key=namespace_key,
            subject_kind=subject.subject_kind,
            origin=subject.origin,
            target=subject.target,
            opened_at=subject.terminal_at,
            windows_s=self._windows_s,
            baseline=dict(baseline_vector),
            baseline_identity=dict(baseline_identity),
            result_scope_key="",
            command_id=(subject.key.removeprefix("core-command:")
                        if subject.subject_kind == "core_command" else subject.command_id),
        )
        if predecessor is not None:
            retired = replace(self._as_pending(predecessor), status="invalid",
                closed_at=subject.terminal_at,
                reasons=("proof_superseded_by_linked_radio_frame",))
            self._record(retired)
            self._pending.pop(predecessor.proof_id)
        self._pending[proof_id] = pending
        self._save_pending(pending.continuity_key, subject.terminal_at)
        proof = self._as_pending(pending)
        self._last = proof
        return proof

    def prepare_external_subject(
        self,
        subject: RefereeSubject,
        *,
        recent_contexts: tuple[IntelligenceContext, ...],
    ) -> RefereeSubject:
        """Keep the completed before-window across a short, linked radio change.

        A modem can publish the cell before the band. That intermediate frame
        is not a completed baseline. Only an already pending external change
        can carry its original baseline forward; this is not a history search.
        """
        if subject.subject_kind != "external_radio_change" or not recent_contexts:
            return subject
        namespace = _namespace_identity_key(subject.matrix_identity)
        if self._matching_live_baseline(
            namespace_key=namespace,
            baseline_scope_key=subject.baseline_scope_key,
            created_at=subject.created_at,
        ) is not None:
            return subject
        previous = recent_contexts[-1]
        if (
            previous.matrix_identity.namespace != subject.matrix_identity.namespace
            or context_scope_key(previous) != subject.baseline_scope_key
            or previous.matrix_identity.matrix_sequence != subject.baseline_through_sequence
        ):
            raise ValueError("external_referee_previous_context_mismatch")
        linked = [
            pending for pending in self._pending.values()
            if pending.subject_kind == "external_radio_change"
            and pending.namespace_key == namespace
            and 0.0 <= subject.terminal_at - pending.opened_at <= 30.0
            and subject.terminal_at < pending.due_at
            and (
                pending.result_scope_key == context_scope_key(previous)
                # An incomplete radio identity cannot form a result scope.
                # Link only its exact opening frame, never a later look-alike.
                or (not pending.result_scope_key and pending.opened_at == previous.accepted_at)
            )
            and _target_state(pending.target, previous) == "matches"
        ]
        if len(linked) > 1:
            raise ValueError("external_referee_baseline_chain_ambiguous")
        if not linked:
            return subject
        pending = linked[0]
        before = next((
            context for context in reversed(recent_contexts)
            if context.matrix_identity.namespace == subject.matrix_identity.namespace
            and context_scope_key(context) == pending.baseline_scope_key
            and 0.0 <= pending.opened_at - context.accepted_at <= 30.0
        ), None)
        if before is None:
            return subject
        return replace(
            subject,
            baseline_scope_key=pending.baseline_scope_key,
            baseline_through_sequence=before.matrix_identity.matrix_sequence,
            learning_context_key=pending.learning_context_key,
            learning_context=CoreFrozenPayload.from_mapping(pending.learning_context),
            superseded_proof_id=pending.proof_id,
        )

    @staticmethod
    def _append_pending_probe(
        pending: _PendingProof,
        context: IntelligenceContext,
    ) -> bool:
        if not pending.result_scope_key or not pending.samples:
            return False
        before = len(pending.samples)
        pending.samples = _append_unique_probe_sample(
            pending.samples,
            _current_probe_attempt_sample(
                context,
                scope_key=pending.result_scope_key,
                identity=pending.samples[-1]["identity"],
            ),
        )
        return len(pending.samples) != before

    def admit_context(
        self,
        context: IntelligenceContext,
        *,
        transition: RadioTransitionFrame,
    ) -> tuple[RefereeProof, ...]:
        # Historical snapshot readers do not materialize live transitions.
        from arch4.controller_frames import RadioTransitionFrame
        from arch4.radio_transition_authority import (
            GUC_COMMAND, LOWER_CONTROLLER, LOWER_CONTROLLER_DURING_COMMAND,
        )
        if not isinstance(context, IntelligenceContext):
            raise TypeError("new_horizon_referee_context_required")
        if not isinstance(transition, RadioTransitionFrame):
            raise TypeError("new_horizon_referee_transition_required")
        if transition.matrix_identity != context.matrix_identity:
            raise ValueError("new_horizon_referee_transition_context_mismatch")
        namespace_key = _namespace_key(context)
        emitted: list[RefereeProof] = list(
            self._admit_active_context(
                context,
                observed_at=context.accepted_at,
                sample_metrics=True,
            )
        )
        updated_scopes: set[str] = set()
        pending_at_admission = tuple(self._pending.items())
        for proof_id, pending in pending_at_admission:
            if pending.namespace_key != namespace_key:
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_namespace_changed_before_close",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            continuity_state = context_continuity_observation_state(
                context,
                expected_key=pending.continuity_key,
            )
            if continuity_state == "changed":
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_continuity_changed_before_close",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            if continuity_state == "unavailable":
                if self._append_pending_probe(pending, context):
                    updated_scopes.add(pending.continuity_key)
                if context.accepted_at <= pending.due_at + 30.0:
                    continue
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_continuity_observation_window_missed",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            if context.accepted_at < pending.opened_at:
                continue
            target_state = _target_state(pending.target, context)
            if (pending.origin == LOWER_CONTROLLER_DURING_COMMAND
                and pending.command_id
                and pending.result_scope_key == context_scope_key(context)
                and target_state == "matches"
                and any(
                    confirmed.subject_kind == "core_command"
                    and confirmed.command_id == pending.command_id
                    and confirmed.namespace_key == pending.namespace_key
                    and confirmed.continuity_key == pending.continuity_key
                    # The pool stage can have a different baseline from the
                    # complete command. Match the resulting configuration, not
                    # the earlier stage. An unbound command binds on this read.
                    and confirmed.result_scope_key in ("", pending.result_scope_key)
                    and pending.opened_at <= confirmed.opened_at <= context.accepted_at
                    and _target_state(confirmed.target, context) == "matches"
                    for _, confirmed in pending_at_admission
                )):
                proof = self._invalid_pending(pending, context,
                    reason="proof_superseded_by_confirmed_command")
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            if target_state == "changed":
                if (
                    transition.attribution_state == "valid"
                    and transition.fresh
                    and transition.source == LOWER_CONTROLLER
                ):
                    proof = self._target_did_not_hold(pending, context)
                elif (
                    transition.attribution_state == "valid"
                    and transition.fresh
                    and transition.source
                    in {GUC_COMMAND, LOWER_CONTROLLER_DURING_COMMAND}
                ):
                    proof = self._invalid_pending(
                        pending,
                        context,
                        reason="proof_superseded_by_new_command",
                    )
                else:
                    proof = self._invalid_pending(
                        pending,
                        context,
                        reason="proof_target_change_attribution_unavailable",
                    )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            if target_state != "matches":
                if self._append_pending_probe(pending, context):
                    updated_scopes.add(pending.continuity_key)
                if context.accepted_at <= pending.due_at + 30.0:
                    continue
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_target_observation_window_missed",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            context_identity = context_radio_identity(context)
            if not context_identity:
                if self._append_pending_probe(pending, context):
                    updated_scopes.add(pending.continuity_key)
                if context.accepted_at <= pending.due_at + 30.0:
                    continue
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_observation_window_missed_or_unqualified",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            exact_result_scope = context_scope_key(context)
            if not pending.result_scope_key:
                pending.result_scope_key = exact_result_scope
                updated_scopes.add(pending.continuity_key)
            elif pending.result_scope_key != exact_result_scope:
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_result_scope_changed_before_close",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            if not _proof_context_qualified(context):
                if self._append_pending_probe(pending, context):
                    updated_scopes.add(pending.continuity_key)
                if context.accepted_at <= pending.due_at + 30.0:
                    continue
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_observation_window_missed_or_unqualified",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            sample = {
                **referee_sample_metrics(context),
                "accepted_at": context.accepted_at,
                "scope_key": context_scope_key(context),
                "identity": context_identity,
            }
            if not pending.samples or pending.samples[-1]["accepted_at"] != (
                context.accepted_at
            ):
                pending.samples = (*pending.samples, sample)
                updated_scopes.add(pending.continuity_key)
            if context.accepted_at < pending.due_at:
                continue
            if context.accepted_at > pending.due_at + 30.0:
                proof = self._invalid_pending(
                    pending,
                    context,
                    reason="proof_observation_window_missed_or_unqualified",
                )
                self._pending.pop(proof_id)
                self._record(proof)
                self._last = proof
                emitted.append(proof)
                updated_scopes.add(pending.continuity_key)
                continue
            observed_vector = referee_service_vector(
                (
                    row
                    for row in pending.samples
                    if float(row["accepted_at"]) <= pending.due_at + 30.0
                ),
                reachability_receipt=_context_reachability_receipt(
                    context,
                    opened_at=pending.opened_at,
                    ended_at=pending.due_at,
                    complete=True,
                ),
            )
            pending.completed = True
            proofs, component_grades = self._grade(pending, context)
            for proof in proofs:
                self._record(
                    proof,
                    baseline_scope_key=pending.baseline_scope_key,
                    result_scope_key=pending.result_scope_key,
                    baseline_vector=pending.baseline,
                    baseline_identity=pending.baseline_identity,
                    observed_vector=observed_vector,
                    component_grades=component_grades,
                    result_identity=context_identity,
                )
                self._last = proof
                emitted.append(proof)
            self._pending.pop(proof_id)
            updated_scopes.add(pending.continuity_key)
        for updated_continuity in updated_scopes:
            self._save_pending(updated_continuity, context.accepted_at)
        return tuple(emitted)

    def admit_reachability(
        self,
        context: IntelligenceContext,
        *,
        observed_at: float,
    ) -> tuple[RefereeProof, ...]:
        """Advance only the continuous window; never resample radio metrics."""

        if not isinstance(context, IntelligenceContext):
            raise TypeError("new_horizon_referee_context_required")
        return self._admit_active_context(
            context,
            observed_at=observed_at,
            sample_metrics=False,
        )

    def _admit_active_context(
        self,
        context: IntelligenceContext,
        *,
        observed_at: float,
        sample_metrics: bool,
    ) -> tuple[RefereeProof, ...]:
        stamp = float(observed_at)
        if not math.isfinite(stamp) or stamp < context.accepted_at:
            raise ValueError("new_horizon_referee_observation_time_invalid")
        if type(sample_metrics) is not bool:
            raise TypeError("new_horizon_referee_sample_metrics_bool_required")
        active = self._active
        continuity_key = context_continuity_key(context)
        namespace_key = _namespace_key(context)
        target = current_context_target(context)
        evidence = _context_reachability_evidence(context)
        emitted: list[RefereeProof] = []

        # A runtime namespace replacement ends the old physical session.  Its
        # evidence cannot be combined with the new session even when the same
        # band happens to be selected again.
        if active is not None and active.namespace_key != namespace_key:
            active.evaluated_at = max(active.evaluated_at, stamp)
            emitted.extend(self._complete_mature_active_windows(active))
            self._retain_partial_active(
                active,
                ended_at=stamp,
                evidence=active.latest_reachability_evidence,
            )
            active = None
            self._active = None

        if CONNECTION_SETTLING_REASON in context.missing_required:
            # Namespace cleanup above still applies. Warmup cannot open a
            # learning window or feed an existing one with startup metrics.
            return tuple(emitted)
        if target is None:
            # One sparse Matrix frame must not terminate a valid radio
            # context.  Core reachability still advances and the live row
            # remains visible until a different complete target is accepted.
            if active is not None:
                self._observe_identity(active, context.accepted_at, False)
                active.latest_reachability_evidence = evidence
                active.evaluated_at = max(active.evaluated_at, stamp)
                emitted.extend(self._complete_mature_active_windows(active))
            return tuple(emitted)
        scope_key = context_scope_key(context)
        identity = context_radio_identity(context)
        if not identity:
            if active is not None:
                self._observe_identity(active, context.accepted_at, False)
                active.latest_reachability_evidence = evidence
                active.evaluated_at = max(active.evaluated_at, stamp)
                emitted.extend(self._complete_mature_active_windows(active))
            return tuple(emitted)
        changed = (
            active is None
            or active.scope_key != scope_key
            or active.continuity_key != continuity_key
            or active.target.target_signature != target.target_signature
        )
        if changed and active is not None:
            active.latest_reachability_evidence = evidence
            active.evaluated_at = max(active.evaluated_at, stamp)
            emitted.extend(self._complete_mature_active_windows(active))
            self._retain_partial_active(
                active,
                ended_at=stamp,
                evidence=evidence,
            )
        if changed:
            active = _ActiveContext(
                scope_key=scope_key,
                continuity_key=continuity_key,
                namespace_key=namespace_key,
                target=target,
                opened_at=context.accepted_at,
                latest={
                    **referee_sample_metrics(context),
                    "observed_at": context.accepted_at,
                },
                latest_identity=identity,
                latest_reachability_evidence=evidence,
                evaluated_at=stamp,
                learning_context=context_learning_components(context),
            )
            self._active = active
        active.latest_reachability_evidence = evidence
        active.evaluated_at = max(active.evaluated_at, stamp)
        if sample_metrics:
            active.latest = {
                **referee_sample_metrics(context),
                "observed_at": context.accepted_at,
            }
        active.latest_identity = identity
        if sample_metrics:
            self._observe_identity(active, context.accepted_at, all(
                context.fact(path).disposition == "available" and context.fact(path).retention is None
                for path in ("radio.serving_band", "rf.cell_id", "rf.pci", "rf.earfcn_dl")))
        if sample_metrics and not _proof_context_qualified(context):
            emitted.extend(self._complete_mature_active_windows(active))
            return tuple(emitted)
        if sample_metrics:
            sample = {
                **active.latest,
                "accepted_at": context.accepted_at,
                "scope_key": scope_key,
                "identity": identity,
            }
            if (
                not active.initial_samples
                or active.initial_samples[-1]["accepted_at"] != context.accepted_at
            ):
                active.initial_samples = (*active.initial_samples, sample)
        emitted.extend(self._complete_mature_active_windows(active))
        return tuple(emitted)

    def _complete_mature_active_windows(
        self,
        active: _ActiveContext,
    ) -> tuple[RefereeProof, ...]:
        """Close every fully covered non-overlapping window in order."""

        emitted: list[RefereeProof] = []
        window_s = self._windows_s[-1]
        if active.evaluated_at - active.opened_at >= window_s * 2.0:
            # In normal operation each ping revision reaches Referee. Two
            # silent windows mean a suspended process, a clock jump, or a
            # broken input lane. Keep one honest partial record and restart at
            # live time; never manufacture a large run of empty grades.
            previous_opened_at = active.opened_at
            self._retain_partial_active(
                active,
                ended_at=active.opened_at + window_s,
                evidence=active.latest_reachability_evidence,
            )
            reset_at = active.evaluated_at
            self._history.record(
                event_key=(
                    "referee-discontinuity:"
                    + hashlib.sha256(
                        (
                            f"{active.namespace_key}|{active.scope_key}|"
                            f"{previous_opened_at:.6f}|{reset_at:.6f}"
                        ).encode("utf-8")
                    ).hexdigest()
                ),
                scope_key=active.continuity_key,
                kind=REFEREE_TIMELINE_DISCONTINUITY_HISTORY_KIND,
                observed_at=reset_at,
                payload={
                    "schema": "NewHorizonRefereeTimelineDiscontinuity.v1",
                    "opened_at": previous_opened_at,
                    "reset_at": reset_at,
                    "reason": "two_referee_windows_without_processing",
                },
            )
            active.opened_at = reset_at
            active.learning_context = build_learning_context(
                exact_scope_key=active.scope_key,
                observed_at=reset_at,
            )
            active.initial_samples = tuple(
                row
                for row in active.initial_samples
                if float(row["accepted_at"]) >= reset_at
            )
            return ()
        while True:
            due_at = active.opened_at + window_s
            hour_end = fixed_hour(active.opened_at)[1]
            if due_at > hour_end + 0.000001:
                if active.evaluated_at < hour_end:
                    break
                self._retain_partial_active(
                    active, ended_at=hour_end,
                    evidence=active.latest_reachability_evidence,
                )
                active.opened_at = hour_end
                active.learning_context = build_learning_context(
                    exact_scope_key=active.scope_key, observed_at=hour_end,
                )
                active.initial_samples = tuple(
                    row for row in active.initial_samples
                    if float(row["accepted_at"]) >= hour_end
                )
                continue
            if active.evaluated_at < due_at:
                break
            completed = self._complete_active_window(active, due_at=due_at)
            if completed is not None:
                emitted.append(completed)
        return tuple(emitted)

    def _complete_active_window(
        self,
        active: _ActiveContext,
        *,
        due_at: float,
    ) -> RefereeProof | None:
        """Persist one complete interval and advance the active window."""

        rows = tuple(
            row for row in active.initial_samples if float(row["accepted_at"]) <= due_at
        )
        carry = tuple(
            row for row in active.initial_samples if float(row["accepted_at"]) > due_at
        )
        if not rows:
            # Reachability can advance while the accepted Matrix/service lane
            # has no qualified sample. Keep that interval visible as partial,
            # but never turn it into a grade or a learning baseline.
            self._retain_partial_active(
                active,
                ended_at=due_at,
                evidence=active.latest_reachability_evidence,
            )
            active.opened_at = due_at
            active.learning_context = build_learning_context(
                exact_scope_key=active.scope_key,
                observed_at=due_at,
            )
            active.initial_samples = carry
            return None
        observed = referee_service_vector(
            rows,
            reachability_receipt=_reachability_receipt_from_evidence(
                active.latest_reachability_evidence,
                opened_at=active.opened_at,
                ended_at=due_at,
                complete=True,
            ),
        )
        observed.update(self._attributable_availability(active, due_at))
        grades = _quality.context_quality_grades(observed)
        identity_at_close = dict(
            rows[-1]["identity"] if rows else active.latest_identity
        )
        result_target = active.target
        assessment_id = hashlib.sha256(
            (
                f"context-window|{active.namespace_key}|{active.scope_key}|"
                f"{result_target.target_signature}|{active.opened_at:.6f}|"
                f"{due_at:.6f}"
            ).encode("utf-8")
        ).hexdigest()
        learning_context = require_learning_context(active.learning_context)
        learning_context_key = _learning_context_key(learning_context)
        service_grade = grades["performance_overall_outcome"]
        service = RefereeProof(
            proof_id=_dimension_proof_id(assessment_id, "service_outcome"),
            assessment_id=assessment_id,
            decision_id="context-window:" + assessment_id,
            scope_key=active.continuity_key,
            learning_context_key=learning_context_key,
            subject_kind="active_context_window",
            origin="continuous_context",
            target=result_target,
            dimension="service_outcome",
            status="graded" if service_grade is not None else "invalid",
            grade=service_grade,
            final_assessment=True,
            reasons=(
                "qualified_context_window_graded"
                if service_grade is not None
                else "qualified_context_window_vector_missing",
            ),
            opened_at=active.opened_at,
            due_at=due_at,
            closed_at=due_at,
        )
        emitted = (service,)
        for proof in emitted:
            self._record(
                proof,
                result_scope_key=active.scope_key,
                observed_vector=observed,
                component_grades=grades,
                learning_context=learning_context,
                result_identity=identity_at_close,
                assessment_mode="context_quality",
            )
            self._last = proof
        self._cache_context_assessment(
            assessment_id=assessment_id,
            target_signature=result_target.target_signature,
            scope_key=active.scope_key,
            continuity_key=active.continuity_key,
            namespace_key=active.namespace_key,
            learning_context=learning_context,
            learning_context_key=learning_context_key,
            identity=identity_at_close,
            observed=observed,
            grades=grades,
            opened_at=active.opened_at,
            due_at=due_at,
            closed_at=due_at,
        )
        active.completed_count += 1
        active.opened_at = due_at
        active.learning_context = build_learning_context(
            exact_scope_key=active.scope_key,
            observed_at=due_at,
        )
        active.initial_samples = carry
        return service

    def _retain_partial_active(
        self,
        active: _ActiveContext,
        *,
        ended_at: float,
        evidence: CoreReachabilityEvidenceView,
    ) -> None:
        closed_at = min(float(ended_at), active.opened_at + self._windows_s[-1])
        if closed_at <= active.opened_at:
            return
        rows = tuple(
            row
            for row in active.initial_samples
            if float(row["accepted_at"]) <= closed_at
        )
        observed = referee_service_vector(
            rows,
            reachability_receipt=_reachability_receipt_from_evidence(
                evidence,
                opened_at=active.opened_at,
                ended_at=closed_at,
                complete=False,
            ),
        )
        observed.update(self._attributable_availability(active, closed_at))
        partial_id = hashlib.sha256(
            (
                f"partial-context|{active.namespace_key}|{active.scope_key}|"
                f"{active.opened_at:.6f}|{closed_at:.6f}"
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema": REFEREE_PARTIAL_EVIDENCE_SCHEMA,
            "assessment_id": partial_id,
            "target_signature": active.target.target_signature,
            "scope_key": active.scope_key,
            "continuity_key": active.continuity_key,
            "learning_context": dict(active.learning_context),
            "learning_context_key": _learning_context_key(
                require_learning_context(active.learning_context)
            ),
            "identity": dict(active.latest_identity),
            "observed_vector": observed,
            "component_grades": {},
            "opened_at": active.opened_at,
            "due_at": active.opened_at + self._windows_s[-1],
            "closed_at": closed_at,
            "maturity": "partial",
        }
        self._history.record(
            event_key="partial:" + partial_id,
            scope_key=active.continuity_key,
            kind=REFEREE_PARTIAL_HISTORY_KIND,
            observed_at=closed_at,
            payload=payload,
        )
        self._assessment_history.admit_partial(
            event_key="partial:" + partial_id, continuity_key=active.continuity_key)
        self._cache_context_assessment(
            assessment_id=partial_id,
            target_signature=active.target.target_signature,
            scope_key=active.scope_key,
            continuity_key=active.continuity_key,
            namespace_key=active.namespace_key,
            learning_context=active.learning_context,
            learning_context_key=str(payload["learning_context_key"]),
            identity=active.latest_identity,
            observed=observed,
            grades={},
            opened_at=active.opened_at,
            due_at=active.opened_at + self._windows_s[-1],
            closed_at=closed_at,
            maturity="partial",
        )

    @staticmethod
    def _observe_identity(active: _ActiveContext, stamp: float, known: bool) -> None:
        row = (stamp, known)
        if not active.identity_observations or active.identity_observations[-1] != row:
            active.identity_observations = (*active.identity_observations, row)

    @staticmethod
    def _attributable_availability(active: _ActiveContext, end: float) -> dict[str, float]:
        # Only spans bounded by observations of this serving identity can
        # teach its availability. Missing identity ends the span. Core still
        # owns every up/down decision and excludes planned action time.
        spans = []
        first = last = None
        for stamp, known in active.identity_observations:
            if known:
                first = stamp if first is None else first
                last = stamp
            elif first is not None:
                spans.append((first, last))
                first = last = None
        if first is not None:
            spans.append((first, last))
        observed = working = 0.0
        for start, stop in spans:
            start, stop = max(start, active.opened_at), min(stop, end)
            if stop <= start:
                continue
            receipt = _reachability_receipt_from_evidence(active.latest_reachability_evidence,
                opened_at=start, ended_at=stop, complete=False)
            if receipt.availability_pct is not None:
                observed += receipt.measured_seconds
                working += receipt.measured_seconds * receipt.availability_pct / 100.0
        previous = tuple(row for row in active.identity_observations if row[0] <= end)[-1:]
        active.identity_observations = previous + tuple(row for row in active.identity_observations if row[0] > end)
        return dict(internet_attributed_s=observed, internet_working_s=working)

    def context_timeline(self) -> dict[str, Any]:
        """Return current window data. History stores completed rows."""

        active = self._active
        if active is None:
            return {}
        current_end = min(
            active.evaluated_at,
            active.opened_at + self._windows_s[-1],
            active.latest_reachability_evidence.observed_at,
        )
        current_window = referee_service_vector(
            active.initial_samples,
            reachability_receipt=(
                _reachability_receipt_from_evidence(
                    active.latest_reachability_evidence,
                    opened_at=active.opened_at,
                    ended_at=current_end,
                    complete=False,
                )
                if current_end >= active.opened_at
                else None
            ),
        )
        return {
            "scope_key": active.scope_key,
            "continuity_key": active.continuity_key,
            "target_signature": active.target.target_signature,
            "opened_at": active.opened_at,
            "due_at": active.opened_at + self._windows_s[-1],
            "evaluated_at": active.evaluated_at,
            "current_window": current_window,
            "current_identity": dict(active.latest_identity),
            "final_mature": active.completed_count > 0,
            "completed_count": active.completed_count,
            "recent_assessments": [
                dict(row)
                for row in sorted(
                    self._recent_context_assessments.values(),
                    key=lambda item: float(item["closed_at"]),
                    reverse=True,
                )[:8]
            ],
        }

    def _cache_context_assessment(
        self,
        *,
        assessment_id: str,
        target_signature: str,
        scope_key: str,
        continuity_key: str,
        namespace_key: str,
        learning_context: Mapping[str, Any],
        learning_context_key: str,
        identity: Mapping[str, Any],
        observed: Mapping[str, Any],
        grades: Mapping[str, int | float | None],
        opened_at: float,
        due_at: float,
        closed_at: float,
        maturity: str = "completed",
    ) -> None:
        self._recent_context_assessments[assessment_id] = {
            "assessment_id": assessment_id,
            "target_signature": target_signature,
            "scope_key": scope_key,
            "continuity_key": continuity_key,
            "namespace_key": namespace_key,
            "learning_context": dict(learning_context),
            "learning_context_key": learning_context_key,
            "identity": dict(identity),
            "observed_vector": dict(observed),
            "component_grades": dict(grades),
            "opened_at": float(opened_at),
            "due_at": float(due_at),
            "closed_at": float(closed_at),
            "maturity": maturity,
        }
        ordered = sorted(
            self._recent_context_assessments.values(),
            key=lambda item: float(item["closed_at"]),
            reverse=True,
        )[:8]
        self._recent_context_assessments = {
            str(item["assessment_id"]): item for item in ordered
        }

    def _restore_recent_context_assessments(
        self,
        snapshot: RefereeAssessmentHistorySnapshot,
    ) -> None:
        continuity_key = snapshot.continuity_key
        restored: dict[str, dict[str, Any]] = {}
        for assessment in snapshot.assessments:
            payload = next(
                (
                    item
                    for item in assessment.payloads
                    if item.get("dimension") == "service_outcome"
                ),
                None,
            )
            if (
                not isinstance(payload, Mapping)
                or payload.get("assessment_mode") != "context_quality"
                or payload.get("subject_kind") != "active_context_window"
            ):
                continue
            assessment_id = str(payload.get("assessment_id") or "")
            identity = payload.get("result_identity")
            observed = payload.get("observed_vector")
            grades = payload.get("component_grades")
            if (
                not assessment_id
                or not isinstance(identity, Mapping)
                or not isinstance(observed, Mapping)
                or not isinstance(grades, Mapping)
            ):
                continue
            observed = referee_history_vector(payload, "observed_vector")
            restored[assessment_id] = {
                "assessment_id": assessment_id,
                "target_signature": str(payload.get("target_signature") or ""),
                "scope_key": str(
                    payload.get("result_scope_key")
                    or dict(identity).get("scope_key")
                    or ""
                ),
                "continuity_key": str(payload.get("continuity_key") or continuity_key),
                # Data from a previous process cannot be the baseline for the
                # current live transition. It remains forecast and history data.
                "namespace_key": "",
                "learning_context": dict(
                    payload.get("learning_context")
                    if isinstance(payload.get("learning_context"), Mapping)
                    else {}
                ),
                "learning_context_key": str(payload.get("learning_context_key") or ""),
                "identity": dict(identity),
                "observed_vector": dict(observed),
                "component_grades": dict(grades),
                "opened_at": float(payload.get("opened_at") or 0.0),
                "due_at": float(payload.get("due_at") or 0.0),
                "closed_at": float(payload.get("closed_at") or 0.0),
                "maturity": "completed",
            }
        for row in self._history.query(
            scope_key=continuity_key,
            kind=REFEREE_PARTIAL_HISTORY_KIND,
            limit=64,
        ):
            payload = row.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != REFEREE_PARTIAL_EVIDENCE_SCHEMA
                or payload.get("maturity") != "partial"
            ):
                continue
            assessment_id = str(payload.get("assessment_id") or "")
            identity = payload.get("identity")
            observed = payload.get("observed_vector")
            grades = payload.get("component_grades")
            if (
                not assessment_id
                or not isinstance(identity, Mapping)
                or not isinstance(observed, Mapping)
                or not isinstance(grades, Mapping)
            ):
                continue
            # Permanent pre-model partial archive: retain recorded, ungraded
            # facts. Current/model-tagged data must use the sole formula owner.
            if "quality_model" in observed:
                observed = _quality.project_service_vector(observed)
            restored[assessment_id] = {
                "assessment_id": assessment_id,
                "target_signature": str(payload.get("target_signature") or ""),
                "scope_key": str(payload.get("scope_key") or ""),
                "continuity_key": str(payload.get("continuity_key") or continuity_key),
                "namespace_key": "",
                "learning_context": dict(
                    payload.get("learning_context")
                    if isinstance(payload.get("learning_context"), Mapping)
                    else {}
                ),
                "learning_context_key": str(payload.get("learning_context_key") or ""),
                "identity": dict(identity),
                "observed_vector": dict(observed),
                "component_grades": dict(grades),
                "opened_at": float(payload.get("opened_at") or 0.0),
                "due_at": float(payload.get("due_at") or 0.0),
                "closed_at": float(payload.get("closed_at") or 0.0),
                "maturity": "partial",
            }
        # Returning provider/RAT continuity in this same process must not turn
        # its proven live baselines into namespace-empty, previous-run data.
        restored.update({key: row for key, row in self._recent_context_assessments.items()
                         if row.get("namespace_key")})
        self._recent_context_assessments = {}
        for item in sorted(
            restored.values(),
            key=lambda value: float(value["closed_at"]),
        )[-8:]:
            self._recent_context_assessments[str(item["assessment_id"])] = item

    def context_final_mature(self) -> bool:
        active = self._active
        return bool(active is not None and active.completed_count > 0)

    def _grade(
        self,
        pending: _PendingProof,
        context: IntelligenceContext,
    ) -> tuple[tuple[RefereeProof, ...], dict[str, int | float | None]]:
        window_s = pending.windows_s[-1]
        base_id = pending.proof_id
        due_at = pending.opened_at + window_s
        after = referee_service_vector(
            (
                row
                for row in pending.samples
                if float(row["accepted_at"]) <= due_at + 30.0
            ),
            reachability_receipt=_context_reachability_receipt(
                context,
                opened_at=pending.opened_at,
                ended_at=due_at,
                complete=True,
            ),
        )
        result_target = _exact_result_target(pending.target, context)
        component_grades = _quality.context_quality_grades(after)
        service_grade = component_grades['performance_overall_outcome']
        service = RefereeProof(
            proof_id=_dimension_proof_id(base_id, "service_outcome"),
            assessment_id=base_id,
            decision_id=pending.decision_id,
            scope_key=pending.continuity_key,
            learning_context_key=pending.learning_context_key,
            subject_kind=pending.subject_kind,
            origin=pending.origin,
            target=result_target,
            dimension="service_outcome",
            status="graded" if service_grade is not None else "invalid",
            grade=service_grade,
            final_assessment=True,
            reasons=(
                "qualified_absolute_service_measured"
                if service_grade is not None
                else "qualified_service_vector_missing",
            ),
            opened_at=pending.opened_at,
            due_at=due_at,
            closed_at=context.accepted_at,
        )
        component_grades["performance_overall_outcome"] = service.grade
        proofs: list[RefereeProof] = [service]
        if is_brain_decision_origin(pending.origin):
            prediction = self._frozen_prediction(pending)
            rules = _rules_proof(
                pending,
                service=service,
                predicted_grades=prediction,
                internet_availability_pct=after.get("internet_availability_pct"),
                closed_at=context.accepted_at,
            )
            proofs.append(rules)
        return tuple(proofs), component_grades

    def _frozen_prediction(
        self,
        pending: _PendingProof,
    ) -> dict[str, float | None] | None:
        decision_id = pending.decision_id.removeprefix("core-command:")
        try:
            snapshot = self._decision_history.current(pending.continuity_key)
        except RuntimeError:
            snapshot = self._decision_history.refresh(pending.continuity_key)
        row = snapshot.event("decision-ledger:" + decision_id)
        payload = row.get("payload") if row is not None else None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != DECISION_LEDGER_SCHEMA
        ):
            return None
        prediction = payload.get("predicted_grades")
        components = (
            prediction.get("components") if isinstance(prediction, Mapping) else None
        )
        if not isinstance(components, list):
            return None
        return {
            str(item.get("dimension") or ""): item.get("grade")
            for item in components
            if isinstance(item, Mapping)
        }

    def _finish_invalid(
        self,
        subject: RefereeSubject,
        *,
        scope_key: str,
        reason: str,
    ) -> RefereeProof:
        proof = RefereeProof(
            proof_id=hashlib.sha256(subject.key.encode("utf-8")).hexdigest(),
            assessment_id=hashlib.sha256(subject.key.encode("utf-8")).hexdigest(),
            decision_id=subject.key,
            scope_key=scope_key,
            learning_context_key=scope_key,
            subject_kind=subject.subject_kind,
            origin=subject.origin,
            target=subject.target,
            dimension="service_outcome",
            status="invalid",
            grade=None,
            final_assessment=False,
            reasons=(reason,),
            opened_at=subject.terminal_at,
            due_at=subject.terminal_at,
            closed_at=subject.terminal_at,
        )
        self._record(proof)
        self._last = proof
        return proof

    @staticmethod
    def _target_did_not_hold(
        pending: _PendingProof,
        context: IntelligenceContext,
    ) -> RefereeProof:
        return RefereeProof(
            proof_id=pending.proof_id,
            assessment_id=pending.proof_id,
            decision_id=pending.decision_id,
            scope_key=pending.continuity_key,
            learning_context_key=pending.learning_context_key,
            subject_kind=pending.subject_kind,
            origin=pending.origin,
            target=pending.target,
            dimension="service_outcome",
            status="invalid",
            grade=None,
            final_assessment=False,
            reasons=("target_did_not_hold:lower_controller_drift",),
            opened_at=pending.opened_at,
            due_at=pending.due_at,
            closed_at=context.accepted_at,
        )

    @staticmethod
    def _invalid_pending(
        pending: _PendingProof,
        context: IntelligenceContext,
        *,
        reason: str,
    ) -> RefereeProof:
        return RefereeProof(
            proof_id=pending.proof_id,
            assessment_id=pending.proof_id,
            decision_id=pending.decision_id,
            scope_key=pending.continuity_key,
            learning_context_key=pending.learning_context_key,
            subject_kind=pending.subject_kind,
            origin=pending.origin,
            target=pending.target,
            dimension="service_outcome",
            status="invalid",
            grade=None,
            final_assessment=False,
            reasons=(reason,),
            opened_at=pending.opened_at,
            due_at=pending.due_at,
            closed_at=context.accepted_at,
        )

    @staticmethod
    def _as_pending(pending: _PendingProof) -> RefereeProof:
        return RefereeProof(
            proof_id=pending.proof_id,
            assessment_id=pending.proof_id,
            decision_id=pending.decision_id,
            scope_key=pending.continuity_key,
            learning_context_key=pending.learning_context_key,
            subject_kind=pending.subject_kind,
            origin=pending.origin,
            target=pending.target,
            dimension="service_outcome",
            status="pending",
            grade=None,
            final_assessment=False,
            reasons=("awaiting_delayed_owner_observation",),
            opened_at=pending.opened_at,
            due_at=pending.due_at,
            closed_at=0.0,
        )

    def _record(
        self,
        proof: RefereeProof,
        *,
        baseline_scope_key: str = "unavailable",
        result_scope_key: str = "unavailable",
        baseline_vector: dict[str, Any] | None = None,
        baseline_identity: Mapping[str, Any] | None = None,
        observed_vector: dict[str, Any] | None = None,
        component_grades: dict[str, int | float | None] | None = None,
        learning_context: Mapping[str, Any] | None = None,
        result_identity: Mapping[str, Any] | None = None,
        assessment_mode: str = "transition",
    ) -> None:
        if assessment_mode not in {"transition", "context_quality"}:
            raise ValueError("new_horizon_referee_assessment_mode_invalid")
        self._history.record(
            event_key="proof:" + proof.proof_id,
            scope_key=proof.scope_key,
            kind=REFEREE_PROOF_HISTORY_KIND,
            observed_at=proof.closed_at,
            payload={
                "schema": REFEREE_PROOF_EVIDENCE_SCHEMA,
                "proof_id": proof.proof_id,
                "assessment_id": proof.assessment_id,
                "decision_id": proof.decision_id,
                "subject_kind": proof.subject_kind,
                "origin": proof.origin,
                "action_family": proof.target.action_family,
                "target_signature": proof.target.target_signature,
                "target": proof.target.target.to_mapping(),
                "dimension": proof.dimension,
                "status": proof.status,
                "grade": proof.grade,
                "final_assessment": proof.final_assessment,
                "reasons": list(proof.reasons),
                "opened_at": proof.opened_at,
                "due_at": proof.due_at,
                "closed_at": proof.closed_at,
                "continuity_key": proof.scope_key,
                "learning_context_key": proof.learning_context_key,
                "learning_context": (
                    dict(learning_context)
                    if learning_context is not None
                    else dict(self._pending.get(proof.assessment_id).learning_context)
                    if proof.assessment_id in self._pending
                    else {}
                ),
                "assessment_mode": assessment_mode,
                "baseline_scope_key": baseline_scope_key,
                "result_scope_key": result_scope_key,
                "baseline_identity": dict(baseline_identity or {}),
                "result_identity": dict(result_identity or {}),
                "baseline_vector": dict(baseline_vector or {}),
                "observed_vector": dict(observed_vector or {}),
                "component_grades": dict(component_grades or {}),
                "window_seconds": max(
                    0.0,
                    float(proof.due_at) - float(proof.opened_at),
                ),
            },
        )

    def _save_pending(self, continuity_key: str, updated_at: float) -> None:
        rows = [
            {
                "proof_id": item.proof_id,
                "decision_id": item.decision_id,
                "baseline_scope_key": item.baseline_scope_key,
                "learning_context_key": item.learning_context_key,
                "learning_context": dict(item.learning_context),
                "continuity_key": item.continuity_key,
                "namespace_key": item.namespace_key,
                "subject_kind": item.subject_kind,
                "origin": item.origin,
                "target": {
                    "action_family": item.target.action_family,
                    "target_signature": item.target.target_signature,
                    "payload": item.target.target.to_mapping(),
                },
                "opened_at": item.opened_at,
                "windows_s": list(item.windows_s),
                "baseline": item.baseline,
                "baseline_identity": item.baseline_identity,
                "result_scope_key": item.result_scope_key,
                "samples": list(item.samples),
                "completed": item.completed,
                "command_id": item.command_id,
            }
            for item in self._pending.values()
            if item.continuity_key == continuity_key
        ]
        self._history.save_checkpoint(
            scope_key=continuity_key,
            name="pending_proofs_v14",
            updated_at=updated_at,
            payload={
                "schema": "NewHorizonPendingProofCheckpoint.v14",
                "rows": rows,
            },
        )

    def restore_context(
        self,
        context: IntelligenceContext,
    ) -> RefereeAssessmentHistorySnapshot:
        if not isinstance(context, IntelligenceContext):
            raise TypeError("new_horizon_referee_restore_context_required")
        continuity_key = context_continuity_key(context)
        owner = (_namespace_key(context), continuity_key)
        if self._restored_context_owner == owner:
            return self._assessment_history.current(
                continuity_key,
                now=context.accepted_at,
            )
        # Recalculate saved raw vectors before any consumer restores the current
        # proof generation. This also applies to Referee's GUI data.
        decision_history = self._decision_history.refresh(
            continuity_key, now=context.accepted_at
        )
        if context.confirmed_continuity.to_mapping()["quality_model"] == LTE_QUALITY_MODEL:
            recalculate_imported_referee_history_once(
                self._history,
                scope_key=continuity_key,
                decision_history=decision_history,
                now=context.accepted_at,
            )
        snapshot = self._assessment_history.refresh(
            continuity_key,
            now=context.accepted_at,
        )
        self._restore_recent_context_assessments(snapshot)
        payload = self._history.load_checkpoint(
            scope_key=continuity_key,
            name="pending_proofs_v14",
        )
        model = require_quality_model(context.confirmed_continuity.to_mapping()["quality_model"])
        if payload is None and model == LTE_QUALITY_MODEL:
            retired = self._history.load_checkpoint(scope_key=continuity_key,name="pending_proofs_v13")
            if retired is not None:
                payload = adapt_retired_lte_pending_v13(retired)
        if payload is None:
            self._restored_context_owner = owner
            return snapshot
        if payload.get("schema") != "NewHorizonPendingProofCheckpoint.v14":
            raise ValueError("new_horizon_pending_proof_checkpoint_schema_invalid")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("new_horizon_pending_proof_checkpoint_invalid")
        for row in rows:
            if not isinstance(row, dict) or row.get("continuity_key") != continuity_key:
                raise ValueError("new_horizon_pending_proof_scope_invalid")
            for vector in (row["baseline"], *row["samples"]):
                if vector and require_quality_model(vector["quality_model"]) != model:
                    raise ValueError("new_horizon_pending_proof_quality_model_mismatch")
            proof_id = str(row["proof_id"])
            if self._history.contains(event_key="proof:" + proof_id):
                continue
            windows_s = tuple(float(value) for value in row["windows_s"])
            if windows_s != self._windows_s or row.get("completed") is not False:
                raise ValueError("new_horizon_pending_proof_window_invalid")
            learning_context = require_learning_context(row["learning_context"])
            learning_context_key = str(row["learning_context_key"])
            if learning_context_key != _learning_context_key(learning_context):
                raise ValueError(
                    "new_horizon_pending_proof_learning_context_key_mismatch"
                )
            pending = _PendingProof(
                proof_id=proof_id,
                decision_id=str(row["decision_id"]),
                baseline_scope_key=str(row["baseline_scope_key"]),
                learning_context_key=learning_context_key,
                learning_context=learning_context,
                continuity_key=continuity_key,
                namespace_key=str(row["namespace_key"]),
                subject_kind=str(row["subject_kind"]),
                origin=str(row["origin"]),
                target=RefereeTarget(
                    action_family=str(row["target"]["action_family"]),
                    target_signature=str(row["target"]["target_signature"]),
                    target=CoreFrozenPayload.from_mapping(
                        dict(row["target"]["payload"])
                    ),
                ),
                opened_at=float(row["opened_at"]),
                windows_s=windows_s,
                baseline=dict(row["baseline"]),
                baseline_identity=dict(row["baseline_identity"]),
                result_scope_key=str(row["result_scope_key"]),
                samples=tuple(dict(value) for value in row["samples"]),
                completed=False,
                command_id=str(row.get("command_id") or ""),
            )
            self._pending[pending.proof_id] = pending
        self._restored_context_owner = owner
        return snapshot

    def status(self) -> dict[str, Any]:
        return {
            "owner": "54guc.new_horizon.proof_referee",
            "pending_count": len(self._pending),
            "active_context": self._active is not None,
            "active_context_final_mature": self.context_final_mature(),
            "active_context_reference_ready": bool(
                self._active is not None and bool(self._active.initial_samples)
            ),
            "last_status": self._last.status if self._last else "",
            "last_grade": self._last.grade if self._last else None,
            "last_window_seconds": (
                max(0.0, self._last.due_at - self._last.opened_at)
                if self._last
                else None
            ),
        }


_LEGAL_LTE_BANDWIDTHS_MHZ = (1.4, 3.0, 5.0, 10.0, 15.0, 20.0)


def _legacy_lte_bandwidth_mhz(
    vector: Mapping[str, Any],
) -> float | None:
    """Recover an unambiguous LTE PBAND bandwidth from an older ECI vector."""

    explicit = _number(vector.get("dl_bandwidth_mhz"))
    if explicit is not None and explicit > 0.0:
        ranked = sorted(
            (abs(explicit - value), value) for value in _LEGAL_LTE_BANDWIDTHS_MHZ
        )
    else:
        old_eci = _number(vector.get("eci_points"))
        congestion = _number(vector.get("congestion_pct"))
        ca_active_pct = _number(vector.get("ca_active_pct"))
        if (
            old_eci is None
            or congestion is None
            or ca_active_pct is None
            or not 0.0 <= congestion < 100.0
            or not 0.0 <= ca_active_pct <= 100.0
        ):
            return None
        usable_fraction = 1.0 - congestion / 100.0
        inferred = old_eci / usable_fraction - 10.0 * ca_active_pct / 100.0
        ranked = sorted(
            (abs(inferred - value), value) for value in _LEGAL_LTE_BANDWIDTHS_MHZ
        )
    if ranked[0][0] > 2.5:
        return None
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 0.25:
        return None
    return float(ranked[0][1])


def _legacy_bandwidth_identity(
    identity: Mapping[str, Any] | None,
) -> tuple[int, int, int, int] | None:
    """Return the radio identity used to keep PBAND bandwidth stable."""

    if not isinstance(identity, Mapping):
        return None
    pband, cell_id, earfcn, pci = (
        identity.get(field) for field in ("pband", "cell_id", "earfcn", "pci")
    )
    if any(type(value) is not int for value in (pband, cell_id, earfcn, pci)):
        return None
    return int(pband), int(cell_id), int(earfcn), int(pci)


def _legacy_bandwidth_consensus(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[int, int, int, int], float]:
    """Select repeated bandwidth evidence that clearly agrees for one identity."""

    evidence: dict[tuple[int, int, int, int], Counter[float]] = defaultdict(Counter)
    for row in rows:
        if row.get("dimension") != "service_outcome":
            continue
        for vector_name, identity_name in (
            ("observed_vector", "result_identity"),
            ("baseline_vector", "baseline_identity"),
        ):
            vector = row.get(vector_name)
            identity = _legacy_bandwidth_identity(row.get(identity_name))
            if not isinstance(vector, Mapping) or identity is None:
                continue
            candidate = _legacy_lte_bandwidth_mhz(vector)
            if candidate is not None:
                evidence[identity][candidate] += 1
    selected: dict[tuple[int, int, int, int], float] = {}
    for identity, counts in evidence.items():
        total = sum(counts.values())
        if total < 3:
            continue
        value, count = counts.most_common(1)[0]
        if count / total >= 0.75:
            selected[identity] = float(value)
    return selected


def _legacy_cqb_cell(
    vector: Mapping[str, Any],
    *,
    bandwidth_mhz: float,
) -> float | None:
    """Remove the old BQ bandwidth factor but keep its observed penalty."""

    explicit = _number(vector.get("cqb_cell"))
    if explicit is not None and 0.0 <= explicit <= 1.0:
        return explicit
    old_bq = _number(vector.get("bq_cell"))
    old_cq = _number(vector.get("cq_cell"))
    if old_bq is None or old_cq is None or old_bq <= 0.0 or not 0.0 <= old_cq <= 1.0:
        return None
    supports_ca = _number(vector.get("ca_active_pct")) is not None
    enabled_weight = 1.0 if supports_ca else 1.0 - 0.215
    bandwidth_weight = 0.200 / enabled_weight
    structural_weight = 1.0 - bandwidth_weight
    if structural_weight <= 0.0:
        return None
    bandwidth_factor = max(0.0, min(1.0, bandwidth_mhz / 20.0))
    structural = (old_bq - bandwidth_weight * bandwidth_factor) / structural_weight
    penalty_factor = max(0.0, min(1.0, old_cq / old_bq))
    return max(0.0, min(1.0, structural * penalty_factor))


def _migrate_legacy_service_vector(
    vector: Mapping[str, Any],
    *,
    bandwidth_mhz: float | None = None,
) -> dict[str, Any] | None:
    explicit = _number(vector.get("dl_bandwidth_mhz"))
    bandwidth_mhz = (
        explicit if explicit is not None and explicit > 0.0 else bandwidth_mhz
    )
    if bandwidth_mhz is None:
        return None
    cqb_cell = _legacy_cqb_cell(vector, bandwidth_mhz=bandwidth_mhz)
    if cqb_cell is None:
        return None
    return {
        **dict(vector),
        "quality_model": LTE_QUALITY_MODEL,
        "dl_bandwidth_mhz": bandwidth_mhz,
        "eci_points": _quality.capacity_points({"quality_model": LTE_QUALITY_MODEL, "dl_bandwidth_mhz": bandwidth_mhz}),
        "cqb_cell": cqb_cell,
        # Older Referee generations calculated availability from radio-sample
        # RTT fields. That was not the Core interval contract, so it cannot be
        # carried forward as proven Internet evidence.
        "internet_availability_pct": None,
        "internet_probe_count": 0,
        "internet_evidence_state": "legacy_unproven",
        "internet_current_state": "unknown",
        "semantic_migration": "v17_core_reachability_owner_cutover",
    }


def _exact_legacy_lte_transition_identity(
    identity: object,
    *,
    current_vectors: bool = False,
) -> dict[str, Any] | None:
    """Accept only a fully identified LTE side of a legacy transition."""

    if not isinstance(identity, Mapping):
        return None
    copied = dict(identity)
    network_type = str(copied.get("network_type") or "")
    pband = copied.get("pband")
    pool = copied.get("ca_pool")
    cell_id = copied.get("cell_id")
    earfcn = copied.get("earfcn")
    pci = copied.get("pci")
    if (
        network_type not in ({"LTE (4G)", "LTE-A (4G+)", "4G", "4G+"}
                             | ({"NR NSA (5G)"} if current_vectors else set()))
        or type(pband) is not int
        or pband <= 0
        or not isinstance(pool, (list, tuple))
        or not pool
        or any(type(item) is not int or item <= 0 for item in pool)
        or pband not in pool
        or type(cell_id) is not int
        or cell_id <= 0
        or type(earfcn) is not int
        or earfcn < 0
        or type(pci) is not int
        or pci < 0
    ):
        return None
    copied["ca_pool"] = list(sorted(set(pool)))
    if network_type == "NR NSA (5G)":
        # Current evidence already identifies both radio legs. Never invent
        # NR identity from an older LTE-only measurement.
        for key in ("nr_band", "nr_pci", "nr_arfcn"):
            value = copied.get(key)
            if type(value) is not int or value < (1 if key == "nr_band" else 0):
                return None
    return copied


def _import_target_matches_result(
    payload: Mapping[str, Any], identity: Mapping[str, Any], signature: str,
) -> bool:
    """Check the commanded field, without turning a pool action into PBAND."""
    try:
        target = core_command_target_from_payload(payload["target"])
        pool = tuple(identity["ca_pool"])
        if isinstance(target, CoreBandSetTarget):
            return (payload.get("action_family") == "lte_ca_pool"
                    and target.bands == pool
                    and signature == core_lte_band_signature(pool))
        if isinstance(target, CorePBandTarget):
            return (payload.get("action_family") == "lte_radio_selection"
                    and target.pband == identity["pband"] and target.ca_pool == pool
                    and signature == core_lte_pband_signature(target.pband, pool))
    except (KeyError, TypeError, ValueError):
        return False
    return False


def _legacy_transition_window_is_complete(payload: Mapping[str, Any]) -> bool:
    opened_at = _number(payload.get("opened_at"))
    due_at = _number(payload.get("due_at"))
    closed_at = _number(payload.get("closed_at"))
    window_seconds = _number(payload.get("window_seconds"))
    if opened_at is None or due_at is None or closed_at is None:
        return False
    if abs((due_at - opened_at) - FINAL_GRADE_WINDOW_S) > 0.001:
        return False
    if window_seconds is not None and abs(window_seconds - FINAL_GRADE_WINDOW_S) > 0.001:
        return False
    return due_at <= closed_at <= due_at + 30.0


def _legacy_absolute_component_grades(
    observed: Mapping[str, Any],
) -> dict[str, float | None] | None:
    grades = _quality.context_quality_grades(observed)
    if (
        grades["performance_capacity"] is None
        or grades["performance_communication_quality"] is None
        or (
            grades["performance_latency"] is None
            and grades["performance_jitter"] is None
        )
    ):
        return None
    return grades


def recalculate_imported_referee_history_once(
    history: AdvisoryHistory,
    *,
    scope_key: str,
    decision_history: DecisionLifecycleHistorySnapshot,
    now: float | None = None,
) -> int:
    """Regrade imported raw evidence once for one continuity scope."""

    if not isinstance(history, AdvisoryHistory):
        raise TypeError("new_horizon_referee_replay_history_required")
    if type(scope_key) is not str or not scope_key:
        raise ValueError("new_horizon_referee_replay_scope_required")
    if now is not None and (
        type(now) not in {int, float} or not math.isfinite(now) or now <= 0
    ):
        raise ValueError("new_horizon_referee_replay_clock_invalid")
    if (
        not isinstance(decision_history, DecisionLifecycleHistorySnapshot)
        or decision_history.continuity_key != scope_key
    ):
        raise ValueError("new_horizon_referee_replay_decision_history_required")
    completed = history.load_checkpoint(
        scope_key=scope_key,
        name=_SERVICE_REWARD_IMPORT_RECALCULATION,
    )
    if completed is not None:
        if (
            completed.get("schema") != _SERVICE_REWARD_IMPORT_RECALCULATION_SCHEMA
            or completed.get("formula_schema") not in {REFEREE_PROOF_EVIDENCE_SCHEMA, RETIRED_LTE_PROOF_SCHEMA}
            or completed.get("weights") != _quality.SERVICE_REWARD_WEIGHTS
        ):
            raise ValueError("new_horizon_service_reward_import_marker_invalid")
        return 0
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    retained = history.query_scoped_kinds(
        scope_key=scope_key,
        kinds=tuple(kind for kind, _schema in PRIOR_REFEREE_PROOF_CONTRACTS),
        limit_per_kind=None,
        since=None if now is None else float(now) - history.retention_seconds,
    )
    for prior_kind, prior_schema in PRIOR_REFEREE_PROOF_CONTRACTS:
        for row in retained[prior_kind]:
            payload = row.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != prior_schema
            ):
                continue
            assessment_id = str(payload.get("assessment_id") or "")
            if assessment_id:
                provenance = payload.get("replay_provenance")
                source_schema = (
                    str(provenance.get("source_schema") or "")
                    if isinstance(provenance, Mapping)
                    else ""
                ) or prior_schema
                source_assessment_id = (
                    str(provenance.get("source_assessment_id") or "")
                    if isinstance(provenance, Mapping)
                    else ""
                ) or assessment_id
                lineage = (source_schema, source_assessment_id)
                existing = grouped.get(lineage)
                if existing is None:
                    grouped[lineage] = [payload]
                elif existing[0].get("schema") == prior_schema:
                    existing.append(payload)

    legacy_rows = tuple(row for rows in grouped.values() for row in rows)
    bandwidth_by_identity = _legacy_bandwidth_consensus(legacy_rows)
    attributable_decisions = {
        str(row["payload"].get("proposal_id") or "")
        for row in decision_history.attributable_decisions
        if isinstance(row.get("payload"), Mapping)
        and str(row["payload"].get("proposal_id") or "")
    }
    replayed = 0
    replayed_context = 0
    replayed_transition = 0
    saved_hours = history.load_checkpoint(scope_key=scope_key, name=AQ_HOURLY_CHECKPOINT)
    retained_measurements = {
        interval["assessment_id"]
        for raw in (saved_hours or {}).get("hours", ())
        for interval in require_aq_hour(raw)["intervals"]
    } | set((saved_hours or {}).get("excluded", {}))
    for (prior_schema, prior_assessment_id), rows in grouped.items():
        service_source = next(
            (row for row in rows if row.get("dimension") == "service_outcome"),
            None,
        )
        if not isinstance(service_source, Mapping):
            continue
        if service_source.get("status") != "graded" or service_source.get("final_assessment") is not True:
            continue
        if str(service_source.get("continuity_key") or "") != scope_key:
            continue
        observed = service_source.get("observed_vector")
        baseline = service_source.get("baseline_vector")
        learning_context = service_source.get("learning_context")
        result_identity = service_source.get("result_identity")
        result_scope_key = str(service_source.get("result_scope_key") or "")
        target = service_source.get("target")
        target_signature = str(service_source.get("target_signature") or "")
        closed_at = _number(service_source.get("closed_at"))
        if (
            not isinstance(observed, Mapping)
            or not observed
            or not isinstance(learning_context, Mapping)
            or not learning_context
            or not isinstance(target, Mapping)
            or not target
            or not target_signature
            or closed_at is None
            or closed_at <= 0.0
        ):
            continue
        try:
            learning_context = require_learning_context(learning_context)
        except (TypeError, ValueError):
            # An incompatible retired row stays diagnostic. It cannot prevent
            # other valid rows, or current monitoring, from being restored.
            continue
        stored_learning_key = str(service_source.get("learning_context_key") or "")
        if stored_learning_key != _learning_context_key(learning_context):
            continue
        if isinstance(result_identity, Mapping) and result_identity:
            replay_result_identity = dict(result_identity)
        elif result_scope_key:
            # Some older transition proofs have the correct device and provider
            # scope but no readable identity object. Keep that isolation instead
            # of discarding complete raw vectors.
            replay_result_identity = {
                "continuity_key": scope_key,
                "result_scope_key": result_scope_key,
                "target_signature": target_signature,
            }
        else:
            continue
        observed_identity = _legacy_bandwidth_identity(result_identity)
        baseline_identity = _legacy_bandwidth_identity(
            service_source.get("baseline_identity")
        )
        current_vectors = service_source.get("schema") == "NewHorizonRefereeProofEvidence.v17"
        # v17 already owns ECI/CQB and Core connectivity correctly. Only its
        # transition reward semantics change; do not recalculate physical data.
        migrated_observed = dict(observed) if current_vectors else _migrate_legacy_service_vector(
            observed,
            bandwidth_mhz=(
                bandwidth_by_identity.get(observed_identity)
                if observed_identity is not None
                else None
            ),
        )
        if migrated_observed is None:
            continue
        migrated_baseline = dict(baseline) if current_vectors and isinstance(baseline, Mapping) else (
            _migrate_legacy_service_vector(
                baseline,
                bandwidth_mhz=(
                    bandwidth_by_identity.get(baseline_identity)
                    if baseline_identity is not None
                    else None
                ),
            )
            if isinstance(baseline, Mapping) and baseline
            else None
        )
        # This existing import boundary is for retired LTE evidence only.
        # Old NR grades stay in their original records; the new population is empty.
        if any(v and (v.get("nr_active") is True or v.get("quality_model") == NR_QUALITY_MODEL)
               for v in (migrated_observed, migrated_baseline)):
            continue
        migrated_observed["quality_model"] = LTE_QUALITY_MODEL
        if migrated_baseline:
            migrated_baseline["quality_model"] = LTE_QUALITY_MODEL
        mode = str(service_source.get("assessment_mode") or "")
        if mode == "context_quality":
            component_grades = _quality.context_quality_grades(migrated_observed)
            vector_migration = "complete_legacy_context_vector_to_v17"
        elif mode == "transition":
            baseline_identity = _exact_legacy_lte_transition_identity(
                service_source.get("baseline_identity"), current_vectors=current_vectors,
            )
            result_identity = _exact_legacy_lte_transition_identity(
                service_source.get("result_identity"), current_vectors=current_vectors,
            )
            baseline_scope_key = str(
                service_source.get("baseline_scope_key") or ""
            )
            result_scope_key = str(service_source.get("result_scope_key") or "")
            decision_id = str(service_source.get("decision_id") or "")
            proposal_id = decision_id.removeprefix("core-command:")
            if (
                service_source.get("status") != "graded"
                or service_source.get("final_assessment") is not True
                or baseline_identity is None
                or result_identity is None
                or migrated_baseline is None
                or not baseline_scope_key
                or baseline_scope_key == "unavailable"
                or not result_scope_key
                or result_scope_key == "unavailable"
                or not _legacy_transition_window_is_complete(service_source)
                or not _import_target_matches_result(service_source, result_identity, target_signature)
                or (
                    is_brain_decision_origin(service_source.get("origin"))
                    and proposal_id not in attributable_decisions
                )
            ):
                continue
            component_grades = _legacy_absolute_component_grades(migrated_observed)
            if component_grades is None:
                continue
            replay_result_identity = result_identity
            vector_migration = "complete_legacy_transition_vector_to_v17"
        else:
            continue
        grade = component_grades.get("performance_overall_outcome")
        if mode == "transition":
            grade = _quality.weighted_service_reward(component_grades)
        if type(grade) not in {int, float} or isinstance(grade, bool):
            continue
        # Preserve v17 measurement identity: some of these blocks are already
        # in the v1 hourly checkpoint. Only the proof revision gets a new ID.
        replay_assessment_id = str(service_source["assessment_id"]) if current_vectors else hashlib.sha256(
            (
                "current-formula-replay|"
                + prior_schema
                + "|"
                + prior_assessment_id
                + "|"
                + REFEREE_PROOF_EVIDENCE_SCHEMA
            ).encode("utf-8")
        ).hexdigest()
        if not current_vectors:
            # Compaction may have retired the v17 proof while retaining its
            # v16 diagnostic original. Keep the proven measurement lineage;
            # the hourly owner still verifies the full interval signature.
            v17_id = hashlib.sha256(
                ("current-formula-replay|" + prior_schema + "|"
                 + prior_assessment_id + "|NewHorizonRefereeProofEvidence.v17").encode("utf-8")
            ).hexdigest()
            if v17_id in retained_measurements:
                replay_assessment_id = v17_id
        proof_id = _dimension_proof_id(replay_assessment_id, REFEREE_PROOF_EVIDENCE_SCHEMA + ":service_outcome")
        recorded_any = history.record(
            event_key="proof:" + proof_id,
            scope_key=scope_key,
            kind=REFEREE_PROOF_HISTORY_KIND,
            observed_at=closed_at,
            payload={
                **dict(service_source),
                "schema": REFEREE_PROOF_EVIDENCE_SCHEMA,
                "learning_context": learning_context,
                "learning_context_key": stored_learning_key,
                "proof_id": proof_id,
                "assessment_id": replay_assessment_id,
                "dimension": "service_outcome",
                "status": "graded",
                "grade": grade,
                "final_assessment": True,
                "reasons": ["complete_raw_evidence_regraded_current_formula"],
                "component_grades": dict(component_grades),
                "baseline_vector": dict(migrated_baseline or {}),
                "observed_vector": dict(migrated_observed),
                "result_identity": replay_result_identity,
                "replay_provenance": {
                    "source_schema": prior_schema,
                    "source_assessment_id": prior_assessment_id,
                    "formula_schema": REFEREE_PROOF_EVIDENCE_SCHEMA,
                    "vector_migration": vector_migration,
                },
            },
        )
        replayed += int(recorded_any)
        replayed_context += int(recorded_any and mode == "context_quality")
        replayed_transition += int(recorded_any and mode == "transition")
    history.save_checkpoint(
        scope_key=scope_key,
        name=_SERVICE_REWARD_IMPORT_RECALCULATION,
        updated_at=time() if now is None else float(now),
        payload={
            "schema": _SERVICE_REWARD_IMPORT_RECALCULATION_SCHEMA,
            "formula_schema": REFEREE_PROOF_EVIDENCE_SCHEMA,
            "weights": dict(_quality.SERVICE_REWARD_WEIGHTS),
            "replayed_count": replayed,
            "replayed_context_count": replayed_context,
            "replayed_transition_count": replayed_transition,
        },
    )
    return replayed


__all__ = (
    "ProofReferee",
    "FINAL_GRADE_WINDOW_S",
    "DECISION_LIFECYCLE_TIMEOUT_S",
    "REFEREE_BASELINE_RESERVATION_KIND",
    "REFEREE_BASELINE_RESERVATION_SCHEMA",
    "REFEREE_PROOF_EVIDENCE_SCHEMA",
    "REFEREE_PROOF_HISTORY_KIND",
    "REFEREE_PARTIAL_EVIDENCE_SCHEMA",
    "REFEREE_PARTIAL_HISTORY_KIND",
    "REFEREE_TIMELINE_DISCONTINUITY_HISTORY_KIND",
    "PRIOR_REFEREE_PROOF_CONTRACTS",
    "context_action_lifecycle_key",
    "context_continuity_key",
    "context_continuity_observation_state",
    "context_learning_components",
    "context_learning_key",
    "current_context_target",
    "context_scope_key",
    "referee_sample_metrics",
    "referee_service_vector",
    "normalize_referee_assessment",
    "referee_assessment_is_complete",
    "referee_assessment_training_proofs",
    "RefereeAssessmentEvidence",
    "RefereeAssessmentHistoryOwner",
    "RefereeAssessmentHistorySnapshot",
    "recalculate_imported_referee_history_once",
)
