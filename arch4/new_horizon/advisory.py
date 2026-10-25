"""Coordinate New Horizon context, short-term memory, and its owners."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import hashlib
import json
from threading import RLock
from pathlib import Path
from time import localtime, strftime, time
from typing import Any, Mapping

from arch4.action_origin import is_brain_decision_origin
from arch4.core_contracts.connected_settlement import CONNECTION_SETTLING_REASON
from arch4.controller_frames import RadioTransitionFrame
from arch4.core_contracts.intelligence_boundary import (
    SIGNAL_BASELINE_FORMULA_GENERATION,
    IntelligenceMatrixObservation,
    IntelligenceOperationalConstraintView,
    IntelligenceProposalDispatchResult,
    IntelligenceSignalBaselineSnapshot,
    intelligence_signal_scope_key,
)
from arch4.core_contracts.outcomes import CoreCommandOutcomeRecord
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFact
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CorePBandTarget,
    core_lte_band_signature,
    core_lte_pband_signature,
    core_command_target_payload,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.intelligence_proposals import BrainCommandProposal
from arch4.intelligence_presentation_contract import BAND_CONTEXT_SCHEMA
from arch4.new_horizon.contracts import (
    AdvisoryPublication,
    DECISION_LEDGER_KIND,
    DECISION_LEDGER_SCHEMA,
    DECISION_TERMINAL_KIND,
    DECISION_TERMINAL_SCHEMA,
    IntelligenceContext,
    IntelligenceSurface,
    RefereeProof,
    RefereeSubject,
    RefereeTarget,
    PROPOSAL_RESULT_EVIDENCE_SCHEMA,
    PROPOSAL_RESULT_HISTORY_KIND,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.continuity import CONTINUITY_PATHS, resolve_continuity, bind_device_history, learning_device_key, owns_initial_sim_history
from arch4.new_horizon.decision_history import DecisionLifecycleHistoryOwner
from arch4.new_horizon.decision import proposal_source_state
from arch4.new_horizon.intelligence_impact import (
    INTELLIGENCE_IMPACT_WINDOW_S,
    build_intelligence_impact,
    empty_intelligence_impact,
    record_hold_attribution,
)
from arch4.new_horizon.learning import CandidateForecast
from arch4.new_horizon.explore import ExploreCoordinator, ExploreProbeRequest
from arch4.new_horizon.proof import (
    DECISION_LIFECYCLE_TIMEOUT_S,
    REFEREE_BASELINE_RESERVATION_KIND,
    REFEREE_PROOF_HISTORY_KIND,
    ProofReferee,
    RefereeAssessmentHistoryOwner,
    context_action_lifecycle_key,
    context_continuity_key,
    context_continuity_observation_state,
    context_learning_components,
    context_learning_key,
    context_scope_key,
    current_context_target,
    referee_assessment_is_complete,
    referee_sample_metrics,
)
from arch4.new_horizon.performance import (
    band_physical_action_audit,
    band_presentation,
    record_internet_pause,
    record_performance_evidence,
    record_physical_result,
    record_target_retention,
    update_band_observation_maxima,
)
from arch4.new_horizon.reasoning import AdvisoryReasoning
from arch4.new_horizon.nr_service_recovery import NrServiceRecovery
from arch4.core_contracts.command_targets import CoreRadioModeTarget


_FACT_PATHS = (
    "health.radio_connected",
    "identity.backend",
    "identity.profile_id",
    "identity.provider_name",
    "identity.provider_plmn",
    "identity.sim_network",
    "capability.runtime_capabilities",
    "capability.session_capability_id",
    "capability.session_lte_bands",
    "radio.ca_active",
    "radio.carrier_count",
    "radio.configured_band_pool",
    "radio.configured_band_pool_signature",
    "radio.network_type",
    "radio.nr5g_active",
    "radio.nr_arfcn",
    "radio.nr_band",
    "radio.nr_cell_id",
    "radio.nr_pci",
    "radio.nr_sinr_db",
    "radio.nr_rsrp_dbm",
    "radio.nr_rsrq_db",
    "slow.radio_mode",
    "radio.serving_band",
    "slow.dl_bandwidth_mhz",
    "rf.rsrp_dbm",
    "rf.rsrq_db",
    "rf.rssi_dbm",
    "rf.sinr_db",
    "rf.cell_id",
    "rf.earfcn_dl",
    "rf.pci",
    "traffic.dl_mbps",
    "traffic.has_traffic",
    "traffic.ul_mbps",
)


@dataclass(frozen=True, slots=True)
class ProposalEmissionReservation:
    """Advisory-owned result of one proposal reservation attempt."""

    status: str
    proposal_id: str

    def __post_init__(self) -> None:
        if self.status not in {
            "reserved",
            "already_pending",
            "already_handled",
        }:
            raise ValueError("new_horizon_proposal_reservation_status_invalid")
        if not isinstance(self.proposal_id, str) or not self.proposal_id:
            raise ValueError("new_horizon_proposal_reservation_id_required")

    @property
    def reserved(self) -> bool:
        return self.status == "reserved"


_COMMON_REQUIRED_AVAILABLE = frozenset(
    {
        "health.radio_connected",
        "identity.backend",
        "identity.profile_id",
        "radio.network_type",
        "rf.rsrp_dbm",
        "rf.rsrq_db",
        "rf.sinr_db",
        "traffic.has_traffic",
    }
)
_LTE_REQUIRED_AVAILABLE = frozenset(
    {
        "radio.ca_active",
        "radio.carrier_count",
        "radio.configured_band_pool_signature",
        "radio.serving_band",
    }
)
_NR_REQUIRED_AVAILABLE = frozenset(
    {
        "radio.nr_arfcn",
        "radio.nr_band",
        "radio.nr_pci",
    }
)
_ROOT_SURFACES = ("quality", "latency", "ca")

# Diagnostic-only copies. Keep the prefix so these fields cannot become an
# alternative source of grading or forecasting inputs. History delta-codes
# each field separately; do not copy whole Core/Matrix/GUI frames here.
_OBSERVATION_DIAGNOSTIC_FIELDS = {
    "quality": (
        "cq_cell", "cq_cell_valid", "cq_cell_source",
        "cqb_cell", "cqb_cell_valid", "cqb_cell_source",
        "ri", "ri_valid", "ri_source",
        "ri_stability_pct", "ri_stability_valid",
        "rsrq_baseline", "rsrq_baseline_valid",
        "congestion_rsrq_gap_db", "congestion_rsrq_pct",
        "congestion_rsrq_reason", "congestion_trusted_rsrq",
        "congestion_rtt_base_pct", "congestion_trusted_rtt",
        "congestion_active_jitter_pct", "congestion_trusted_active_jitter",
        "congestion_weights", "congestion_pct_valid", "congestion_source",
    ),
    "latency": (
        "rtt_sliding_avg", "rtt_baseline", "rtt_effective_baseline",
        "rtt_delta", "rtt_delta_valid", "rtt_delta_block_reason",
        "rtt_dl_jitter_trimmed_ms", "rtt_dl_jitter_valid",
        "rtt_dl_jitter_reason", "rtt_dl_jitter_baseline_ms",
        "rtt_dl_jitter_delta_ms", "rtt_dl_jitter_bucket",
        "rtt_active_jitter_mad_ms", "rtt_active_jitter_step_p95_ms",
        "rtt_active_jitter_samples", "rtt_active_jitter_valid",
    ),
}
_REQUIRED_SURFACES = frozenset({"quality", "latency"})
_FRESH_EVIDENCE_FACTS = frozenset(
    {
        # Radio connected is current state, like PBAND, not a new measurement.
        # Matrix can retain it across a traffic-only refresh. Availability and
        # Core surface validity still apply; command readback is separate.
        "rf.rsrp_dbm",
        "rf.rsrq_db",
        "rf.rssi_dbm",
        "rf.sinr_db",
        "traffic.dl_mbps",
        "traffic.has_traffic",
        "traffic.ul_mbps",
    }
)
_MAX_CONTEXTS = 32
_MAX_SUBJECTS = 64


def _required_available_paths(
    facts: tuple[CoreAcceptedCurrentFact, ...],
) -> frozenset[str]:
    """Choose the RAT-specific denominator from the network type."""

    network = next(fact for fact in facts if fact.path == "radio.network_type")
    network_type = (
        str(network.value.get("value") or "")
        if network.disposition == "available"
        else ""
    )
    if network_type == "NR SA (5G)":
        return _COMMON_REQUIRED_AVAILABLE
    if network_type == "NR NSA (5G)":
        return (
            _COMMON_REQUIRED_AVAILABLE
            | _LTE_REQUIRED_AVAILABLE
            | _NR_REQUIRED_AVAILABLE
        )
    return _COMMON_REQUIRED_AVAILABLE | _LTE_REQUIRED_AVAILABLE


def _band_signature(bands: tuple[int, ...]) -> str:
    return core_lte_band_signature(bands)


def _referee_target_from_command(
    record: CoreCommandOutcomeRecord, *, settled_result: bool = False,
) -> RefereeTarget:
    target = record.receipt.action_key.target
    if (settled_result and isinstance(target, CorePBandTarget)
            and record.receipt.proof_ready
            and record.receipt.terminal_status.value == "settled"):
        # Grade the authoritative settled configuration, while the action key
        # and Explore retain the original nonbinding PBAND request.
        actual = record.receipt.as_dict()["receipts"].get("settle", {}).get("pband")
        if type(actual) is int and actual in target.ca_pool:
            target = replace(target, pband=actual)
    payload = core_command_target_payload(target)
    if (
        isinstance(target, CoreBandSetTarget)
        and target.command_kind == "BAND_SELECTION"
    ):
        action_family = "lte_ca_pool"
        signature = _band_signature(target.bands)
    elif isinstance(target, CorePBandTarget):
        action_family = "lte_radio_selection"
        signature = (
            core_lte_pband_signature(target.pband, target.ca_pool)
            if target.ca_pool
            else f"PBAND=B{target.pband}|CA=UNAVAILABLE"
        )
    else:
        action_family = target.command_kind.lower()
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        signature = hashlib.sha256(encoded).hexdigest()
    return RefereeTarget(
        action_family=action_family,
        target_signature=signature,
        target=CoreFrozenPayload.from_mapping(payload),
    )


def _constraint_surface(
    registers: Mapping[str, Any],
    name: str,
) -> IntelligenceSurface:
    raw = registers.get(name)
    if not isinstance(raw, Mapping):
        raise ValueError(f"intelligence_context_register_missing:{name}")
    row = dict(raw)
    value = row.get("value")
    payload = (
        dict(value) if isinstance(value, Mapping) else {"present": False, "value": None}
    )
    return IntelligenceSurface(
        name=name,
        sequence=int(row.get("sequence", 0)),
        valid=row.get("valid") is True,
        stale=row.get("stale") is True,
        value=CoreFrozenPayload.from_mapping(payload),
    )


def _root_surface(
    record: IntelligenceMatrixObservation,
    name: str,
) -> IntelligenceSurface:
    identity = record.root.matrix_identity
    available = record.constraints.core_snapshot.current_health.allows_measurement(
        record.root.commit.matrix_source_time
    )
    for write in record.root.register_transaction.writes:
        if write.name == name:
            return IntelligenceSurface(
                name=name,
                sequence=identity.matrix_sequence,
                valid=write.valid and available,
                stale=write.stale or not available,
                value=write.value,
            )
    raise ValueError(f"intelligence_context_root_surface_missing:{name}")


def _reachability_surface(
    constraints: IntelligenceOperationalConstraintView,
) -> IntelligenceSurface:
    """Adapt Core's current Internet truth without recalculating it."""

    view = constraints.core_reachability
    return IntelligenceSurface(
        name="reachability",
        sequence=view.register_sequence,
        valid=True,
        stale=False,
        value=CoreFrozenPayload.from_mapping(view.as_dict()),
    )


