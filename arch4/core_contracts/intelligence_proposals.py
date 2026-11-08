"""Immutable proposal contracts shared across the CGF-Intelligence boundary."""

from __future__ import annotations

from dataclasses import dataclass
import math

from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CorePBandTarget,
    CoreRadioModeTarget,
    core_radio_mode_signature,
    core_command_target_from_payload,
    core_lte_band_signature,
    core_lte_pband_signature,
)
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.payload import CoreFrozenPayload


LEARNING_GRADE_DIMENSIONS = (
    "service_outcome",
)

GRADE_DIMENSIONS = (
    *LEARNING_GRADE_DIMENSIONS,
    "rules_safety",
)


def intelligence_grade_max(dimension: str) -> float:
    """Only the total service grade has the additional active-NR class."""
    if dimension == 'service_change':
        return 3.5
    return 4.0 if dimension in {'service_outcome','performance_overall_outcome'} else 3.0


def valid_intelligence_grade(value: object, *, dimension: str) -> bool:
    return (type(value) in {int,float} and math.isfinite(float(value))
            and (-3.5 if dimension == 'service_change' else -3.0) <= float(value) <= intelligence_grade_max(dimension))


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
class ActionTargetEvidence:
    action_family: str
    command_kind: str
    target_signature: str
    target: CoreFrozenPayload

    def __post_init__(self) -> None:
        _text(self.action_family, "action_target_evidence_family_required")
        command = _text(
            self.command_kind,
            "action_target_evidence_command_required",
        )
        if command != command.upper():
            raise ValueError("action_target_evidence_command_not_canonical")
        _text(
            self.target_signature,
            "action_target_evidence_signature_required",
        )
        if not isinstance(self.target, CoreFrozenPayload):
            raise TypeError("action_target_evidence_payload_required")


