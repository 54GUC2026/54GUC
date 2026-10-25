"""Track learned statistics and let Brain choose the final action."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade, intelligence_grade_max

from dataclasses import dataclass, replace
import math
from time import localtime
from typing import Any, Iterable, Mapping

from arch4.action_origin import is_brain_decision_origin
from arch4.new_horizon.hourly_aq import aq_population_statistics
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.intelligence_proposals import (
    BrainCommandProposal,
    BrainDecisionEligibility,
    DecisionGradeComponent,
    DecisionGradeVector,
    LEARNING_GRADE_DIMENSIONS,
)
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CONTROL_AUTOPILOT,
)
from arch4.core_contracts.command_targets import (
    CorePBandTarget,
    core_command_target_from_payload,
    core_lte_pband_signature,
)
from arch4.new_horizon.contracts import (
    AdvisoryDecision,
    AdvisoryOpportunity,
    BRAIN_DECISION_FRAME_SCHEMA,
    DECISION_LEDGER_SCHEMA,
    IntelligenceContext,
    LearningPosterior,
    RefereeProof,
    RefereeTarget,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.evidence_weighting import EvidenceWeights
from arch4.new_horizon.decision_history import (
    DecisionLifecycleHistoryOwner,
    DecisionLifecycleHistorySnapshot,
)
from arch4.new_horizon.learning import (
    CandidateForecast,
    CrystalBall,
)
from arch4.new_horizon.proof import RefereeAssessmentHistoryOwner, RefereeAssessmentHistorySnapshot, context_continuity_key, context_learning_components, context_learning_key, context_radio_identity, context_scope_key, current_context_target, referee_sample_metrics, referee_service_vector
from arch4.new_horizon.service_quality import normalized_referee_service_value


def proposal_source_state(context: IntelligenceContext) -> tuple[object, ...]:
    """One complete, canonical source identity for a proposal's radio visit."""

    identity = context_radio_identity(context)
    if not identity:
        return ()
    return (
        identity.get("pband"),
        tuple(identity.get("ca_pool") or ()),
        identity.get("cell_id"),
        identity.get("earfcn"),
        identity.get("pci"),
        identity.get("nr_band"),
        identity.get("nr_cell_id"),
        identity.get("nr_arfcn"),
        identity.get("nr_pci"),
    )