def _reachability_evidence_surface(
    constraints: IntelligenceOperationalConstraintView,
) -> IntelligenceSurface:
    """Adapt Core's bounded evidence without becoming another timeline."""

    view = constraints.core_reachability_evidence
    return IntelligenceSurface(
        name="reachability_evidence",
        sequence=view.register_sequence,
        valid=True,
        stale=False,
        value=CoreFrozenPayload.from_mapping(view.as_dict()),
    )


def _emergency_reachability_surface(
    constraints: IntelligenceOperationalConstraintView,
) -> IntelligenceSurface:
    """Adapt Core's verified emergency state without recalculating it."""

    view = constraints.core_emergency_reachability
    return IntelligenceSurface(
        name="emergency_reachability",
        sequence=view.verification_round_count,
        valid=True,
        stale=False,
        value=CoreFrozenPayload.from_mapping(view.as_dict()),
    )


class ContextIntake:
    """Decode CGF customer publications for New Horizon."""

    def build(
        self,
        record: IntelligenceMatrixObservation,
        *,
        previous: IntelligenceContext | None,
    ) -> IntelligenceContext:
        if not isinstance(record, IntelligenceMatrixObservation):
            raise TypeError("intelligence_context_matrix_observation_required")
        constraints = record.constraints
        if not isinstance(constraints, IntelligenceOperationalConstraintView):
            raise TypeError("intelligence_context_constraint_view_required")
        root = record.root
        identity = root.matrix_identity
        if (
            constraints.matrix_identity != identity
            or constraints.accepted_root_fingerprint != root.root_fingerprint
            or constraints.core_snapshot.matrix_identity != identity
            or constraints.core_snapshot.accepted_root_fingerprint
            != root.root_fingerprint
        ):
            raise ValueError("intelligence_context_owner_root_mismatch")
        if (
            previous is not None
            and previous.matrix_identity.namespace == identity.namespace
        ):
            if identity.matrix_sequence <= previous.matrix_identity.matrix_sequence:
                raise ValueError("intelligence_context_generation_not_newer")
            sequence_gap = max(
                0,
                identity.matrix_sequence - previous.matrix_identity.matrix_sequence - 1,
            )
        else:
            sequence_gap = 0
        accepted = root.current_facts
        # A frozen older catalog has no mode-control observation. Preserve its
        # original facts; absence disables only the new rule, never synthesize it.
        facts = tuple(accepted.exact(path) for path in _FACT_PATHS
                      if path != "slow.radio_mode" or path in accepted.declared_paths)
        provider_view = constraints.foundation_band_policy.to_mapping()
        provider_policy = provider_view.get("policy")
        if provider_view.get("disposition") == "available":
            if (
                not isinstance(provider_policy, Mapping)
                or provider_policy.get("schema") != "FoundationProviderBandProfile.v1"
                or provider_policy.get("owner") != "54guc.foundation.band_auto_policy"
                or provider_policy.get("rat") != "lte"
            ):
                raise ValueError("intelligence_context_provider_policy_invalid")
            provider_fact = next(
                fact for fact in facts if fact.path == "identity.provider_plmn"
            )
            provider_value = (
                str(provider_fact.value.get("value") or "")
                if provider_fact.disposition == "available"
                else ""
            )
            if (
                not provider_value
                or provider_policy.get("band_auto_provider_plmn") != provider_value
            ):
                raise ValueError("intelligence_context_provider_policy_scope_mismatch")
        required_available = _required_available_paths(facts)
        network_fact = next(fact for fact in facts if fact.path == "radio.network_type")
        network_type = (
            str(network_fact.value.get("value") or "")
            if network_fact.disposition == "available"
            else ""
        )
        missing = tuple(
            fact.path
            for fact in facts
            if fact.path in required_available and fact.disposition != "available"
        )
        missing += tuple(
            "retained:" + fact.path
            for fact in facts
            if fact.path in _FRESH_EVIDENCE_FACTS
            and fact.disposition == "available"
            and fact.retention is not None
        )
        if network_type == "NR SA (5G)":
            missing += ("unsupported.nr_sa",)
        registers = {
            name: constraints.core_snapshot.registers.require(name)
            for name in ("mutation", "action_auxiliary")
        }
        surfaces = (
            *(_root_surface(record, name) for name in _ROOT_SURFACES),
            _reachability_surface(constraints),
            _reachability_evidence_surface(constraints),
            _emergency_reachability_surface(constraints),
            _constraint_surface(registers, "mutation"),
            _constraint_surface(registers, "action_auxiliary"),
        )
        missing += tuple(
            f"surface.{surface.name}"
            for surface in surfaces
            if surface.name in _REQUIRED_SURFACES
            and (not surface.valid or surface.stale)
        )
        frame = root.observation_frame
        continuity_values = {
            fact.path: fact.value.get("value") if fact.disposition == "available" else None
            for fact in facts if fact.path in CONTINUITY_PATHS}
        confirmed = (previous.confirmed_continuity.to_mapping()
                     if previous is not None and previous.matrix_identity.namespace == identity.namespace
                     else {})
        sim_fact = accepted.exact("identity.sim_network")
        sim_value = sim_fact.value.get("value") if sim_fact.disposition == "available" else None
        subscription = sim_value.get("subscription_key") if isinstance(sim_value, Mapping) else None
        network_fact = accepted.exact("identity.provider_plmn")
        network = network_fact.value.get("value") if network_fact.disposition == "available" else None
        # A transient loss of registration is still the confirmed session. A
        # changed SIM never borrows the previous SIM's network binding.
        if not network and subscription and subscription == confirmed.get("learning_subscription_key"):
            network = confirmed.get("learning_network_plmn")
        continuity_values.update(learning_subscription_key=subscription, learning_network_plmn=network)
        quality_surface = next(s for s in surfaces if s.name == "quality")
        continuity_values["quality_model"] = quality_surface.value.get("quality_model")
        if not subscription or not network:
            missing += ("identity.sim_network_scope",)
        context = IntelligenceContext(
            matrix_identity=identity,
            root_fingerprint=root.root_fingerprint,
            identity_code=frame.identity_code,
            confirmed_continuity=CoreFrozenPayload.from_mapping(resolve_continuity(
                identity_code=frame.identity_code, values=continuity_values, confirmed=confirmed)),
            sampled_at=frame.sampled_at,
            accepted_at=frame.accepted_at,
            facts=facts,
            surfaces=surfaces,
            foundation_schedule=constraints.foundation_schedule,
            foundation_band_policy=constraints.foundation_band_policy,
            product_control=constraints.product_control,
            command_context=CoreCommandIngressContext.from_payload(
                constraints.core_snapshot.command_context.to_mapping()
            ),
            complete=not missing,
            missing_required=missing,
            sequence_gap=sequence_gap,
        )
        # Physical stages may arrive separately. Keep the raw observation, but
        # use the same target qualification as Referee before calling it complete.
        if network_type != "NR SA (5G)" and current_context_target(context) is None:
            context = replace(context, complete=False,
                              missing_required=(*missing, "radio.current_target"))
        return context

    def refresh_constraints(
        self,
        record: IntelligenceMatrixObservation,
        current: IntelligenceContext,
    ) -> IntelligenceContext:
        """Refresh every legal same-root owner publication together."""

        if not isinstance(record, IntelligenceMatrixObservation):
            raise TypeError("intelligence_context_matrix_observation_required")
        if not isinstance(current, IntelligenceContext):
            raise TypeError("intelligence_context_current_required")
        if (
            record.root.matrix_identity != current.matrix_identity
            or record.root.root_fingerprint != current.root_fingerprint
        ):
            raise ValueError("intelligence_context_constraint_refresh_root_mismatch")
        replacements = {
            **{name: _root_surface(record, name) for name in _ROOT_SURFACES},
            "reachability": _reachability_surface(record.constraints),
            "reachability_evidence": _reachability_evidence_surface(record.constraints),
            "emergency_reachability": _emergency_reachability_surface(
                record.constraints
            ),
        }
        refreshed_names = {
            surface.name for surface in current.surfaces if surface.name in replacements
        }
        if refreshed_names != set(replacements):
            raise RuntimeError("intelligence_context_refresh_surface_missing")
        missing = tuple(item for item in current.missing_required
                        if item not in {"surface." + name for name in _REQUIRED_SURFACES})
        missing += tuple("surface." + name for name, surface in replacements.items()
                         if name in _REQUIRED_SURFACES and (not surface.valid or surface.stale))
        return replace(
            current,
            complete=not missing,
            missing_required=missing,
            surfaces=tuple(
                replacements.get(surface.name, surface) for surface in current.surfaces
            ),
            foundation_schedule=record.constraints.foundation_schedule,
            foundation_band_policy=record.constraints.foundation_band_policy,
            product_control=record.constraints.product_control,
            command_context=CoreCommandIngressContext.from_payload(
                record.constraints.core_snapshot.command_context.to_mapping()
            ),
        )

    def external_referee_subject(
        self,
        record: IntelligenceMatrixObservation,
        context: IntelligenceContext,
        *,
        previous: IntelligenceContext | None,
    ) -> RefereeSubject | None:
        """Adapt an observed network outcome without claiming command success."""

        if not isinstance(record, IntelligenceMatrixObservation):
            raise TypeError("external_referee_matrix_observation_required")
        if not isinstance(context, IntelligenceContext):
            raise TypeError("external_referee_context_required")
        transition = record.root.radio_transition
        if (
            previous is None
            or previous.matrix_identity.namespace != context.matrix_identity.namespace
            or previous.matrix_identity.matrix_sequence
            != context.matrix_identity.matrix_sequence - 1
            or transition.attribution_state != "valid"
            or not transition.fresh
            or transition.source not in {"lower_controller", "lower_controller_during_54guc_command"}
            or not transition.lower_controller_learning_opportunity
            or transition.change_kind in {"", "initial_selection"}
        ):
            return None
        target = current_context_target(context)
        if target is None:
            return None
        before, after = transition.before, transition.after
        if (before.primary_band == after.primary_band
                and before.configured_band_pool != after.configured_band_pool):
            # Explicit action-family adapter; the observed target was qualified
            # above, not reconstructed independently from transitional fields.
            bands = tuple(target.target.get("value")["ca_pool"])
            target = RefereeTarget(
                action_family="lte_ca_pool",
                target_signature=_band_signature(bands),
                target=CoreFrozenPayload.from_mapping(
                    core_command_target_payload(CoreBandSetTarget(bands=bands))),
            )
        learning_context = context_learning_components(previous)
        return RefereeSubject(
            key="radio-transition:" + transition.transition_id,
            evidence_sequence=transition.source_generation,
            matrix_identity=transition.matrix_identity,
            baseline_through_sequence=previous.matrix_identity.matrix_sequence,
            baseline_scope_key=context_scope_key(previous),
            learning_context_key=context_learning_key(previous),
            learning_context=CoreFrozenPayload.from_mapping(learning_context),
            subject_kind="external_radio_change",
            origin=transition.source,
            target=target,
            terminal_status="observed",
            reason=transition.change_kind,
            created_at=previous.accepted_at,
            terminal_at=context.accepted_at,
            proof_ready=True,
            command_id=transition.command_id,
        )


