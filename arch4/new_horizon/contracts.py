"""Small immutable contracts used by New Horizon Intelligence."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade

from dataclasses import dataclass
import math

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFact
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.command_targets import CoreBandSetTarget, core_command_target_from_payload
from arch4.core_contracts.intelligence_proposals import (
    ActionTargetEvidence,
    BrainDecisionEligibility,
    BrainCommandProposal,
    DecisionGradeVector,
    GRADE_DIMENSIONS,
    LEARNING_GRADE_DIMENSIONS,
)


DECISION_LEDGER_KIND = "decision_contribution_v12"
DECISION_LEDGER_SCHEMA = "NewHorizonDecisionContribution.v12"
# Adapter for history written by the previous decision formula. These records
# are attribution/display evidence for explicit history migration. Current
# decision selection still accepts only DECISION_LEDGER_KIND above.
PRIOR_DECISION_LEDGER_CONTRACTS = (
    ("decision_contribution_v11", "NewHorizonDecisionContribution.v11"),
    ("decision_contribution_v10", "NewHorizonDecisionContribution.v10"),
    ("decision_contribution_v9", "NewHorizonDecisionContribution.v9"),
    ("decision_contribution_v8", "NewHorizonDecisionContribution.v8"),
    ("decision_contribution_v7", "NewHorizonDecisionContribution.v7"),
    ("decision_contribution_v6", "NewHorizonDecisionContribution.v6"),
)
DECISION_TERMINAL_KIND = "decision_terminal_v2"
DECISION_TERMINAL_SCHEMA = "NewHorizonDecisionTerminal.v2"
PROPOSAL_RESULT_HISTORY_KIND = "proposal_result"
PROPOSAL_RESULT_EVIDENCE_SCHEMA = "NewHorizonProposalResultEvidence.v2"
HOLD_ATTRIBUTION_KIND = "intelligence_hold_attribution_v1"
HOLD_ATTRIBUTION_SCHEMA = "NewHorizonHoldAttribution.v1"
BRAIN_DECISION_FRAME_SCHEMA = "NewHorizonBrainDecisionFrame.v3"
FAILED_PHYSICAL_TARGET_RETRY_S = 2.0 * 60.0 * 60.0


def _text(value: str, reason: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(reason)
    return value


def _time(value: float, reason: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(reason)
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(reason)
    return result


@dataclass(frozen=True, slots=True)
class IntelligenceSurface:
    name: str
    sequence: int
    valid: bool
    stale: bool
    value: CoreFrozenPayload

    def __post_init__(self) -> None:
        _text(self.name, "intelligence_surface_name_required")
        if type(self.sequence) is not int or self.sequence < 0:
            raise TypeError("intelligence_surface_sequence_invalid")
        if type(self.valid) is not bool or type(self.stale) is not bool:
            raise TypeError("intelligence_surface_state_invalid")
        if not isinstance(self.value, CoreFrozenPayload):
            raise TypeError("intelligence_surface_payload_required")


@dataclass(frozen=True, slots=True)
class IntelligenceContext:
    matrix_identity: MatrixGenerationIdentity
    root_fingerprint: str
    identity_code: str
    confirmed_continuity: CoreFrozenPayload
    sampled_at: float
    accepted_at: float
    facts: tuple[CoreAcceptedCurrentFact, ...]
    surfaces: tuple[IntelligenceSurface, ...]
    foundation_schedule: CoreFrozenPayload
    foundation_band_policy: CoreFrozenPayload
    product_control: CoreFrozenPayload
    command_context: CoreCommandIngressContext
    complete: bool
    missing_required: tuple[str, ...]
    sequence_gap: int

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("intelligence_context_matrix_identity_required")
        if type(self.root_fingerprint) is not str or len(self.root_fingerprint) != 64:
            raise TypeError("intelligence_context_root_fingerprint_invalid")
        _text(self.identity_code, "intelligence_context_identity_code_required")
        if not isinstance(self.confirmed_continuity, CoreFrozenPayload):
            raise TypeError("intelligence_context_confirmed_continuity_required")
        _time(self.sampled_at, "intelligence_context_sampled_at_invalid")
        _time(self.accepted_at, "intelligence_context_accepted_at_invalid")
        if self.accepted_at < self.sampled_at:
            raise ValueError("intelligence_context_timestamp_order_invalid")
        if any(not isinstance(item, CoreAcceptedCurrentFact) for item in self.facts):
            raise TypeError("intelligence_context_typed_facts_required")
        if any(
            item.source_sequence > self.matrix_identity.matrix_sequence
            for item in self.facts
        ):
            raise ValueError("intelligence_context_fact_generation_mismatch")
        if any(not isinstance(item, IntelligenceSurface) for item in self.surfaces):
            raise TypeError("intelligence_context_typed_surfaces_required")
        if not isinstance(self.foundation_schedule, CoreFrozenPayload):
            raise TypeError("intelligence_context_schedule_required")
        if not isinstance(self.foundation_band_policy, CoreFrozenPayload):
            raise TypeError("intelligence_context_band_policy_required")
        if not isinstance(self.product_control, CoreFrozenPayload):
            raise TypeError("intelligence_context_product_control_required")
        if not isinstance(self.command_context, CoreCommandIngressContext):
            raise TypeError("intelligence_context_command_context_required")
        if (
            self.command_context.matrix_identity != self.matrix_identity
            or self.command_context.identity_code != self.identity_code
        ):
            raise ValueError("intelligence_context_command_context_mismatch")
        if type(self.complete) is not bool:
            raise TypeError("intelligence_context_completeness_invalid")
        if self.complete is bool(self.missing_required):
            raise ValueError("intelligence_context_completeness_mismatch")
        if type(self.sequence_gap) is not int or self.sequence_gap < 0:
            raise TypeError("intelligence_context_sequence_gap_invalid")

    def fact(self, path: str) -> CoreAcceptedCurrentFact:
        for fact in self.facts:
            if fact.path == path:
                return fact
        raise KeyError(f"intelligence_context_fact_not_selected:{path}")

    def surface(self, name: str) -> IntelligenceSurface:
        for surface in self.surfaces:
            if surface.name == name:
                return surface
        raise KeyError(f"intelligence_context_surface_not_selected:{name}")


@dataclass(frozen=True, slots=True)
class AdvisoryOpportunity:
    opportunity_id: str
    kind: str
    target: str
    action_target: ActionTargetEvidence | None
    reason: str
    eligible: bool
    uncertainty: float
    disruption_cost: float
    evidence_refs: tuple[str, ...]
    strategy_service_grade: float | None = None
    historical_working_pct: float | None = None
    companion_support: tuple[tuple[int, float], ...] = ()

    def __post_init__(self) -> None:
        for name in ("opportunity_id", "kind", "target", "reason"):
            _text(getattr(self, name), f"advisory_opportunity_{name}_required")
        if self.kind not in {"policy", "advisory", "diagnostic"}:
            raise ValueError("advisory_opportunity_kind_invalid")
        if self.action_target is not None and not isinstance(
            self.action_target,
            ActionTargetEvidence,
        ):
            raise TypeError("advisory_opportunity_action_target_invalid")
        if self.kind == "advisory" and self.action_target is None:
            raise ValueError("advisory_opportunity_positive_target_required")
        if self.kind == "policy" and self.action_target is not None:
            raise ValueError("advisory_opportunity_policy_target_forbidden")
        if (
            self.action_target is not None
            and self.target != self.action_target.target_signature
        ):
            raise ValueError("advisory_opportunity_target_signature_mismatch")
        if type(self.eligible) is not bool:
            raise TypeError("advisory_opportunity_eligibility_invalid")
        if (
            type(self.uncertainty) not in {int, float}
            or not 0.0 <= float(self.uncertainty) <= 1.0
        ):
            raise ValueError("advisory_opportunity_uncertainty_invalid")
        if (
            type(self.disruption_cost) not in {int, float}
            or not 0.0 <= float(self.disruption_cost) <= 1.0
        ):
            raise ValueError("advisory_opportunity_disruption_cost_invalid")
        if self.strategy_service_grade is not None and (
            isinstance(self.strategy_service_grade, bool)
            or type(self.strategy_service_grade) not in {int, float}
            or not math.isfinite(float(self.strategy_service_grade))
            or not valid_intelligence_grade(self.strategy_service_grade,dimension="service_outcome")
            or self.action_target is None
        ):
            raise ValueError("advisory_opportunity_strategy_grade_invalid")
        if self.historical_working_pct is not None and (
            type(self.historical_working_pct) not in {int, float}
            or not math.isfinite(self.historical_working_pct)
            or not 0 <= self.historical_working_pct <= 100
            or not self.evidence_refs
        ):
            raise ValueError("advisory_opportunity_working_evidence_invalid")
        if self.companion_support:
            command = (core_command_target_from_payload(self.action_target.target.to_mapping())
                       if self.action_target is not None else None)
            if (not self.opportunity_id.startswith("best_effort_expand:")
                    or not isinstance(command, CoreBandSetTarget)
                    or type(self.companion_support) is not tuple
                    or any(type(row) is not tuple or len(row) != 2 for row in self.companion_support)
                    or any(type(band) is not int or band not in command.bands
                           or type(hours) not in {int, float} or not math.isfinite(hours) or hours <= 0
                           for band, hours in self.companion_support)
                    or len({band for band, _ in self.companion_support}) != len(self.companion_support)
                    or not any(ref.startswith("local-band-use:") for ref in self.evidence_refs)):
                raise ValueError("advisory_opportunity_companion_evidence_invalid")
        if any(
            type(item) is not str or not item or item != item.strip()
            for item in self.evidence_refs
        ) or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("advisory_opportunity_evidence_refs_invalid")
        if self.action_target is not None and len(self.evidence_refs) < 2:
            raise ValueError("advisory_opportunity_action_evidence_required")
        if self.action_target is None and self.evidence_refs:
            raise ValueError("advisory_opportunity_policy_evidence_forbidden")


@dataclass(frozen=True, slots=True)
class AdvisoryForecast:
    status: str
    match_tier: str
    expected_eci_points: float | None
    expected_latency_ms: float | None
    expected_jitter_ms: float | None
    expected_cqb: float | None
    expected_internet_availability_pct: float | None
    confidence: float
    uncertainty: float
    evidence_samples: int
    reason: str
    accuracy_factor: float | None = None
    accuracy_assessments: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"predicted", "insufficient_evidence"}:
            raise ValueError("advisory_forecast_status_invalid")
        if self.match_tier not in {
            "exact",
            "related_context",
            "unavailable",
        }:
            raise ValueError("advisory_forecast_match_tier_invalid")
        if (self.status == "predicted") is (self.match_tier == "unavailable"):
            raise ValueError("advisory_forecast_status_tier_mismatch")
        for name in (
            "expected_eci_points",
            "expected_latency_ms",
            "expected_jitter_ms",
            "expected_cqb",
            "expected_internet_availability_pct",
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(f"advisory_forecast_{name}_invalid")
        if self.expected_cqb is not None and not 0.0 <= float(self.expected_cqb) <= 1.0:
            raise ValueError("advisory_forecast_expected_cqb_invalid")
        if self.expected_internet_availability_pct is not None and not (
            0.0 <= float(self.expected_internet_availability_pct) <= 100.0
        ):
            raise ValueError(
                "advisory_forecast_expected_internet_availability_pct_invalid"
            )
        for name in ("confidence", "uncertainty"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"advisory_forecast_{name}_invalid")
        if type(self.evidence_samples) is not int or self.evidence_samples < 0:
            raise TypeError("advisory_forecast_sample_count_invalid")
        if self.accuracy_factor is not None and not (
            type(self.accuracy_factor) in {int, float}
            and math.isfinite(float(self.accuracy_factor))
            and 0.0 <= float(self.accuracy_factor) <= 1.0
        ):
            raise ValueError("advisory_forecast_accuracy_invalid")
        if type(self.accuracy_assessments) is not int or self.accuracy_assessments < 0:
            raise ValueError("advisory_forecast_accuracy_count_invalid")
        if (self.accuracy_factor is None) != (self.accuracy_assessments == 0):
            raise ValueError("advisory_forecast_accuracy_state_invalid")
        _text(self.reason, "advisory_forecast_reason_required")


@dataclass(frozen=True, slots=True)
class RefereeTarget:
    action_family: str
    target_signature: str
    target: CoreFrozenPayload

    def __post_init__(self) -> None:
        _text(self.action_family, "referee_target_family_required")
        _text(self.target_signature, "referee_target_signature_required")
        if not isinstance(self.target, CoreFrozenPayload):
            raise TypeError("referee_target_payload_required")
        core_command_target_from_payload(self.target.to_mapping())


@dataclass(frozen=True, slots=True)
class RefereeProof:
    proof_id: str
    assessment_id: str
    decision_id: str
    scope_key: str
    learning_context_key: str
    subject_kind: str
    origin: str
    target: RefereeTarget
    dimension: str
    status: str
    grade: float | None
    final_assessment: bool
    reasons: tuple[str, ...]
    opened_at: float
    due_at: float
    closed_at: float

    def __post_init__(self) -> None:
        for name in (
            "proof_id",
            "assessment_id",
            "decision_id",
            "scope_key",
            "learning_context_key",
            "subject_kind",
            "origin",
        ):
            _text(getattr(self, name), f"referee_proof_{name}_required")
        if self.subject_kind not in {
            "core_command",
            "external_radio_change",
            "active_context_window",
        }:
            raise ValueError("referee_proof_subject_kind_invalid")
        if not isinstance(self.target, RefereeTarget):
            raise TypeError("referee_proof_target_required")
        if self.dimension not in GRADE_DIMENSIONS:
            raise ValueError("referee_proof_dimension_invalid")
        if self.status not in {"pending", "invalid", "graded"}:
            raise ValueError("referee_proof_status_invalid")
        if type(self.final_assessment) is not bool:
            raise TypeError("referee_proof_final_assessment_invalid")
        if self.status == "pending" and self.final_assessment:
            raise ValueError("referee_proof_pending_final_forbidden")
        if self.status == "graded":
            if (
                isinstance(self.grade, bool)
                or type(self.grade) not in {int, float}
                or not math.isfinite(float(self.grade))
                or not valid_intelligence_grade(self.grade,dimension=self.dimension)
            ):
                raise ValueError("referee_proof_grade_invalid")
        elif self.grade is not None:
            raise ValueError("referee_proof_nonterminal_grade_invalid")
        if not self.reasons or any(
            type(item) is not str or not item or item != item.strip()
            for item in self.reasons
        ):
            raise ValueError("referee_proof_reasons_required")
        _time(self.opened_at, "referee_proof_opened_at_invalid")
        _time(self.due_at, "referee_proof_due_at_invalid")
        if self.due_at < self.opened_at:
            raise ValueError("referee_proof_due_order_invalid")
        if type(self.closed_at) not in {int, float}:
            raise TypeError("referee_proof_closed_at_invalid")
        if self.status == "pending" and float(self.closed_at) != 0.0:
            raise ValueError("referee_proof_pending_closed_at_invalid")
        if self.status != "pending":
            _time(self.closed_at, "referee_proof_closed_at_invalid")


@dataclass(frozen=True, slots=True)
class LearningPosterior:
    opportunity_id: str
    dimension: str
    pulls: int
    mean_grade: float
    variance: float
    negative_count: int

    def __post_init__(self) -> None:
        _text(
            self.opportunity_id,
            "learning_posterior_opportunity_required",
        )
        if self.dimension not in GRADE_DIMENSIONS:
            raise ValueError("learning_posterior_dimension_invalid")
        for name in ("pulls", "negative_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"learning_posterior_{name}_invalid")
        for name in ("mean_grade", "variance"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError(f"learning_posterior_{name}_invalid")
        if self.variance < 0.0 or self.negative_count > self.pulls:
            raise ValueError("learning_posterior_state_invalid")


@dataclass(frozen=True, slots=True)
class EvidenceReadiness:
    state: str
    proposal: str
    confidence: float
    evidence_samples: int

    def __post_init__(self) -> None:
        if self.state not in {"collecting", "ready", "insufficient_evidence"}:
            raise ValueError("evidence_readiness_state_invalid")
        _text(self.proposal, "evidence_readiness_proposal_required")
        if (
            type(self.confidence) not in {int, float}
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("evidence_readiness_confidence_invalid")
        if type(self.evidence_samples) is not int or self.evidence_samples < 0:
            raise TypeError("evidence_readiness_sample_count_invalid")


@dataclass(frozen=True, slots=True)
class AdvisoryDecision:
    selected_opportunity_id: str
    reason: str
    explanation: str
    predicted_grades: DecisionGradeVector
    eligibility: BrainDecisionEligibility
    hold_opportunities: tuple[CoreFrozenPayload, ...] = ()

    def __post_init__(self) -> None:
        _text(
            self.selected_opportunity_id,
            "advisory_decision_opportunity_required",
        )
        _text(self.reason, "advisory_decision_reason_required")
        _text(self.explanation, "advisory_decision_explanation_required")
        if not isinstance(self.predicted_grades, DecisionGradeVector):
            raise TypeError("advisory_decision_predicted_grades_required")
        if self.predicted_grades.basis != "prediction":
            raise ValueError("advisory_decision_prediction_basis_required")
        if not isinstance(self.eligibility, BrainDecisionEligibility):
            raise TypeError("advisory_decision_eligibility_required")
        if not isinstance(self.hold_opportunities, tuple) or any(
            not isinstance(item, CoreFrozenPayload) for item in self.hold_opportunities
        ):
            raise TypeError("advisory_decision_hold_opportunities_invalid")
        if self.hold_opportunities and self.selected_opportunity_id != "do_nothing":
            raise ValueError("advisory_decision_hold_opportunities_require_hold")


@dataclass(frozen=True, slots=True)
class AdvisoryPublication:
    context: IntelligenceContext
    opportunities: tuple[AdvisoryOpportunity, ...]
    forecast: AdvisoryForecast
    decision: AdvisoryDecision
    posteriors: tuple[LearningPosterior, ...]
    learned_proof_count: int
    evidence_readiness: EvidenceReadiness
    last_proof: RefereeProof | None
    proposal: BrainCommandProposal | None
    published_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.context, IntelligenceContext):
            raise TypeError("advisory_publication_context_required")
        if not self.opportunities or any(
            not isinstance(item, AdvisoryOpportunity) for item in self.opportunities
        ):
            raise TypeError("advisory_publication_opportunities_required")
        if sum(item.opportunity_id == "do_nothing" for item in self.opportunities) != 1:
            raise ValueError("advisory_publication_do_nothing_required")
        if not isinstance(self.forecast, AdvisoryForecast):
            raise TypeError("advisory_publication_forecast_required")
        if not isinstance(self.decision, AdvisoryDecision):
            raise TypeError("advisory_publication_decision_required")
        if len(self.posteriors) != len(LEARNING_GRADE_DIMENSIONS) or any(
            not isinstance(item, LearningPosterior) for item in self.posteriors
        ):
            raise TypeError("advisory_publication_posteriors_required")
        if (
            tuple(item.dimension for item in self.posteriors)
            != LEARNING_GRADE_DIMENSIONS
        ):
            raise ValueError("advisory_publication_posteriors_incomplete")
        if type(self.learned_proof_count) is not int or self.learned_proof_count < 0:
            raise TypeError("advisory_publication_learned_proof_count_invalid")
        if not isinstance(self.evidence_readiness, EvidenceReadiness):
            raise TypeError("advisory_publication_evidence_readiness_required")
        if self.last_proof is not None and not isinstance(
            self.last_proof,
            RefereeProof,
        ):
            raise TypeError("advisory_publication_proof_invalid")
        if self.proposal is not None and not isinstance(
            self.proposal,
            BrainCommandProposal,
        ):
            raise TypeError("advisory_publication_proposal_invalid")
        if self.decision.selected_opportunity_id not in {
            item.opportunity_id for item in self.opportunities
        }:
            raise ValueError("advisory_publication_selection_unknown")
        _time(self.published_at, "advisory_publication_timestamp_invalid")


@dataclass(frozen=True, slots=True)
class RefereeSubject:
    key: str
    evidence_sequence: int
    matrix_identity: MatrixGenerationIdentity
    baseline_through_sequence: int
    baseline_scope_key: str
    learning_context_key: str
    learning_context: CoreFrozenPayload | None
    subject_kind: str
    origin: str
    target: RefereeTarget
    terminal_status: str
    reason: str
    created_at: float
    terminal_at: float
    proof_ready: bool
    command_id: str = ""
    superseded_proof_id: str = ""

    def __post_init__(self) -> None:
        if type(self.command_id) is not str:
            raise TypeError("referee_subject_command_id_invalid")
        if (type(self.superseded_proof_id) is not str
                or (self.superseded_proof_id and self.subject_kind != "external_radio_change")):
            raise ValueError("referee_subject_superseded_proof_invalid")
        _text(self.key, "referee_subject_key_required")
        if type(self.evidence_sequence) is not int or self.evidence_sequence <= 0:
            raise TypeError("referee_subject_sequence_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("referee_subject_matrix_identity_required")
        if (
            type(self.baseline_through_sequence) is not int
            or self.baseline_through_sequence <= 0
            or self.baseline_through_sequence > self.matrix_identity.matrix_sequence
        ):
            raise ValueError("referee_subject_baseline_sequence_invalid")
        _text(
            self.baseline_scope_key,
            "referee_subject_baseline_scope_key_required",
        )
        _text(
            self.learning_context_key,
            "referee_subject_learning_context_key_required",
        )
        if self.subject_kind not in {"core_command", "external_radio_change"}:
            raise ValueError("referee_subject_kind_invalid")
        if self.learning_context is None:
            if self.learning_context_key != "unavailable":
                raise ValueError(
                    "referee_subject_learning_context_availability_mismatch"
                )
            if self.subject_kind == "external_radio_change":
                raise ValueError("referee_subject_external_learning_context_required")
        else:
            if not isinstance(self.learning_context, CoreFrozenPayload):
                raise TypeError("referee_subject_learning_context_required")
            from arch4.new_horizon.learning_context import (
                learning_context_key,
                require_learning_context,
            )

            context = require_learning_context(self.learning_context.to_mapping())
            if self.learning_context_key != learning_context_key(context):
                raise ValueError("referee_subject_learning_context_key_mismatch")
            if (
                self.baseline_scope_key != "unavailable"
                and context["exact_scope_key"] != self.baseline_scope_key
            ):
                raise ValueError("referee_subject_learning_context_scope_mismatch")
        _text(self.origin, "referee_subject_origin_required")
        if not isinstance(self.target, RefereeTarget):
            raise TypeError("referee_subject_target_required")
        _text(self.terminal_status, "referee_subject_status_required")
        if (
            self.subject_kind == "external_radio_change"
            and self.terminal_status != "observed"
        ):
            raise ValueError("referee_subject_external_status_invalid")
        if type(self.reason) is not str or self.reason != self.reason.strip():
            raise TypeError("referee_subject_reason_invalid")
        _time(self.created_at, "referee_subject_created_timestamp_invalid")
        _time(self.terminal_at, "referee_subject_timestamp_invalid")
        if self.terminal_at < self.created_at:
            raise ValueError("referee_subject_timestamp_order_invalid")
        if type(self.proof_ready) is not bool:
            raise TypeError("referee_subject_proof_ready_invalid")


__all__ = (
    "AdvisoryDecision",
    "AdvisoryForecast",
    "AdvisoryOpportunity",
    "AdvisoryPublication",
    "FAILED_PHYSICAL_TARGET_RETRY_S",
    "BRAIN_DECISION_FRAME_SCHEMA",
    "DECISION_LEDGER_KIND",
    "DECISION_LEDGER_SCHEMA",
    "PRIOR_DECISION_LEDGER_CONTRACTS",
    "DECISION_TERMINAL_KIND",
    "DECISION_TERMINAL_SCHEMA",
    "HOLD_ATTRIBUTION_KIND",
    "HOLD_ATTRIBUTION_SCHEMA",
    "EvidenceReadiness",
    "IntelligenceContext",
    "IntelligenceSurface",
    "LearningPosterior",
    "RefereeProof",
    "RefereeSubject",
    "RefereeTarget",
)