def _fact(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    return fact.value.require("value") if fact.disposition == "available" else None


def _adviser_contribution(
    *,
    adviser: str,
    dimension: str,
    predicted_grade: float,
    confidence: float,
    evidence_refs: Iterable[str],
    used: bool,
) -> dict[str, Any]:
    return {
        "adviser": adviser,
        "dimension": dimension,
        "stance": (
            "support"
            if predicted_grade > 0
            else "oppose"
            if predicted_grade < 0
            else "abstain"
        ),
        "used": bool(used),
        "predicted_grade": float(predicted_grade),
        "confidence": min(1.0, max(0.0, float(confidence))),
        "evidence_refs": list(dict.fromkeys(str(item) for item in evidence_refs)),
    }


@dataclass(frozen=True, slots=True)
class BanditRank:
    """One target's absolute quality on Brain's -1..1 scale."""

    opportunity_id: str
    arm_id: str
    evidence_state: str
    service_value: float | None
    uncertainty: float
    downside: float
    information_value: float
    posterior: LearningPosterior
    service_evidence_refs: tuple[str, ...] = ()
    diagnostic_state: str = "no_matching_exact_context"
    effective_evidence: float = 0.0
    strategy_service_value: float | None = None
    known_target_evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecentSituation:
    context_count: int
    context_tenure_s: float
    severe_congestion_duration_s: float
    internet_outage_duration_s: float
    local_hour: int
    day_class: str
    load_class: str


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    opportunity: AdvisoryOpportunity
    rank: BanditRank
    forecast: CandidateForecast


@dataclass(frozen=True, slots=True)
class BrainDecisionFrame:
    context: IntelligenceContext
    current_service: CoreFrozenPayload
    recent: RecentSituation
    candidates: tuple[DecisionCandidate, ...]
    radio_stability_hold_remaining_s: float
    required_opportunity_id: str
    context_final_mature: bool

    def candidate(self, opportunity_id: str) -> DecisionCandidate:
        return next(
            item
            for item in self.candidates
            if item.opportunity.opportunity_id == opportunity_id
        )


def _surface_number(
    context: IntelligenceContext,
    surface_name: str,
    field: str,
) -> float | None:
    surface = context.surface(surface_name)
    value = surface.value.get(field)
    if (
        not surface.valid
        or surface.stale
        or isinstance(value, bool)
        or type(value) not in {int, float}
    ):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def internet_failed(context: IntelligenceContext) -> bool:
    """Return Core's statistically confirmed Internet-emergency truth."""

    reachability = context.surface("emergency_reachability")
    return bool(
        reachability.valid
        and not reachability.stale
        and reachability.value.get("confirmed") is True
    )


def emergency_pool_rescue_allowed(context: IntelligenceContext) -> bool:
    """Consume Core's one rescue slot; Intelligence owns no emergency timer."""
    mutation = context.surface("mutation")
    if not internet_failed(context) or not mutation.valid or mutation.stale:
        return False
    sources = mutation.value.get("sources")
    policy = sources.get("band_action_policy") if isinstance(sources, Mapping) else None
    incident = context.surface("emergency_reachability").value.get("incident_id")
    return bool(isinstance(policy, Mapping)
                and incident and policy.get("emergency_band_rescue_incident_id") == incident
                and policy.get("emergency_band_rescue_active") is True
                and float(policy.get("emergency_band_rescue_deadline_at") or 0.) > context.accepted_at)


def _consecutive_duration(
    contexts: tuple[IntelligenceContext, ...],
    predicate: Any,
) -> float:
    if not contexts or not predicate(contexts[-1]):
        return 0.0
    newest = contexts[-1].accepted_at
    oldest = newest
    for item in reversed(contexts):
        if not predicate(item):
            break
        oldest = item.accepted_at
    return max(0.0, newest - oldest)


def build_brain_decision_frame(
    context: IntelligenceContext,
    contexts: Iterable[IntelligenceContext],
    opportunities: Iterable[AdvisoryOpportunity],
    ranks: Iterable[BanditRank],
    forecasts: Mapping[str, CandidateForecast],
    *,
    radio_stability_hold_remaining_s: float,
    required_opportunity_id: str,
    context_final_mature: bool,
) -> BrainDecisionFrame:
    recent_contexts = tuple(
        item
        for item in contexts
        if item.matrix_identity.namespace == context.matrix_identity.namespace
    )[-32:]
    if not recent_contexts or recent_contexts[-1] != context:
        raise RuntimeError("new_horizon_brain_current_stm_context_required")
    current_scope = context_scope_key(context)
    current_visit = []
    for item in reversed(recent_contexts):
        if context_scope_key(item) != current_scope:
            break
        current_visit.append(item)
    same_scope = tuple(reversed(current_visit))
    when = localtime(context.accepted_at)
    traffic = tuple(_fact(item, "traffic.has_traffic") for item in same_scope)
    rank_by_id = {item.opportunity_id: item for item in ranks}
    candidates = tuple(
        DecisionCandidate(
            opportunity=opportunity,
            rank=rank_by_id[opportunity.opportunity_id],
            forecast=forecasts[opportunity.opportunity_id],
        )
        for opportunity in opportunities
        if opportunity.eligible and opportunity.opportunity_id in rank_by_id
    )
    if sum(item.opportunity.opportunity_id == "do_nothing" for item in candidates) != 1:
        raise RuntimeError("new_horizon_brain_do_nothing_candidate_required")
    return BrainDecisionFrame(
        context=context,
        current_service=CoreFrozenPayload.from_mapping(
            referee_service_vector(
                {
                    **referee_sample_metrics(item),
                    "accepted_at": item.accepted_at,
                }
                for item in same_scope
            )
        ),
        recent=RecentSituation(
            context_count=len(recent_contexts),
            context_tenure_s=(
                max(0.0, context.accepted_at - same_scope[0].accepted_at)
                if same_scope
                else 0.0
            ),
            severe_congestion_duration_s=_consecutive_duration(
                recent_contexts,
                lambda item: (
                    (_surface_number(item, "quality", "congestion_pct") or 0.0) >= 90.0
                ),
            ),
            internet_outage_duration_s=_consecutive_duration(
                recent_contexts, internet_failed
            ),
            local_hour=int(when.tm_hour),
            day_class="weekend" if when.tm_wday >= 5 else "weekday",
            load_class=(
                "loaded"
                if any(value is True for value in traffic)
                else "idle"
                if all(value is False for value in traffic)
                else "unknown"
            ),
        ),
        candidates=candidates,
        radio_stability_hold_remaining_s=float(radio_stability_hold_remaining_s),
        required_opportunity_id=required_opportunity_id,
        context_final_mature=context_final_mature,
    )


@dataclass(frozen=True, slots=True)
class ProvenArmEvidence:
    arm_id: str
    target: RefereeTarget
    posterior: LearningPosterior
    positive_proof_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LearnedRadioDestination:
    """One qualified destination. Discovery details are not a grade."""

    target: RefereeTarget
    assessment_count: int
    proof_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BanditTargetStatistics:
    """Exact-target knowledge safe to show in the GUI."""

    arm_id: str
    proof_count: int
    mean_service_grade: float | None
    uncertainty: float
    negative_ratio: float | None


class ContextualBandit:
    """Track exact-context statistics without selecting an action."""

    def __init__(
        self,
        history: AdvisoryHistory,
        *,
        assessment_history: RefereeAssessmentHistoryOwner,
    ) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_bandit_history_required")
        self._history = history
        if not isinstance(assessment_history, RefereeAssessmentHistoryOwner):
            raise TypeError("new_horizon_bandit_referee_history_owner_required")
        if assessment_history.history is not history:
            raise ValueError("new_horizon_bandit_referee_history_store_mismatch")
        self._assessment_history = assessment_history
        self._scope_key = ""
        self._posteriors: dict[tuple[str, str], LearningPosterior] = {}
        self._targets: dict[str, RefereeTarget] = {}
        self._positive_proof_refs: dict[str, tuple[str, ...]] = {}
        self._proof_refs: dict[tuple[str, str], tuple[str, ...]] = {}
        self._admitted_assessment_ids: set[str] = set()
        self._assessment_snapshot: RefereeAssessmentHistorySnapshot | None = None
        self._source_population_id = ""
        self._service_rows: dict[str, list[tuple[dict[str, Any], float]]] = {}
        self._evaluation_at = 0.0

    @staticmethod
    def arm_id_for(
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
    ) -> str:
        target = opportunity.action_target
        target_key = (
            target.action_family + ":" + target.target_signature
            if target is not None
            else "policy:" + opportunity.opportunity_id
        )
        return context_learning_key(context) + "|" + target_key

    def posterior_for(
        self,
        arm_id: str,
        dimension: str = "service_outcome",
    ) -> LearningPosterior:
        if dimension not in LEARNING_GRADE_DIMENSIONS:
            raise ValueError("new_horizon_posterior_dimension_invalid")
        key = (arm_id, dimension)
        current = self._posteriors.get(key)
        if current is None:
            current = LearningPosterior(
                opportunity_id=arm_id,
                dimension=dimension,
                pulls=0,
                mean_grade=0.0,
                variance=0.0,
                negative_count=0,
            )
            self._posteriors[key] = current
        return current

    def posteriors_for(self, arm_id: str) -> tuple[LearningPosterior, ...]:
        return tuple(
            self.posterior_for(arm_id, dimension)
            for dimension in LEARNING_GRADE_DIMENSIONS
        )

    @property
    def learned_proof_count(self) -> int:
        """Return the deduplicated count of final assessments."""

        return len(self._admitted_assessment_ids)

    @property
    def source_population_id(self) -> str:
        """Return the canonical Referee population used for current learning."""

        return self._source_population_id

    def _binding_state(self) -> tuple[Any, ...]:
        return (
            self._scope_key,
            self._posteriors,
            self._targets,
            self._positive_proof_refs,
            self._proof_refs,
            self._admitted_assessment_ids,
            self._assessment_snapshot,
            self._source_population_id,
            self._service_rows,
            self._evaluation_at,
        )

    def _restore_binding_state(self, state: tuple[Any, ...]) -> None:
        (
            self._scope_key,
            self._posteriors,
            self._targets,
            self._positive_proof_refs,
            self._proof_refs,
            self._admitted_assessment_ids,
            self._assessment_snapshot,
            self._source_population_id,
            self._service_rows,
            self._evaluation_at,
        ) = state

    def bind_context(
        self,
        context: IntelligenceContext,
        assessment_history: RefereeAssessmentHistorySnapshot,
    ) -> str:
        scope_key = context_continuity_key(context)
        if (
            not isinstance(assessment_history, RefereeAssessmentHistorySnapshot)
            or assessment_history.continuity_key != scope_key
        ):
            raise ValueError("new_horizon_bandit_referee_history_scope_mismatch")
        if (
            scope_key == self._scope_key
            and assessment_history is self._assessment_snapshot
            and int(context.accepted_at // 60) == int(self._evaluation_at // 60)
            and assessment_history.evidence_time_equivalent(self._evaluation_at, context.accepted_at)
        ):
            return scope_key
        previous = self._binding_state()
        try:
            self._posteriors = {}
            self._targets = {}
            self._positive_proof_refs = {}
            self._proof_refs = {}
            self._admitted_assessment_ids = set()
            self._service_rows = {}
            self._evaluation_at = context.accepted_at
            for row in assessment_history.aq_advice(now=self._evaluation_at):
                arm_id = row["learning_context_key"] + "|" + row["action_family"] + ":" + row["target_signature"]
                self._targets[arm_id] = RefereeTarget(
                    action_family=row["action_family"], target_signature=row["target_signature"],
                    target=CoreFrozenPayload.from_mapping(row["target"]),
                )
                self._service_rows.setdefault(arm_id, []).append((row, row["aq"]))
                ref = row["assessment_id"]
                self._proof_refs[(arm_id, "service_outcome")] = (*self._proof_refs.get((arm_id, "service_outcome"), ()), ref)
                if row["aq"] > 0:
                    self._positive_proof_refs[arm_id] = (*self._positive_proof_refs.get(arm_id, ()), ref)
                self._admitted_assessment_ids.add(ref)
            for arm_id, entries in self._service_rows.items():
                self._posteriors[(arm_id, "service_outcome")] = LearningPosterior(
                    opportunity_id=arm_id,
                    dimension="service_outcome",
                    **aq_population_statistics((row for row,_ in entries), now=self._evaluation_at),
                )
            self._scope_key = scope_key
            self._assessment_snapshot = assessment_history
            self._source_population_id = assessment_history.population_id
        except Exception:
            self._restore_binding_state(previous)
            raise
        return scope_key

    def admit_proof(self, _proof: RefereeProof) -> bool:
        raise RuntimeError("new_horizon_bandit_assessment_admission_required")

    @staticmethod
    def _aq_weights(entries: Iterable[tuple[dict[str, Any], float]]) -> EvidenceWeights:
        masses = tuple(row["effective_blocks"] for row, _ in entries)
        mass = math.fsum(masses)
        return EvidenceWeights(tuple(value / mass for value in masses) if mass else (), mass)

    def _candidate_evidence(
        self, context: IntelligenceContext, opportunity: AdvisoryOpportunity,
    ) -> tuple[LearningPosterior, tuple[dict[str, Any], ...], str]:
        if context_continuity_key(context) != self._scope_key:
            raise RuntimeError("new_horizon_bandit_context_not_bound")
        target = opportunity.action_target or current_context_target(context)
        rows, tier, _ = RefereeAssessmentHistorySnapshot.matching_aq_advice(
            (row for entries in self._service_rows.values() for row, _ in entries),
            target=None if target is None else core_command_target_from_payload(target.target.to_mapping()),
            current_learning=context_learning_components(context),
            current_identity=context_radio_identity(context),
            purpose="current_connection" if opportunity.action_target is None else "band_request",
            now=context.accepted_at,
        )
        return LearningPosterior(
            opportunity_id=self.arm_id_for(context, opportunity), dimension="service_outcome",
            **aq_population_statistics(rows, now=context.accepted_at),
        ), tuple(rows), tier

    def rank(
        self,
        context: IntelligenceContext,
        opportunities: Iterable[AdvisoryOpportunity],
        *,
        adviser_credit: Mapping[str, Any],
    ) -> tuple[BanditRank, ...]:
        ranks: list[BanditRank] = []
        for opportunity in opportunities:
            if not opportunity.eligible:
                continue
            arm_id = self.arm_id_for(context, opportunity)
            posterior, service_rows, match_tier = self._candidate_evidence(context, opportunity)
            target = opportunity.action_target or current_context_target(context)
            known_target_rows = RefereeAssessmentHistorySnapshot.target_aq_advice(
                (row for entries in self._service_rows.values() for row, _ in entries),
                target=None if target is None else core_command_target_from_payload(target.target.to_mapping()),
                now=context.accepted_at,
                current_identity=context_radio_identity(context),
            )
            weights = self._aq_weights((row, row["aq"]) for row in service_rows)
            effective = weights.effective_count
            learned_grade = float(posterior.mean_grade) if posterior.pulls else None
            strategy_grade = (
                float(opportunity.strategy_service_grade)
                if opportunity.strategy_service_grade is not None
                else None
            )
            adviser_grade = strategy_grade
            if learned_grade is not None and adviser_grade is not None:
                # Staged strategy uses measured history, never a fixed band score.
                combined_grade = (learned_grade * effective + adviser_grade) / (
                    effective + 1.0
                )
            elif learned_grade is not None:
                combined_grade = learned_grade
            else:
                combined_grade = adviser_grade
            service_value = combined_grade / 3.0 if combined_grade is not None else None
            evidence_uncertainty = min(
                1.0,
                math.sqrt(posterior.variance + 1.0) / math.sqrt(effective + 1.0) / 3.0,
            )
            information_value = (
                0.0
                if opportunity.action_target is None
                else min(
                    1.0,
                    max(opportunity.uncertainty, evidence_uncertainty)
                    / math.sqrt(effective + 1.0),
                )
            )
            observed_downside = (
                posterior.negative_count / posterior.pulls if posterior.pulls else 0.0
            )
            downside = min(
                1.0,
                observed_downside + float(opportunity.disruption_cost),
            )
            ranks.append(
                BanditRank(
                    opportunity_id=opportunity.opportunity_id,
                    arm_id=arm_id,
                    evidence_state=(
                        "learned_with_strategy"
                        if posterior.pulls and adviser_grade is not None
                        else "learned"
                        if posterior.pulls
                        else "strategy_adviser"
                        if strategy_grade is not None
                        else "unknown"
                    ),
                    service_value=service_value,
                    uncertainty=evidence_uncertainty,
                    downside=downside,
                    information_value=information_value,
                    posterior=posterior,
                    service_evidence_refs=tuple(row["assessment_id"] for row in service_rows),
                    diagnostic_state=(
                        match_tier + "_context_learned"
                        if posterior.pulls
                        else "no_matching_exact_context"
                    ),
                    effective_evidence=effective,
                    strategy_service_value=None
                    if adviser_grade is None
                    else adviser_grade / 3.0,
                    known_target_evidence_refs=tuple(row["assessment_id"] for row in known_target_rows),
                )
            )
        return tuple(ranks)

    def learned_radio_destinations(
        self,
    ) -> tuple[LearnedRadioDestination, ...]:
        """Return each qualified LTE target found in this continuity.

        This list discovers destinations only. Grades for each source context
        remain separate and are not merged here.
        """

        targets: dict[str, RefereeTarget] = {}
        counts: dict[str, int] = {}
        refs: dict[str, list[str]] = {}
        for arm_id, target in self._targets.items():
            if target.action_family != "lte_radio_selection":
                continue
            posterior = self._posteriors.get((arm_id, "service_outcome"))
            if posterior is None or posterior.pulls <= 0:
                continue
            command = core_command_target_from_payload(target.target.to_mapping())
            if not isinstance(command, CorePBandTarget) or not command.ca_pool:
                raise RuntimeError("new_horizon_learned_full_lte_target_invalid")
            signature = core_lte_pband_signature(
                command.pband,
                command.ca_pool,
            )
            if target.target_signature != signature:
                raise RuntimeError("new_horizon_learned_lte_signature_mismatch")
            previous = targets.get(signature)
            if (
                previous is not None
                and previous.target.to_mapping() != target.target.to_mapping()
            ):
                raise RuntimeError("new_horizon_learned_lte_destination_conflict")
            targets[signature] = target
            # Qualification still means measured ten-minute blocks, not the
            # number of hourly containers used to keep those measurements.
            measured_blocks = sum(int(row["coverage_s"] / 600) for row, _ in self._service_rows[arm_id])
            counts[signature] = counts.get(signature, 0) + measured_blocks
            known_refs = refs.setdefault(signature, [])
            for proof_ref in self._proof_refs.get(
                (arm_id, "service_outcome"),
                (),
            ):
                if proof_ref not in known_refs:
                    known_refs.append(proof_ref)
        return tuple(
            LearnedRadioDestination(
                target=targets[signature],
                assessment_count=counts[signature],
                proof_refs=tuple(refs[signature]),
            )
            for signature in sorted(targets)
        )

    def evidence_for(
        self,
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
    ) -> ProvenArmEvidence | None:
        target = opportunity.action_target
        if target is None:
            return None
        arm_id = self.arm_id_for(context, opportunity)
        posterior, rows, _ = self._candidate_evidence(context, opportunity)
        return ProvenArmEvidence(
            arm_id=arm_id,
            target=RefereeTarget(
                action_family=target.action_family,
                target_signature=target.target_signature,
                target=target.target,
            ),
            posterior=posterior,
            positive_proof_refs=tuple(row["assessment_id"] for row in rows if row["aq"] > 0),
        )

    def presentation_statistics(self) -> tuple[BanditTargetStatistics, ...]:
        grouped: dict[str, list[LearningPosterior]] = {}
        for arm_id, target in self._targets.items():
            grouped.setdefault(target.target_signature, []).append(
                self.posterior_for(arm_id, "service_outcome")
            )
        rows: list[BanditTargetStatistics] = []
        for signature, posteriors in sorted(grouped.items()):
            pulls = sum(item.pulls for item in posteriors)
            mean = (
                sum(item.mean_grade * item.pulls for item in posteriors) / pulls
                if pulls
                else None
            )
            negatives = sum(item.negative_count for item in posteriors)
            variance = max((item.variance for item in posteriors), default=0.0)
            rows.append(
                BanditTargetStatistics(
                    arm_id="contextual:" + signature,
                    proof_count=pulls,
                    mean_service_grade=mean,
                    uncertainty=min(
                        1.0,
                        math.sqrt(variance + 1.0) / math.sqrt(pulls + 1.0) / 3.0,
                    ),
                    negative_ratio=(negatives / pulls if pulls else None),
                )
            )
        return tuple(rows)


class Brain:
    """Judge the candidates and make the final selection."""

    _NORMAL_EXPLORATION_INFORMATION_MIN = 0.75
    _NORMAL_EXPLORATION_NET_VALUE_MIN = 0.50
    _NORMAL_ACTION_MIN_AQ_GAIN = 0.2
    _POLICY_VALUE = {
        "do_nothing": 0.15,
    }

    @staticmethod
    def _candidate_prediction(
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
        rank: BanditRank,
        *,
        recent: RecentSituation,
        radio_stability_hold_remaining_s: float,
        context_final_mature: bool = False,
    ) -> tuple[DecisionGradeVector, BrainDecisionEligibility]:
        """Judge one candidate from the complete context."""

        connected = _fact(context, "health.radio_connected") is True
        has_traffic = recent.load_class != "idle"
        congestion = context.surface("quality").value.get("congestion_pct")
        control = context.product_control.to_mapping()
        mutation = context.surface("mutation")
        mutation_active = mutation.value.get("active")
        sources = mutation.value.get("sources")
        band_policy = (
            sources.get("band_action_policy") if isinstance(sources, Mapping) else None
        )
        explore = opportunity.opportunity_id.startswith("explore:")
        emergency_pool_rescue = opportunity.opportunity_id.startswith(
            "emergency_pool_rescue:"
        )
        pool_rescue_active = bool(
            emergency_pool_rescue
            and emergency_pool_rescue_allowed(context)
        )
        urgent_degradation = bool(
            pool_rescue_active
            or (internet_failed(context) or recent.severe_congestion_duration_s >= 60.0)
            and rank.posterior.pulls >= 2
            and rank.posterior.mean_grade > 0.0
        )
        explore_authorized = bool(
            explore
            and control.get("explore_requested") is True
            and str(control.get("explore_session_id") or "")
            in opportunity.opportunity_id
        )
        if opportunity.action_target is None:
            decision_grade, decision_reason = 0, "non_mutating_policy_neutral"
            rules_grade, rules_reason = 1, "non_mutating_policy_rules_clear"
        elif (
            control.get("control_mode") != INTELLIGENCE_CONTROL_AUTOPILOT
            or control.get("control_available") is not True
        ):
            decision_grade, decision_reason = -3, "product_control_not_autopilot"
            rules_grade, rules_reason = -3, "autopilot_rule_not_satisfied"
        elif explore and not explore_authorized:
            decision_grade, decision_reason = -3, "explore_session_not_authorized"
            rules_grade, rules_reason = -3, "explore_authorization_rule_failed"
        elif (
            opportunity.action_target is not None
            and not context_final_mature
            and not explore
            and not urgent_degradation
        ):
            decision_grade, decision_reason = (
                -3,
                "brain_waiting_for_final_10m_context_assessment",
            )
            rules_grade, rules_reason = -3, "final_context_evidence_rule_pending"
        elif radio_stability_hold_remaining_s > 0.0 and not pool_rescue_active:
            decision_grade, decision_reason = (
                -3,
                "brain_radio_stability_hold_active",
            )
            rules_grade, rules_reason = -3, "radio_stability_rule_pending"
        elif (
            isinstance(band_policy, Mapping)
            and band_policy.get("emergency_recovery_hold_active") is True
        ):
            decision_grade, decision_reason = (
                -3,
                "emergency_recovery_hold_active",
            )
            rules_grade, rules_reason = -3, "emergency_recovery_rule_pending"
        elif not connected and not pool_rescue_active:
            decision_grade, decision_reason = -3, "radio_not_connected"
            rules_grade, rules_reason = -3, "connected_radio_rule_failed"
        elif not mutation.valid or mutation.stale:
            decision_grade, decision_reason = -3, "operational_constraint_unavailable"
            rules_grade, rules_reason = -3, "current_constraint_rule_unproved"
        elif mutation_active is True:
            decision_grade, decision_reason = -3, "owner_action_active"
            rules_grade, rules_reason = -3, "single_mutation_rule_failed"
        elif not isinstance(band_policy, Mapping):
            decision_grade, decision_reason = -3, "band_action_policy_unavailable"
            rules_grade, rules_reason = -3, "band_policy_rule_unproved"
        elif band_policy.get("operator_cooldown_active") is True:
            decision_grade, decision_reason = -3, "operator_band_cooldown_active"
            rules_grade, rules_reason = -3, "operator_cooldown_rule_failed"
        elif pool_rescue_active:
            decision_grade = 1
            decision_reason = "emergency_pool_rescue_for_failed_internet"
            rules_grade = 1
            rules_reason = "emergency_pool_rescue_rules_clear"
        elif opportunity.opportunity_id.startswith("best_effort_expand:"):
            # Complete the temporary restriction after all authority, hold,
            # maturity and capability checks. Traffic is not an endless veto.
            decision_grade, decision_reason = 1, "complete_temporary_pool_restriction"
            rules_grade, rules_reason = 1, "pool_expansion_rules_clear"
        elif (
            has_traffic
            and not explore
            and not (
                rank.posterior.pulls >= 2
                and rank.posterior.mean_grade > 0.0
                and type(congestion) in {int, float}
                and float(congestion) >= 35.0
            )
        ):
            decision_grade, decision_reason = -3, "active_user_traffic_unproven_target"
            rules_grade, rules_reason = -3, "active_traffic_protection_rule_failed"
        elif has_traffic and not explore:
            decision_grade, decision_reason = 1, "degraded_service_proven_escape"
            rules_grade, rules_reason = 1, "active_traffic_escape_rules_clear"
        elif explore:
            decision_grade, decision_reason = 1, "user_authorized_explore"
            rules_grade, rules_reason = 1, "explore_rules_clear"
        else:
            decision_grade, decision_reason = 1, "owner_constraints_clear"
            rules_grade, rules_reason = 1, "all_known_rules_clear"
        service_grade = (
            round(max(-3.0, min(intelligence_grade_max("service_outcome"), rank.service_value * 3.0)), 2)
            if rank.service_value is not None
            else None
        )
        vector = DecisionGradeVector(
            components=(
                DecisionGradeComponent(
                    dimension="service_outcome",
                    grade=service_grade,
                    confidence=(
                        min(0.95, rank.posterior.pulls / 10.0)
                        if rank.posterior.pulls
                        else 0.55
                        if opportunity.strategy_service_grade is not None
                        else 0.0
                    ),
                    reason=(
                        "same_scope_observed_service_history"
                        if rank.posterior.pulls
                        else "qualified_staged_strategy_history"
                        if opportunity.strategy_service_grade is not None
                        else "service_evidence_not_yet_observed"
                    ),
                ),
                DecisionGradeComponent(
                    dimension="rules_safety",
                    grade=rules_grade,
                    confidence=1.0,
                    reason=rules_reason,
                ),
            ),
            basis="prediction",
        )
        return vector, BrainDecisionEligibility(
            eligible=opportunity.action_target is None
            or (decision_grade > 0 and rules_grade > 0),
            rules_clear=opportunity.action_target is None or rules_grade > 0,
            reason=decision_reason,
        )

    def choose(
        self,
        frame: BrainDecisionFrame,
    ) -> AdvisoryDecision:
        context = frame.context
        candidates = {
            item.opportunity.opportunity_id: item.opportunity
            for item in frame.candidates
        }
        ranked = tuple(item.rank for item in frame.candidates)
        forecasts = {
            item.opportunity.opportunity_id: item.forecast for item in frame.candidates
        }
        if "do_nothing" not in candidates or not ranked:
            raise RuntimeError("new_horizon_brain_complete_opportunity_set_required")
        judgments = {
            rank.opportunity_id: self._candidate_prediction(
                context,
                candidates[rank.opportunity_id],
                rank,
                recent=frame.recent,
                radio_stability_hold_remaining_s=(
                    frame.radio_stability_hold_remaining_s
                ),
                context_final_mature=frame.context_final_mature,
            )
            for rank in ranked
        }
        eligibility = {key: value[1] for key, value in judgments.items()}

        current_service = frame.current_service.to_mapping()
        current_service_value = normalized_referee_service_value(current_service)
        urgent = (
            internet_failed(context)
            or frame.recent.internet_outage_duration_s > 0.0
            or frame.recent.severe_congestion_duration_s >= 60.0
        )
        # One hourly population, before any normal band/AQ comparison. These
        # are observed cells, never cell-selection commands or probabilities.
        cell_views = {key: value.cell_evidence for key, value in forecasts.items()}

        def purpose_override(item: BanditRank) -> bool:
            return bool(
                urgent or item.opportunity_id == frame.required_opportunity_id
                or item.opportunity_id.startswith(("emergency_pool_rescue:", "best_effort_expand:"))
            )

        def resulting_service(item: BanditRank) -> float | None:
            if (
                item.opportunity_id == "do_nothing"
                and current_service_value is not None
            ):
                # Compare a destination against the service being delivered now,
                # not an older good period on the currently selected band.
                return current_service_value
            forecast = forecasts[item.opportunity_id]
            measured = forecast.expected_service_value
            if measured is not None:
                # Staged recovery/expansion carries its own measured history.
                strategy = item.strategy_service_value
                if strategy is not None and item.opportunity_id != "do_nothing":
                    weight = forecast.effective_evidence
                    measured = (measured * weight + strategy) / (weight + 1.0)
                return measured
            return (
                current_service_value
                if item.opportunity_id == "do_nothing"
                else item.service_value
            )

        do_nothing = next(
            item for item in ranked if item.opportunity_id == "do_nothing"
        )
        current_forecast = forecasts["do_nothing"]
        current_expected = resulting_service(do_nothing)
        predictions = {}
        for item in ranked:
            vector = judgments[item.opportunity_id][0]
            expected = resulting_service(item)
            predictions[item.opportunity_id] = replace(
                vector,
                components=tuple(
                    replace(
                        component,
                        grade=expected * 3.0
                        if expected is not None
                        else None,
                        confidence=forecasts[item.opportunity_id].confidence,
                        reason="expected_absolute_quality",
                    )
                    if component.dimension == "service_outcome"
                    else component
                    for component in vector.components
                ),
            )

        def measured_reliability(candidate: CandidateForecast, reference: CandidateForecast) -> bool:
            # Admission uses measured exposure and availability. Uncertainty,
            # downside and disruption are already paid in net_service_value;
            # a second confidence cutoff would discard better cleaned evidence.
            current_availability = reference.expected_internet_availability_pct
            if not (
                reference.status == "predicted"
                and reference.match_tier == "exact"
                and reference.effective_evidence >= 3.0
                and current_availability is not None
                and float(current_availability) >= 95.0
            ):
                return True
            candidate_availability = candidate.expected_internet_availability_pct
            return bool(
                candidate.status == "predicted"
                and candidate.effective_evidence >= 3.0
                and candidate_availability is not None
                and float(candidate_availability) >= float(current_availability) - 5.0
            )

        def reliability_allows(item: BanditRank) -> bool:
            """Keep a proven stable choice when other evidence is weak."""

            if item.opportunity_id == "do_nothing":
                return True
            opportunity_id = item.opportunity_id
            working = forecasts[opportunity_id].service_evidence
            if working is not None and working["state"] == "NW" and not internet_failed(context):
                # A measured failure is not an attractive unknown option.
                # Explicit user exploration is handled before this filter.
                return False
            if purpose_override(item):
                return True
            return measured_reliability(forecasts[opportunity_id], current_forecast)

        def candidate_value(item: BanditRank) -> float:
            return (resulting_service(item) or 0.0) + 0.05 * self._POLICY_VALUE.get(
                item.opportunity_id, 0.0
            )

        def net_value(item: BanditRank, value: float, forecast: CandidateForecast,
                      reference: float | None) -> float:
            if candidates[item.opportunity_id].action_target is None:
                return value
            downside = forecast.expected_downside(reference,
                nr_service_reference=_fact(context, "radio.network_type") in {"NR NSA (5G)", "NR SA (5G)"})
            # No measured reference/outcome means no quantified downside;
            # normal evidence/reliability admission still applies.
            return (
                value
                - min(0.15 / 3.0, 0.25 * candidates[item.opportunity_id].disruption_cost)
                - 0.10 * forecast.uncertainty
                - (downside if downside is not None else 0.0)
            )

        def net_service_value(item: BanditRank) -> float:
            forecast = forecasts[item.opportunity_id]
            value = candidate_value(item)
            if candidates[item.opportunity_id].action_target is not None and not purpose_override(item):
                if current_expected is not None:
                    value = current_expected + supported_gain(
                        value - current_expected, cell_views[item.opportunity_id]["decision_support"])
            return net_value(item, value, forecast, current_expected)

        def supported_gain(gain: float, support: float | None) -> float:
            # Evidence credit for a positive promise, not a new AQ or a cell
            # probability. Missing history applies no cell preference; a bad
            # forecast is not made attractive by discounting its negative AQ.
            return gain * support if gain > 0.0 and support is not None else gain

        def normal_margin(gain: float) -> bool:
            # AQ is -3..3; this score is AQ/3. Costs were already deducted.
            minimum = self._NORMAL_ACTION_MIN_AQ_GAIN / 3.0
            return gain >= minimum or math.isclose(gain, minimum, rel_tol=0.0, abs_tol=1e-12)

        def clears_action_margin(item: BanditRank) -> bool:
            gain = net_service_value(item) - net_service_value(do_nothing)
            if (
                item.opportunity_id.startswith("explore:")
                or urgent
            ):
                return gain > 0.0
            return normal_margin(gain)

        def wait_for_next_hour(admissible: tuple[BanditRank, ...]) -> bool:
            clock = localtime(context.accepted_at)
            if urgent or clock.tm_min < 30 or current_expected is None or current_expected < 0.0:
                return False
            reference = current_forecast.next_hour
            if reference is None or reference.prediction_hour != (clock.tm_hour + 1) % 24:
                return False
            reference_view = reference.cell_evidence
            reference_value = reference.expected_service_value
            if reference_view["decision_support"] is None or reference_value is None:
                return False
            stay = reference_value + 0.05 * self._POLICY_VALUE["do_nothing"]
            for item in admissible:
                if candidates[item.opportunity_id].action_target is None or purpose_override(item):
                    continue
                following = forecasts[item.opportunity_id].next_hour
                if following is None or following.prediction_hour != reference.prediction_hour:
                    continue
                if following.service_evidence is not None and following.service_evidence["state"] == "NW":
                    continue
                if not measured_reliability(following, reference):
                    continue
                view = following.cell_evidence
                value = following.expected_service_value
                support = view["decision_support"]
                # Do not borrow the current-hour rank for a future hour.
                if value is None or support is None:
                    continue
                value = reference_value + supported_gain(
                    value - reference_value, min(support, reference_view["decision_support"]))
                if normal_margin(net_value(
                        item, value, following, reference_value) - stay):
                    return True
            return False

        selected_for_staged_expansion = False
        selected_for_pool_rescue = False
        selected_for_next_hour = False
        if frame.required_opportunity_id:
            selected = next(
                (
                    item
                    for item in ranked
                    if item.opportunity_id == frame.required_opportunity_id
                ),
                None,
            )
            if selected is None:
                raise RuntimeError("new_horizon_brain_required_opportunity_missing")
            selected_for_information_gain = False
        else:
            admissible = tuple(
                item for item in ranked if eligibility[item.opportunity_id].eligible
            )
            actionable = tuple(
                item
                for item in admissible
                if candidates[item.opportunity_id].action_target is not None
                and reliability_allows(item)
                and clears_action_margin(item)
            )
            staged_expansions = tuple(
                item
                for item in admissible
                if item.opportunity_id.startswith("best_effort_expand:")
            )
            pool_rescues = tuple(
                item
                for item in admissible
                if item.opportunity_id.startswith("emergency_pool_rescue:")

            )
            selected = (
                max(
                    pool_rescues,
                    key=lambda item: (
                        candidate_value(item),
                        item.opportunity_id,
                    ),
                )
                if pool_rescues
                else max(
                    staged_expansions,
                    key=lambda item: (
                        len(candidates[item.opportunity_id].companion_support),
                        math.fsum(hours for _, hours in candidates[item.opportunity_id].companion_support),
                        candidate_value(item),
                        item.opportunity_id,
                    ),
                )
                if staged_expansions
                else max(
                    actionable,
                    key=lambda item: (
                        net_service_value(item),
                        item.opportunity_id,
                    ),
                    default=None,
                )
            )
            selected_for_pool_rescue = bool(pool_rescues)
            selected_for_staged_expansion = bool(staged_expansions)
            selected_for_information_gain = False
            if not pool_rescues and not staged_expansions and wait_for_next_hour(admissible):
                selected = do_nothing
                selected_for_next_hour = True
            if selected is None:

                def knowledge_value(item: BanditRank) -> float:
                    return net_service_value(item) + 0.25 * item.information_value

                knowledge_gap = tuple(
                    item
                    for item in admissible
                    if (
                        candidates[item.opportunity_id].action_target is not None
                        and frame.context_final_mature
                        and frame.recent.load_class == "idle"
                        and reliability_allows(item)
                        and forecasts[item.opportunity_id].evidence_assessments == 0
                        and not item.known_target_evidence_refs
                        and item.information_value
                        >= self._NORMAL_EXPLORATION_INFORMATION_MIN
                        and item.information_value
                        - candidates[item.opportunity_id].disruption_cost
                        >= self._NORMAL_EXPLORATION_NET_VALUE_MIN
                        and knowledge_value(item) > candidate_value(do_nothing)
                    )
                )
                selected = max(
                    knowledge_gap,
                    key=lambda item: (
                        knowledge_value(item),
                        item.opportunity_id,
                    ),
                    default=do_nothing,
                )
                selected_for_information_gain = selected is not do_nothing
        margin = net_service_value(selected) - net_service_value(do_nothing)
        if frame.required_opportunity_id:
            explanation = "Explore selected the user-requested test target."
        elif selected_for_pool_rescue:
            explanation = (
                "Internet is unavailable; add one historically proven band "
                "without forcing the serving band."
            )
        elif selected_for_staged_expansion:
            explanation = (
                "Add the best eligible companions after the one-band "
                "best-effort stability hold."
            )
        elif selected_for_information_gain:
            explanation = (
                "Test the unknown target: information value still beats the current "
                "connection after uncertainty and disruption cost."
            )
        elif selected_for_next_hour:
            explanation = (
                "Keep acceptable service for now; the next hour has a better "
                "supported band/cell option. Recheck when that hour starts."
            )
        elif selected.opportunity_id == "do_nothing":
            explanation = (
                "Keep the current connection: no eligible target has enough "
                "completed evidence or forecast value to justify disruption."
            )
        else:
            evidence_sources: list[str] = []
            if selected.posterior.pulls:
                evidence_sources.append("completed target evidence")
            selected_forecast = forecasts[selected.opportunity_id]
            if selected_forecast.evidence_assessments:
                evidence_sources.append(
                    selected_forecast.match_tier.replace("_", " ") + " forecast"
                )
            explanation = (
                ("Select the recovery target: " if purpose_override(selected)
                 else "Select using hourly cell support: ")
                + (", ".join(evidence_sources) or "live service comparison")
                + " beats keeping the current connection after disruption cost."
            )
        # Public blame for staying needs an alternative known *then*, not a
        # better result discovered later. Reuse this exact decision's checks;
        # Impact must not implement a second selection policy.
        hold_opportunities = ()
        if (selected.opportunity_id == "do_nothing" and not selected_for_next_hour
                and frame.radio_stability_hold_remaining_s <= 0.0
                and current_expected is not None):
            hold_opportunities = tuple(
                CoreFrozenPayload.from_mapping({
                    "schema": "BrainHoldOpportunity.v1",
                    "opportunity_id": item.opportunity_id,
                    "forecast": {key: value for key, value in forecasts[item.opportunity_id].to_mapping().items()
                                 if key in {"status", "prediction_hour", "target_signature", "result_contexts"}},
                })
                for item in ranked
                if candidates[item.opportunity_id].action_target is not None
                and eligibility[item.opportunity_id].eligible
                and not purpose_override(item)
                and reliability_allows(item) and clears_action_margin(item)
                and forecasts[item.opportunity_id].status == "predicted"
                and forecasts[item.opportunity_id].prediction_hour == frame.recent.local_hour
                and forecasts[item.opportunity_id].effective_evidence >= 3.0
                and bool(forecasts[item.opportunity_id].evidence_refs)
                and (cell_views[item.opportunity_id]["observed_support"] or 0.0) > 0.0
            )
        return AdvisoryDecision(
            selected_opportunity_id=selected.opportunity_id,
            reason=(
                "user_authorized_explore_brain_selection"
                if frame.required_opportunity_id
                else "brain_emergency_pool_rescue"
                if selected_for_pool_rescue
                else "brain_best_effort_pool_expansion"
                if selected_for_staged_expansion
                else "brain_safe_idle_information_gain"
                if selected_for_information_gain
                else "brain_wait_next_hour"
                if selected_for_next_hour
                else "do_nothing_hysteresis_hold"
                if selected.opportunity_id == "do_nothing"
                else "brain_candidate_beats_do_nothing_margin=" + format(margin, ".3f")
            ),
            explanation=explanation,
            predicted_grades=predictions[selected.opportunity_id],
            eligibility=eligibility[selected.opportunity_id],
            hold_opportunities=hold_opportunities,
        )


class DecisionSelection:
    """Give learned statistics to Brain for final selection."""

    def __init__(
        self,
        history: AdvisoryHistory,
        *,
        assessment_history: RefereeAssessmentHistoryOwner,
        decision_history: DecisionLifecycleHistoryOwner,
    ) -> None:
        self._history = history
        if not isinstance(assessment_history, RefereeAssessmentHistoryOwner):
            raise TypeError("new_horizon_decision_referee_history_owner_required")
        if assessment_history.history is not history:
            raise ValueError("new_horizon_decision_referee_history_store_mismatch")
        self._assessment_history = assessment_history
        if not isinstance(decision_history, DecisionLifecycleHistoryOwner):
            raise TypeError("new_horizon_decision_history_owner_required")
        if decision_history.history is not history:
            raise ValueError("new_horizon_decision_history_store_mismatch")
        self._decision_history = decision_history
        self._bandit = ContextualBandit(
            history,
            assessment_history=self._assessment_history,
        )
        self._crystal_ball = CrystalBall(
            history,
            assessment_history=self._assessment_history,
            decision_history=self._decision_history,
        )
        self._brain = Brain()
        self._selected_arm_id = "do_nothing"
        self._selected_rank: BanditRank | None = None
        self._selected_forecast: CandidateForecast | None = None
        self._selected_contributions: tuple[dict[str, Any], ...] = ()
        self._selected_frame: BrainDecisionFrame | None = None
        self._selected_decision: AdvisoryDecision | None = None
        self._presentation_continuity_key = ""
        self._presentation_forecasts: tuple[CandidateForecast, ...] = ()
        self._presentation_selected_opportunity_id = ""

    @classmethod
    def _proposal_semantic_key_parts(
        cls,
        context: IntelligenceContext,
        *,
        action_family: str,
        target_signature: str,
        emergency: bool = False,
    ) -> str:
        current = current_context_target(context)
        if (current is None and not emergency) or not action_family or not target_signature:
            raise ValueError("new_horizon_semantic_proposal_target_required")
        control = context.product_control.to_mapping()
        rescue_slot = context.surface("mutation").value.get("sources", {}).get("band_action_policy", {})
        return "|".join(
            (
                context_continuity_key(context),
                ("emergency:" + str(context.surface("emergency_reachability").value.get("incident_id") or "closed")
                 + ":" + str(rescue_slot.get("emergency_band_rescue_opened_at") or ""))
                if emergency else current.target_signature,
                repr(proposal_source_state(context)),
                action_family,
                target_signature,
                str(control.get("control_mode") or ""),
                "1" if control.get("control_available") is True else "0",
                str(control.get("explore_session_id") or ""),
            )
        )

    @classmethod
    def command_intention_key(
        cls,
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
    ) -> str:
        target = opportunity.action_target
        if target is None:
            raise ValueError("new_horizon_semantic_proposal_target_required")
        return cls._proposal_semantic_key_parts(
            context,
            action_family=target.action_family,
            target_signature=target.target_signature,
            emergency=opportunity.opportunity_id.startswith("emergency_pool_rescue:"),
        )

    @classmethod
    def proposal_semantic_key(
        cls,
        context: IntelligenceContext,
        proposal: BrainCommandProposal,
    ) -> str:
        target = proposal.action_target
        return cls._proposal_semantic_key_parts(
            context,
            action_family=target.action_family,
            target_signature=target.target_signature,
            emergency=proposal.execution_intent == "emergency_radio_down_recover",
        )

    @property
    def posteriors(self) -> tuple[LearningPosterior, ...]:
        return self._bandit.posteriors_for(self._selected_arm_id)

    @property
    def learned_proof_count(self) -> int:
        return self._bandit.learned_proof_count

    def history_population_status(self) -> dict[str, Any]:
        """Report whether every derived adviser uses one Referee population."""

        learning_population_id = self._bandit.source_population_id
        forecast_population_id = self._crystal_ball.source_population_id
        coherent = bool(
            learning_population_id and learning_population_id == forecast_population_id
        )
        return {
            "schema": "NewHorizonLearningPopulationStatus.v1",
            "coherent": coherent,
            "learning_population_id": learning_population_id,
            "forecast_population_id": forecast_population_id,
            "source_assessment_count": (self._crystal_ball.source_assessment_count),
            "learned_assessment_count": self._bandit.learned_proof_count,
        }

    @property
    def presentation_statistics(self) -> tuple[BanditTargetStatistics, ...]:
        return self._bandit.presentation_statistics()

    @property
    def candidate_forecasts(self) -> tuple[CandidateForecast, ...]:
        """Return current forecast candidates in Brain-frame order."""

        return self._presentation_forecasts

    @property
    def candidate_forecast_selection_id(self) -> str:
        """Return the selection saved with the candidate frame."""

        return self._presentation_selected_opportunity_id

    def completed_learning_cycle_ring(self) -> tuple[dict[str, Any], ...]:
        return self._crystal_ball.completed_learning_cycle_ring()

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
            raise ValueError("new_horizon_decision_referee_history_scope_mismatch")
        previous = (
            self._presentation_continuity_key,
            self._presentation_forecasts,
            self._presentation_selected_opportunity_id,
            self._bandit._binding_state(),
            self._crystal_ball._binding_state(),
        )
        try:
            if (
                self._presentation_continuity_key
                and self._presentation_continuity_key != continuity
            ):
                self._presentation_forecasts = ()
                self._presentation_selected_opportunity_id = ""
            self._presentation_continuity_key = continuity
            self._crystal_ball.bind_context(
                context,
                assessment_history,
                decision_history,
            )
            bound = self._bandit.bind_context(context, assessment_history)
            if (
                self._bandit.source_population_id
                != self._crystal_ball.source_population_id
            ):
                raise RuntimeError("new_horizon_learning_population_split")
            return bound
        except Exception:
            (
                self._presentation_continuity_key,
                self._presentation_forecasts,
                self._presentation_selected_opportunity_id,
                bandit_state,
                crystal_ball_state,
            ) = previous
            self._bandit._restore_binding_state(bandit_state)
            self._crystal_ball._restore_binding_state(crystal_ball_state)
            raise

    def require_bound_context(self, context: IntelligenceContext) -> None:
        """Fail when a caller skipped the canonical owner-snapshot bind."""

        continuity = context_continuity_key(context)
        if (
            self._presentation_continuity_key != continuity
            or self._bandit._scope_key != continuity
            or self._crystal_ball._continuity_key != continuity
        ):
            raise RuntimeError("new_horizon_decision_context_not_bound")
        if not self.history_population_status()["coherent"]:
            raise RuntimeError("new_horizon_learning_population_split")

    def select_do_nothing(self, context: IntelligenceContext) -> None:
        """Select no action without reusing an older decision."""

        opportunity = AdvisoryOpportunity(
            opportunity_id="do_nothing",
            kind="policy",
            target="retain_current_configuration",
            action_target=None,
            reason="current_context_non_actionable",
            eligible=True,
            uncertainty=0.0,
            disruption_cost=0.0,
            evidence_refs=(),
        )
        self._selected_arm_id = self._bandit.arm_id_for(context, opportunity)
        self._selected_rank = None
        self._selected_forecast = None
        self._selected_contributions = ()
        self._selected_frame = None
        self._selected_decision = None

    def adviser_chain_snapshot(self) -> dict[str, Any]:
        """Return evidence showing which advisers informed Brain."""

        frame = self._selected_frame
        decision = self._selected_decision
        if frame is None or decision is None:
            return {
                "schema": "NewHorizonAdviserChainSnapshot.v2",
                "status": "current_context_incomplete",
                "selected_opportunity_id": "do_nothing",
                "selected_brain_eligibility": {},
                "candidates": [],
                "selected_contributions": [],
            }
        return {
            "schema": "NewHorizonAdviserChainSnapshot.v2",
            "status": "complete",
            "situation": {
                "day_class": frame.recent.day_class,
                "load_class": frame.recent.load_class,
                "severe_congestion": (frame.recent.severe_congestion_duration_s > 0.0),
                "internet_outage": frame.recent.internet_outage_duration_s > 0.0,
                "context_final_mature": frame.context_final_mature,
                "radio_stability_hold_active": (
                    frame.radio_stability_hold_remaining_s > 0.0
                ),
                "required_opportunity_id": frame.required_opportunity_id,
            },
            "selected_opportunity_id": decision.selected_opportunity_id,
            "selection_reason": decision.reason,
            "selected_brain_eligibility": decision.eligibility.to_payload(),
            "selected_contributions": [
                dict(item) for item in self._selected_contributions
            ],
            "candidates": [
                {
                    "opportunity_id": item.opportunity.opportunity_id,
                    "opportunity_discovered": item.opportunity.eligible,
                    "reason": item.opportunity.reason,
                    "target_signature": (
                        item.opportunity.action_target.target_signature
                        if item.opportunity.action_target is not None
                        else ""
                    ),
                    "strategy_service_grade": (item.opportunity.strategy_service_grade),
                    "historical_working_pct": item.opportunity.historical_working_pct,
                    "companion_support": [
                        {"band": band, "weighted_hours": hours}
                        for band, hours in item.opportunity.companion_support
                    ],
                    "bandit": {
                        "evidence_state": item.rank.evidence_state,
                        "diagnostic_state": item.rank.diagnostic_state,
                        "service_value": item.rank.service_value,
                        "absolute_quality_grade": (
                            item.rank.posterior.mean_grade
                            if item.rank.posterior.pulls
                            else None
                        ),
                        "effective_evidence": item.rank.effective_evidence,
                        "service_proofs": item.rank.posterior.pulls,
                        "known_target_evidence_count": len(item.rank.known_target_evidence_refs),
                        "uncertainty": item.rank.uncertainty,
                        "downside": item.rank.downside,
                        "information_value": item.rank.information_value,
                    },
                    "crystal_ball": {
                        "status": item.forecast.status,
                        "match_tier": item.forecast.match_tier,
                        "confidence": item.forecast.confidence,
                        "quality_confidence": item.forecast.quality_confidence,
                        "cell_evidence": item.forecast.cell_evidence,
                        "uncertainty": item.forecast.uncertainty,
                        "evidence_assessments": (item.forecast.evidence_assessments),
                        "accuracy_factor": item.forecast.accuracy_factor,
                        "accuracy_assessments": item.forecast.accuracy_assessments,
                        "expected_service_value": item.forecast.expected_service_value,
                        "cell_outcome_spread": item.forecast.service_spread,
                        "prediction_hour": item.forecast.prediction_hour,
                        "prediction_at": item.forecast.prediction_at,
                        "next_hour": item.forecast.next_hour.to_mapping()
                        if item.forecast.next_hour is not None else None,
                    },
                }
                for item in frame.candidates
            ],
        }

    def admit_proof(self, _proof: RefereeProof) -> bool:
        raise RuntimeError(
            "new_horizon_decision_selection_assessment_admission_required"
        )

    def posterior_for(
        self,
        arm_id: str,
        dimension: str = "service_outcome",
    ) -> LearningPosterior:
        return self._bandit.posterior_for(arm_id, dimension)

    def posterior_for_target(
        self,
        context: IntelligenceContext,
        *,
        action_family: str,
        target_signature: str,
        dimension: str = "service_outcome",
    ) -> LearningPosterior:
        arm_id = (
            context_learning_key(context) + "|" + action_family + ":" + target_signature
        )
        return self._bandit.posterior_for(arm_id, dimension)

    def learned_radio_destinations(
        self,
    ) -> tuple[LearnedRadioDestination, ...]:
        return self._bandit.learned_radio_destinations()

    def evidence_for(
        self,
        context: IntelligenceContext,
        opportunity: AdvisoryOpportunity,
    ) -> ProvenArmEvidence | None:
        return self._bandit.evidence_for(context, opportunity)

    @property
    def selected_forecast(self) -> CandidateForecast | None:
        return self._selected_forecast

    @property
    def selected_contributions(self) -> tuple[dict[str, Any], ...]:
        return self._selected_contributions

    def selected_proposal_evidence(
        self,
        *,
        proposal: BrainCommandProposal,
        context: IntelligenceContext,
    ) -> dict[str, Any]:
        if self._selected_forecast is None:
            raise RuntimeError("new_horizon_selected_forecast_missing")
        if self._selected_frame is None:
            raise RuntimeError("new_horizon_selected_decision_frame_missing")
        if self._selected_decision is None:
            raise RuntimeError("new_horizon_selected_decision_missing")
        frame = self._selected_frame
        source_target = current_context_target(context)
        if source_target is None:
            raise ValueError("new_horizon_decision_source_target_required")
        return {
            "schema": DECISION_LEDGER_SCHEMA,
            "proposal_id": proposal.proposal_id,
            "source_signature": source_target.target_signature,
            "learning_context": context_learning_components(context),
            "forecast": self._selected_forecast.to_mapping(),
            "predicted_grades": proposal.predicted_grades.to_payload(),
            "explanation": self._selected_decision.explanation.strip(),
            "contributions": [dict(item) for item in self._selected_contributions],
            "decision_frame": {
                "schema": BRAIN_DECISION_FRAME_SCHEMA,
                "root_fingerprint": frame.context.root_fingerprint,
                "matrix_sequence": frame.context.matrix_identity.matrix_sequence,
                "accepted_at": frame.context.accepted_at,
                "current_service": frame.current_service.to_mapping(),
                "recent": {
                    "context_count": frame.recent.context_count,
                    "context_tenure_s": frame.recent.context_tenure_s,
                    "severe_congestion_duration_s": (
                        frame.recent.severe_congestion_duration_s
                    ),
                    "internet_outage_duration_s": (
                        frame.recent.internet_outage_duration_s
                    ),
                    "local_hour": frame.recent.local_hour,
                    "day_class": frame.recent.day_class,
                    "load_class": frame.recent.load_class,
                },
                "candidate_ids": [
                    item.opportunity.opportunity_id for item in frame.candidates
                ],
                "required_opportunity_id": frame.required_opportunity_id,
                "context_final_mature": frame.context_final_mature,
                "radio_stability_hold_remaining_s": (
                    frame.radio_stability_hold_remaining_s
                ),
            },
        }

    def decide(
        self,
        context: IntelligenceContext,
        contexts: tuple[IntelligenceContext, ...],
        opportunities: tuple[AdvisoryOpportunity, ...],
        *,
        radio_stability_hold_remaining_s: float = 0.0,
        required_opportunity_id: str = "",
        context_final_mature: bool = False,
    ) -> AdvisoryDecision:
        adviser_credit = self._crystal_ball.adviser_credit
        ranks = self._bandit.rank(
            context,
            opportunities,
            adviser_credit=adviser_credit,
        )
        forecasts = self._crystal_ball.forecasts(context, opportunities)
        frame = build_brain_decision_frame(
            context,
            contexts,
            opportunities,
            ranks,
            forecasts,
            radio_stability_hold_remaining_s=radio_stability_hold_remaining_s,
            required_opportunity_id=required_opportunity_id,
            context_final_mature=context_final_mature,
        )
        self._selected_frame = frame
        decision = self._brain.choose(frame)
        self._presentation_forecasts = tuple(item.forecast for item in frame.candidates)
        self._presentation_selected_opportunity_id = decision.selected_opportunity_id
        selected = next(
            item
            for item in ranks
            if item.opportunity_id == decision.selected_opportunity_id
        )
        self._selected_arm_id = selected.arm_id
        self._selected_rank = selected
        self._selected_forecast = forecasts[selected.opportunity_id]
        self._selected_decision = decision
        selected_opportunity = next(
            item
            for item in opportunities
            if item.opportunity_id == selected.opportunity_id
        )
        contributions: list[dict[str, Any]] = []
        actionable = selected_opportunity.action_target is not None
        bandit_service_used = bool(
            actionable
            and selected.posterior.pulls
        )
        contributions.append(
            _adviser_contribution(
                adviser="bandit",
                dimension="service_outcome",
                predicted_grade=(
                    selected.posterior.mean_grade
                    if selected.posterior.pulls
                    else 0.0
                ),
                confidence=(
                    min(0.95, selected.effective_evidence / 10.0)
                    if bandit_service_used
                    else 0.0
                ),
                evidence_refs=selected.service_evidence_refs,
                used=bandit_service_used,
            )
        )
        forecast_service = self._selected_forecast.expected_service_value
        crystal_service_used = bool(
            actionable
            and self._selected_forecast.status == "predicted"
            and forecast_service is not None
        )
        contributions.append(
            _adviser_contribution(
                adviser="crystal_ball",
                dimension="service_outcome",
                predicted_grade=forecast_service * 3.0 if crystal_service_used else 0.0,
                confidence=(
                    self._selected_forecast.confidence if crystal_service_used else 0.0
                ),
                evidence_refs=(
                    self._selected_forecast.evidence_refs
                    if self._selected_forecast.status == "predicted"
                    else ()
                ),
                used=crystal_service_used,
            )
        )
        self._selected_contributions = tuple(contributions)
        return decision


__all__ = (
    "BanditRank",
    "BanditTargetStatistics",
    "BrainDecisionFrame",
    "Brain",
    "ContextualBandit",
    "DecisionCandidate",
    "DecisionSelection",
    "ProvenArmEvidence",
    "RecentSituation",
    "build_brain_decision_frame",
)