class AdvisorySTM:
    """Bounded working memory. It does not own current CGF values."""

    def __init__(self) -> None:
        self._contexts: deque[IntelligenceContext] = deque(maxlen=_MAX_CONTEXTS)
        self._subjects: deque[RefereeSubject] = deque(maxlen=_MAX_SUBJECTS)

    @property
    def current(self) -> IntelligenceContext | None:
        return self._contexts[-1] if self._contexts else None

    @property
    def contexts(self) -> tuple[IntelligenceContext, ...]:
        return tuple(self._contexts)

    @property
    def subjects(self) -> tuple[RefereeSubject, ...]:
        return tuple(self._subjects)

    def context_for(self, matrix_identity) -> IntelligenceContext | None:
        """Return the retained context for an exact Matrix root."""

        return next(
            (
                item
                for item in reversed(self._contexts)
                if item.matrix_identity == matrix_identity
            ),
            None,
        )

    def admit_context(self, context: IntelligenceContext) -> None:
        if not isinstance(context, IntelligenceContext):
            raise TypeError("advisory_stm_context_required")
        current = self.current
        if (
            current is not None
            and current.matrix_identity.namespace != context.matrix_identity.namespace
        ):
            self._contexts.clear()
            self._subjects.clear()
        self._contexts.append(context)

    def replace_current(self, context: IntelligenceContext) -> None:
        """Replace all same-root boundary views without adding a Matrix root."""

        current = self.current
        if (
            current is None
            or context.matrix_identity != current.matrix_identity
            or context.root_fingerprint != current.root_fingerprint
        ):
            raise ValueError("advisory_stm_control_refresh_root_mismatch")
        self._contexts[-1] = context

    def subject_completed(self, subject: RefereeSubject) -> bool:
        if not isinstance(subject, RefereeSubject):
            raise TypeError("advisory_stm_referee_subject_required")
        retained = next((item for item in self._subjects if item.key == subject.key), None)
        if retained is None:
            return False
        # Baseline fields are derived from the retained origin context; a late
        # duplicate receipt may outlive that context. Receipt facts cannot change.
        fields = ("evidence_sequence", "matrix_identity", "subject_kind", "origin",
                  "target", "terminal_status", "reason", "created_at", "terminal_at",
                  "proof_ready", "command_id", "superseded_proof_id")
        if any(getattr(retained, field) != getattr(subject, field) for field in fields):
            raise ValueError("advisory_stm_subject_identity_conflict")
        return True

    def admit_subject(self, subject: RefereeSubject) -> bool:
        """Commit a subject only after its downstream owners have completed."""
        if self.subject_completed(subject):
            return False
        self._subjects.append(subject)
        return True


