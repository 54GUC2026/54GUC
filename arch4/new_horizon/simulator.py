"""Deterministic New Horizon loop simulator configured by a script."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from arch4.action_origin import ActionOriginKind
from arch4.controller_frames import RadioTransitionFrame
from arch4.core_contracts.outcomes import CoreCommandOutcomeRecord
from arch4.core_contracts.intelligence_boundary import (
    IntelligenceProposalDispatchResult,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.new_horizon.advisory import AdvisoryEngine
from arch4.new_horizon.contracts import (
    AdvisoryDecision,
    AdvisoryPublication,
    IntelligenceContext,
    RefereeProof,
    RefereeSubject,
    RefereeTarget,
)


@dataclass(frozen=True, slots=True)
class SimulationObservation:
    context: IntelligenceContext
    transition: RadioTransitionFrame

    def __post_init__(self) -> None:
        if not isinstance(self.context, IntelligenceContext):
            raise TypeError("new_horizon_simulation_context_required")
        if not isinstance(self.transition, RadioTransitionFrame):
            raise TypeError("new_horizon_simulation_transition_required")
        if self.transition.matrix_identity != self.context.matrix_identity:
            raise ValueError("new_horizon_simulation_transition_mismatch")


@dataclass(frozen=True, slots=True)
class SimulationScenario:
    name: str
    baseline: IntelligenceContext
    subject: RefereeSubject
    warmup: tuple[SimulationObservation, ...]
    outcome_adapter: Callable[[RefereeSubject, IntelligenceContext], CoreCommandOutcomeRecord]
    observations: tuple[SimulationObservation, ...]
    evaluation: IntelligenceContext
    proof_windows_s: tuple[float, ...] = (3.0,)
    physical_results: tuple[RefereeSubject, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError("new_horizon_simulation_name_required")
        if not isinstance(self.baseline, IntelligenceContext):
            raise TypeError("new_horizon_simulation_baseline_required")
        if not isinstance(self.subject, RefereeSubject):
            raise TypeError("new_horizon_simulation_subject_required")
        if not callable(self.outcome_adapter):
            raise TypeError("new_horizon_simulation_outcome_adapter_required")
        previous = None
        for observation in self.warmup:
            context = observation.context
            if (context.accepted_at >= self.baseline.accepted_at
                    or context.matrix_identity.namespace != self.baseline.matrix_identity.namespace
                    or context.matrix_identity.matrix_sequence >= self.baseline.matrix_identity.matrix_sequence):
                raise ValueError("new_horizon_simulation_warmup_order_invalid")
            if previous is not None and (context.accepted_at <= previous.accepted_at
                    or context.matrix_identity.matrix_sequence <= previous.matrix_identity.matrix_sequence):
                raise ValueError("new_horizon_simulation_warmup_order_invalid")
            previous = context
        if not self.observations:
            raise ValueError("new_horizon_simulation_observations_required")
        if not isinstance(self.evaluation, IntelligenceContext):
            raise TypeError("new_horizon_simulation_evaluation_required")
        if self.evaluation.accepted_at < self.observations[-1].context.accepted_at:
            raise ValueError("new_horizon_simulation_evaluation_precedes_evidence")
        if any(not isinstance(item, RefereeSubject) for item in self.physical_results):
            raise TypeError("new_horizon_simulation_physical_result_required")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    name: str
    before: AdvisoryDecision
    proofs: tuple[RefereeProof, ...]
    after: AdvisoryDecision
    before_eligible_opportunity_ids: tuple[str, ...]
    after_eligible_opportunity_ids: tuple[str, ...]
    learned_proof_count: int
    pending_proof_count: int


class NewHorizonSimulator:
    """Run the production Referee, learning, and Brain components."""

    def __init__(self, history_path: str | Path) -> None:
        self._history_path = Path(history_path)

    @staticmethod
    def _admit_observation(
        engine: AdvisoryEngine,
        observation: SimulationObservation,
    ) -> tuple[AdvisoryPublication, tuple[RefereeProof, ...]]:
        """Explicit accepted-facts boundary; orchestration belongs to Advisory."""
        context = observation.context
        publication, proofs = engine._admit_accepted_context(
            context, transition=observation.transition,
            observation_key="simulation:" + context.root_fingerprint,
            causes=("core_root",), observed_at=context.accepted_at,
            now=context.accepted_at,
        )
        if publication is None:
            raise ValueError("new_horizon_simulation_duplicate_observation")
        return publication, proofs

    def run(self, scenario: SimulationScenario) -> SimulationResult:
        engine: AdvisoryEngine | None = AdvisoryEngine(
            self._history_path,
            proof_windows_s=scenario.proof_windows_s,
        )
        try:
            baseline = scenario.baseline
            for observation in scenario.warmup:
                self._admit_observation(engine, observation)
            baseline_transition = replace(
                scenario.observations[0].transition,
                matrix_identity=baseline.matrix_identity,
                source_generation=baseline.matrix_identity.matrix_sequence,
            )
            before, _ = self._admit_observation(engine, SimulationObservation(baseline, baseline_transition))
            proposal = before.proposal
            if proposal is not None:
                reservation = engine.reserve_proposal_emission(proposal)
                if not reservation.reserved:
                    raise RuntimeError("new_horizon_simulation_decision_reservation_required")
                if not engine.admit_proposal_emission(proposal):
                    raise RuntimeError("new_horizon_simulation_proposal_emission_required")
                engine.admit_proposal_result(
                    IntelligenceProposalDispatchResult(
                        sequence=1,
                        proposal_id=proposal.proposal_id,
                        proposal=proposal,
                        submitted=True,
                        reason="simulation_submitted",
                        result=CoreFrozenPayload.from_mapping(
                            {"submitted": True, "reason": "simulation_submitted"}
                        ),
                        published_at=baseline.accepted_at,
                    )
                )
                subject = replace(
                    scenario.subject, key="core-command:" + proposal.proposal_id,
                    origin=ActionOriginKind.SMART_LOOP.value,
                    target=RefereeTarget(action_family=proposal.action_target.action_family,
                        target_signature=proposal.action_target.target_signature,
                        target=proposal.action_target.target),
                )
                engine.admit_outcome(scenario.outcome_adapter(subject, baseline))
            proofs: list[RefereeProof] = []
            for observation in scenario.observations:
                _, emitted = self._admit_observation(engine, observation)
                proofs.extend(emitted)
            for subject in scenario.physical_results:
                engine.admit_outcome(scenario.outcome_adapter(subject, baseline))
            referee_status = engine.status()["proof"]
            # Recreate the whole Advisory owner before the final check. This
            # proves lifecycle and grade evidence survive a restart together.
            engine.close()
            engine = AdvisoryEngine(
                self._history_path,
                proof_windows_s=scenario.proof_windows_s,
            )
            evaluation = scenario.evaluation
            transition = replace(baseline_transition, matrix_identity=evaluation.matrix_identity,
                                 source_generation=evaluation.matrix_identity.matrix_sequence)
            after, _ = self._admit_observation(engine, SimulationObservation(evaluation, transition))
            return SimulationResult(
                name=scenario.name,
                before=before.decision,
                proofs=tuple(proofs),
                after=after.decision,
                before_eligible_opportunity_ids=tuple(
                    item.opportunity_id
                    for item in before.opportunities
                    if item.eligible
                ),
                after_eligible_opportunity_ids=tuple(
                    item.opportunity_id for item in after.opportunities if item.eligible
                ),
                learned_proof_count=after.learned_proof_count,
                pending_proof_count=int(referee_status["pending_count"]),
            )
        finally:
            if engine is not None:
                engine.close()


__all__ = (
    "NewHorizonSimulator",
    "SimulationObservation",
    "SimulationResult",
    "SimulationScenario",
)
