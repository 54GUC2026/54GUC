"""Immutable cells of the sole CGF/Intelligence Matrix boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.action_reachability import (
    CoreCurrentReachabilityView,
    CoreReachabilityEvidenceView,
)
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyReachabilityView,
)
from arch4.core_contracts.intelligence_proposals import BrainCommandProposal
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.outcomes import CoreCommandOutcomeRecord
from arch4.core_contracts.runtime_snapshot import CoreRuntimeSnapshot
from arch4.intelligence_presentation_contract import (
    BAND_CONTEXT_SOURCE_ID,
    FORECAST_SOURCE_ID,
    IMPACT_SOURCE_ID,
    LEARNING_CYCLE_HISTORY_SOURCE_ID,
    NEW_HORIZON_NGUI_SOURCE_IDS,
    REFEREE_SUMMARY_SOURCE_ID,
    validate_band_context_evidence,
    validate_forecast_presentation,
    validate_intelligence_impact,
    validate_learning_cycle_history,
    validate_referee_summary,
)


INTELLIGENCE_CLIENT_ID = "54guc.intelligence.customer"
INTELLIGENCE_MATRIX_OWNER = "54guc.product.intelligence_matrix"
INTELLIGENCE_MATRIX_CONTRACT_VERSION = "ProductIntelligenceMatrix.v1"
INTELLIGENCE_CONSTRAINT_VIEW_SCHEMA = "IntelligenceOperationalConstraintView.v3"
INTELLIGENCE_CONSTRAINT_VIEW_OWNER = INTELLIGENCE_MATRIX_OWNER
INTELLIGENCE_PROVIDER_POLICY_SCHEMA = "IntelligenceProviderBandPolicyView.v1"
INTELLIGENCE_PRODUCT_CONTROL_SCHEMA = "IntelligenceProductControlView.v1"
INTELLIGENCE_CONTROL_STANDALONE = "CGF_STANDALONE"
INTELLIGENCE_CONTROL_OBSERVE = "INTELLIGENCE_OBSERVE"
INTELLIGENCE_CONTROL_AUTOPILOT = "INTELLIGENCE_AUTOPILOT"
INTELLIGENCE_CONTROL_MODES = frozenset(
    {
        INTELLIGENCE_CONTROL_STANDALONE,
        INTELLIGENCE_CONTROL_OBSERVE,
        INTELLIGENCE_CONTROL_AUTOPILOT,
    }
)
INTELLIGENCE_MATRIX_OBSERVATION_SCHEMA = "IntelligenceMatrixObservation.v2"
INTELLIGENCE_OBSERVATION_CAUSES = (
    "matrix_root",
    "reachability",
    "emergency",
    "constraint",
)
INTELLIGENCE_MATRIX_INPUT_SCHEMA = "IntelligenceMatrixInputSnapshot.v1"
INTELLIGENCE_MATRIX_OUTPUT_SCHEMA = "IntelligenceMatrixOutputSnapshot.v1"
INTELLIGENCE_PROPOSAL_DISPATCH_SCHEMA = (
    "IntelligenceProposalDispatchResult.v1"
)
INTELLIGENCE_HEALTH_SCHEMA = "IntelligenceHealthSnapshot.v1"
INTELLIGENCE_PRESENTATION_SCHEMA = "IntelligencePresentationSnapshot.v2"
INTELLIGENCE_SIGNAL_BASELINE_SCHEMA = "IntelligenceSignalBaselineSnapshot.v2"
SIGNAL_BASELINE_FORMULA_GENERATION = "rsrq_floor_v1+rtt_quiet_floor_v1"


def _validate_constraint_root(
    matrix_identity: MatrixGenerationIdentity,
    accepted_root_fingerprint: str,
    core_snapshot: CoreRuntimeSnapshot,
) -> None:
    if not isinstance(matrix_identity, MatrixGenerationIdentity):
        raise TypeError("intelligence_constraint_matrix_identity_required")
    if (
        type(accepted_root_fingerprint) is not str
        or len(accepted_root_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in accepted_root_fingerprint
        )
    ):
        raise TypeError("intelligence_constraint_root_fingerprint_invalid")
    if not isinstance(core_snapshot, CoreRuntimeSnapshot):
        raise TypeError("intelligence_constraint_core_snapshot_required")
    if (
        core_snapshot.matrix_identity != matrix_identity
        or core_snapshot.accepted_root_fingerprint != accepted_root_fingerprint
    ):
        raise ValueError("intelligence_constraint_core_snapshot_root_mismatch")


def _validate_reachability_views(
    matrix_identity: MatrixGenerationIdentity,
    core_reachability: CoreCurrentReachabilityView,
    core_reachability_evidence: CoreReachabilityEvidenceView,
    core_emergency_reachability: CoreEmergencyReachabilityView,
) -> None:
    if not isinstance(core_reachability, CoreCurrentReachabilityView):
        raise TypeError("intelligence_constraint_core_reachability_required")
    if (
        core_reachability.session_scope_key
        and core_reachability.session_scope_key != matrix_identity.session_scope_key
    ):
        raise ValueError("intelligence_constraint_core_reachability_scope_mismatch")
    if not isinstance(core_reachability_evidence, CoreReachabilityEvidenceView):
        raise TypeError(
            "intelligence_constraint_core_reachability_evidence_required"
        )
    if (
        core_reachability_evidence.session_scope_key
        and core_reachability_evidence.session_scope_key
        != matrix_identity.session_scope_key
    ):
        raise ValueError(
            "intelligence_constraint_core_reachability_evidence_scope_mismatch"
        )
    if (
        core_reachability.session_scope_key
        and core_reachability_evidence.session_scope_key
        != core_reachability.session_scope_key
    ):
        raise ValueError(
            "intelligence_constraint_core_reachability_views_mismatch"
        )
    if not isinstance(core_emergency_reachability, CoreEmergencyReachabilityView):
        raise TypeError(
            "intelligence_constraint_core_emergency_reachability_required"
        )
    if (
        core_emergency_reachability.session_scope_key
        and core_emergency_reachability.session_scope_key
        != matrix_identity.session_scope_key
    ):
        raise ValueError(
            "intelligence_constraint_core_emergency_reachability_scope_mismatch"
        )


def _validate_band_policy_payload(foundation_band_policy: CoreFrozenPayload) -> None:
    if not isinstance(foundation_band_policy, CoreFrozenPayload):
        raise TypeError("intelligence_constraint_band_policy_payload_required")
    band_policy = foundation_band_policy.to_mapping()
    if (
        band_policy.get("schema") != INTELLIGENCE_PROVIDER_POLICY_SCHEMA
        or band_policy.get("owner") != INTELLIGENCE_MATRIX_OWNER
        or band_policy.get("disposition") not in {"available", "unavailable"}
        or not isinstance(band_policy.get("policy"), dict)
        or type(band_policy.get("reason")) is not str
    ):
        raise ValueError("intelligence_constraint_band_policy_invalid")


def _validate_product_control_payload(product_control_payload: CoreFrozenPayload) -> None:
    if not isinstance(product_control_payload, CoreFrozenPayload):
        raise TypeError("intelligence_constraint_product_control_required")
    product_control = product_control_payload.to_mapping()
    if (
        product_control.get("schema") != INTELLIGENCE_PRODUCT_CONTROL_SCHEMA
        or product_control.get("owner") != INTELLIGENCE_MATRIX_OWNER
        or product_control.get("control_mode") not in INTELLIGENCE_CONTROL_MODES
        or type(product_control.get("control_available")) is not bool
        or type(product_control.get("connected_settled")) is not bool
        or type(product_control.get("autopilot_requested")) is not bool
        or type(product_control.get("explore_requested")) is not bool
        or type(product_control.get("explore_session_id")) is not str
        or type(product_control.get("explore_started_at")) not in {int, float}
        or type(product_control.get("explore_deadline_at")) not in {int, float}
        or type(product_control.get("explore_reason")) is not str
        or type(product_control.get("control_generation")) is not int
        or type(product_control.get("control_reason")) is not str
    ):
        raise ValueError("intelligence_constraint_product_control_invalid")
    explore_requested = product_control["explore_requested"]
    explore_session_id = product_control["explore_session_id"]
    explore_started_at = float(product_control["explore_started_at"])
    explore_deadline_at = float(product_control["explore_deadline_at"])
    invalid_times = (
        not math.isfinite(explore_started_at)
        or not math.isfinite(explore_deadline_at)
        or explore_started_at < 0.0
        or explore_deadline_at < 0.0
    )
    invalid_active = explore_requested and (
        not explore_session_id
        or explore_started_at <= 0.0
        or explore_deadline_at <= explore_started_at
    )
    invalid_inactive = not explore_requested and (
        bool(explore_session_id) or bool(explore_started_at) or bool(explore_deadline_at)
    )
    if invalid_times or invalid_active or invalid_inactive:
        raise ValueError("intelligence_constraint_explore_state_invalid")


@dataclass(frozen=True, slots=True)
class IntelligenceOperationalConstraintView:
    """Immutable current owner publications; never a second truth store."""

    matrix_identity: MatrixGenerationIdentity
    accepted_root_fingerprint: str
    core_snapshot: CoreRuntimeSnapshot
    core_reachability: CoreCurrentReachabilityView
    core_reachability_evidence: CoreReachabilityEvidenceView
    core_emergency_reachability: CoreEmergencyReachabilityView
    foundation_schedule: CoreFrozenPayload
    foundation_band_policy: CoreFrozenPayload
    product_control: CoreFrozenPayload
    schedule_revision: int
    schedule_observed_at: float
    schema: str = INTELLIGENCE_CONSTRAINT_VIEW_SCHEMA
    owner: str = INTELLIGENCE_CONSTRAINT_VIEW_OWNER

    def __post_init__(self) -> None:
        _validate_constraint_root(
            self.matrix_identity,
            self.accepted_root_fingerprint,
            self.core_snapshot,
        )
        _validate_reachability_views(
            self.matrix_identity,
            self.core_reachability,
            self.core_reachability_evidence,
            self.core_emergency_reachability,
        )
        if not isinstance(self.foundation_schedule, CoreFrozenPayload):
            raise TypeError("intelligence_constraint_schedule_payload_required")
        _validate_band_policy_payload(self.foundation_band_policy)
        _validate_product_control_payload(self.product_control)
        if type(self.schedule_revision) is not int or self.schedule_revision < 0:
            raise TypeError("intelligence_constraint_schedule_revision_invalid")
        if (
            type(self.schedule_observed_at) is not float
            or not math.isfinite(self.schedule_observed_at)
            or self.schedule_observed_at < 0.0
        ):
            raise TypeError("intelligence_constraint_schedule_time_invalid")
        if (
            self.schema != INTELLIGENCE_CONSTRAINT_VIEW_SCHEMA
            or self.owner != INTELLIGENCE_CONSTRAINT_VIEW_OWNER
        ):
            raise ValueError("intelligence_constraint_owner_schema_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "accepted_root_fingerprint": self.accepted_root_fingerprint,
            "core_snapshot": self.core_snapshot.to_payload(),
            "core_reachability": self.core_reachability.as_dict(),
            "core_reachability_evidence": (
                self.core_reachability_evidence.as_dict()
            ),
            "core_emergency_reachability": (
                self.core_emergency_reachability.as_dict()
            ),
            "foundation_schedule": self.foundation_schedule.to_mapping(),
            "foundation_band_policy": self.foundation_band_policy.to_mapping(),
            "product_control": self.product_control.to_mapping(),
            "schedule_revision": self.schedule_revision,
            "schedule_observed_at": self.schedule_observed_at,
        }


def _nonnegative_int(value: object, reason: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(reason)
    return value


def _finite_time(value: object, reason: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(reason)
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(reason)
    return result


def _text(value: object, reason: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or value != value.strip():
        raise TypeError(reason)
    if not allow_empty and not value:
        raise ValueError(reason)
    return value


def intelligence_signal_scope_key(
    *,
    identity_code: str,
    profile_id: str,
    provider_plmn: str,
    network_type: str,
    serving_band: int,
    configured_band_pool: tuple[int, ...],
    cell_id: int,
    earfcn: int,
    pci: int,
    bandwidth_state: str,
    bandwidth_mhz: float,
    formula_generation: str = SIGNAL_BASELINE_FORMULA_GENERATION,
) -> str:
    """Return the one cross-boundary signal-baseline context identity."""

    upper_network_type = str(network_type).upper()
    rat = (
        "NR_SA"
        if "NR SA" in upper_network_type
        else "NR_NSA"
        if "NR NSA" in upper_network_type
        else "LTE"
        if "LTE" in upper_network_type or "4G" in upper_network_type
        else str(network_type)
    )
    material = {
        "identity_code": identity_code,
        "profile_id": profile_id,
        "provider_plmn": provider_plmn,
        "rat": rat,
        "serving_band": serving_band,
        "configured_band_pool": list(configured_band_pool),
        "cell_id": cell_id,
        "earfcn": earfcn,
        "pci": pci,
        "bandwidth_state": bandwidth_state,
        "bandwidth_mhz": float(bandwidth_mhz),
        "formula_generation": formula_generation,
    }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class IntelligenceMatrixObservation:
    """One coherent accepted root plus constraints assembled by CGF."""

    key: str
    root: CoreAcceptedGenerationRoot
    constraints: IntelligenceOperationalConstraintView
    causes: tuple[str, ...]
    published_at: float
    schema: str = INTELLIGENCE_MATRIX_OBSERVATION_SCHEMA
    owner: str = INTELLIGENCE_MATRIX_OWNER

    def __post_init__(self) -> None:
        _text(self.key, "intelligence_matrix_observation_key_required")
        if not isinstance(self.root, CoreAcceptedGenerationRoot):
            raise TypeError("intelligence_matrix_observation_root_required")
        if not isinstance(
            self.constraints,
            IntelligenceOperationalConstraintView,
        ):
            raise TypeError(
                "intelligence_matrix_observation_constraints_required"
            )
        if (
            self.constraints.matrix_identity != self.root.matrix_identity
            or self.constraints.accepted_root_fingerprint
            != self.root.root_fingerprint
        ):
            raise ValueError("intelligence_matrix_observation_root_mismatch")
        if (
            not isinstance(self.causes, tuple)
            or not self.causes
            or any(type(item) is not str for item in self.causes)
            or any(item not in INTELLIGENCE_OBSERVATION_CAUSES for item in self.causes)
            or len(set(self.causes)) != len(self.causes)
            or self.causes
            != tuple(
                item for item in INTELLIGENCE_OBSERVATION_CAUSES if item in self.causes
            )
        ):
            raise ValueError("intelligence_matrix_observation_causes_invalid")
        _finite_time(
            self.published_at,
            "intelligence_matrix_observation_time_invalid",
        )
        if (
            self.schema != INTELLIGENCE_MATRIX_OBSERVATION_SCHEMA
            or self.owner != INTELLIGENCE_MATRIX_OWNER
        ):
            raise ValueError("intelligence_matrix_observation_contract_invalid")


@dataclass(frozen=True, slots=True)
class IntelligenceProposalDispatchResult:
    """Product-adapter result for one proposal latched through the Matrix."""

    sequence: int
    proposal_id: str
    proposal: BrainCommandProposal
    submitted: bool
    reason: str
    result: CoreFrozenPayload
    published_at: float
    schema: str = INTELLIGENCE_PROPOSAL_DISPATCH_SCHEMA
    owner: str = INTELLIGENCE_MATRIX_OWNER

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise TypeError("intelligence_proposal_dispatch_sequence_required")
        _text(self.proposal_id, "intelligence_proposal_dispatch_id_required")
        if not isinstance(self.proposal, BrainCommandProposal):
            raise TypeError("intelligence_proposal_dispatch_proposal_required")
        if self.proposal.proposal_id != self.proposal_id:
            raise ValueError("intelligence_proposal_dispatch_proposal_mismatch")
        if type(self.submitted) is not bool:
            raise TypeError("intelligence_proposal_dispatch_submitted_required")
        _text(
            self.reason,
            "intelligence_proposal_dispatch_reason_invalid",
            allow_empty=True,
        )
        if not isinstance(self.result, CoreFrozenPayload):
            raise TypeError("intelligence_proposal_dispatch_result_required")
        _finite_time(
            self.published_at,
            "intelligence_proposal_dispatch_time_invalid",
        )
        if (
            self.schema != INTELLIGENCE_PROPOSAL_DISPATCH_SCHEMA
            or self.owner != INTELLIGENCE_MATRIX_OWNER
        ):
            raise ValueError("intelligence_proposal_dispatch_contract_invalid")


@dataclass(frozen=True, slots=True)
class IntelligenceHealthSnapshot:
    """Latest Intelligence-owned health; never CGF product health."""

    state: str
    fault_count: int
    last_error: str
    input_revision: int
    observed_at: float
    control_risk: str = "normal"
    explore_active: bool = False
    explore_session_id: str = ""
    explore_stop_requested: bool = False
    explore_reason: str = ""
    schema: str = INTELLIGENCE_HEALTH_SCHEMA
    owner: str = "54guc.new_horizon.advisory_runtime"

    def __post_init__(self) -> None:
        if self.state not in {
            "starting",
            "running",
            "degraded",
            "stopping",
            "stopped",
        }:
            raise ValueError("intelligence_health_state_invalid")
        if self.control_risk not in {"normal", "recoverable", "critical"}:
            raise ValueError("intelligence_health_control_risk_invalid")
        if type(self.explore_active) is not bool:
            raise TypeError("intelligence_health_explore_active_required")
        if type(self.explore_session_id) is not str:
            raise TypeError("intelligence_health_explore_session_id_required")
        if type(self.explore_stop_requested) is not bool:
            raise TypeError("intelligence_health_explore_stop_required")
        if type(self.explore_reason) is not str:
            raise TypeError("intelligence_health_explore_reason_required")
        if self.explore_active and not self.explore_session_id:
            raise ValueError("intelligence_health_explore_identity_missing")
        if self.explore_stop_requested and not self.explore_reason:
            raise ValueError("intelligence_health_explore_stop_reason_missing")
        _nonnegative_int(
            self.fault_count,
            "intelligence_health_fault_count_invalid",
        )
        _text(
            self.last_error,
            "intelligence_health_last_error_invalid",
            allow_empty=True,
        )
        _nonnegative_int(
            self.input_revision,
            "intelligence_health_input_revision_invalid",
        )
        _finite_time(
            self.observed_at,
            "intelligence_health_observed_at_invalid",
        )
        if self.schema != INTELLIGENCE_HEALTH_SCHEMA:
            raise ValueError("intelligence_health_schema_invalid")


@dataclass(frozen=True, slots=True)
class IntelligenceSignalBaselineSnapshot:
    """Exact-context learned floors returned through the Product Matrix."""

    namespace: RuntimeGenerationNamespace
    signal_scope_key: str
    subscription_key: str
    rsrq_baseline_db: float | None
    rtt_baseline_ms: float | None
    learned_at: float
    published_at: float
    formula_generation: str = SIGNAL_BASELINE_FORMULA_GENERATION
    schema: str = INTELLIGENCE_SIGNAL_BASELINE_SCHEMA
    owner: str = "54guc.new_horizon.advisory_history"

    def __post_init__(self) -> None:
        if (type(self.subscription_key) is not str or len(self.subscription_key) != 64
                or any(c not in "0123456789abcdef" for c in self.subscription_key)):
            raise ValueError("intelligence_signal_baseline_subscription_required")
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("intelligence_signal_baseline_namespace_required")
        if (
            type(self.signal_scope_key) is not str
            or len(self.signal_scope_key) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.signal_scope_key
            )
        ):
            raise ValueError("intelligence_signal_baseline_scope_key_invalid")
        if self.rsrq_baseline_db is None and self.rtt_baseline_ms is None:
            raise ValueError("intelligence_signal_baseline_value_required")
        if self.rsrq_baseline_db is not None:
            value = float(self.rsrq_baseline_db)
            if not math.isfinite(value) or not -25.0 <= value < 0.0:
                raise ValueError("intelligence_signal_baseline_rsrq_invalid")
            object.__setattr__(self, "rsrq_baseline_db", value)
        if self.rtt_baseline_ms is not None:
            value = float(self.rtt_baseline_ms)
            if not math.isfinite(value) or not 0.0 < value < 10_000.0:
                raise ValueError("intelligence_signal_baseline_rtt_invalid")
            object.__setattr__(self, "rtt_baseline_ms", value)
        _finite_time(self.learned_at, "intelligence_signal_baseline_learned_at_invalid")
        _finite_time(self.published_at, "intelligence_signal_baseline_published_at_invalid")
        if self.published_at < self.learned_at:
            raise ValueError("intelligence_signal_baseline_time_order_invalid")
        if self.formula_generation != SIGNAL_BASELINE_FORMULA_GENERATION:
            raise ValueError("intelligence_signal_baseline_formula_invalid")
        if (
            self.schema != INTELLIGENCE_SIGNAL_BASELINE_SCHEMA
            or self.owner != "54guc.new_horizon.advisory_history"
        ):
            raise ValueError("intelligence_signal_baseline_contract_invalid")

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.namespace,
            self.signal_scope_key,
            self.subscription_key,
            self.rsrq_baseline_db,
            self.rtt_baseline_ms,
            self.learned_at,
        )


@dataclass(frozen=True, slots=True)
class IntelligencePresentationSnapshot:
    """Intelligence-owned page facts before Product adds control readback."""

    namespace: RuntimeGenerationNamespace
    sequence: int
    published_at: float
    values: CoreFrozenPayload
    validity: CoreFrozenPayload
    freshness: CoreFrozenPayload
    reasons: CoreFrozenPayload
    sampled_at: CoreFrozenPayload
    sample_sequences: CoreFrozenPayload
    schema: str = INTELLIGENCE_PRESENTATION_SCHEMA
    owner: str = "54guc.new_horizon.advisory_runtime"

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("intelligence_presentation_namespace_required")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("intelligence_presentation_sequence_invalid")
        _finite_time(
            self.published_at,
            "intelligence_presentation_time_invalid",
        )
        if self.schema != INTELLIGENCE_PRESENTATION_SCHEMA:
            raise ValueError("intelligence_presentation_schema_invalid")
        if self.owner != "54guc.new_horizon.advisory_runtime":
            raise ValueError("intelligence_presentation_owner_invalid")
        payloads = (
            self.values,
            self.validity,
            self.freshness,
            self.reasons,
            self.sampled_at,
            self.sample_sequences,
        )
        if any(not isinstance(item, CoreFrozenPayload) for item in payloads):
            raise TypeError("intelligence_presentation_payload_required")
        expected = set(NEW_HORIZON_NGUI_SOURCE_IDS)
        if set(self.values.to_mapping()) != expected or any(
            set(item.to_mapping()) != expected for item in payloads[1:]
        ):
            raise ValueError("intelligence_presentation_denominator_mismatch")
        validate_band_context_evidence(
            self.values.to_mapping()[BAND_CONTEXT_SOURCE_ID]
        )
        validate_forecast_presentation(
            self.values.to_mapping()[FORECAST_SOURCE_ID]
        )
        validate_intelligence_impact(
            self.values.to_mapping()[IMPACT_SOURCE_ID]
        )
        validate_learning_cycle_history(
            self.values.to_mapping()[LEARNING_CYCLE_HISTORY_SOURCE_ID]
        )
        validate_referee_summary(
            self.values.to_mapping()[REFEREE_SUMMARY_SOURCE_ID]
        )


@dataclass(frozen=True, slots=True)
class IntelligenceMatrixInputSnapshot:
    """Immutable current CGF-to-Intelligence bank snapshot."""

    revision: int
    published_at: float
    observation: IntelligenceMatrixObservation | None
    outcomes: tuple[CoreCommandOutcomeRecord, ...]
    proposal_results: tuple[IntelligenceProposalDispatchResult, ...]
    outcome_evicted_through_sequence: int = 0
    proposal_result_evicted_through_sequence: int = 0
    schema: str = INTELLIGENCE_MATRIX_INPUT_SCHEMA
    owner: str = INTELLIGENCE_MATRIX_OWNER

    def __post_init__(self) -> None:
        _nonnegative_int(self.revision, "intelligence_input_revision_invalid")
        _finite_time(self.published_at, "intelligence_input_time_invalid")
        if self.observation is not None and not isinstance(
            self.observation,
            IntelligenceMatrixObservation,
        ):
            raise TypeError("intelligence_input_observation_invalid")
        if any(
            not isinstance(item, CoreCommandOutcomeRecord)
            for item in self.outcomes
        ):
            raise TypeError("intelligence_input_outcomes_invalid")
        if any(
            not isinstance(item, IntelligenceProposalDispatchResult)
            for item in self.proposal_results
        ):
            raise TypeError("intelligence_input_proposal_results_invalid")
        _nonnegative_int(
            self.outcome_evicted_through_sequence,
            "intelligence_input_outcome_eviction_invalid",
        )
        _nonnegative_int(
            self.proposal_result_evicted_through_sequence,
            "intelligence_input_proposal_result_eviction_invalid",
        )
        if (
            self.schema != INTELLIGENCE_MATRIX_INPUT_SCHEMA
            or self.owner != INTELLIGENCE_MATRIX_OWNER
        ):
            raise ValueError("intelligence_input_contract_invalid")


@dataclass(frozen=True, slots=True)
class IntelligenceMatrixOutputSnapshot:
    """Immutable current Intelligence-to-CGF bank snapshot."""

    revision: int
    published_at: float
    presentation: IntelligencePresentationSnapshot | None
    proposal: BrainCommandProposal | None
    health: IntelligenceHealthSnapshot | None
    signal_baselines: IntelligenceSignalBaselineSnapshot | None = None
    schema: str = INTELLIGENCE_MATRIX_OUTPUT_SCHEMA
    owner: str = INTELLIGENCE_MATRIX_OWNER

    def __post_init__(self) -> None:
        _nonnegative_int(self.revision, "intelligence_output_revision_invalid")
        _finite_time(self.published_at, "intelligence_output_time_invalid")
        if self.presentation is not None and not isinstance(
            self.presentation,
            IntelligencePresentationSnapshot,
        ):
            raise TypeError("intelligence_output_presentation_invalid")
        if self.proposal is not None and not isinstance(
            self.proposal,
            BrainCommandProposal,
        ):
            raise TypeError("intelligence_output_proposal_invalid")
        if self.health is not None and not isinstance(
            self.health,
            IntelligenceHealthSnapshot,
        ):
            raise TypeError("intelligence_output_health_invalid")
        if self.signal_baselines is not None and not isinstance(
            self.signal_baselines,
            IntelligenceSignalBaselineSnapshot,
        ):
            raise TypeError("intelligence_output_signal_baselines_invalid")
        if (
            self.schema != INTELLIGENCE_MATRIX_OUTPUT_SCHEMA
            or self.owner != INTELLIGENCE_MATRIX_OWNER
        ):
            raise ValueError("intelligence_output_contract_invalid")


__all__ = (
    "INTELLIGENCE_HEALTH_SCHEMA",
    "INTELLIGENCE_MATRIX_INPUT_SCHEMA",
    "INTELLIGENCE_MATRIX_CONTRACT_VERSION",
    "INTELLIGENCE_MATRIX_OBSERVATION_SCHEMA",
    "INTELLIGENCE_OBSERVATION_CAUSES",
    "INTELLIGENCE_MATRIX_OUTPUT_SCHEMA",
    "INTELLIGENCE_MATRIX_OWNER",
    "INTELLIGENCE_PRESENTATION_SCHEMA",
    "INTELLIGENCE_SIGNAL_BASELINE_SCHEMA",
    "INTELLIGENCE_PROPOSAL_DISPATCH_SCHEMA",
    "INTELLIGENCE_CONSTRAINT_VIEW_OWNER",
    "INTELLIGENCE_CONSTRAINT_VIEW_SCHEMA",
    "INTELLIGENCE_CONTROL_AUTOPILOT",
    "INTELLIGENCE_CONTROL_MODES",
    "INTELLIGENCE_CONTROL_OBSERVE",
    "INTELLIGENCE_CONTROL_STANDALONE",
    "INTELLIGENCE_PRODUCT_CONTROL_SCHEMA",
    "INTELLIGENCE_PROVIDER_POLICY_SCHEMA",
    "INTELLIGENCE_CLIENT_ID",
    "SIGNAL_BASELINE_FORMULA_GENERATION",
    "IntelligenceHealthSnapshot",
    "IntelligenceMatrixInputSnapshot",
    "IntelligenceMatrixObservation",
    "IntelligenceMatrixOutputSnapshot",
    "IntelligenceOperationalConstraintView",
    "IntelligencePresentationSnapshot",
    "IntelligenceProposalDispatchResult",
    "IntelligenceSignalBaselineSnapshot",
    "intelligence_signal_scope_key",
)