def _fact_value(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    if fact.disposition != "available" or (
        path in _FRESH_EVIDENCE_FACTS and fact.retention is not None
    ):
        return None
    return fact.value.require("value")


def _quality_value(context: IntelligenceContext, name: str) -> Any | None:
    quality = context.surface("quality")
    if not quality.valid or quality.stale:
        return None
    return quality.value.get(name)


def _observation_diagnostics(context: IntelligenceContext) -> dict[str, Any]:
    """Retain Core's diagnostic values and validity, without interpreting them."""

    result: dict[str, Any] = {}
    for name, fields in _OBSERVATION_DIAGNOSTIC_FIELDS.items():
        surface = context.surface(name)
        result[f"diagnostic_{name}_surface_valid"] = surface.valid
        result[f"diagnostic_{name}_surface_stale"] = surface.stale
        for field in fields:
            # Preserve invalid/held values with their flags: useful for RCA,
            # but never a substitute for a qualified current measurement.
            result[f"diagnostic_{field}"] = surface.value.get(field)
    return result


def _surface_value(
    context: IntelligenceContext,
    surface_name: str,
    name: str,
) -> Any | None:
    surface = context.surface(surface_name)
    if not surface.valid or surface.stale:
        return None
    return surface.value.get(name)


def _performance_scope_key(context: IntelligenceContext) -> str:
    material = "|".join(
        (
            context_scope_key(context),
            str(_fact_value(context, "radio.serving_band") or ""),
            str(
                _fact_value(
                    context,
                    "radio.configured_band_pool_signature",
                )
                or ""
            ),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AdvisoryEngine:
    """Coordinate the owners without adding another source of truth."""

    def __init__(
        self,
        history_path: str | Path,
        *,
        proof_windows_s: tuple[float, ...] = (600.0,),
        identity_evidence: Any = None,
    ) -> None:
        self._lock = RLock()
        self._intake = ContextIntake()
        self._stm = AdvisorySTM()
        self._nr_recovery = NrServiceRecovery()
        self._history = AdvisoryHistory(history_path)
        self._identity_evidence = identity_evidence
        self._assessment_history = RefereeAssessmentHistoryOwner(self._history)
        self._decision_history = DecisionLifecycleHistoryOwner(self._history)
        self._referee = ProofReferee(
            self._history,
            windows_s=proof_windows_s,
            assessment_history=self._assessment_history,
            decision_history=self._decision_history,
        )
        self._reasoning = AdvisoryReasoning(
            self._history,
            assessment_history=self._assessment_history,
            decision_history=self._decision_history,
        )
        self._explore = ExploreCoordinator(self._history)
        self._publication: AdvisoryPublication | None = None
        self._scope_key = ""
        self._continuity_key = ""
        self._action_lifecycle_key = ""
        self._observation_count = 0
        self._observation_key = ""
        self._pending_proposal_id = ""
        self._handled_proposal_intentions: set[str] = set()
        self._proposal_source_state: tuple[object, ...] = ()
        self._impact_revision = 0
        self._impact_cache_key: tuple[object, ...] = ()
        self._impact_cache = empty_intelligence_impact()
        self._impact_cache_at = 0.0

    def has_context(self) -> bool:
        """Return whether runtime has established a Matrix context."""

        with self._lock:
            return self._stm.current is not None

    def _reconcile_pending_proposal(
        self,
        *,
        now: float,
        expire: bool = True,
    ) -> str:
        """Project one pending proposal from canonical owner history."""

        previous_id = self._pending_proposal_id
        if not self._continuity_key:
            return previous_id
        self._decision_history.current(self._continuity_key, now=now)
        pending_rows = self._pending_decision_rows()
        if expire:
            stamp = float(now)
            for proposal_id, decided_at in pending_rows:
                if stamp - decided_at > DECISION_LIFECYCLE_TIMEOUT_S:
                    self._record_decision_terminal(
                        proposal_id=proposal_id,
                        reason="decision_lifecycle_deadline_expired",
                        observed_at=stamp,
                    )
            pending_rows = self._pending_decision_rows()
        pending = tuple(proposal_id for proposal_id, _decided_at in pending_rows)
        if len(pending) > 1:
            raise RuntimeError("new_horizon_multiple_live_decisions")
        durable_id = pending[0] if pending else ""
        if (
            self._pending_proposal_id
            and durable_id
            and self._pending_proposal_id != durable_id
        ):
            raise RuntimeError("new_horizon_pending_decision_owner_mismatch")
        self._pending_proposal_id = durable_id
        if previous_id and not durable_id:
            self._impact_revision += 1
        return durable_id

    def _pending_decision_rows(self) -> tuple[tuple[str, float], ...]:
        snapshot = self._decision_history.current(self._continuity_key)
        closed: set[str] = {
            str(row["payload"].get("proposal_id") or "")
            for row in snapshot.terminals
        }
        closed.update(
            str(row["payload"].get("proposal_id") or "")
            for row in snapshot.proposal_results
            if row["payload"].get("submitted") is False
        )
        assessment_snapshot = self._assessment_history.current(
            self._continuity_key
        )
        for assessment in assessment_snapshot.assessments:
            for proof in assessment.proofs:
                decision_id = str(proof.decision_id or "")
                if decision_id.startswith("core-command:"):
                    closed.add(decision_id.removeprefix("core-command:"))
        rows: list[tuple[str, float]] = []
        for row in snapshot.current_decisions:
            proposal_id = str(row["payload"].get("proposal_id") or "")
            if proposal_id and proposal_id not in closed:
                rows.append((proposal_id, float(row["observed_at"])))
        return tuple(sorted(rows, key=lambda item: (item[1], item[0])))

    def _has_current_decision(self, proposal_id: str) -> bool:
        identity = str(proposal_id or "").strip()
        if not identity or not self._continuity_key:
            return False
        return any(
            str(row["payload"].get("proposal_id") or "") == identity
            for row in self._decision_history.current(
                self._continuity_key
            ).current_decisions
        )

    def _refresh_reasoning_history(self, context: IntelligenceContext) -> None:
        continuity_key = context_continuity_key(context)
        if continuity_key != self._continuity_key:
            return
        self._reasoning.bind_context(
            context,
            self._assessment_history.current(
                continuity_key,
                now=context.accepted_at,
            ),
            self._decision_history.current(continuity_key, now=context.accepted_at),
        )

    def _record_decision_terminal(
        self,
        *,
        proposal_id: str,
        reason: str,
        observed_at: float,
    ) -> bool:
        identity = str(proposal_id or "").strip()
        why = str(reason or "").strip()
        if not identity:
            raise ValueError("new_horizon_terminal_proposal_id_required")
        if not why:
            raise ValueError("new_horizon_terminal_reason_required")
        self._decision_history.current(self._continuity_key, now=observed_at)
        pending = {item[0] for item in self._pending_decision_rows()}
        event_key = "decision-terminal:" + identity
        if identity not in pending:
            if not self._history.contains(event_key=event_key):
                return False
            recorded = False
        else:
            recorded = self._history.record(
                event_key=event_key,
                scope_key=self._continuity_key,
                kind=DECISION_TERMINAL_KIND,
                observed_at=float(observed_at),
                payload={
                    "schema": DECISION_TERMINAL_SCHEMA,
                    "proposal_id": identity,
                    "reason": why,
                },
            )
        # A previous attempt may have recorded the terminal and failed while
        # updating its read owner. Keyed admission is safe in either case.
        self._decision_history.admit_live(
            event_key=event_key,
            continuity_key=self._continuity_key,
        )
        current = self._stm.current
        if current is not None:
            self._refresh_reasoning_history(current)
        return recorded

    def _terminalize_pending_proposal(
        self,
        *,
        reason: str,
        observed_at: float,
    ) -> bool:
        proposal_id = self._reconcile_pending_proposal(now=observed_at)
        if not proposal_id:
            return False
        recorded = self._record_decision_terminal(
            proposal_id=proposal_id,
            reason=reason,
            observed_at=observed_at,
        )
        self._reconcile_pending_proposal(now=observed_at)
        if recorded:
            self._impact_revision += 1
        return recorded

    def _advance_proposal_context(self, context: IntelligenceContext) -> None:
        """Keep duplicate suppression within a confirmed radio visit.

        A radio change does not close an in-flight decision. Its durable
        lifecycle and Core's action hold remain independent of this cache.
        Missing readings cannot start a new visit or unlock a repeated request.
        """

        lifecycle_key = context_action_lifecycle_key(context)
        if (
            self._action_lifecycle_key
            and lifecycle_key
            and lifecycle_key != self._action_lifecycle_key
        ):
            self._handled_proposal_intentions.clear()
            self._proposal_source_state = ()
        if lifecycle_key:
            self._action_lifecycle_key = lifecycle_key
        source_state = proposal_source_state(context)
        if source_state:
            if self._proposal_source_state and source_state != self._proposal_source_state:
                self._handled_proposal_intentions.clear()
            self._proposal_source_state = source_state

    def _admit_referee_assessments(
        self,
        proofs: tuple[RefereeProof, ...],
        context: IntelligenceContext,
    ) -> None:
        continuity_key = context_continuity_key(context)
        grouped: dict[str, list[RefereeProof]] = {}
        for proof in proofs:
            grouped.setdefault(proof.assessment_id, []).append(proof)
        admitted = False
        for group in grouped.values():
            proof_rows = tuple(group)
            complete = referee_assessment_is_complete(proof_rows)
            for proof in proof_rows:
                record_target_retention(
                    self._history, proof, context, continuity_key=continuity_key,
                )
            if not complete:
                # An invalid assessment ends the attempt without becoming AQ.
                # Do this before the training filter, through the same terminal
                # owner used for rejected or failed commands.
                if not any(proof.status == "pending" for proof in proof_rows):
                    for proof in proof_rows:
                        if (
                            proof.status == "invalid"
                            and proof.subject_kind == "core_command"
                            and is_brain_decision_origin(proof.origin)
                            and proof.decision_id.startswith("core-command:")
                            and (
                                proof.scope_key == self._continuity_key
                                or (
                                    proof.scope_key == "unavailable"
                                    and continuity_key == self._continuity_key
                                )
                            )
                        ):
                            if self._record_decision_terminal(
                                proposal_id=proof.decision_id.removeprefix("core-command:"),
                                reason=";".join(proof.reasons),
                                observed_at=proof.closed_at,
                            ):
                                self._impact_revision += 1
                continue
            if proof_rows[0].scope_key != self._continuity_key:
                # A RAT/provider change may close a mature previous window
                # after the new population was bound. Referee has persisted
                # it in its original scope; that scope's normal refresh owns
                # subsequent admission. Never rebind or relabel it here.
                for proof in proof_rows:
                    retained = self._history.load_evidence(event_key="proof:" + proof.proof_id)
                    if retained is None or retained["scope_key"] != proof.scope_key:
                        raise RuntimeError("new_horizon_noncurrent_assessment_evidence_missing")
                continue
            assessment = self._assessment_history.admit_live(proof_rows)
            admitted = True
            if any(proof.status != "pending" for proof in assessment.proofs):
                self._impact_revision += 1
        if admitted and continuity_key == self._continuity_key:
            assessment_history = self._assessment_history.current(
                continuity_key,
                now=context.accepted_at,
            )
            decision_history = self._decision_history.current(
                continuity_key, now=context.accepted_at
            )
            self._reasoning.bind_context(
                context,
                assessment_history,
                decision_history,
            )
        self._reconcile_pending_proposal(now=context.accepted_at)

    def _bind_context_owners(
        self,
        context: IntelligenceContext,
        *,
        scope_key: str,
        continuity_key: str,
    ) -> bool:
        """Bind every reader to the one resolved continuity population."""

        if scope_key != self._scope_key:
            assessment_history = self._referee.restore_context(context)
        else:
            assessment_history = self._assessment_history.current(
                continuity_key,
                now=context.accepted_at,
            )
        if assessment_history.continuity_key != continuity_key:
            raise RuntimeError("new_horizon_referee_history_owner_continuity_mismatch")
        decision_history = self._decision_history.current(
            continuity_key, now=context.accepted_at
        )
        if decision_history.continuity_key != assessment_history.continuity_key:
            raise RuntimeError("new_horizon_history_owner_continuity_mismatch")
        self._reasoning.bind_context(
            context,
            assessment_history,
            decision_history,
        )
        self._scope_key = scope_key
        self._continuity_key = continuity_key
        return True

    @staticmethod
    def _baseline_context(
        context: IntelligenceContext,
    ) -> dict[str, Any] | None:
        values = {
            "profile_id": _fact_value(context, "identity.profile_id"),
            "provider_plmn": _fact_value(context, "identity.provider_plmn"),
            "network_type": _fact_value(context, "radio.network_type"),
            "serving_band": _fact_value(context, "radio.serving_band"),
            "configured_band_pool": _fact_value(
                context,
                "radio.configured_band_pool",
            ),
            "cell_id": _fact_value(context, "rf.cell_id"),
            "earfcn": _fact_value(context, "rf.earfcn_dl"),
            "pci": _fact_value(context, "rf.pci"),
        }
        pool = values["configured_band_pool"]
        if not isinstance(pool, (list, tuple)):
            return None
        configured_pool = tuple(sorted(set(pool)))
        if (
            not isinstance(values["profile_id"], str)
            or not values["profile_id"]
            or not isinstance(values["provider_plmn"], str)
            or not values["provider_plmn"]
            or not isinstance(values["network_type"], str)
            or not values["network_type"]
            or type(values["serving_band"]) is not int
            or values["serving_band"] <= 0
            or not configured_pool
            or any(type(band) is not int or band <= 0 for band in configured_pool)
            or values["serving_band"] not in configured_pool
            or type(values["cell_id"]) is not int
            or values["cell_id"] <= 0
            or type(values["earfcn"]) is not int
            or values["earfcn"] < 0
            or type(values["pci"]) is not int
            or values["pci"] < 0
        ):
            return None
        bandwidth_fact = context.fact("slow.dl_bandwidth_mhz")
        bandwidth_state = bandwidth_fact.disposition
        bandwidth = (
            float(bandwidth_fact.value.require("value"))
            if bandwidth_state == "available"
            else 0.0
        )
        if bandwidth_state == "available" and bandwidth <= 0.0:
            return None
        return {
            "identity_code": context.identity_code,
            "profile_id": values["profile_id"],
            "provider_plmn": values["provider_plmn"],
            "network_type": values["network_type"],
            "serving_band": values["serving_band"],
            "configured_band_pool": configured_pool,
            "cell_id": values["cell_id"],
            "earfcn": values["earfcn"],
            "pci": values["pci"],
            "bandwidth_state": bandwidth_state,
            "bandwidth_mhz": bandwidth,
        }

    def signal_baseline_snapshot(
        self,
        *,
        published_at: float | None = None,
    ) -> IntelligenceSignalBaselineSnapshot | None:
        """Return the History floor pair for the current context."""

        with self._lock:
            context = self._stm.current
            if context is None:
                return None
            if "identity.sim_network_scope" in context.missing_required:
                return None
            baseline_context = self._baseline_context(context)
            if baseline_context is None:
                return None
            signal_scope_key = intelligence_signal_scope_key(**baseline_context)
            _durable_key, checkpoint = self._signal_history_checkpoint(context, baseline_context)
            if not isinstance(checkpoint, Mapping):
                return None
            if (
                checkpoint.get("schema") != "NewHorizonSignalBaselines.v1"
                or checkpoint.get("formula_generation")
                != SIGNAL_BASELINE_FORMULA_GENERATION
            ):
                raise ValueError("new_horizon_signal_baseline_checkpoint_invalid")
            return IntelligenceSignalBaselineSnapshot(
                namespace=context.matrix_identity.namespace,
                signal_scope_key=signal_scope_key,
                subscription_key=context.confirmed_continuity.to_mapping()["learning_subscription_key"],
                rsrq_baseline_db=checkpoint.get("rsrq_baseline_db"),
                rtt_baseline_ms=checkpoint.get("rtt_baseline_ms"),
                learned_at=float(checkpoint.get("learned_at", 0.0)),
                published_at=max(
                    float(checkpoint.get("learned_at", 0.0)),
                    float(time() if published_at is None else published_at),
                ),
            )

    def _signal_history_checkpoint(self, context: IntelligenceContext, values: Mapping[str, Any]):
        """Durable device lookup; the publication still uses its current live key."""
        binding = context.confirmed_continuity.to_mapping()
        durable_key = intelligence_signal_scope_key(**{**values, "identity_code": learning_device_key(binding)})
        if binding.get("quality_model") == "5g-combined-v1":
            return durable_key, self._history.load_checkpoint(scope_key=durable_key, name="signal_baselines_v1")
        old_keys = tuple(intelligence_signal_scope_key(**{**values, "identity_code": alias["identity_code"]})
                         for alias in binding.get("history_aliases", ()) if ("learning_subscription_key" not in binding or owns_initial_sim_history(binding)) and alias["profile_id"] == values["profile_id"])
        if owns_initial_sim_history(binding):
            prior = {key: value for key, value in binding.items() if key != "initial_sim_network"}
            old_keys += (intelligence_signal_scope_key(**{**values, "identity_code": learning_device_key(prior)}),)
        return durable_key, self._history.adopt_legacy_device_checkpoint(
            scope_key=durable_key, name="signal_baselines_v1", legacy_keys=old_keys)

    def _update_signal_baseline_checkpoint(
        self,
        context: IntelligenceContext,
    ) -> bool:
        if "identity.sim_network_scope" in context.missing_required:
            return False
        baseline_context = self._baseline_context(context)
        if baseline_context is None:
            return False
        rsrq_value = _quality_value(context, "rsrq_baseline")
        rsrq_valid = bool(_quality_value(context, "rsrq_baseline_valid"))
        rtt_value = _surface_value(context, "latency", "rtt_baseline")
        rtt_delta_valid = bool(_surface_value(context, "latency", "rtt_delta_valid"))
        rsrq = (
            float(rsrq_value)
            if rsrq_valid
            and type(rsrq_value) in {int, float}
            and -25.0 <= float(rsrq_value) < 0.0
            else None
        )
        rtt = (
            float(rtt_value)
            if rtt_delta_valid
            and type(rtt_value) in {int, float}
            and 0.0 < float(rtt_value) < 10_000.0
            else None
        )
        if rsrq is None and rtt is None:
            return False
        signal_scope_key, stored = self._signal_history_checkpoint(context, baseline_context)
        previous = stored or {}
        previous_rsrq = previous.get("rsrq_baseline_db")
        previous_rtt = previous.get("rtt_baseline_ms")
        best_rsrq = (
            max(float(previous_rsrq), rsrq)
            if previous_rsrq is not None and rsrq is not None
            else rsrq
            if rsrq is not None
            else previous_rsrq
        )
        best_rtt = (
            min(float(previous_rtt), rtt)
            if previous_rtt is not None and rtt is not None
            else rtt
            if rtt is not None
            else previous_rtt
        )
        changed = best_rsrq != previous_rsrq or best_rtt != previous_rtt
        if not changed:
            return False
        self._history.save_checkpoint(
            scope_key=signal_scope_key,
            name="signal_baselines_v1",
            updated_at=context.accepted_at,
            payload={
                "schema": "NewHorizonSignalBaselines.v1",
                "formula_generation": SIGNAL_BASELINE_FORMULA_GENERATION,
                "rsrq_baseline_db": best_rsrq,
                "rtt_baseline_ms": best_rtt,
                "learned_at": context.accepted_at,
            },
        )
        return True

    def _record_passive_performance(self, context: IntelligenceContext) -> bool:
        if "identity.sim_network_scope" in context.missing_required:
            return False
        dl_mbps = _fact_value(context, "traffic.dl_mbps")
        if (
            isinstance(dl_mbps, bool)
            or type(dl_mbps) not in {int, float}
            or float(dl_mbps) <= 0.0
        ):
            return False
        scope_key = _performance_scope_key(context)
        binding = context.confirmed_continuity.to_mapping()
        legacy_keys = tuple(_performance_scope_key(replace(context,
            confirmed_continuity=CoreFrozenPayload.from_mapping({**binding, "history_device_key": alias["identity_code"]})))
            for alias in binding.get("history_aliases", ()) if ("learning_subscription_key" not in binding or owns_initial_sim_history(binding)) and alias["profile_id"] == binding["identity.profile_id"])
        if owns_initial_sim_history(binding):
            prior = {key: value for key, value in binding.items() if key != "initial_sim_network"}
            legacy_keys += (_performance_scope_key(replace(context,
                confirmed_continuity=CoreFrozenPayload.from_mapping(prior))),)
        checkpoint = self._history.adopt_legacy_device_checkpoint(
            scope_key=scope_key, name="passive_dl_peak_v2", legacy_keys=legacy_keys)
        previous = (
            float(checkpoint.get("dl_mbps", 0.0))
            if isinstance(checkpoint, Mapping)
            else 0.0
        )
        measured = float(dl_mbps)
        if measured <= previous:
            return False
        checkpoint_payload = {
            "schema": "NewHorizonPassiveDlPeak.v2",
            "dl_mbps": measured,
        }
        recorded = record_performance_evidence(
            self._history,
            context,
            event_key="performance-v2:passive:" + context.root_fingerprint,
            scope_key=scope_key,
            continuity_key=context_continuity_key(context),
            record_type="passive_high_water",
            dl_mbps=measured,
            observed_at=context.accepted_at,
            details={
                "previous_dl_mbps": previous,
                "local_minute": strftime(
                    "%Y-%m-%d %H:%M",
                    localtime(context.accepted_at),
                ),
                "rsrp_dbm": _fact_value(context, "rf.rsrp_dbm"),
                "rsrq_db": _fact_value(context, "rf.rsrq_db"),
                "sinr_db": _fact_value(context, "rf.sinr_db"),
                "congestion_pct": _quality_value(context, "congestion_pct"),
                "rtt_ms": _surface_value(
                    context,
                    "latency",
                    "rtt_sliding_avg",
                ),
            },
        )
        self._history.save_checkpoint(
            scope_key=scope_key,
            name="passive_dl_peak_v2",
            updated_at=context.accepted_at,
            payload=checkpoint_payload,
        )
        return recorded

    def _record_evaluation(self, observation_key: str, causes: tuple[str, ...],
                           publication: AdvisoryPublication) -> None:
        """Retain the actual evaluation, including holds, before publication."""
        context = publication.context
        identity = context.matrix_identity
        decision = publication.decision
        event_key = "brain-evaluation:" + hashlib.sha256(
            observation_key.encode("utf-8")).hexdigest()
        self._history.record(
            event_key=event_key,
            scope_key=context_continuity_key(context),
            kind="brain_evaluation_v1",
            observed_at=publication.published_at,
            payload={
                "schema": "NewHorizonBrainEvaluation.v1",
                "source_epoch": identity.source_epoch,
                "lifecycle_generation": identity.lifecycle_generation,
                "matrix_sequence": identity.matrix_sequence,
                "root_fingerprint": context.root_fingerprint,
                "accepted_at": context.accepted_at,
                "causes": list(causes),
                "complete": context.complete,
                "missing_required": list(context.missing_required),
                "selected_opportunity_id": decision.selected_opportunity_id,
                "reason": decision.reason,
                "explanation": decision.explanation,
                "eligibility": decision.eligibility.to_payload(),
                "predicted_grades": decision.predicted_grades.to_payload(),
                "hold_opportunities": [item.to_mapping() for item in decision.hold_opportunities],
                "proposal_id": publication.proposal.proposal_id if publication.proposal else None,
                "pending_proposal_id": self._pending_proposal_id,
                "adviser_chain": self._reasoning.adviser_chain_snapshot(),
                "opportunities": [
                    {"opportunity_id": item.opportunity_id,
                     "eligible": item.eligible, "reason": item.reason,
                     "target_signature": item.action_target.target_signature if item.action_target else ""}
                    for item in publication.opportunities
                ],
                "current_service": referee_sample_metrics(context),
            },
        )

    def admit_observation(
        self,
        record: IntelligenceMatrixObservation,
        *,
        now: float | None = None,
    ) -> AdvisoryPublication | None:
        with self._lock:
            current = self._stm.current
            same_root = bool(
                current is not None
                and current.matrix_identity == record.root.matrix_identity
            )
            if same_root:
                if current.root_fingerprint != record.root.root_fingerprint:
                    raise ValueError(
                        "intelligence_context_generation_fingerprint_collision"
                    )
                if record.key == self._observation_key:
                    return None
                context = self._intake.refresh_constraints(
                    record,
                    current,
                )
            else:
                context = self._intake.build(
                    record,
                    previous=current,
                )
                identity_evidence = (
                    self._identity_evidence(record.root.observation_frame)
                    if self._identity_evidence is not None else {})
                context = replace(context, confirmed_continuity=CoreFrozenPayload.from_mapping(
                    bind_device_history(self._history,
                        physical_scope=context.matrix_identity.namespace.session_scope_key,
                        current=context.confirmed_continuity.to_mapping(), observed_at=context.accepted_at,
                        identity_evidence=identity_evidence)))
            external_subject = (None if same_root else
                self._intake.external_referee_subject(record, context, previous=current))
            publication, _proofs = self._admit_accepted_context(
                context, transition=record.root.radio_transition,
                observation_key=record.key, causes=record.causes,
                observed_at=record.published_at, now=now,
                external_subject=external_subject,
            )
            return publication

    def _admit_accepted_context(
        self,
        context: IntelligenceContext,
        *,
        transition: RadioTransitionFrame,
        observation_key: str,
        causes: tuple[str, ...],
        observed_at: float,
        now: float | None = None,
        external_subject: RefereeSubject | None = None,
    ) -> tuple[AdvisoryPublication | None, tuple[RefereeProof, ...]]:
        """One live sequence, also used by the explicit simulation boundary."""
        if not isinstance(context, IntelligenceContext):
            raise TypeError("new_horizon_accepted_context_required")
        if (not isinstance(transition, RadioTransitionFrame)
                or transition.matrix_identity != context.matrix_identity):
            raise ValueError("new_horizon_accepted_transition_mismatch")
        if not isinstance(observation_key, str) or not observation_key:
            raise ValueError("new_horizon_accepted_observation_key_required")
        if type(causes) is not tuple or not causes:
            raise ValueError("new_horizon_accepted_observation_causes_required")
        with self._lock:
            current = self._stm.current
            same_root = current is not None and current.matrix_identity == context.matrix_identity
            if same_root:
                if current.root_fingerprint != context.root_fingerprint:
                    raise ValueError("intelligence_context_generation_fingerprint_collision")
                if observation_key == self._observation_key:
                    return None, ()
            learning_settled = context.product_control.to_mapping()["connected_settled"]
            if type(learning_settled) is not bool:
                raise TypeError("intelligence_connected_settled_bool_required")
            missing = tuple(item for item in context.missing_required
                            if item != CONNECTION_SETTLING_REASON)
            if not learning_settled:
                missing += (CONNECTION_SETTLING_REASON,)
                external_subject = None
            context = replace(context, complete=not missing, missing_required=missing)
            defer_continuity_binding = False
            if same_root:
                self._stm.replace_current(context)
            else:
                next_continuity = context_continuity_key(context)
                next_action_lifecycle = context_action_lifecycle_key(context)
                # Referee must first close a proof due on this same sample.
                # Reading pending state here is enough for continuity routing;
                # lifecycle expiry runs after proof admission below.
                pending_id = self._reconcile_pending_proposal(
                    now=context.accepted_at,
                    expire=False,
                )
                if (
                    pending_id
                    and self._action_lifecycle_key
                    and next_action_lifecycle
                    and next_action_lifecycle != self._action_lifecycle_key
                ):
                    self._terminalize_pending_proposal(
                        reason="decision_action_owner_changed",
                        observed_at=context.accepted_at,
                    )
                    pending_id = ""
                defer_continuity_binding = bool(
                    pending_id
                    and self._continuity_key
                    and next_continuity != self._continuity_key
                )
                if external_subject is not None:
                    external_subject = self._referee.prepare_external_subject(
                        external_subject,
                        recent_contexts=self._stm.contexts,
                    )
                if external_subject is not None:
                    hold_recorded = record_hold_attribution(
                        self._history,
                        publication=self._publication,
                        transition=transition,
                        subject_key=external_subject.key,
                        continuity_key=next_continuity,
                        observed_at=context.accepted_at,
                    )
                    if hold_recorded or self._history.contains(
                        event_key="hold-attribution:" + external_subject.key
                    ):
                        if next_continuity == self._continuity_key:
                            self._decision_history.admit_live(
                                event_key="hold-attribution:" + external_subject.key,
                                continuity_key=next_continuity,
                                now=context.accepted_at,
                            )
                        # A changed scope is loaded by _bind_context_owners below.
                        if hold_recorded:
                            self._impact_revision += 1
                if external_subject is not None and not self._stm.subject_completed(
                    external_subject
                ):
                    external_proof = self._referee.admit_subject(
                        external_subject,
                    )
                    self._admit_referee_assessments(
                        (external_proof,),
                        context,
                    )
                    self._stm.admit_subject(external_subject)
                self._stm.admit_context(context)
            scope_key = context_scope_key(context)
            next_continuity = context_continuity_key(context)
            if not defer_continuity_binding:
                self._bind_context_owners(
                    context,
                    scope_key=scope_key,
                    continuity_key=next_continuity,
                )
            if same_root and "reachability" in causes:
                emitted_proofs = self._referee.admit_reachability(
                    context,
                    observed_at=observed_at,
                )
            elif same_root:
                emitted_proofs = ()
            else:
                emitted_proofs = self._referee.admit_context(
                    context,
                    transition=transition,
                )
            self._admit_referee_assessments(emitted_proofs, context)
            if defer_continuity_binding and not self._pending_proposal_id:
                self._bind_context_owners(
                    context,
                    scope_key=scope_key,
                    continuity_key=next_continuity,
                )
                defer_continuity_binding = False
            self._advance_proposal_context(context)
            auxiliary = context.surface("action_auxiliary")
            if auxiliary.valid and not auxiliary.stale:
                pause = auxiliary.value.get("internet_pause")
                if isinstance(pause, Mapping):
                    record_internet_pause(
                        self._history,
                        continuity_key=context_continuity_key(context),
                        observed_at=context.accepted_at,
                        pause=pause,
                    )
            if not same_root:
                service_sample = referee_sample_metrics(context)
                self._history.admit_observation(
                    event_key=context.root_fingerprint,
                    scope_key=scope_key,
                    observed_at=context.accepted_at,
                    payload={
                        "schema": "NewHorizonObservationEvidence.v5",
                        "continuity_key": context_continuity_key(context),
                        "scope_key": scope_key,
                        "identity_code": context.identity_code,
                        "matrix_sequence": context.matrix_identity.matrix_sequence,
                        "sampled_at": context.sampled_at,
                        "accepted_at": context.accepted_at,
                        "complete": context.complete,
                        "missing_required": list(context.missing_required),
                        "sequence_gap": context.sequence_gap,
                        "eci_points": service_sample["eci_points"],
                        "quality_model": service_sample["quality_model"],
                        "quality_capacity_fraction": service_sample["quality_capacity_fraction"],
                        "quality_total_bw_mhz": service_sample["quality_total_bw_mhz"],
                        "quality_radio_inputs": service_sample["quality_radio_inputs"],
                        "cqb_cell": service_sample["cqb_cell"],
                        "low_load_latency_valid": service_sample[
                            "low_load_latency_valid"
                        ],
                        "low_load_latency_reason": service_sample[
                            "low_load_latency_reason"
                        ],
                        "low_load_latency_mbps": service_sample[
                            "low_load_latency_mbps"
                        ],
                        "rtt_probe_sequence": service_sample["rtt_probe_sequence"],
                        "rtt_probe_status": service_sample["rtt_probe_status"],
                        "rtt_freshness": service_sample["rtt_freshness"],
                        "rtt_value_ms": service_sample["rtt_value_ms"],
                        "congestion_pct": _quality_value(context, "congestion_pct"),
                        "dl_mbps": _fact_value(context, "traffic.dl_mbps"),
                        "ul_mbps": _fact_value(context, "traffic.ul_mbps"),
                        "has_traffic": _fact_value(
                            context,
                            "traffic.has_traffic",
                        ),
                        "serving_band": _fact_value(context, "radio.serving_band"),
                        "ca_pool": _fact_value(
                            context,
                            "radio.configured_band_pool_signature",
                        ),
                        "ca_active": _fact_value(context, "radio.ca_active"),
                        "ca_observation": service_sample["ca_observation"],
                        "carrier_count": _fact_value(
                            context,
                            "radio.carrier_count",
                        ),
                        "network_type": _fact_value(
                            context,
                            "radio.network_type",
                        ),
                        "rsrp_dbm": _fact_value(context, "rf.rsrp_dbm"),
                        "rsrq_db": _fact_value(context, "rf.rsrq_db"),
                        "rssi_dbm": _fact_value(context, "rf.rssi_dbm"),
                        "sinr_db": _fact_value(context, "rf.sinr_db"),
                        "nr_sinr_db": _fact_value(context, "radio.nr_sinr_db"),
                        "nr_rsrp_dbm": _fact_value(context, "radio.nr_rsrp_dbm"),
                        "nr_rsrq_db": _fact_value(context, "radio.nr_rsrq_db"),
                        "cell_id": _fact_value(context, "rf.cell_id"),
                        "earfcn": _fact_value(context, "rf.earfcn_dl"),
                        "pci": _fact_value(context, "rf.pci"),
                        **_observation_diagnostics(context),
                    },
                )
                if learning_settled:
                    self._update_signal_baseline_checkpoint(context)
                    self._record_passive_performance(context)
                    update_band_observation_maxima(
                        self._history,
                        context,
                        continuity_key=context_continuity_key(context),
                    )
                self._observation_count += 1
            if same_root and causes == ("reachability",):
                if self._publication is None:
                    raise RuntimeError(
                        "intelligence_reachability_refresh_publication_missing"
                    )
                published_at = float(time() if now is None else now)
                self._publication = replace(
                    self._publication,
                    context=context,
                    last_proof=self._referee.last,
                    proposal=None,
                    published_at=published_at,
                )
                self._observation_key = observation_key
                return self._publication, emitted_proofs
            explore_opportunity = self._explore.synchronize(context)
            if defer_continuity_binding and self._pending_proposal_id:
                published_at = float(time() if now is None else now)
                (
                    opportunities,
                    forecast,
                    decision,
                    evidence_readiness,
                ) = self._reasoning.build_non_actionable(
                    context,
                    reason="physical_action_in_flight",
                    explanation=(
                        "Keep the current connection while the submitted "
                        "radio action is settling."
                    ),
                )
                self._publication = AdvisoryPublication(
                    context=context,
                    opportunities=opportunities,
                    forecast=forecast,
                    decision=decision,
                    posteriors=self._reasoning.posteriors,
                    learned_proof_count=self._reasoning.learned_proof_count,
                    evidence_readiness=evidence_readiness,
                    last_proof=self._referee.last,
                    proposal=None,
                    published_at=published_at,
                )
                self._record_evaluation(observation_key, causes, self._publication)
                self._observation_key = observation_key
                return self._publication, emitted_proofs
            nr_recovery = (self._nr_recovery.evaluate(self._stm.contexts)
                if not self._pending_proposal_id and not self._reasoning.outage_recovery_actionable(context) else None)
            if nr_recovery is not None:
                published_at = float(time() if now is None else now)
                opportunities, forecast, decision, readiness, proposal = self._reasoning.build_nr_service_recovery(
                    context, nr_recovery, now=published_at)
                self._publication = AdvisoryPublication(context=context, opportunities=opportunities,
                    forecast=forecast, decision=decision, posteriors=self._reasoning.posteriors,
                    learned_proof_count=self._reasoning.learned_proof_count, evidence_readiness=readiness,
                    last_proof=self._referee.last, proposal=proposal, published_at=published_at)
                self._record_evaluation(observation_key, causes, self._publication)
                self._observation_key = observation_key
                return self._publication, emitted_proofs
            if not context.complete and not self._reasoning.outage_recovery_actionable(
                context
            ):
                published_at = float(time() if now is None else now)
                (
                    opportunities,
                    forecast,
                    decision,
                    evidence_readiness,
                ) = self._reasoning.build_non_actionable(context)
                self._publication = AdvisoryPublication(
                    context=context,
                    opportunities=opportunities,
                    forecast=forecast,
                    decision=decision,
                    posteriors=self._reasoning.posteriors,
                    learned_proof_count=self._reasoning.learned_proof_count,
                    evidence_readiness=evidence_readiness,
                    last_proof=self._referee.last,
                    proposal=None,
                    published_at=published_at,
                )
                self._record_evaluation(observation_key, causes, self._publication)
                self._observation_key = observation_key
                return self._publication, emitted_proofs
            (
                opportunities,
                forecast,
                decision,
                evidence_readiness,
            ) = self._reasoning.build(
                context,
                self._stm.contexts,
                explore_opportunity=explore_opportunity,
                context_final_mature=self._referee.context_final_mature(),
            )
            published_at = float(time() if now is None else now)
            proposal = (
                None
                if self._pending_proposal_id
                else self._reasoning.build_proposal(
                    context,
                    opportunities,
                    decision,
                    now=published_at,
                )
            )
            publication = AdvisoryPublication(
                context=context,
                opportunities=opportunities,
                forecast=forecast,
                decision=decision,
                posteriors=self._reasoning.posteriors,
                learned_proof_count=self._reasoning.learned_proof_count,
                evidence_readiness=evidence_readiness,
                last_proof=self._referee.last,
                proposal=proposal,
                published_at=published_at,
            )
            self._record_evaluation(observation_key, causes, publication)
            self._publication = publication
            self._observation_key = observation_key
            return publication, emitted_proofs

    def admit_outcome(self, record: CoreCommandOutcomeRecord) -> bool:
        if not isinstance(record, CoreCommandOutcomeRecord):
            raise TypeError("referee_core_outcome_record_required")
        receipt = record.receipt
        subject = RefereeSubject(
            key="core-command:" + receipt.transaction.command_id,
            evidence_sequence=receipt.receipt_sequence,
            matrix_identity=receipt.transaction.matrix_identity,
            baseline_through_sequence=(
                receipt.transaction.matrix_identity.matrix_sequence
            ),
            baseline_scope_key="unavailable",
            learning_context_key="unavailable",
            learning_context=None,
            subject_kind="core_command",
            origin=receipt.action_key.origin_kind,
            target=_referee_target_from_command(record, settled_result=True),
            terminal_status=receipt.terminal_status.value,
            reason=receipt.reason,
            created_at=receipt.transaction.created_ts,
            terminal_at=receipt.terminal_ts,
            # Command Authority's physical settlement proof is retained in its
            # receipt. A mode change has no comparable AQ destination/forecast;
            # ordinary passive LTE/NR context assessments continue unchanged.
            proof_ready=receipt.proof_ready and not isinstance(receipt.action_key.target, CoreRadioModeTarget),
        )
        with self._lock:
            if self._stm.subject_completed(subject):
                return False
            origin_context = self._stm.context_for(subject.matrix_identity)
            exact_origin_context = origin_context
            action_id = subject.key.removeprefix("core-command:")
            current = self._stm.current
            if (
                origin_context is None
                and current is not None
                and current.matrix_identity.namespace
                == subject.matrix_identity.namespace
            ):
                # A long modem action may outlive the 32-root STM window. A
                # current context in the same session still has device and
                # provider continuity.
                origin_context = current
            if origin_context is None:
                if current is not None:
                    pending_id = self._reconcile_pending_proposal(
                        now=receipt.terminal_ts
                    )
                    if action_id == pending_id:
                        self._terminalize_pending_proposal(
                            reason="outcome_origin_context_unavailable",
                            observed_at=receipt.terminal_ts,
                        )
                raise RuntimeError("new_horizon_outcome_origin_context_unavailable")
            if exact_origin_context is not None:
                learning_context = context_learning_components(exact_origin_context)
                source_learning_context_key = context_learning_key(exact_origin_context)
                brain_receipt = dict(
                    receipt.action_key.as_dict().get("brain_decision_receipt") or {}
                )
                expected_learning_context_key = str(
                    brain_receipt.get("learning_context_key") or ""
                )
                if (
                    expected_learning_context_key
                    and expected_learning_context_key != source_learning_context_key
                ):
                    raise ValueError(
                        "new_horizon_command_learning_context_key_mismatch"
                    )
                subject = replace(
                    subject,
                    baseline_scope_key=context_scope_key(exact_origin_context),
                    learning_context_key=source_learning_context_key,
                    learning_context=CoreFrozenPayload.from_mapping(learning_context),
                )
            elif is_brain_decision_origin(subject.origin):
                frozen_contract = self._referee.reserved_subject_contract(
                    subject_key=subject.key,
                    matrix_identity=subject.matrix_identity,
                )
                if frozen_contract is None:
                    raise RuntimeError(
                        "new_horizon_outcome_frozen_subject_contract_missing"
                    )
                subject = replace(
                    subject,
                    baseline_scope_key=str(frozen_contract["baseline_scope_key"]),
                    learning_context_key=str(frozen_contract["learning_context_key"]),
                    learning_context=CoreFrozenPayload.from_mapping(
                        frozen_contract["learning_context"]
                    ),
                )
            record_physical_result(
                self._history,
                subject,
                origin_context,
                continuity_key=context_continuity_key(origin_context),
            )
            submitted_brain_action = bool(
                is_brain_decision_origin(subject.origin)
                and self._has_current_decision(action_id)
            )
            # Core's terminal receipt records a failed physical command. Referee
            # grades service only after a settled command with enough proof. Do
            # not create another grade for the same failure.
            if subject.proof_ready and subject.terminal_status == "settled":
                terminal_proof = self._referee.admit_subject(subject)
                self._admit_referee_assessments(
                    (terminal_proof,),
                    origin_context,
                )
            elif submitted_brain_action:
                terminalized = self._record_decision_terminal(
                    proposal_id=action_id,
                    reason=(
                        "radio_mode_execution_only_not_aq_learning" if isinstance(receipt.action_key.target, CoreRadioModeTarget) else
                        "core_outcome_not_proof_ready:"
                        + subject.terminal_status
                        + ":"
                        + str(subject.reason or "unspecified")
                    ),
                    observed_at=subject.terminal_at,
                )
                if terminalized:
                    self._impact_revision += 1
                self._reconcile_pending_proposal(now=subject.terminal_at)
            if current is not None:
                target = _referee_target_from_command(record)
                self._explore.admit_outcome(
                    command_id=action_id,
                    origin=subject.origin,
                    target_signature=target.target_signature,
                    terminal_status=subject.terminal_status,
                    terminal_at=subject.terminal_at,
                    context=current,
                )
            self._stm.admit_subject(subject)
            return True

    def admit_proposal_result(
        self,
        record: IntelligenceProposalDispatchResult,
    ) -> bool:
        if not isinstance(record, IntelligenceProposalDispatchResult):
            raise TypeError("new_horizon_proposal_result_required")
        matrix_identity = record.proposal.command_context.matrix_identity
        with self._lock:
            context = self._stm.context_for(matrix_identity)
            if context is None:
                raise RuntimeError("new_horizon_proposal_result_context_unavailable")
            event_key = "proposal_result:" + record.proposal_id
            if self._history.contains(event_key=event_key):
                return False
            self._explore.admit_proposal_result(
                record.proposal_id,
                record.proposal.opportunity_id,
                record.submitted,
                record.reason,
                context,
            )
            recorded = self._history.record(
                event_key=event_key,
                scope_key=context_continuity_key(context),
                kind=PROPOSAL_RESULT_HISTORY_KIND,
                observed_at=record.published_at,
                payload={
                    "schema": PROPOSAL_RESULT_EVIDENCE_SCHEMA,
                    "proposal_id": record.proposal_id,
                    "submitted": record.submitted,
                    "reason": record.reason,
                },
            )
            if recorded:
                self._decision_history.admit_live(
                    event_key=event_key,
                    continuity_key=context_continuity_key(context),
                )
                self._refresh_reasoning_history(context)
            self._reconcile_pending_proposal(now=record.published_at)
            if recorded and not record.submitted:
                self._impact_revision += 1
            if self._publication is not None:
                self._publication = replace(
                    self._publication,
                    posteriors=self._reasoning.posteriors,
                    learned_proof_count=self._reasoning.learned_proof_count,
                )
            return recorded

    def reserve_proposal_emission(
        self,
        proposal: BrainCommandProposal,
    ) -> ProposalEmissionReservation:
        """Reserve the one in-flight Intelligence proposal atomically."""
        if not isinstance(proposal, BrainCommandProposal):
            raise TypeError("new_horizon_typed_proposal_required")
        matrix_identity = proposal.command_context.matrix_identity
        with self._lock:
            self._reconcile_pending_proposal(now=proposal.created_at)
            context = self._stm.context_for(matrix_identity)
            if context is None:
                raise RuntimeError("new_horizon_proposal_emission_context_unavailable")
            # An old retained command context must not rewind visit tracking.
            self._advance_proposal_context(self._stm.current)
            semantic_key = self._reasoning.proposal_semantic_key(
                proposal=proposal,
                context=context,
            )
            if semantic_key in self._handled_proposal_intentions:
                return ProposalEmissionReservation(
                    status="already_handled",
                    proposal_id=proposal.proposal_id,
                )
            if self._pending_proposal_id:
                if self._pending_proposal_id == proposal.proposal_id:
                    return ProposalEmissionReservation(
                        status="already_pending",
                        proposal_id=proposal.proposal_id,
                    )
                raise RuntimeError("new_horizon_proposal_already_pending")
            evidence = self._reasoning.selected_proposal_evidence(
                proposal=proposal,
                context=context,
            )
            event_key = "decision-ledger:" + proposal.proposal_id
            if not self._history.record(
                event_key=event_key,
                scope_key=context_continuity_key(context),
                kind=DECISION_LEDGER_KIND,
                observed_at=proposal.created_at,
                payload=evidence,
            ):
                return ProposalEmissionReservation(
                    status="already_handled",
                    proposal_id=proposal.proposal_id,
                )
            self._decision_history.admit_live(
                event_key=event_key,
                continuity_key=context_continuity_key(context),
            )
            self._refresh_reasoning_history(context)
            try:
                self._referee.reserve_subject_baseline(
                    subject_key="core-command:" + proposal.proposal_id,
                    context=context,
                    observed_at=proposal.created_at,
                )
            except BaseException:
                self._record_decision_terminal(
                    proposal_id=proposal.proposal_id,
                    reason="referee_baseline_reservation_failed",
                    observed_at=proposal.created_at,
                )
                raise
            self._handled_proposal_intentions.add(semantic_key)
            self._pending_proposal_id = proposal.proposal_id
            return ProposalEmissionReservation(
                status="reserved",
                proposal_id=proposal.proposal_id,
            )

    def admit_proposal_emission(self, proposal: BrainCommandProposal) -> bool:
        """Record a proposal after Product Matrix accepts it."""

        if not isinstance(proposal, BrainCommandProposal):
            raise TypeError("new_horizon_typed_proposal_required")
        matrix_identity = proposal.command_context.matrix_identity
        with self._lock:
            if self._pending_proposal_id != proposal.proposal_id:
                raise RuntimeError("new_horizon_proposal_emission_not_reserved")
            context = self._stm.context_for(matrix_identity)
            if context is None:
                raise RuntimeError("new_horizon_proposal_emission_context_unavailable")
            return self._history.record(
                event_key="proposal_emitted:" + proposal.proposal_id,
                scope_key=context_scope_key(context),
                kind="proposal_emitted",
                observed_at=proposal.created_at,
                payload={
                    "schema": "NewHorizonProposalEmissionEvidence.v1",
                    "proposal_id": proposal.proposal_id,
                    "opportunity_id": proposal.opportunity_id,
                    "root_fingerprint": context.root_fingerprint,
                },
            )

    def release_proposal_emission(self, _proposal_id: str) -> None:
        raise RuntimeError("new_horizon_proposal_terminal_rejection_required")

    def reject_proposal_emission(
        self,
        *,
        proposal: BrainCommandProposal,
        reason: str,
        observed_at: float,
    ) -> bool:
        """Close a reservation rejected before Matrix accepts it."""

        if not isinstance(proposal, BrainCommandProposal):
            raise TypeError("new_horizon_proposal_rejection_proposal_required")
        with self._lock:
            pending_id = self._reconcile_pending_proposal(now=observed_at)
            if pending_id != proposal.proposal_id:
                raise RuntimeError("new_horizon_proposal_rejection_not_reserved")
            recorded = self._record_decision_terminal(
                proposal_id=proposal.proposal_id,
                reason=reason,
                observed_at=observed_at,
            )
            if recorded:
                self._impact_revision += 1
            self._reconcile_pending_proposal(now=observed_at)
            return recorded

    def current(self) -> AdvisoryPublication | None:
        with self._lock:
            return self._publication

    def claim_capacity_probe(self) -> ExploreProbeRequest | None:
        with self._lock:
            context = self._stm.current
            return None if context is None else self._explore.claim_probe(context)

    def admit_capacity_probe(
        self,
        request: ExploreProbeRequest,
        result: Mapping[str, Any],
    ) -> bool:
        with self._lock:
            context = self._stm.current
            if context is None:
                return False
            return self._explore.admit_probe_result(request, result, context)

    def explore_health(self) -> dict[str, Any]:
        with self._lock:
            return self._explore.health()

    def referee_status(self) -> dict[str, Any]:
        """Return Referee state without reading disk history."""

        with self._lock:
            return self._referee.status()

    def presentation_state(
        self,
    ) -> tuple[
        AdvisoryPublication | None,
        dict[str, Any],
        tuple[CandidateForecast, ...],
        str,
    ]:
        """Return one atomic Intelligence presentation snapshot."""

        with self._lock:
            publication = self._publication
            candidates = self._reasoning.candidate_forecasts
            selected_opportunity_id = self._reasoning.candidate_forecast_selection_id
            matched = self._assessment_history.status()
        return (
            publication,
            {**self._history.status(), "matched_assessment_count": matched["assessment_count"]},
            candidates,
            selected_opportunity_id,
        )

    def completed_learning_cycle_ring(self) -> tuple[dict[str, Any], ...]:
        """Return the fixed customer ring of completed learning proof."""

        with self._lock:
            self._reconcile_pending_proposal(now=time())
            return self._reasoning.completed_learning_cycle_ring()

    def recovery_status(self, context: IntelligenceContext | None = None) -> dict[str, Any]:
        """Readonly recovery explanation; it grants no command permission."""
        with self._lock:
            if context is not None and (self._stm.current is None
                    or context.matrix_identity != self._stm.current.matrix_identity):
                return self._nr_recovery.status(())
            return self._nr_recovery.status(self._stm.contexts)

    def diagnostic_snapshot(self, *, captured_at: float) -> dict[str, Any]:
        """Return a bounded causal chain safe for support export."""

        with self._lock:
            publication = self._publication
            current = self._stm.current
            pending_proposal_id = self._pending_proposal_id
            forecast = publication.forecast if publication is not None else None
            physical_action_audit = (
                band_physical_action_audit(
                    self._history,
                    current,
                    continuity_key=context_continuity_key(current),
                )
                if current is not None
                else None
            )
            referee_assessment_status = self._assessment_history.status()
            decision_history_status = self._decision_history.status()
            learning_population = self._reasoning.history_population_status()
            recovery_status = self._nr_recovery.status(self._stm.contexts)
        recent = {
            kind: list(self._history.recent_kind(kind=kind, limit=8))
            for kind in (
                DECISION_LEDGER_KIND,
                DECISION_TERMINAL_KIND,
                "proposal_emitted",
                PROPOSAL_RESULT_HISTORY_KIND,
                REFEREE_BASELINE_RESERVATION_KIND,
                REFEREE_PROOF_HISTORY_KIND,
            )
        }
        return {
            "schema": "NewHorizonDiagnosticSnapshot.v2",
            "owner": "54guc.new_horizon.advisory_engine",
            "captured_at": float(captured_at),
            "recovery_status": recovery_status,
            "matrix_sequence": (
                current.matrix_identity.matrix_sequence if current else 0
            ),
            "selected_opportunity": (
                publication.decision.selected_opportunity_id
                if publication is not None
                else ""
            ),
            "pending_proposal_id": pending_proposal_id,
            "forecast": (
                {
                    "status": forecast.status,
                    "match_tier": forecast.match_tier,
                    "confidence": forecast.confidence,
                    "uncertainty": forecast.uncertainty,
                    "evidence_samples": forecast.evidence_samples,
                    "accuracy_factor": forecast.accuracy_factor,
                    "accuracy_assessments": forecast.accuracy_assessments,
                    "reason": forecast.reason,
                }
                if forecast is not None
                else {}
            ),
            "adviser_chain": self._reasoning.adviser_chain_snapshot(),
            "physical_action_audit": [
                {
                    "target_signature": item.target_signature,
                    "result_count": item.result_count,
                    "settled_count": item.settled_count,
                    "recent_terminal_statuses": list(item.recent_terminal_statuses),
                }
                for item in (
                    physical_action_audit.targets if physical_action_audit else ()
                )
            ][-12:],
            "recent_chain": recent,
            "history": self._history.status(),
            "referee_assessment_history": referee_assessment_status,
            "decision_lifecycle_history": decision_history_status,
            "learning_population": learning_population,
        }

    def record_runtime_fault(
        self,
        *,
        operation: str,
        error_type: str,
        detail: str,
        observed_at: float,
        occurrence: int,
    ) -> None:
        """Keep distinct causes in the existing compressed History checkpoint.

        Runtime supplies exception details, never raw requests or credentials.
        Repeats update one entry; the journal coalesces checkpoint writes. A
        later fault must not erase the explanation for an earlier failure.
        """

        with self._lock:
            saved = self._history.load_checkpoint(
                scope_key="new_horizon_runtime", name="runtime_faults_v2")
            if saved is not None and saved.get("schema") != "NewHorizonRuntimeFaults.v2":
                raise ValueError("new_horizon_runtime_fault_checkpoint_invalid")
            faults = dict(saved["faults"]) if saved is not None else {}
            identity = dict(operation=str(operation)[:128], error_type=str(error_type)[:128],
                            detail=str(detail)[:1024])
            key = hashlib.sha256(json.dumps(dict(identity, detail=str(detail)), sort_keys=True).encode()).hexdigest()
            previous = faults.get(key)
            evicted = int(saved["evicted_causes"]) if saved is not None else 0
            if previous is None and len(faults) >= 32:
                oldest = min(faults, key=lambda k: (faults[k]["last_at"], k))
                del faults[oldest]
                evicted += 1
            faults[key] = dict(identity,
                first_at=previous["first_at"] if previous is not None else float(observed_at),
                last_at=float(observed_at), count=previous["count"] + 1 if previous is not None else 1)
            self._history.save_checkpoint(
                scope_key="new_horizon_runtime", name="runtime_faults_v2", updated_at=observed_at,
                payload=dict(schema="NewHorizonRuntimeFaults.v2", faults=faults,
                             latest_key=key, evicted_causes=evicted, occurrence=int(occurrence)))

    def presentation_projections(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return Referee history and Intelligence impact together."""

        with self._lock:
            context = self._stm.current
            if context is None:
                return (
                    {
                        "schema": BAND_CONTEXT_SCHEMA,
                        "current_timeline": [],
                        "recent_assessments": [],
                        "history_note": "Current context is unavailable.",
                    },
                    empty_intelligence_impact(),
                )
            now = context.accepted_at
            continuity_key = self._continuity_key
            if not continuity_key:
                raise RuntimeError("new_horizon_referee_history_owner_not_bound")
            cache_key = (
                continuity_key,
                self._impact_revision,
                int(now // 60.0),
            )
            assessment_history = self._assessment_history.current(continuity_key, now=now)
            decision_history = self._decision_history.current(continuity_key, now=now)
            if (cache_key != self._impact_cache_key
                    or not assessment_history.evidence_time_equivalent(self._impact_cache_at, now)
                    or not decision_history.evidence_time_equivalent(self._impact_cache_at, now)):
                since = now - INTELLIGENCE_IMPACT_WINDOW_S - 720.0
                self._impact_cache = build_intelligence_impact(
                    continuity_key=continuity_key,
                    proof_records=tuple(
                        record
                        for record in assessment_history.records
                        if float(record["observed_at"]) >= since
                    ),
                    decision_history=decision_history,
                    now=now,
                )
                self._impact_cache_key = cache_key
                self._impact_cache_at = now
            return (
                band_presentation(
                    referee_timeline=self._referee.context_timeline(),
                ),
                dict(self._impact_cache),
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._stm.current
            publication = self._publication
            status = {
                "schema": "NewHorizonAdvisoryEngineStatus.v1",
                "owner": "54guc.new_horizon.advisory_engine",
                "context_count": len(self._stm.contexts),
                "subject_count": len(self._stm.subjects),
                "matrix_sequence": (
                    current.matrix_identity.matrix_sequence if current else 0
                ),
                "selected_opportunity": (
                    publication.decision.selected_opportunity_id if publication else ""
                ),
                "decision_eligible": (
                    publication.decision.eligibility.eligible if publication else False
                ),
                "proof": self._referee.status(),
                "referee_assessment_history": self._assessment_history.status(),
                "decision_lifecycle_history": self._decision_history.status(),
                "learning_population": self._reasoning.history_population_status(),
                "learned_proof_count": self._reasoning.learned_proof_count,
                "explore": self._explore.health(),
            }
        status["history"] = self._history.status()
        return status

    def flush_for_support(self, *, cutoff: float, timeout_s: float) -> None:
        """Serialize with admissions; only the existing History writer drains."""
        from time import monotonic
        deadline = monotonic() + timeout_s
        if not self._lock.acquire(timeout=max(0., timeout_s)):
            raise TimeoutError("support_history_admission_timeout")
        try:
            self._history.flush_for_support(cutoff=cutoff, timeout_s=max(.001, deadline - monotonic()))
        finally:
            self._lock.release()

    def close(self, *, timeout_s: float = 2.0) -> None:
        """Require History to drain within the remaining shutdown budget."""

        self._history.close(timeout_s=timeout_s)


__all__ = ("AdvisoryEngine", "AdvisorySTM", "ContextIntake")
