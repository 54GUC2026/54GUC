"""Build opportunities, forecasts, proposals, and evidence-readiness advice."""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations
import math
import hashlib
from typing import Any, Iterable, Mapping

from arch4.core_contracts.intelligence_proposals import (
    BrainDecisionEligibility,
    ActionTargetEvidence,
    BrainCommandProposal,
    DecisionGradeComponent,
    DecisionGradeVector,
    GRADE_DIMENSIONS,
)
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CorePBandTarget,
    CoreRadioModeTarget,
    core_radio_mode_signature,
    core_command_target_from_payload,
    core_lte_band_signature,
    core_lte_pband_signature,
    core_command_target_payload,
)
from arch4.new_horizon.contracts import (
    FAILED_PHYSICAL_TARGET_RETRY_S,
    AdvisoryDecision,
    AdvisoryForecast,
    AdvisoryOpportunity,
    EvidenceReadiness,
    IntelligenceContext,
    LearningPosterior,
    RefereeProof,
)
from arch4.new_horizon.decision import (
    emergency_pool_rescue_allowed,
    BanditTargetStatistics,
    DecisionSelection,
    internet_failed,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.hourly_aq import aq_pool_key
from arch4.new_horizon.decision_history import (
    DecisionLifecycleHistoryOwner,
    DecisionLifecycleHistorySnapshot,
)
from arch4.new_horizon.learning import CandidateForecast
from arch4.new_horizon.performance import band_physical_target_eligibility
from arch4.new_horizon.proof import (
    RefereeAssessmentHistoryOwner,
    RefereeAssessmentHistorySnapshot,
    context_continuity_key,
    context_discloses_ca,
    context_learning_components,
)
from arch4.new_horizon.contracts import DECISION_LEDGER_SCHEMA
from arch4.new_horizon.nr_service_recovery import NrRecoveryDecision
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CONTROL_AUTOPILOT,
)


OPPORTUNITY_DISPOSITIONS = {
    "do_nothing": "active",
    "learned_lte_destination": "active",
    "lte_anchor_exploration": "active",
    "cell_lock": "deferred_missing_typed_capability_and_proof",
    "nr_lock": "deferred_missing_typed_capability_and_proof",
    "arbitrary_scan": "deferred_missing_typed_capability_and_proof",
    "capacity_probe": "active",
    "explore": "active",
    "best_effort_anchor": "active",
    "best_effort_pool_expansion": "active",
    "emergency_pool_rescue": "active",
}

NEW_HORIZON_EXECUTION_INTENTS = frozenset(
    {
        "anchor_lock",
        "emergency_radio_down_recover",
        "explore",
        "preemptive_congestion",
    }
)


def _execution_intent(opportunity_id: str) -> str:
    if opportunity_id.startswith("explore:"):
        return "explore"
    if opportunity_id.startswith("lte_anchor:"):
        return "anchor_lock"
    if opportunity_id.startswith("learned_lte:"):
        return "preemptive_congestion"
    if opportunity_id.startswith("best_effort_"):
        return "anchor_lock"
    if opportunity_id.startswith("emergency_pool_rescue:"):
        return "emergency_radio_down_recover"
    raise RuntimeError(
        "new_horizon_actionable_opportunity_intent_unclassified:" + opportunity_id
    )


def _normal_primary_target(
    band: int, *, current_pool: tuple[int, ...], available_pool: tuple[int, ...],
) -> CorePBandTarget:
    """One normal action per primary; do not trim a working broad pool."""
    if band not in available_pool:
        raise ValueError("new_horizon_normal_primary_not_available")
    retained = tuple(b for b in current_pool if b in available_pool)
    pool = (retained if aq_pool_key(retained) == ("broad",)
            else available_pool)
    return CorePBandTarget(pband=band, ca_pool=tuple(sorted(set((*pool, band)))))


def _fact(context: IntelligenceContext, path: str) -> Any | None:
    value = context.fact(path)
    return value.value.require("value") if value.disposition == "available" else None


def _quality(context: IntelligenceContext, field: str) -> float | None:
    surface = context.surface("quality")
    if not surface.valid or surface.stale:
        return None
    value = surface.value.get(field)
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _radio_stability_hold_remaining(context: IntelligenceContext) -> float:
    """Read Core's stability clock from the typed mutation data."""

    mutation = context.surface("mutation")
    if not mutation.valid or mutation.stale:
        return 0.0
    sources = mutation.value.get("sources")
    policy = sources.get("band_action_policy") if isinstance(sources, Mapping) else None
    if not isinstance(policy, Mapping):
        return 0.0
    remaining = policy.get("radio_stability_hold_remaining_s")
    if isinstance(remaining, bool) or type(remaining) not in {int, float}:
        raise TypeError("new_horizon_radio_stability_remaining_invalid")
    parsed = float(remaining)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError("new_horizon_radio_stability_remaining_invalid")
    return parsed