@dataclass(frozen=True, slots=True)
class DecisionGradeComponent:
    dimension: str
    grade: float | None
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if self.dimension not in GRADE_DIMENSIONS:
            raise ValueError("decision_grade_dimension_invalid")
        if self.grade is not None:
            if (
                isinstance(self.grade, bool)
                or type(self.grade) not in {int, float}
                or not math.isfinite(float(self.grade))
                or not valid_intelligence_grade(self.grade,dimension=self.dimension)
            ):
                raise ValueError("decision_grade_value_invalid")
        if (
            type(self.confidence) not in {int, float}
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("decision_grade_confidence_invalid")
        _text(self.reason, "decision_grade_reason_required")

    def to_payload(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "grade": self.grade,
            "confidence": float(self.confidence),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DecisionGradeVector:
    components: tuple[DecisionGradeComponent, ...]
    basis: str = "prediction"

    def __post_init__(self) -> None:
        if self.basis not in {"prediction", "observation"}:
            raise ValueError("decision_grade_basis_invalid")
        if any(
            not isinstance(item, DecisionGradeComponent)
            for item in self.components
        ):
            raise TypeError("decision_grade_vector_components_required")
        if (
            len(self.components) != len(GRADE_DIMENSIONS)
            or tuple(item.dimension for item in self.components)
            != GRADE_DIMENSIONS
        ):
            raise ValueError("decision_grade_vector_dimensions_invalid")

    def component(self, dimension: str) -> DecisionGradeComponent:
        if dimension not in GRADE_DIMENSIONS:
            raise ValueError("decision_grade_dimension_invalid")
        return self.components[GRADE_DIMENSIONS.index(dimension)]

    def to_payload(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "components": [item.to_payload() for item in self.components],
        }


@dataclass(frozen=True, slots=True)
class BrainDecisionEligibility:
    """Brain-owned permission to select one candidate; never a learned grade."""

    eligible: bool
    rules_clear: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool or type(self.rules_clear) is not bool:
            raise TypeError("brain_decision_eligibility_boolean_required")
        _text(self.reason, "brain_decision_eligibility_reason_required")
        if self.eligible and not self.rules_clear:
            raise ValueError("brain_decision_eligibility_rules_contradiction")

    def to_payload(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "rules_clear": self.rules_clear,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BrainCommandProposal:
    proposal_id: str
    command_context: CoreCommandIngressContext
    profile_id: str
    opportunity_id: str
    execution_intent: str
    action_target: ActionTargetEvidence
    confidence: float
    evidence_refs: tuple[str, ...]
    predicted_grades: DecisionGradeVector
    eligibility: BrainDecisionEligibility
    verification_requirement: str
    rollback_requirement: str
    capability_reference: str
    created_at: float
    expires_at: float
    schema: str = "BrainCommandProposal.v4"
    owner: str = "54guc.new_horizon.brain"

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "profile_id",
            "opportunity_id",
            "execution_intent",
            "verification_requirement",
            "rollback_requirement",
            "capability_reference",
        ):
            _text(getattr(self, name), f"brain_proposal_{name}_required")
        if not isinstance(self.command_context, CoreCommandIngressContext):
            raise TypeError("brain_proposal_command_context_required")
        if not isinstance(self.action_target, ActionTargetEvidence):
            raise TypeError("brain_proposal_action_target_required")
        target = core_command_target_from_payload(
            self.action_target.target.to_mapping()
        )
        if isinstance(target, CoreBandSetTarget):
            expected_family = "lte_ca_pool"
            expected_kind = "BAND_SELECTION"
            signature = core_lte_band_signature(target.bands)
        elif isinstance(target, CorePBandTarget) and target.pband > 0:
            expected_family = "lte_radio_selection"
            expected_kind = "PBAND"
            signature = core_lte_pband_signature(
                target.pband,
                target.ca_pool,
            )
        elif isinstance(target, CoreRadioModeTarget):
            expected_family, expected_kind = "radio_mode", "RADIO_MODE"
            signature = core_radio_mode_signature(target)
            expected_intent = "nr_service_lte_fallback" if target.mode == "lte_only" else "nr_service_trial_restore"
            if self.execution_intent != expected_intent:
                raise ValueError("brain_radio_mode_intent_mismatch")
            if not isinstance(self.predicted_grades, DecisionGradeVector):
                raise TypeError("brain_proposal_predicted_grades_required")
            if self.predicted_grades.component("service_outcome").grade is not None:
                raise ValueError("brain_radio_mode_learned_gain_forbidden")
        else:
            raise ValueError("brain_proposal_typed_radio_target_required")
        if (
            self.action_target.action_family != expected_family
            or self.action_target.command_kind != expected_kind
        ):
            raise ValueError("brain_proposal_radio_target_family_mismatch")
        if signature != self.action_target.target_signature:
            raise ValueError("brain_proposal_radio_signature_mismatch")
        for name in ("confidence",):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError(f"brain_proposal_{name}_invalid")
        if not 0.0 < float(self.confidence) <= 1.0:
            raise ValueError("brain_proposal_confidence_invalid")
        if len(self.evidence_refs) < 2 or any(
            type(value) is not str or not value or value != value.strip()
            for value in self.evidence_refs
        ):
            raise ValueError("brain_proposal_evidence_required")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("brain_proposal_evidence_duplicate")
        if not isinstance(self.predicted_grades, DecisionGradeVector):
            raise TypeError("brain_proposal_predicted_grades_required")
        if self.predicted_grades.basis != "prediction":
            raise ValueError("brain_proposal_prediction_basis_required")
        if not isinstance(self.eligibility, BrainDecisionEligibility):
            raise TypeError("brain_proposal_eligibility_required")
        if not self.eligibility.eligible or not self.eligibility.rules_clear:
            raise ValueError("brain_proposal_ineligible")
        created = _time(self.created_at, "brain_proposal_created_at_invalid")
        expires = _time(self.expires_at, "brain_proposal_expires_at_invalid")
        if expires <= created:
            raise ValueError("brain_proposal_expiry_order_invalid")
        if expires - created > 10.0:
            raise ValueError("brain_proposal_expiry_too_long")
        if self.schema != "BrainCommandProposal.v4":
            raise ValueError("brain_proposal_schema_invalid")
        if self.owner != "54guc.new_horizon.brain":
            raise ValueError("brain_proposal_owner_invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "proposal_id": self.proposal_id,
            "command_context": self.command_context.to_payload(),
            "profile_id": self.profile_id,
            "opportunity_id": self.opportunity_id,
            "execution_intent": self.execution_intent,
            "action_target": {
                "action_family": self.action_target.action_family,
                "command_kind": self.action_target.command_kind,
                "target_signature": self.action_target.target_signature,
                "target": self.action_target.target.to_mapping(),
            },
            "confidence": float(self.confidence),
            "evidence_refs": list(self.evidence_refs),
            "predicted_grades": self.predicted_grades.to_payload(),
            "eligibility": self.eligibility.to_payload(),
            "verification_requirement": self.verification_requirement,
            "rollback_requirement": self.rollback_requirement,
            "capability_reference": self.capability_reference,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


__all__ = (
    "ActionTargetEvidence",
    "BrainDecisionEligibility",
    "BrainCommandProposal",
    "DecisionGradeComponent",
    "DecisionGradeVector",
    "GRADE_DIMENSIONS",
    "LEARNING_GRADE_DIMENSIONS",
)