def _emergency_recovery_hold_remaining(context: IntelligenceContext) -> float:
    """Read Foundation's Core-enforced post-restart hold."""

    mutation = context.surface("mutation")
    if not mutation.valid or mutation.stale:
        return 0.0
    sources = mutation.value.get("sources")
    policy = sources.get("band_action_policy") if isinstance(sources, Mapping) else None
    if not isinstance(policy, Mapping):
        return 0.0
    remaining = policy.get("emergency_recovery_hold_remaining_s", 0.0)
    if isinstance(remaining, bool) or type(remaining) not in {int, float}:
        raise TypeError("new_horizon_emergency_recovery_hold_invalid")
    parsed = float(remaining)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError("new_horizon_emergency_recovery_hold_invalid")
    return parsed


def _positive_bands(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    bands: set[int] = set()
    for raw in value:
        if type(raw) is not int or raw <= 0:
            return ()
        bands.add(raw)
    return tuple(sorted(bands))


def _action_target(target: CoreBandSetTarget | CorePBandTarget) -> ActionTargetEvidence:
    if isinstance(target, CoreBandSetTarget):
        family = "lte_ca_pool"
        signature = core_lte_band_signature(target.bands)
    else:
        family = "lte_radio_selection"
        signature = core_lte_pband_signature(target.pband, target.ca_pool)
    return ActionTargetEvidence(
        action_family=family,
        command_kind=target.command_kind,
        target_signature=signature,
        target=CoreFrozenPayload.from_mapping(core_command_target_payload(target)),
    )


def _radio_command_matches_current(
    context: IntelligenceContext,
    target: CoreBandSetTarget | CorePBandTarget,
) -> bool:
    """Return whether a radio command already matches Modem Live values."""

    current_pool = _positive_bands(_fact(context, "radio.configured_band_pool"))
    if isinstance(target, CoreBandSetTarget):
        return (
            target.command_kind == "BAND_SELECTION"
            and bool(current_pool)
            and current_pool == target.bands
        )
    current_pband = _fact(context, "radio.serving_band")
    return (
        type(current_pband) is int
        and current_pband == target.pband
        and (
            not target.ca_pool
            or (bool(current_pool) and current_pool == target.ca_pool)
        )
    )


def _runtime_action_metadata(
    context: IntelligenceContext,
    key: str,
) -> Mapping[str, Any] | None:
    """Read a Modem Live action fact without reopening the modem profile."""
    capabilities = _fact(context, "capability.runtime_capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    contract = capabilities.get("capability_contract")
    actions = contract.get("actions") if isinstance(contract, Mapping) else None
    metadata = actions.get(key) if key and isinstance(actions, Mapping) else None
    return metadata if isinstance(metadata, Mapping) else None


def _action_metadata(
    context: IntelligenceContext,
    opportunity: AdvisoryOpportunity,
) -> Mapping[str, Any] | None:
    """Read Modem Live action data without reopening a modem profile."""

    if opportunity.action_target is None:
        return None
    key = (
        "pband"
        if opportunity.action_target.command_kind == "PBAND"
        else "band_select"
        if opportunity.action_target.command_kind == "BAND_SELECTION"
        else ""
    )
    return _runtime_action_metadata(context, key)


def _staged_pool_strategy(context: IntelligenceContext) -> bool:
    """Use the one staged pool strategy when direct PBAND is not reliable.

    Missing capability is not proof of an unavailable actuator. Pool-only
    operation requires explicit unsupported PBAND and supported writable pool.
    """

    pband = _runtime_action_metadata(context, "pband") or {}
    pool = _runtime_action_metadata(context, "band_select") or {}
    return bool(
        (
            (pband.get("write_allowed") is True
             and str(pband.get("overlay_trust") or "").lower() == "degraded")
            or (pband.get("write_allowed") is False
                and str(pband.get("overlay_trust") or "").lower() == "unsupported")
        )
        and pool.get("write_allowed") is True
        and str(pool.get("overlay_trust") or "").lower() == "supported"
    )


def _best_effort_opportunity(opportunity: AdvisoryOpportunity) -> bool:
    return opportunity.opportunity_id.startswith("best_effort_")


def _bind_action_capability(
    context: IntelligenceContext,
    opportunity: AdvisoryOpportunity,
) -> AdvisoryOpportunity:
    """Bind Modem Live capabilities, not learned physical results."""

    if opportunity.action_target is None:
        return opportunity
    target = core_command_target_from_payload(opportunity.action_target.target.to_mapping())
    pool = target.bands if isinstance(target, CoreBandSetTarget) else target.ca_pool if isinstance(target, CorePBandTarget) else None
    if pool is not None and len(pool) < 2 and not opportunity.opportunity_id.startswith("emergency_pool_rescue:"):
        return replace(opportunity, eligible=False, reason="brain_lte_pool_requires_two_bands")
    metadata = _action_metadata(context, opportunity)
    trust = str((metadata or {}).get("overlay_trust") or "").lower()
    write_allowed = (metadata or {}).get("write_allowed") is True
    return replace(
        opportunity,
        eligible=(
            opportunity.eligible
            and write_allowed
            and trust in {"supported", "degraded"}
        ),
        reason=(
            "modem_live_action_trust_unavailable"
            if not write_allowed or trust not in {"supported", "degraded"}
            else opportunity.reason
        ),
    )


def _bind_physical_eligibility(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    opportunity: AdvisoryOpportunity,
) -> AdvisoryOpportunity:
    """Apply the two-hour retry rule to the exact target."""

    if opportunity.action_target is None or opportunity.opportunity_id.startswith("explore:") or (
        opportunity.opportunity_id.startswith("emergency_pool_rescue:")
        and emergency_pool_rescue_allowed(context)
    ):
        return opportunity
    physical = band_physical_target_eligibility(
        history,
        context,
        continuity_key=context_continuity_key(context),
        target_signature=opportunity.action_target.target_signature,
        observed_at=context.accepted_at,
        retry_window_s=FAILED_PHYSICAL_TARGET_RETRY_S,
    )
    if physical.eligible:
        return opportunity
    return replace(
        opportunity,
        eligible=False,
        reason=(
            "exact_physical_target_retry_window_active:"
            + str(int(math.ceil(physical.retry_remaining_s)))
            + "s"
        ),
    )


def _forecast_surfaces(
    selected: CandidateForecast,
) -> tuple[AdvisoryForecast, EvidenceReadiness]:
    predicted = selected.status == "predicted"
    forecast = AdvisoryForecast(
        status="predicted" if predicted else "insufficient_evidence",
        match_tier=selected.match_tier,
        expected_eci_points=selected.expected_eci_points,
        expected_latency_ms=selected.expected_latency_ms,
        expected_jitter_ms=selected.expected_jitter_ms,
        expected_cqb=selected.expected_cqb,
        expected_internet_availability_pct=(
            selected.expected_internet_availability_pct
        ),
        confidence=selected.confidence,
        uncertainty=selected.uncertainty,
        evidence_samples=selected.evidence_assessments,
        reason=(
            "candidate_specific_" + selected.match_tier + "_crystal_ball_forecast"
            if predicted
            else "matching_context_assessment_required"
        ),
        accuracy_factor=selected.accuracy_factor,
        accuracy_assessments=selected.accuracy_assessments,
    )
    readiness = EvidenceReadiness(
        state="ready" if predicted else "collecting",
        proposal=(
            "review_" + selected.match_tier + "_forecast"
            if predicted
            else "collect_same_scope_owner_evidence"
        ),
        confidence=selected.confidence,
        evidence_samples=selected.evidence_assessments,
    )
    return forecast, readiness


class AdvisoryReasoning:
    """Coordinate advice using accepted facts from each owner."""

    def __init__(
        self,
        history: AdvisoryHistory,
        *,
        assessment_history: RefereeAssessmentHistoryOwner,
        decision_history: DecisionLifecycleHistoryOwner,
    ) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_reasoning_history_required")
        self._history = history
        if not isinstance(assessment_history, RefereeAssessmentHistoryOwner):
            raise TypeError("new_horizon_reasoning_referee_history_owner_required")
        if assessment_history.history is not history:
            raise ValueError("new_horizon_reasoning_referee_history_store_mismatch")
        self._assessment_history = assessment_history
        if not isinstance(decision_history, DecisionLifecycleHistoryOwner):
            raise TypeError("new_horizon_reasoning_decision_history_owner_required")
        if decision_history.history is not history:
            raise ValueError("new_horizon_reasoning_decision_history_store_mismatch")
        self._decision_history = decision_history
        self._nr_health_decision: AdvisoryDecision | None = None
        self._nr_health_opportunity: AdvisoryOpportunity | None = None
        self._decision_selection = DecisionSelection(
            history,
            assessment_history=self._assessment_history,
            decision_history=self._decision_history,
        )

    @property
    def posteriors(self) -> tuple[LearningPosterior, ...]:
        return self._decision_selection.posteriors

    @property
    def learned_proof_count(self) -> int:
        return self._decision_selection.learned_proof_count

    @property
    def presentation_statistics(self) -> tuple[BanditTargetStatistics, ...]:
        return self._decision_selection.presentation_statistics

    @property
    def candidate_forecasts(self) -> tuple[CandidateForecast, ...]:
        if self._nr_health_decision is not None:
            return ()
        return self._decision_selection.candidate_forecasts

    @property
    def candidate_forecast_selection_id(self) -> str:
        if self._nr_health_decision is not None:
            return ""
        return self._decision_selection.candidate_forecast_selection_id

    def completed_learning_cycle_ring(self) -> tuple[dict[str, Any], ...]:
        return self._decision_selection.completed_learning_cycle_ring()

    def adviser_chain_snapshot(self) -> dict[str, Any]:
        """Return diagnostic evidence from the decision coordinator."""

        if self._nr_health_decision is not None:
            decision, opportunity = self._nr_health_decision, self._nr_health_opportunity
            return {"schema": "NewHorizonAdviserChainSnapshot.v2", "status": "complete",
                "decision_source": "nr_service_health_rule", "selection_reason": decision.reason,
                "selected_opportunity_id": decision.selected_opportunity_id,
                "selected_brain_eligibility": decision.eligibility.to_payload(),
                "selected_contributions": [], "candidates": [] if opportunity is None else [{
                    "opportunity_id": opportunity.opportunity_id, "reason": opportunity.reason,
                    "target_signature": opportunity.target, "opportunity_discovered": opportunity.eligible,
                    "bandit": {"evidence_state": "not_used_for_health_recovery", "service_value": None},
                    "crystal_ball": {"status": "insufficient_evidence", "expected_service_value": None}}]}

        return self._decision_selection.adviser_chain_snapshot()

    def history_population_status(self) -> dict[str, Any]:
        """Return the one-owner population coherence proof."""

        return self._decision_selection.history_population_status()

    def bind_context(
        self,
        context: IntelligenceContext,
        assessment_history: RefereeAssessmentHistorySnapshot,
        decision_history: DecisionLifecycleHistorySnapshot,
    ) -> str:
        return self._decision_selection.bind_context(
            context,
            assessment_history,
            decision_history,
        )

    def outage_recovery_actionable(self, context: IntelligenceContext) -> bool:
        """Return whether an incomplete frame has enough rescue evidence."""

        if not emergency_pool_rescue_allowed(context):
            return False
        allowed_missing = {
            "radio.current_target",
            "radio.serving_band",
            "radio.network_type",
            "rf.cell_id",
            "rf.pci",
            "rf.earfcn_dl",
            "surface.latency",
            "radio.ca_active",
            "radio.carrier_count",
            "rf.rsrp_dbm",
            "rf.rsrq_db",
            "rf.sinr_db",
            "traffic.dl_mbps",
            "traffic.ul_mbps",
            "traffic.has_traffic",
            "surface.quality",
        }
        for missing in context.missing_required:
            normalized = missing.removeprefix("retained:")
            if normalized not in allowed_missing:
                return False
        return any(
            item.eligible and item.opportunity_id.startswith("emergency_pool_rescue:")
            for item in (
                _bind_action_capability(context, opportunity)
                for opportunity in self._opportunities(context)
            )
        )

    def admit_proof(self, _proof: RefereeProof) -> bool:
        raise RuntimeError("new_horizon_reasoning_assessment_admission_required")

    def selected_proposal_evidence(
        self,
        *,
        proposal: BrainCommandProposal,
        context: IntelligenceContext,
    ) -> dict[str, Any]:
        target = core_command_target_from_payload(proposal.action_target.target.to_mapping())
        if isinstance(target, CoreRadioModeTarget):
            return {"schema": DECISION_LEDGER_SCHEMA, "proposal_id": proposal.proposal_id,
                    "source_signature": "MODE=" + target.expected_mode,
                    "learning_context": context_learning_components(context),
                    "forecast": {"status": "insufficient_evidence", "match_tier": "unavailable",
                                 "reason": "nr_health_rule_not_learned_gain", "evidence_assessments": 0},
                    "predicted_grades": proposal.predicted_grades.to_payload(),
                    "explanation": "Guarded NSA service recovery; no learned improvement is predicted.",
                    "contributions": [],
                    "decision_frame": {"root_fingerprint": context.root_fingerprint,
                                       "matrix_sequence": context.matrix_identity.matrix_sequence,
                                       "accepted_at": context.accepted_at, "nr_service_recovery": True}}
        return self._decision_selection.selected_proposal_evidence(
            proposal=proposal,
            context=context,
        )

    def proposal_semantic_key(
        self,
        *,
        proposal: BrainCommandProposal,
        context: IntelligenceContext,
    ) -> str:
        """Return proposal identity; Advisory owns the mutable lifecycle."""

        if proposal.action_target.command_kind == "RADIO_MODE":
            mutation = context.surface("mutation").value
            lease = mutation.get("sources", {}).get("radio_mode_lease", {})
            return "|".join(("nr-service", context.identity_code,
                context_continuity_key(context), str(lease.get("root_id") or "initial"),
                proposal.action_target.target_signature))
        return self._decision_selection.proposal_semantic_key(
            context,
            proposal,
        )

    def build(
        self,
        context: IntelligenceContext,
        contexts: Iterable[IntelligenceContext],
        *,
        explore_opportunity: AdvisoryOpportunity | None = None,
        context_final_mature: bool = False,
    ) -> tuple[
        tuple[AdvisoryOpportunity, ...],
        AdvisoryForecast,
        AdvisoryDecision,
        EvidenceReadiness,
    ]:
        self._nr_health_decision = self._nr_health_opportunity = None
        self._decision_selection.require_bound_context(context)
        opportunities = self._opportunities(context)
        if explore_opportunity is not None:
            opportunities = (*opportunities, explore_opportunity)
        opportunities = tuple(
            _bind_action_capability(context, item) for item in opportunities
        )
        opportunities = tuple(
            _bind_physical_eligibility(self._history, context, item)
            for item in opportunities
        )
        decision = self._decision_selection.decide(
            context,
            tuple(contexts),
            opportunities,
            radio_stability_hold_remaining_s=(_radio_stability_hold_remaining(context)),
            required_opportunity_id=(
                explore_opportunity.opportunity_id
                if explore_opportunity is not None
                else ""
            ),
            context_final_mature=context_final_mature,
        )
        selected_forecast = self._decision_selection.selected_forecast
        if selected_forecast is None:
            raise RuntimeError("new_horizon_selected_forecast_missing")
        forecast, evidence_readiness = _forecast_surfaces(selected_forecast)
        return opportunities, forecast, decision, evidence_readiness

    def build_non_actionable(
        self,
        context: IntelligenceContext,
        *,
        reason: str = "current_context_incomplete",
        explanation: str = (
            "Keep the current connection while live facts are incomplete."
        ),
    ) -> tuple[
        tuple[AdvisoryOpportunity, ...],
        AdvisoryForecast,
        AdvisoryDecision,
        EvidenceReadiness,
    ]:
        """Build current data when no action evidence is available."""

        if type(reason) is not str or not reason.strip():
            raise ValueError("new_horizon_non_actionable_reason_required")
        self._nr_health_decision = self._nr_health_opportunity = None
        if type(explanation) is not str or not explanation.strip():
            raise ValueError("new_horizon_non_actionable_explanation_required")
        self._decision_selection.select_do_nothing(context)
        missing = ",".join(context.missing_required) or "current_truth_incomplete"
        opportunity = AdvisoryOpportunity(
            opportunity_id="do_nothing",
            kind="policy",
            target="retain_current_configuration",
            action_target=None,
            reason=reason,
            eligible=True,
            uncertainty=0.0,
            disruption_cost=0.0,
            evidence_refs=(),
        )
        forecast = AdvisoryForecast(
            status="insufficient_evidence",
            match_tier="unavailable",
            expected_eci_points=None,
            expected_latency_ms=None,
            expected_jitter_ms=None,
            expected_cqb=None,
            expected_internet_availability_pct=None,
            confidence=0.0,
            uncertainty=1.0,
            evidence_samples=0,
            reason=(
                "current_context_incomplete:" + missing
                if reason == "current_context_incomplete"
                else reason
            ),
        )
        decision = AdvisoryDecision(
            selected_opportunity_id="do_nothing",
            reason=reason + "_no_action",
            explanation=explanation,
            predicted_grades=DecisionGradeVector(
                components=tuple(
                    DecisionGradeComponent(
                        dimension=dimension,
                        grade=(-3 if dimension == "rules_safety" else None),
                        confidence=(1.0 if dimension == "rules_safety" else 0.0),
                        reason=(
                            "current_context_incomplete"
                            if dimension == "rules_safety"
                            else "current_context_evidence_unavailable"
                        ),
                    )
                    for dimension in GRADE_DIMENSIONS
                ),
                basis="prediction",
            ),
            eligibility=BrainDecisionEligibility(
                eligible=True,
                rules_clear=True,
                reason="non_mutating_incomplete_context_hold",
            ),
        )
        readiness = EvidenceReadiness(
            state="insufficient_evidence",
            proposal="wait_for_complete_current_owner_truth",
            confidence=0.0,
            evidence_samples=0,
        )
        return (opportunity,), forecast, decision, readiness

    def build_nr_service_recovery(self, context: IntelligenceContext, recovery: NrRecoveryDecision, *, now: float):
        """Publish one safety decision without fabricating AQ/forecast evidence."""
        opportunities, forecast, hold, readiness = self.build_non_actionable(
            context, reason=recovery.reason, explanation={
                "nr_mode_lte_recovery_hold": "Keep LTE service while the NR retry timer runs.",
                "nr_mode_waiting_for_stable_lte_service": "Wait for stable LTE service before a controlled NR trial.",
                "nr_mode_command_unconfirmed_manual_review": "The radio-mode result is unconfirmed; automatic retries are suspended.",
            }.get(recovery.reason, "Observe the current NR recovery cycle."))
        target = recovery.target
        if target is None:
            self._nr_health_decision = hold
            return opportunities, forecast, hold, readiness, None
        signature = core_radio_mode_signature(target)
        evidence = ActionTargetEvidence("radio_mode", "RADIO_MODE", signature,
            CoreFrozenPayload.from_mapping(core_command_target_payload(target)))
        opportunity_id = "nr-service:" + signature
        opportunity = AdvisoryOpportunity(opportunity_id, "advisory", signature, evidence,
            recovery.reason, True, 0.0, 1.0, recovery.evidence_refs)
        eligibility = BrainDecisionEligibility(True, True, "fresh_nr_health_evidence_and_guarded_mode_target")
        grades = DecisionGradeVector(tuple(DecisionGradeComponent(dimension,
            None if dimension == "service_outcome" else 3.0,
            0.0 if dimension == "service_outcome" else 1.0,
            "no_learned_gain_claim" if dimension == "service_outcome" else "guarded_recovery")
            for dimension in GRADE_DIMENSIONS))
        decision = AdvisoryDecision(opportunity_id, recovery.reason,
            "Use LTE temporarily after confirmed NSA service loss." if target.mode == "lte_only" else
            "Try the configured 5G bands at the start of this session." if recovery.reason == "nr_mode_startup_mixed_trial" else
            "Restore the previous mixed mode for a controlled NR trial.", grades, eligibility)
        self._nr_health_decision, self._nr_health_opportunity = decision, opportunity
        material = context.root_fingerprint + signature
        proposal = BrainCommandProposal(proposal_id="brain-proposal:" + hashlib.sha256(material.encode()).hexdigest()[:32],
            command_context=context.command_context, profile_id=str(_fact(context, "identity.profile_id")),
            opportunity_id=opportunity_id, execution_intent="nr_service_lte_fallback" if target.mode == "lte_only" else "nr_service_trial_restore",
            action_target=evidence, confidence=1.0, evidence_refs=recovery.evidence_refs,
            predicted_grades=grades, eligibility=eligibility, verification_requirement="fresh_radio_mode_and_preserved_restrictions",
            rollback_requirement="recorded_mode_lease_then_controlled_mixed_trial", capability_reference="radio_mode",
            created_at=float(now), expires_at=float(now) + 10.0)
        return (*opportunities, opportunity), forecast, decision, readiness, proposal

    def build_proposal(
        self,
        context: IntelligenceContext,
        opportunities: tuple[AdvisoryOpportunity, ...],
        decision: AdvisoryDecision,
        *,
        now: float,
    ) -> BrainCommandProposal | None:
        control = context.product_control.to_mapping()
        opportunity = next(
            (
                item
                for item in opportunities
                if item.opportunity_id == decision.selected_opportunity_id
            ),
            None,
        )
        emergency_pool_rescue = bool(
            opportunity is not None
            and opportunity.opportunity_id.startswith("emergency_pool_rescue:")
        )
        if (
            control.get("control_mode") != INTELLIGENCE_CONTROL_AUTOPILOT
            or control.get("control_available") is not True
            or not decision.eligibility.eligible
            or not decision.eligibility.rules_clear
            or _emergency_recovery_hold_remaining(context) > 0.0
            or (
                _radio_stability_hold_remaining(context) > 0.0
                and not emergency_pool_rescue
            )
        ):
            return None
        profile_id = _fact(context, "identity.profile_id")
        if type(profile_id) is not str or not profile_id.strip():
            return None
        required_current_paths = (
            "identity.provider_name",
        ) + (
            ()
            if emergency_pool_rescue
            else (
                "radio.network_type",
                "rf.cell_id",
                "rf.earfcn_dl",
                "rf.pci",
            )
        )
        if any(_fact(context, path) is None for path in required_current_paths):
            return None
        if opportunity is None or opportunity.action_target is None:
            return None
        target = opportunity.action_target
        command_target = core_command_target_from_payload(target.target.to_mapping())
        if not isinstance(
            command_target,
            (CoreBandSetTarget, CorePBandTarget),
        ):
            return None
        if not emergency_pool_rescue and _radio_command_matches_current(context, command_target):
            return None
        evidence = self._decision_selection.evidence_for(context, opportunity)
        if evidence is None:
            return None
        posterior = evidence.posterior
        forecast = self._decision_selection.selected_forecast
        if forecast is None:
            raise RuntimeError("new_horizon_selected_forecast_missing")
        refs = tuple(
            dict.fromkeys(
                (
                    *opportunity.evidence_refs,
                    *evidence.positive_proof_refs,
                    forecast.forecast_id,
                )
            )
        )
        material = "|".join(
            (
                self._decision_selection.command_intention_key(
                    context,
                    opportunity,
                ),
                evidence.arm_id,
                format(context.accepted_at, ".6f"),
            )
        )
        proposal_id = (
            "brain-proposal:"
            + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        )
        confidence = (
            min(0.95, posterior.pulls / 10.0)
            if posterior.pulls
            else 0.55
            if opportunity.strategy_service_grade is not None
            else 0.25
        )
        return BrainCommandProposal(
            proposal_id=proposal_id,
            command_context=context.command_context,
            profile_id=profile_id,
            opportunity_id=opportunity.opportunity_id,
            execution_intent=_execution_intent(opportunity.opportunity_id),
            action_target=target,
            confidence=confidence,
            evidence_refs=tuple(sorted(refs)),
            predicted_grades=decision.predicted_grades,
            eligibility=decision.eligibility,
            verification_requirement="exact_radio_target_readback_and_fresh_sample",
            rollback_requirement="core_terminal_outcome_then_referee_regrade",
            capability_reference=(
                "band_selection" if target.command_kind == "BAND_SELECTION" else "pband"
            ),
            created_at=context.accepted_at,
            expires_at=context.accepted_at + 10.0,
        )

    def _opportunities(
        self,
        context: IntelligenceContext,
    ) -> tuple[AdvisoryOpportunity, ...]:
        values = [
            AdvisoryOpportunity(
                opportunity_id="do_nothing",
                kind="policy",
                target="retain_current_configuration",
                action_target=None,
                reason="mandatory_safe_alternative",
                eligible=True,
                uncertainty=0.0,
                disruption_cost=0.0,
                evidence_refs=(),
            )
        ]
        connected = _fact(context, "health.radio_connected") is True
        ca_active = _fact(context, "radio.ca_active") is True
        congestion = _quality(context, "congestion_pct")
        provider_view = context.foundation_band_policy.to_mapping()
        provider_policy = provider_view.get("policy")
        provider_plmn = str(_fact(context, "identity.provider_plmn") or "")
        if provider_view.get("disposition") != "available":
            return tuple(values)
        if isinstance(provider_policy, Mapping) and "starting_priors" in provider_policy:
            raise ValueError("new_horizon_fixed_band_prior_forbidden")
        if (not isinstance(provider_policy, Mapping)
                or provider_policy.get("owner") != "54guc.foundation.band_auto_policy"
                or provider_policy.get("band_auto_provider_plmn") != provider_plmn
                or not isinstance(provider_policy.get("observed_history_allowed"), bool)):
            raise ValueError("new_horizon_provider_policy_contract_invalid")
        session_bands = _positive_bands(_fact(context, "capability.session_lte_bands"))
        pband_metadata = _runtime_action_metadata(context, "pband") or {}
        pband_trust = str(pband_metadata.get("overlay_trust") or "").lower()
        pband_action_available = bool(
            pband_metadata.get("write_allowed") is True
            and pband_trust in {"supported", "degraded"}
        )
        staged_best_effort = _staged_pool_strategy(context)
        autonomous_pband_strategy_available = bool(
            (pband_action_available and pband_trust == "supported")
            or staged_best_effort
        )
        current_pool = _positive_bands(_fact(context, "radio.configured_band_pool"))
        current_pband = _fact(context, "radio.serving_band")
        capability_id = str(_fact(context, "capability.session_capability_id") or "")
        policy_bands = (
            _positive_bands(provider_policy.get("bands"))
            if isinstance(provider_policy, Mapping)
            else ()
        )
        candidates: dict[tuple[str, str], AdvisoryOpportunity] = {}
        companion_support: dict[int, float] = {}
        companion_population = ""
        if staged_best_effort and len(current_pool) == 1 and context_discloses_ca(context):
            snapshot = self._assessment_history.current(context_continuity_key(context), now=context.accepted_at)
            companion_support = snapshot.local_band_use(now=context.accepted_at)
            companion_population = snapshot.population_id
        learned_destinations = self._decision_selection.learned_radio_destinations()
        session_set = set(session_bands)
        recovery_pool = tuple(band for band in policy_bands if band in session_set)
        policy_set = set(policy_bands)
        if isinstance(provider_policy, Mapping) and provider_policy.get("observed_history_allowed") is True:
            # Actual retained primary-band visits are network observations.
            # A configured CA mask or typed suggestion is not carrier proof.
            for learned in learned_destinations:
                observed = core_command_target_from_payload(learned.target.target.to_mapping())
                if isinstance(observed, CorePBandTarget):
                    policy_set.add(observed.pband)
        policy_bands = tuple(sorted(policy_set))
        legal_pool = tuple(band for band in policy_bands if band in session_set)
        # Only Foundation-authorized evidence supplies destinations. A saved
        # CA mask alone must not become a second band-availability authority.
        normal_available_pool = legal_pool
        if (emergency_pool_rescue_allowed(context) and recovery_pool and capability_id
                and provider_view.get("disposition") == "available"
                and isinstance(provider_policy, Mapping)
                and provider_policy.get("band_auto_provider_plmn") == provider_plmn):
            target = _action_target(CoreBandSetTarget(bands=recovery_pool))
            candidates[(target.action_family, target.target_signature)] = AdvisoryOpportunity(
                opportunity_id="emergency_pool_rescue:all_allowed",
                kind="advisory", target=target.target_signature, action_target=target,
                reason="restore_all_allowed_bands", eligible=True,
                uncertainty=1.0, disruption_cost=0.30,
                evidence_refs=("modem-live-session-capability:" + capability_id,
                               "foundation-band-policy:" + str(provider_policy.get("band_auto_policy_revision") or "")),
            )
        if (
            connected
            and autonomous_pband_strategy_available
            and provider_view.get("disposition") == "available"
            and isinstance(provider_policy, Mapping)
            and provider_policy.get("band_auto_provider_plmn") == provider_plmn
            and session_bands
            and capability_id
            and len(legal_pool) >= 2
        ):
            revision = str(provider_policy.get("band_auto_policy_revision") or "")
            candidate_specs: list[
                tuple[
                    CoreBandSetTarget | CorePBandTarget,
                    str,
                    str,
                ]
            ] = []
            if staged_best_effort and len(current_pool) == 1:
                anchor = current_pool[0]
                if type(current_pband) is int and current_pband == anchor:
                    companions = tuple(
                        band for band in legal_pool if band != anchor
                    )
                    companion_count = min(2, len(companions))
                    for selected_companions in combinations(
                        companions,
                        companion_count,
                    ):
                        expanded_pool = tuple(
                            sorted((anchor, *selected_companions))
                        )
                        candidate_specs.append(
                            (
                                CoreBandSetTarget(bands=expanded_pool),
                                "best_effort_expand:"
                                + "+".join(
                                    f"B{band}" for band in selected_companions
                                ),
                                "best_effort_add_companions",
                            )
                        )
            elif staged_best_effort:
                candidate_specs.extend(
                    (
                        (_normal_primary_target(band, current_pool=current_pool,
                                                available_pool=legal_pool)
                         if pband_action_available
                         else CoreBandSetTarget(bands=legal_pool)),
                        f"best_effort_anchor:B{band}",
                        "best_effort_select_anchor_keep_pool",
                    )
                    for band in legal_pool
                    if type(current_pband) is not int
                    or band != current_pband
                )
                # Let ordinary evidence/margin/hold admission assess adding a third
                # legal option; never discard either currently allowed band.
                if len(current_pool) == 2 and set(current_pool) <= set(legal_pool):
                    candidate_specs.extend(
                        (CoreBandSetTarget(bands=tuple(sorted((*current_pool, band)))),
                         f"best_effort_pool_option:B{band}",
                         "best_effort_broader_pool_option")
                        for band in legal_pool if band not in current_pool
                    )
            else:
                candidate_specs.extend(
                    (
                        _normal_primary_target(band, current_pool=current_pool,
                                               available_pool=normal_available_pool),
                        f"lte_anchor:B{band}",
                        "",
                    )
                    for band in legal_pool
                )
            for command_target, opportunity_id, staged_reason in (
                candidate_specs
            ):
                target = _action_target(command_target)
                command = core_command_target_from_payload(target.target.to_mapping())
                if not isinstance(command, (CoreBandSetTarget, CorePBandTarget)):
                    raise RuntimeError("new_horizon_provider_lte_target_invalid")
                if _radio_command_matches_current(context, command):
                    continue
                posterior = self._decision_selection.posterior_for_target(
                    context,
                    action_family=target.action_family,
                    target_signature=target.target_signature,
                )
                reason = (
                    staged_reason
                    if staged_reason
                    else "owner_congestion_above_exploration_threshold"
                    if congestion is not None and congestion >= 35.0
                    else "owner_reports_ca_inactive"
                    if not ca_active
                    else "bounded_idle_anchor_exploration"
                )
                opportunity = AdvisoryOpportunity(
                    opportunity_id=opportunity_id,
                    kind="advisory",
                    target=target.target_signature,
                    action_target=target,
                    reason=reason,
                    eligible=True,
                    uncertainty=(
                        min(
                            1.0,
                            math.sqrt(posterior.variance + 1.0)
                            / math.sqrt(posterior.pulls + 1.0),
                        )
                        if posterior.pulls
                        else 1.0
                    ),
                    disruption_cost=0.40,
                    companion_support=tuple(
                        (band, companion_support[band]) for band in command.bands
                        if band != current_pband and band in companion_support
                    ) if isinstance(command, CoreBandSetTarget) and staged_reason == "best_effort_add_companions" else (),
                    evidence_refs=(
                        f"foundation-band-policy:{revision}:{provider_plmn}",
                        f"modem-live-session-capability:{capability_id}",
                        *(("local-band-use:" + companion_population,)
                          if staged_reason == "best_effort_add_companions" and companion_population else ()),
                        *(
                            (
                                "best-effort-actuation:"
                                + ("degraded" if pband_action_available else "unsupported")
                                + "-pband-supported-pool",
                            )
                            if staged_best_effort
                            else ()
                        ),
                    ),
                )
                candidates[(target.action_family, target.target_signature)] = (
                    opportunity
                )
        normal_counts: dict[str, int] = {}
        for learned in learned_destinations:
            learned_command = core_command_target_from_payload(
                learned.target.target.to_mapping()
            )
            if not isinstance(learned_command, CorePBandTarget):
                raise ValueError("new_horizon_learned_radio_target_invalid")
            if (
                not connected
                or not autonomous_pband_strategy_available
                or staged_best_effort
                or not capability_id
                or len(normal_available_pool) < 2
                or learned_command.pband not in normal_available_pool
            ):
                continue
            learned_command = _normal_primary_target(
                learned_command.pband, current_pool=current_pool,
                available_pool=normal_available_pool,
            )
            if _radio_command_matches_current(context, learned_command):
                continue
            learned_target = _action_target(learned_command)
            key = (
                learned_target.action_family,
                learned_target.target_signature,
            )
            provider = candidates.get(key)
            normal_counts[learned_target.target_signature] = (
                normal_counts.get(learned_target.target_signature, 0) + learned.assessment_count
            )
            refs = tuple(
                dict.fromkeys(
                    (
                        *(provider.evidence_refs if provider is not None else ()),
                        "modem-live-session-capability:" + capability_id,
                        *learned.proof_refs,
                    )
                )
            )
            candidates[key] = AdvisoryOpportunity(
                opportunity_id=("learned_lte:" + learned_target.target_signature),
                kind="advisory",
                target=learned_target.target_signature,
                action_target=learned_target,
                reason="qualified_primary_destination_history",
                eligible=True,
                uncertainty=min(
                    1.0,
                    1.0 / math.sqrt(normal_counts[learned_target.target_signature] + 1.0),
                ),
                disruption_cost=0.30,
                evidence_refs=refs,
            )
        values.extend(candidates[key] for key in sorted(candidates))
        return tuple(values)


__all__ = (
    "AdvisoryReasoning",
    "NEW_HORIZON_EXECUTION_INTENTS",
    "OPPORTUNITY_DISPOSITIONS",
)
