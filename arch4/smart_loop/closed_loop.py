"""Clean-room closed-loop learning controller.

This module is intentionally independent from the historical controller,
arbitration, persistence, proof, and GUI modules. It models the target
smartness spine:

candidate usefulness -> TSAR operationalization -> proof/referee ->
ROI future bias -> simulator/static audit gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import blake2s
from typing import Any, Mapping, Sequence

from arch4.smart_loop.normal_behavior_progress import NormalBehaviorProgress

from arch4.smart_loop.opportunity_matrix import (
    OWNER as OPPORTUNITY_MATRIX_OWNER,
    SCHEMA as OPPORTUNITY_MATRIX_SCHEMA,
    build_opportunity_matrix,
    opportunity_for_target,
    opportunity_target_bands,
)
from arch4.smart_loop.proof_feedback_adapter import build_smart_loop_layered_feedback


SCHEMA = "smart_loop.closed_loop.v1"
OWNER = "arch4.smart_loop.closed_loop"
CANDIDATE_SCHEMA = "smart_loop.candidate.v1"
CLOSURE_AUDIT_SCHEMA = "smart_loop.closure_audit.v1"
BANDIT_DECISION_SCHEMA = "smart_loop.bandit_decision.v1"
BANDIT_REQUIREMENTS_SCHEMA = "smart_loop.bandit_requirements.v1"
BANDIT_REASONING_FLOW_SCHEMA = "smart_loop.bandit_reasoning_flow.v1"
SMART_LOOP_COMMAND_INTENT = "smart_loop_candidate"
RADIO_CONFIGURATION_ACTION = "apply_radio_configuration"
HELD_RADIO_CONFIGURATION_ACTION = "held_radio_configuration"
RECENT_WINDOW_SECONDS = (60, 360, 3600)
MAX_RECENT_OBSERVATIONS = 2400
TARGET_CONFIRMATION_TIMEOUT_S = 180.0
FAILED_OBJECTIVE_HOLD_S = 900.0


@dataclass(frozen=True)
class ModemContext:
    """Runtime truth snapshot consumed by the clean smart loop."""

    identity_code: str = "0x0000000000000000"
    identity_status: str = "unknown"
    model: str = ""
    provider_plmn: str = ""
    cell_id: int = 0
    pci: int = 0
    earfcn: int = 0
    nr_band: str = ""
    nr_pci: int = 0
    nr_arfcn: int = 0
    nr_cell_id: str = ""
    hour: int = 0
    current_pband: int = 0
    allowed_bands: tuple[int, ...] = ()
    ca_pool: tuple[int, ...] = ()
    cq: float | None = None
    congestion_pct: float | None = None
    ca_active: bool | None = None
    link_valid: bool = True
    cooldown_s: float = 0.0
    proof_pending: bool = False
    safety_block: str = ""
    profile_id: str = ""
    firmware: str = ""
    observed_ts: float = 0.0

    @property
    def learning_context_key(self) -> str:
        return "|".join(
            (
                str(self.identity_code or "0x0000000000000000"),
                str(self.provider_plmn or ""),
                str(int(self.cell_id or 0)),
                str(int(self.pci or 0)),
                str(int(self.earfcn or 0)),
                str(int(self.hour or 0)),
                str(self.nr_band or ""),
                str(int(self.nr_pci or 0)),
                str(int(self.nr_arfcn or 0)),
                str(self.nr_cell_id or ""),
            )
        )

    @property
    def authority_bands(self) -> tuple[int, ...]:
        return self.allowed_bands


@dataclass(frozen=True)
class LearningFacts:
    """Quality facts owned by self-learning input publication."""

    band_scores: Mapping[int, float] = field(default_factory=dict)
    evidence_counts: Mapping[int, int] = field(default_factory=dict)
    profile_confidence: float = 0.0
    sample_count: int = 0
    reason: str = ""
    action_roi: Mapping[str, Any] = field(default_factory=dict)
    calibration_fact_package: Mapping[str, Any] = field(default_factory=dict)
    prediction_context: Mapping[str, Any] = field(default_factory=dict)
    measured_capacity_fact: Mapping[str, Any] = field(default_factory=dict)
    passive_dl_peak_fact: Mapping[str, Any] = field(default_factory=dict)

    @property
    def calibration_clean_for_learning(self) -> bool:
        package = _mapping(self.calibration_fact_package)
        return (
            package.get("schema") == "calibration_fact_package.v1"
            and package.get("owner") == "calibration_fact_confidence"
            and bool(package.get("clean_for_learning", False))
        )

    @property
    def calibration_self_learning_strength(self) -> float:
        package = _mapping(self.calibration_fact_package)
        if not self.calibration_clean_for_learning:
            return 0.0
        return _score(package.get("self_learning_input_strength"))

    @property
    def calibration_preemption_ready(self) -> bool:
        package = _mapping(self.calibration_fact_package)
        return self.calibration_clean_for_learning and bool(
            package.get("strong_enough_for_preemption_facts", False)
        )

    @property
    def prediction_clean_for_learning(self) -> bool:
        context = _mapping(self.prediction_context)
        return (
            context.get("schema") == "crystal_ball_horizon_context.v1"
            and _owner_is_crystal_ball(context.get("owner"))
            and bool(context.get("advisory_only", False))
            and not bool(context.get("direct_modem_actuation", True))
        )

    @property
    def measured_capacity_clean_for_learning(self) -> bool:
        fact = _mapping(self.measured_capacity_fact)
        return (
            fact.get("schema") == "measured_capacity_fact.v1"
            and fact.get("owner") == "arch4.smart_loop.capacity_probe"
            and bool(fact.get("measured", False))
            and _score(fact.get("confidence")) > 0.0
        )

    @property
    def passive_dl_peak_clean_for_learning(self) -> bool:
        fact = _mapping(self.passive_dl_peak_fact)
        return (
            fact.get("schema") == "passive_dl_peak_fact.v1"
            and fact.get("owner") == "arch4.smart_loop.capacity_probe"
            and bool(fact.get("observed", False))
            and _float(fact.get("peak_dl_mbps_lower_bound"), 0.0) > 0.0
        )


@dataclass(frozen=True)
class Candidate:
    """One self-learning candidate with a machine-readable reason."""

    action: str
    target_band: int
    context_key: str
    score: float
    confidence: float
    evidence_count: int
    reason_code: str
    reason: str
    reason_ref: str
    roi_bias: float = 0.0
    target_bands: tuple[int, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return self.action in {RADIO_CONFIGURATION_ACTION, "stay_current_anchor"}

    @property
    def effective_target_bands(self) -> tuple[int, ...]:
        bands = _normalize_band_tuple(self.target_bands)
        if bands:
            return bands
        if int(self.target_band or 0) > 0:
            return (int(self.target_band),)
        return ()

    def as_dict(self) -> dict[str, object]:
        candidate_class = "operational" if self.executable else "advisory"
        if (
            self.confidence < SmartLoopCore.min_confidence
            or self.evidence_count < SmartLoopCore.min_evidence
        ):
            candidate_class = "blocked"
        context = _mapping(self.metadata.get("context"))
        target_bands = list(self.effective_target_bands)
        row = {
            "schema": CANDIDATE_SCHEMA,
            "owner": OWNER,
            "action": self.action,
            "band": self.target_band,
            "bands": target_bands,
            "target_bands": target_bands,
            "target_band": self.target_band,
            "context_key": self.context_key,
            "context": dict(context),
            "score": self.score,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "candidate_class": candidate_class,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "reason_ref": self.reason_ref,
            "roi_bias": self.roi_bias,
            "executable": self.executable,
            "direct_action_allowed": bool(
                self.executable and self.action == RADIO_CONFIGURATION_ACTION
            ),
            "direct_action_block_reason": ""
            if self.executable
            else f"non_direct_action:{self.action}",
        }
        row.update(dict(self.metadata or {}))
        return row


@dataclass(frozen=True)
class TsarGate:
    """Technical and safety gate verdict for a candidate."""

    verdict: str
    reason_code: str
    required_next_step: str

    def as_dict(self) -> dict[str, str]:
        return {
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "required_next_step": self.required_next_step,
        }


@dataclass(frozen=True)
class ActionPlan:
    """Executable TSAR-approved action."""

    action: str
    target_band: int
    reason_ref: str
    context_key: str
    command_id: str = ""
    proof_id: str = ""
    reason_code: str = ""
    reason: str = ""
    target_bands: tuple[int, ...] = ()
    objective_key: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        target_bands = list(
            _normalize_band_tuple(self.target_bands)
            or ((self.target_band,) if int(self.target_band or 0) > 0 else ())
        )
        row = {
            "action": self.action,
            "target_band": self.target_band,
            "band": self.target_band,
            "bands": target_bands,
            "target_bands": target_bands,
            "reason_ref": self.reason_ref,
            "context_key": self.context_key,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "objective_key": self.objective_key,
            "availability_intent": _availability_intent_for_plan(self),
        }
        row.update(dict(self.metadata or {}))
        return row


@dataclass(frozen=True)
class ProofWindow:
    """Action proof window opened at execution time."""

    action: str
    target_band: int
    context_key: str
    reason_ref: str
    proof_id: str
    command_id: str
    before_cq: float | None
    before_congestion_pct: float | None
    lever: str = ""
    target_shape: Mapping[str, object] = field(default_factory=dict)
    target_bands: tuple[int, ...] = ()
    objective_key: str = ""
    opened_ts: float = 0.0
    target_confirmation_timeout_s: float = TARGET_CONFIRMATION_TIMEOUT_S
    failed_objective_hold_s: float = FAILED_OBJECTIVE_HOLD_S
    feedback_horizons_done: tuple[int, ...] = ()
    interim_feedback: tuple[Mapping[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        projected_bands = _normalize_band_tuple(self.target_bands)
        if not projected_bands and self.target_band > 0:
            projected_bands = (self.target_band,)
        target_bands = list(projected_bands)
        return {
            "action": self.action,
            "lever": self.lever,
            "target_shape": dict(self.target_shape or {}),
            "target_band": self.target_band,
            "target_bands": target_bands,
            "context_key": self.context_key,
            "reason_ref": self.reason_ref,
            "proof_id": self.proof_id,
            "command_id": self.command_id,
            "objective_key": self.objective_key,
            "opened_ts": round(float(self.opened_ts or 0.0), 3),
            "target_confirmation_timeout_s": round(
                float(self.target_confirmation_timeout_s or 0.0), 3
            ),
            "failed_objective_hold_s": round(
                float(self.failed_objective_hold_s or 0.0), 3
            ),
            "feedback_horizons_done": list(self.feedback_horizons_done),
            "interim_feedback": [dict(row) for row in self.interim_feedback],
            "before_cq": self.before_cq,
            "before_congestion_pct": self.before_congestion_pct,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProofWindow":
        row = dict(raw or {})
        return cls(
            action=str(row.get("action") or ""),
            target_band=int(row.get("target_band") or 0),
            context_key=str(row.get("context_key") or ""),
            reason_ref=str(row.get("reason_ref") or ""),
            proof_id=str(row.get("proof_id") or ""),
            command_id=str(row.get("command_id") or ""),
            before_cq=_optional_float(row.get("before_cq")),
            before_congestion_pct=_optional_float(row.get("before_congestion_pct")),
            lever=str(row.get("lever") or ""),
            target_shape=dict(row.get("target_shape") or {}),
            target_bands=_normalize_band_tuple(row.get("target_bands") or ()),
            objective_key=str(row.get("objective_key") or ""),
            opened_ts=_float(row.get("opened_ts"), 0.0),
            target_confirmation_timeout_s=_float(
                row.get("target_confirmation_timeout_s"), TARGET_CONFIRMATION_TIMEOUT_S
            ),
            failed_objective_hold_s=_float(
                row.get("failed_objective_hold_s"), FAILED_OBJECTIVE_HOLD_S
            ),
            feedback_horizons_done=tuple(
                sorted(
                    {
                        int(value)
                        for value in list(row.get("feedback_horizons_done") or [])
                        if int(value) > 0
                    }
                )
            ),
            interim_feedback=tuple(
                dict(value)
                for value in list(row.get("interim_feedback") or [])
                if isinstance(value, Mapping)
            ),
        )


@dataclass(frozen=True)
class ProofGrade:
    """Referee outcome for a closed proof window."""

    label: str
    reason_code: str
    memory_admitted: bool
    cq_delta: float | None = None
    congestion_delta: float | None = None
    proof_id: str = ""
    command_id: str = ""
    context_key: str = ""
    reason_ref: str = ""
    layered_feedback: Mapping[str, object] = field(default_factory=dict)
    horizon_s: int = 0
    terminal: bool = True
    prediction_error_recorded: bool = False
    policy_update_applied: bool = False
    future_policy_effect_status: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "reason_code": self.reason_code,
            "memory_admitted": self.memory_admitted,
            "cq_delta": self.cq_delta,
            "congestion_delta": self.congestion_delta,
            "proof_id": self.proof_id,
            "command_id": self.command_id,
            "context_key": self.context_key,
            "reason_ref": self.reason_ref,
            "layered_feedback": dict(self.layered_feedback or {}),
            "horizon_s": int(self.horizon_s or 0),
            "terminal": bool(self.terminal),
            "prediction_error_recorded": bool(
                self.prediction_error_recorded
            ),
            "policy_update_applied": bool(self.policy_update_applied),
            "future_policy_effect_status": str(
                self.future_policy_effect_status or ""
            ),
        }


@dataclass(frozen=True)
class ObjectiveHold:
    """SmartLoop-owned pre-decision hold for an objective that just failed."""

    key: str
    action: str
    target_band: int
    lever: str = ""
    target_shape: Mapping[str, object] = field(default_factory=dict)
    target_bands: tuple[int, ...] = ()
    until_ts: float = 0.0
    reason_code: str = ""
    command_id: str = ""
    proof_id: str = ""

    def active(self, now_ts: float) -> bool:
        if float(self.until_ts or 0.0) <= 0.0:
            return False
        return float(now_ts or 0.0) <= 0.0 or float(now_ts or 0.0) < float(
            self.until_ts or 0.0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "action": self.action,
            "lever": self.lever,
            "target_shape": dict(self.target_shape or {}),
            "target_band": int(self.target_band or 0),
            "target_bands": list(
                _normalize_band_tuple(self.target_bands)
                or ((self.target_band,) if int(self.target_band or 0) > 0 else ())
            ),
            "until_ts": round(float(self.until_ts or 0.0), 3),
            "reason_code": self.reason_code,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ObjectiveHold":
        row = dict(raw or {})
        return cls(
            key=str(row.get("key") or ""),
            action=str(row.get("action") or ""),
            target_band=int(row.get("target_band") or 0),
            lever=str(row.get("lever") or ""),
            target_shape=dict(row.get("target_shape") or {}),
            target_bands=_normalize_band_tuple(row.get("target_bands") or ()),
            until_ts=_float(row.get("until_ts"), 0.0),
            reason_code=str(row.get("reason_code") or ""),
            command_id=str(row.get("command_id") or ""),
            proof_id=str(row.get("proof_id") or ""),
        )


@dataclass(frozen=True)
class SmartLoopObservation:
    """One SmartLoop-owned runtime observation for recent-window learning."""

    ts: float
    pband: int
    cq: float | None = None
    congestion_pct: float | None = None
    ca_active: bool | None = None
    link_valid: bool = True
    context_key: str = ""
    identity_code: str = ""
    provider_plmn: str = ""
    cell_id: int = 0
    pci: int = 0
    earfcn: int = 0

    @classmethod
    def from_context(cls, context: ModemContext) -> "SmartLoopObservation":
        return cls(
            ts=float(context.observed_ts or 0.0),
            pband=int(context.current_pband or 0),
            cq=context.cq,
            congestion_pct=context.congestion_pct,
            ca_active=context.ca_active,
            link_valid=bool(context.link_valid),
            context_key=context.learning_context_key,
            identity_code=str(context.identity_code or ""),
            provider_plmn=str(context.provider_plmn or ""),
            cell_id=int(context.cell_id or 0),
            pci=int(context.pci or 0),
            earfcn=int(context.earfcn or 0),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": round(float(self.ts or 0.0), 3),
            "pband": int(self.pband or 0),
            "cq": self.cq,
            "congestion_pct": self.congestion_pct,
            "ca_active": self.ca_active,
            "link_valid": bool(self.link_valid),
            "context_key": self.context_key,
            "identity_code": self.identity_code,
            "provider_plmn": self.provider_plmn,
            "cell_id": int(self.cell_id or 0),
            "pci": int(self.pci or 0),
            "earfcn": int(self.earfcn or 0),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SmartLoopObservation":
        row = dict(raw or {})
        return cls(
            ts=_float(row.get("ts"), 0.0),
            pband=int(row.get("pband") or 0),
            cq=_optional_float(row.get("cq")),
            congestion_pct=_optional_float(row.get("congestion_pct")),
            ca_active=(
                None if row.get("ca_active") is None else bool(row.get("ca_active"))
            ),
            link_valid=bool(row.get("link_valid", True)),
            context_key=str(row.get("context_key") or ""),
            identity_code=str(row.get("identity_code") or ""),
            provider_plmn=str(row.get("provider_plmn") or ""),
            cell_id=int(row.get("cell_id") or 0),
            pci=int(row.get("pci") or 0),
            earfcn=int(row.get("earfcn") or 0),
        )


@dataclass(frozen=True)
class SmartLoopState:
    """All mutable smart-loop truth, carried as immutable snapshots."""

    pending_proof: ProofWindow | None = None
    observations: tuple[SmartLoopObservation, ...] = ()
    objective_holds: Mapping[str, ObjectiveHold] = field(default_factory=dict)
    normal_behavior_progress: NormalBehaviorProgress = field(
        default_factory=NormalBehaviorProgress
    )

    def with_normal_behavior_progress(
        self, progress: NormalBehaviorProgress
    ) -> "SmartLoopState":
        if not isinstance(progress, NormalBehaviorProgress):
            raise TypeError("normal_behavior_progress_required")
        return replace(self, normal_behavior_progress=progress)

    def with_proof(self, proof: ProofWindow | None) -> "SmartLoopState":
        return replace(self, pending_proof=proof)

    def with_objective_hold(self, hold: ObjectiveHold) -> "SmartLoopState":
        holds = dict(self.objective_holds)
        holds[str(hold.key)] = hold
        return replace(self, objective_holds=holds)

    def with_pruned_objective_holds(self, now_ts: float) -> "SmartLoopState":
        if not self.objective_holds:
            return self
        kept = {
            key: hold
            for key, hold in self.objective_holds.items()
            if hold.active(now_ts)
        }
        if len(kept) == len(self.objective_holds):
            return self
        return replace(self, objective_holds=kept)

    def active_hold_for_candidate(
        self, *, context: ModemContext, candidate: "Candidate"
    ) -> ObjectiveHold | None:
        key = smart_loop_objective_key(
            context=context,
            action=RADIO_CONFIGURATION_ACTION
            if candidate.action == HELD_RADIO_CONFIGURATION_ACTION
            else candidate.action,
            target_band=candidate.target_band,
            target_bands=candidate.effective_target_bands,
        )
        hold = self.objective_holds.get(key)
        if hold and hold.active(_context_ts(context)):
            return hold
        return None

    def with_observation(self, context: ModemContext) -> "SmartLoopState":
        state = self.with_pruned_objective_holds(_context_ts(context))
        observation = SmartLoopObservation.from_context(context)
        if (
            observation.ts <= 0.0
            or observation.pband <= 0
            or not observation.link_valid
            or (
                observation.cq is None
                and observation.congestion_pct is None
                and observation.ca_active is None
            )
        ):
            return state
        newest = float(observation.ts)
        cutoff = newest - float(max(RECENT_WINDOW_SECONDS))
        rows = [
            item
            for item in state.observations
            if float(item.ts or 0.0) >= cutoff
            and float(item.ts or 0.0) <= newest
            and _observation_context_matches(item, observation)
        ]
        if rows and rows[-1] == observation:
            return state
        rows.append(observation)
        if len(rows) > MAX_RECENT_OBSERVATIONS:
            rows = rows[-MAX_RECENT_OBSERVATIONS:]
        return replace(state, observations=tuple(rows))

    def admit_grade(self, proof: ProofWindow, grade: ProofGrade) -> "SmartLoopState":
        if not grade.memory_admitted:
            return self.with_proof(None)
        new_holds = dict(self.objective_holds)
        if grade.label == "bad":
            hold = _objective_hold_from_proof(proof=proof, grade=grade)
            if hold is not None:
                new_holds[hold.key] = hold
        # RewardLedger -> durable action_roi is the only future score-bias path.
        # The SmartLoop register still consumes the grade to close proof and
        # install a safety hold, but it must not independently learn the same
        # reward into a second ROI accumulator.
        return replace(self, pending_proof=None, objective_holds=new_holds)

    def admit_interim_feedback(
        self, proof: ProofWindow, grade: ProofGrade
    ) -> "SmartLoopState":
        """Retain a Referee milestone without admitting durable ROI."""

        if grade.terminal or grade.memory_admitted:
            raise ValueError("smart_loop_interim_feedback_must_be_non_terminal")
        horizon = int(grade.horizon_s or 0)
        if horizon <= 0:
            raise ValueError("smart_loop_interim_feedback_horizon_missing")
        if grade.command_id != proof.command_id or grade.proof_id != proof.proof_id:
            raise ValueError("smart_loop_interim_feedback_identity_mismatch")
        done = tuple(sorted(set(proof.feedback_horizons_done) | {horizon}))
        feedback = [dict(row) for row in proof.interim_feedback]
        feedback.append(grade.as_dict())
        updated = replace(
            proof,
            feedback_horizons_done=done,
            interim_feedback=tuple(feedback[-8:]),
        )
        return replace(self, pending_proof=updated)

    def as_dict(self) -> dict[str, object]:
        return {
            "pending_proof": self.pending_proof.as_dict()
            if self.pending_proof
            else None,
            "objective_holds": {
                key: value.as_dict()
                for key, value in sorted(self.objective_holds.items())
            },
            "recent_windows": _recent_window_state_summary(self.observations),
            "normal_behavior_progress": self.normal_behavior_progress.as_dict(),
        }

    def as_persistence_dict(self) -> dict[str, object]:
        """Return exact replay state, including raw context-scoped observations."""

        return {
            "pending_proof": self.pending_proof.as_dict()
            if self.pending_proof
            else None,
            "objective_holds": {
                key: value.as_dict()
                for key, value in sorted(self.objective_holds.items())
            },
            "observations": [value.as_dict() for value in self.observations],
            "normal_behavior_progress": self.normal_behavior_progress.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SmartLoopState":
        row = dict(raw or {})
        if "roi" in row:
            raise ValueError("legacy_smart_loop_roi_state_forbidden")
        holds = {
            str(key): ObjectiveHold.from_dict(value)
            for key, value in dict(row.get("objective_holds") or {}).items()
            if isinstance(value, Mapping)
        }
        proof_raw = row.get("pending_proof")
        proof = (
            ProofWindow.from_dict(proof_raw) if isinstance(proof_raw, Mapping) else None
        )
        observations = tuple(
            SmartLoopObservation.from_dict(value)
            for value in list(row.get("observations") or [])
            if isinstance(value, Mapping)
        )[-MAX_RECENT_OBSERVATIONS:]
        progress_raw = row.get("normal_behavior_progress")
        if not isinstance(progress_raw, Mapping):
            raise ValueError("normal_behavior_progress_state_missing")
        return cls(
            pending_proof=proof,
            observations=observations,
            objective_holds=holds,
            normal_behavior_progress=NormalBehaviorProgress.from_dict(progress_raw),
        )


@dataclass(frozen=True)
class ClosedLoopDecision:
    """One complete smart-loop decision publication."""

    schema: str
    owner: str
    outcome: str
    reason_code: str
    required_next_step: str
    candidates: tuple[Candidate, ...]
    selected: ActionPlan | None
    tsar_gate: TsarGate | None
    opened_proof: ProofWindow | None
    closed_proof: ProofGrade | None
    state: SmartLoopState
    policy_update_applied: bool = False
    future_policy_effect_status: str = ""
    proof_feedback: ProofGrade | None = None
    action_opportunity_set: Mapping[str, object] = field(default_factory=dict)
    contextual_bandit_selection_receipt: Mapping[str, object] = field(
        default_factory=dict
    )
    brain_decision_receipt: Mapping[str, object] = field(default_factory=dict)
    brain_selected_action: Mapping[str, object] = field(default_factory=dict)
    brain_selected_action_proof_template: Mapping[str, object] = field(
        default_factory=dict
    )

    def as_register_payload(self) -> dict[str, object]:
        learning_loop_closed = bool(
            self.closed_proof is not None and self.closed_proof.memory_admitted
        )
        if self.outcome == "action_selected_execution_pending":
            public_next_step = "wait_for_selected_action_lifecycle_receipts"
        elif self.outcome in {"proof_pending", "proof_feedback_recorded"}:
            public_next_step = "wait_for_referee_window"
        elif learning_loop_closed:
            public_next_step = "commit_learning_memory"
        elif self.outcome == "proof_invalid":
            public_next_step = "inspect_proof_contract"
        else:
            public_next_step = self.required_next_step
        diagnostic_signal = self.outcome in {
            "action_rejected",
            "candidate_missing_counterfactual",
            "proof_closed_bad",
            "proof_invalid",
        }
        learning_signal = learning_loop_closed or self.outcome in {
            "action_selected_execution_pending",
            "proof_pending",
            "proof_feedback_recorded",
        }
        selected_projection = _decision_selected_projection(self)
        gate_projection = _decision_gate_projection(self)
        candidate_rows = [candidate.as_dict() for candidate in self.candidates]
        candidate_counts = _candidate_counts(
            candidate_rows=candidate_rows,
            actionable=bool(self.selected or self.brain_selected_action),
        )
        bandit_decision = _bandit_decision(
            decision=self,
            candidate_rows=candidate_rows,
            public_next_step=public_next_step,
        )
        bandit_reasoning_flow = _bandit_reasoning_flow(
            decision=self,
            candidate_rows=candidate_rows,
            candidate_counts=candidate_counts,
            bandit_decision=bandit_decision,
            public_next_step=public_next_step,
        )
        return {
            "schema": self.schema,
            "owner": self.owner,
            "outcome": self.outcome,
            "reason": self.reason_code,
            "reason_code": self.reason_code,
            "required_next_step": public_next_step,
            "learning_loop_closed": learning_loop_closed,
            "learning_signal": learning_signal,
            "diagnostic_signal": diagnostic_signal,
            "candidates": candidate_rows,
            "candidate_counts": candidate_counts,
            "selected": selected_projection or None,
            "tsar_gate": gate_projection or None,
            "inputs": {
                "tsar": gate_projection,
                "self_learning": {
                    "status": self.outcome,
                    "reason": self.reason_code,
                    "required_next_step": public_next_step,
                },
            },
            "opened_proof": self.opened_proof.as_dict() if self.opened_proof else None,
            "closed_proof": self.closed_proof.as_dict() if self.closed_proof else None,
            "proof_feedback": self.proof_feedback.as_dict()
            if self.proof_feedback
            else None,
            "closure_audit": _closure_audit(self),
            "bandit_decision": bandit_decision,
            "bandit_requirements": bandit_reasoning_flow["requirements"],
            "bandit_reasoning_flow": bandit_reasoning_flow,
            "action_opportunity_set": dict(self.action_opportunity_set or {}),
            "contextual_bandit_selection_receipt": dict(
                self.contextual_bandit_selection_receipt or {}
            ),
            "brain_decision_receipt": dict(self.brain_decision_receipt or {}),
            "evidence": {},
            "state": self.state.as_dict(),
        }


def _decision_selected_projection(
    decision: ClosedLoopDecision,
) -> dict[str, object]:
    if decision.selected is not None:
        return decision.selected.as_dict()
    selected = _mapping(decision.brain_selected_action)
    if not selected:
        return {}
    proof = _mapping(decision.brain_selected_action_proof_template)
    params = _mapping(selected.get("params"))
    lever = str(selected.get("lever") or proof.get("lever") or "")
    target_band = 0
    target_bands: list[int] = []
    if lever == "PBAND":
        target_band = int(params.get("pband") or 0)
        target_bands = list(_normalize_band_tuple(params.get("ca_pool") or ()))
        if target_band > 0 and target_band not in target_bands:
            target_bands = sorted(set(target_bands) | {target_band})
    elif lever in {"BAND_SELECTION", "BAND_LOCK"}:
        target_bands = list(_normalize_band_tuple(params.get("bands") or ()))
        target_band = target_bands[0] if target_bands else 0
    else:
        target_band = int(params.get("band") or 0)
        target_bands = [target_band] if target_band > 0 else []
    return {
        "action": str(proof.get("action") or selected.get("intent") or lever),
        "lever": lever,
        "target_shape": params,
        "target_band": target_band,
        "band": target_band,
        "target_bands": target_bands,
        "reason_ref": str(proof.get("reason_ref") or ""),
        "reason_code": str(
            _mapping(selected.get("optimization_metadata")).get("reason_code")
            or selected.get("intent")
            or ""
        ),
        "reason": str(selected.get("reason") or ""),
        "context_key": str(proof.get("context_key") or ""),
        "command_id": str(selected.get("command_id") or ""),
        "proof_id": str(selected.get("proof_id") or ""),
        "objective_key": str(proof.get("objective_key") or ""),
        "opportunity_set_id": str(selected.get("opportunity_set_id") or ""),
        "opportunity_id": str(selected.get("opportunity_id") or ""),
    }


def _decision_gate_projection(decision: ClosedLoopDecision) -> dict[str, str]:
    if decision.tsar_gate is not None:
        return decision.tsar_gate.as_dict()
    if decision.brain_selected_action:
        return {
            "verdict": "act",
            "reason_code": "opportunity_set_bandit_brain_selected",
            "required_next_step": "wait_for_selected_action_lifecycle_receipts",
        }
    return {}


def _candidate_counts(
    *,
    candidate_rows: Sequence[Mapping[str, Any]],
    actionable: bool,
) -> dict[str, int]:
    counts = {
        "total": len(candidate_rows),
        "operational": 0,
        "actionable": 1 if actionable else 0,
        "advisory": 0,
        "blocked": 0,
    }
    for row in candidate_rows:
        candidate_class = str(row.get("candidate_class") or "")
        if candidate_class == "operational":
            counts["operational"] += 1
        elif candidate_class == "blocked":
            counts["blocked"] += 1
        else:
            counts["advisory"] += 1
    return counts


def _bandit_reasoning_flow(
    *,
    decision: ClosedLoopDecision,
    candidate_rows: Sequence[Mapping[str, Any]],
    candidate_counts: Mapping[str, int],
    bandit_decision: Mapping[str, object],
    public_next_step: str,
) -> dict[str, object]:
    selected = _decision_selected_projection(decision)
    gate = _decision_gate_projection(decision)
    context = _primary_bandit_context(candidate_rows=candidate_rows, selected=selected)
    evidence_fields = _candidate_evidence_fields(candidate_rows)
    proof_row = decision.opened_proof.as_dict() if decision.opened_proof else {}
    closed_proof = decision.closed_proof.as_dict() if decision.closed_proof else {}
    proof_feedback = (
        decision.proof_feedback.as_dict() if decision.proof_feedback else {}
    )
    memory_grade_score = _closed_proof_memory_grade_score(decision.closed_proof)
    requirements = _bandit_requirements(
        candidate_rows=candidate_rows,
        candidate_counts=candidate_counts,
        gate=gate,
        selected=selected,
        proof_row=proof_row,
        closed_proof=closed_proof,
        evidence_fields=evidence_fields,
        state=decision.state,
        bandit_decision=bandit_decision,
    )
    return {
        "schema": BANDIT_REASONING_FLOW_SCHEMA,
        "owner": OWNER,
        "requirements": requirements,
        "decision": dict(bandit_decision),
        "context_vector": {
            "schema": "smart_loop.bandit_context_vector.v1",
            "owner": OWNER,
            "context_key": str(
                selected.get("context_key") or context.get("context_key") or ""
            ),
            "identity_code": str(context.get("identity_code") or ""),
            "identity_status": str(context.get("identity_status") or ""),
            "model": str(context.get("model") or ""),
            "provider_plmn": str(context.get("provider_plmn") or ""),
            "cell_id": int(context.get("cell_id") or 0),
            "pci": int(context.get("pci") or 0),
            "earfcn": int(context.get("earfcn") or 0),
            "hour": int(context.get("hour") or 0),
            "current_pband": int(context.get("current_pband") or 0),
            "ca_pool": list(context.get("ca_pool") or []),
            "authority_bands": list(context.get("authority_bands") or []),
            "cq": _round_optional(context.get("cq")),
            "congestion_pct": _round_optional(context.get("congestion_pct")),
            "ca_active": context.get("ca_active")
            if context.get("ca_active") is not None
            else None,
            "nr_band": str(context.get("nr_band") or ""),
            "nr_pci": int(context.get("nr_pci") or 0),
            "nr_arfcn": int(context.get("nr_arfcn") or 0),
            "nr_cell_id": str(context.get("nr_cell_id") or ""),
        },
        "action_space": {
            "schema": "smart_loop.bandit_action_space.v1",
            "owner": OWNER,
            "candidate_counts": dict(candidate_counts),
            "actions": [
                {
                    "action": str(row.get("action") or ""),
                    "candidate_class": str(row.get("candidate_class") or ""),
                    "target_band": int(row.get("target_band") or row.get("band") or 0),
                    "target_bands": list(
                        row.get("target_bands") or row.get("bands") or []
                    ),
                    "score": _float(row.get("score"), 0.0),
                    "confidence": _float(row.get("confidence"), 0.0),
                    "evidence_count": int(row.get("evidence_count") or 0),
                    "reason_code": str(row.get("reason_code") or ""),
                    "reason_ref": str(row.get("reason_ref") or ""),
                }
                for row in candidate_rows
            ],
        },
        "safety_shield": {
            "schema": "smart_loop.bandit_safety_shield.v1",
            "owner": OWNER,
            "tsar_verdict": str(gate.get("verdict") or ""),
            "reason_code": str(gate.get("reason_code") or decision.reason_code or ""),
            "required_next_step": str(
                gate.get("required_next_step") or public_next_step or ""
            ),
            "command_allowed": bool(selected),
            "proof_pending": decision.outcome
            in {"proof_pending", "proof_feedback_recorded"},
        },
        "reward_path": {
            "schema": "smart_loop.bandit_reward_path.v1",
            "owner": OWNER,
            "opened_proof": bool(proof_row),
            "closed_proof": bool(closed_proof),
            "proof_id": str(
                proof_row.get("proof_id")
                or closed_proof.get("proof_id")
                or selected.get("proof_id")
                or ""
            ),
            "command_id": str(
                proof_row.get("command_id")
                or closed_proof.get("command_id")
                or selected.get("command_id")
                or ""
            ),
            "closed_label": str(closed_proof.get("label") or ""),
            "memory_admitted": bool(closed_proof.get("memory_admitted", False)),
            "memory_grade_score": memory_grade_score,
            "grade_scale": [-3, -2, -1, 0, 1, 2, 3],
            "feedback_horizon_s": int(proof_feedback.get("horizon_s") or 0),
            "feedback_label": str(proof_feedback.get("label") or ""),
            "feedback_terminal": bool(proof_feedback.get("terminal", False)),
        },
        "policy_logging": {
            "schema": "smart_loop.bandit_policy_log.v1",
            "owner": OWNER,
            "reason_refs": [
                str(row.get("reason_ref") or "")
                for row in candidate_rows
                if str(row.get("reason_ref") or "")
            ],
            "selected_reason_ref": str(selected.get("reason_ref") or ""),
            "selected_command_id": str(selected.get("command_id") or ""),
            "selected_proof_id": str(
                selected.get("proof_id") or proof_row.get("proof_id") or ""
            ),
            "candidate_evidence_fields": evidence_fields,
        },
        "policy_effect": {
            "schema": "smart_loop.bandit_policy_effect.v1",
            "owner": OWNER,
            "pending_proof": bool(decision.state.pending_proof),
            "prediction_error_recorded": bool(
                decision.closed_proof
                and decision.closed_proof.prediction_error_recorded
            ),
            "policy_update_applied": bool(
                decision.policy_update_applied
            ),
            "future_policy_effect_status": str(
                decision.future_policy_effect_status or ""
            ),
        },
    }


def _bandit_requirements(
    *,
    candidate_rows: Sequence[Mapping[str, Any]],
    candidate_counts: Mapping[str, int],
    gate: Mapping[str, Any],
    selected: Mapping[str, Any],
    proof_row: Mapping[str, Any],
    closed_proof: Mapping[str, Any],
    evidence_fields: Sequence[str],
    state: SmartLoopState,
    bandit_decision: Mapping[str, object],
) -> dict[str, object]:
    context = _primary_bandit_context(candidate_rows=candidate_rows, selected={})
    context_present = bool(context.get("context_key") or context.get("identity_code"))
    discrete_action_space = int(candidate_counts.get("total", 0) or 0) > 0
    scored_candidates = bool(candidate_rows) and all(
        "score" in row
        and "confidence" in row
        and "evidence_count" in row
        and "reason_ref" in row
        for row in candidate_rows
    )
    safety_shield_present = bool(gate.get("verdict") and gate.get("reason_code"))
    decision_present = bool(bandit_decision.get("decision_made"))
    decision_kind = str(bandit_decision.get("decision_kind") or "")
    executable_decision = decision_kind == "execute"
    proof_or_reward_path = (
        bool(
            proof_row
            or closed_proof
            or state.pending_proof
            or (selected.get("command_id") and selected.get("proof_id"))
        )
        if executable_decision
        else decision_present
    )
    reward_path_state = (
        "required_for_executable_decision"
        if executable_decision
        else "not_required_for_non_executable_decision"
    )
    policy_logging_present = bool(evidence_fields) and all(
        str(row.get("reason_ref") or "") and str(row.get("context_key") or "")
        for row in candidate_rows
    )
    future_policy_path = True
    ready = all(
        (
            context_present,
            discrete_action_space,
            scored_candidates,
            safety_shield_present,
            decision_present,
            proof_or_reward_path,
            policy_logging_present,
            future_policy_path,
        )
    )
    return {
        "schema": BANDIT_REQUIREMENTS_SCHEMA,
        "owner": OWNER,
        "context_vector": context_present,
        "discrete_action_space": discrete_action_space,
        "candidate_scores": scored_candidates,
        "safety_shield": safety_shield_present,
        "bandit_decision": decision_present,
        "decision_kind": decision_kind,
        "proof_reward_path": proof_or_reward_path,
        "reward_path_state": reward_path_state,
        "policy_logging": policy_logging_present,
        "future_policy_path": future_policy_path,
        "ready": ready,
    }


def _bandit_decision(
    *,
    decision: ClosedLoopDecision,
    candidate_rows: Sequence[Mapping[str, Any]],
    public_next_step: str,
) -> dict[str, object]:
    selected = _decision_selected_projection(decision)
    gate = _decision_gate_projection(decision)
    candidate = dict(selected or _primary_decision_candidate(candidate_rows))
    action = str(candidate.get("action") or "")
    gate_reason = str(gate.get("reason_code") or "")
    reason_code = str(
        gate_reason or candidate.get("reason_code") or decision.reason_code or ""
    )

    if selected:
        decision_kind = "execute"
        semantic = "brain_selected_action"
        non_executable_reason = ""
    elif gate.get("verdict") == "refused":
        decision_kind = "blocked"
        semantic = "safety_blocked"
        non_executable_reason = reason_code
    elif action == "stay_current_anchor" or reason_code in {
        "current_anchor_already_best",
        "already_on_candidate_configuration",
    }:
        decision_kind = "stay"
        semantic = "stay_current_anchor"
        non_executable_reason = reason_code
    elif action == "avoid_band" or reason_code == "advisory_avoid_band_recorded":
        decision_kind = "advisory"
        semantic = "advisory_only"
        non_executable_reason = reason_code
    elif candidate_rows:
        decision_kind = "advisory"
        semantic = "candidate_suggestion_recorded"
        non_executable_reason = reason_code or "candidate_not_executed"
    else:
        decision_kind = "observe"
        semantic = "observe_only"
        non_executable_reason = reason_code or "no_candidate"

    reason = str(candidate.get("reason") or reason_code or semantic)
    reason_ref = str(
        selected.get("reason_ref")
        or candidate.get("reason_ref")
        or ""
    )
    target_bands = list(
        selected.get("target_bands")
        or candidate.get("target_bands")
        or candidate.get("bands")
        or []
    )
    candidate_reason_refs = [
        str(row.get("reason_ref") or "")
        for row in candidate_rows
        if str(row.get("reason_ref") or "")
    ]
    return {
        "schema": BANDIT_DECISION_SCHEMA,
        "owner": OWNER,
        "decision_made": bool(selected or candidate_rows or gate),
        "decision_kind": decision_kind,
        "decision_semantic": semantic,
        "selected_action": str(action),
        "target_band": int(
            selected.get("target_band")
            or candidate.get("target_band")
            or candidate.get("band")
            or 0
        ),
        "target_bands": target_bands,
        "reason_code": reason_code,
        "reason": reason,
        "reason_ref": reason_ref,
        "required_next_step": str(
            gate.get("required_next_step") or public_next_step or ""
        ),
        "tsar_verdict": str(gate.get("verdict") or ""),
        "score": _float(candidate.get("score"), 0.0),
        "confidence": _float(candidate.get("confidence"), 0.0),
        "evidence_count": int(candidate.get("evidence_count") or 0),
        "non_executable_reason": non_executable_reason,
        "proof_required": decision_kind == "execute",
        "candidate_reason_refs": candidate_reason_refs,
    }


def _primary_decision_candidate(
    candidate_rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    if not candidate_rows:
        return {}
    for row in candidate_rows:
        if str(row.get("candidate_class") or "") == "operational":
            return row
    return candidate_rows[0]


def _stay_current_anchor_gate(candidate: Candidate) -> TsarGate:
    reason_code = str(candidate.reason_code or "").strip()
    if reason_code == "current_anchor_is_best_known":
        return TsarGate("no_action", "current_anchor_already_best", "keep_observing")
    if reason_code == "current_anchor_penalized_by_roi":
        return TsarGate(
            "no_action",
            "current_anchor_penalized_no_better_candidate",
            "generate_better_executable_candidate",
        )
    return TsarGate("no_action", "stay_current_anchor_selected", "keep_observing")


def _primary_bandit_context(
    *,
    candidate_rows: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any],
) -> Mapping[str, Any]:
    selected_context = _mapping(selected.get("context"))
    if selected_context:
        row = dict(selected_context)
        if selected.get("context_key"):
            row["context_key"] = str(selected.get("context_key") or "")
        return row
    for candidate in candidate_rows:
        context = _mapping(candidate.get("context"))
        if context:
            row = dict(context)
            if candidate.get("context_key"):
                row["context_key"] = str(candidate.get("context_key") or "")
            return row
    return {}


def _candidate_evidence_fields(
    candidate_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    fields = (
        "context",
        "opportunity_matrix",
        "calibration_fact_package",
        "prediction_context",
        "recent_window_context",
        "measured_capacity_fact",
        "passive_dl_peak_fact",
        "action_shape_roi",
        "action_shape_roi_context_match",
    )
    present: list[str] = []
    for field_id in fields:
        if any(
            bool(_mapping(row.get(field_id)) or row.get(field_id))
            for row in candidate_rows
        ):
            present.append(field_id)
    return present


def _closed_proof_memory_grade_score(proof: ProofGrade | None) -> int:
    if proof is None:
        return 0
    return _proof_feedback_grade_score(proof)


def _closure_audit(decision: ClosedLoopDecision) -> dict[str, object]:
    opportunity_rows = [
        row
        for row in list(
            _mapping(decision.action_opportunity_set).get("opportunities") or []
        )
        if isinstance(row, Mapping)
    ]
    candidate_generated = bool(
        decision.candidates
        or any(
            str(row.get("action") or "") != "do_nothing"
            for row in opportunity_rows
        )
    )
    tsar_arbitrated = bool(
        decision.tsar_gate is not None or decision.brain_decision_receipt
    )
    action_selected = bool(decision.selected is not None or decision.brain_selected_action)
    selected_action_materialized = bool(decision.brain_selected_action)
    proof_opened = decision.opened_proof is not None
    proof_closed = decision.closed_proof is not None
    proof_feedback_received = decision.proof_feedback is not None
    memory_admitted = bool(
        decision.closed_proof and decision.closed_proof.memory_admitted
    )
    prediction_error_recorded = bool(
        decision.closed_proof
        and decision.closed_proof.prediction_error_recorded
    )
    policy_update_applied = bool(decision.policy_update_applied)
    future_policy_effect_status = str(
        decision.future_policy_effect_status or ""
    )
    links = {
        "candidate_generated": candidate_generated,
        "tsar_arbitrated": tsar_arbitrated,
        "action_selected": action_selected,
        "selected_action_materialized": selected_action_materialized,
        "proof_opened": proof_opened,
        "proof_closed": proof_closed,
        "proof_feedback_received": proof_feedback_received,
        "memory_admitted": memory_admitted,
        "prediction_error_recorded": prediction_error_recorded,
        "policy_update_applied": policy_update_applied,
        "policy_update_owner": "arch4.contextual_bandit_state_store",
    }
    status = _closure_status(decision, links)
    missing = _closure_missing_links(decision, links)
    return {
        "schema": CLOSURE_AUDIT_SCHEMA,
        "owner": OWNER,
        "status": status,
        "reason_code": decision.reason_code,
        "required_next_step": decision.required_next_step,
        "links": links,
        "missing_links": missing,
        "memory_admitted": memory_admitted,
        "prediction_error_recorded": prediction_error_recorded,
        "policy_update_applied": policy_update_applied,
        "future_policy_effect_status": future_policy_effect_status,
    }


def _closure_status(decision: ClosedLoopDecision, links: Mapping[str, bool]) -> str:
    if decision.outcome == "candidate_missing_counterfactual":
        return "candidate_generation_gap"
    if decision.outcome == "proof_invalid":
        return "proof_invalid"
    if decision.outcome in {"proof_pending", "proof_feedback_recorded"}:
        return "proof_pending"
    if decision.outcome == "action_selected_execution_pending":
        return "selected_action_pending_execution"
    if (
        links["proof_closed"]
        and links["memory_admitted"]
        and links["policy_update_applied"]
    ):
        if decision.future_policy_effect_status == "observed":
            return "closed_policy_effect_observed"
        return "closed_policy_update_applied_effect_pending"
    if links["proof_closed"] and links["memory_admitted"]:
        return "closed_reward_admitted_policy_not_applicable"
    if links["proof_closed"]:
        return "proof_closed_not_learned"
    if links["action_selected"] and links["proof_opened"]:
        return "proof_pending"
    if decision.outcome == "action_rejected":
        return "action_rejected"
    if decision.tsar_gate and decision.tsar_gate.verdict == "no_action":
        return "no_action_justified"
    return "observing"


def _closure_missing_links(
    decision: ClosedLoopDecision, links: Mapping[str, bool]
) -> list[str]:
    if decision.outcome == "candidate_missing_counterfactual":
        return [
            "candidate_generated",
            "action_selected",
            "selected_action_materialized",
            "proof_opened",
        ]
    if decision.outcome == "action_rejected":
        return [
            name
            for name in ("selected_action_materialized", "proof_opened")
            if not links[name]
        ]
    if decision.outcome == "proof_invalid":
        return [
            name
            for name in ("memory_admitted",)
            if not links[name]
        ]
    if decision.outcome in {"proof_pending", "proof_feedback_recorded"}:
        return [
            name
            for name in ("proof_closed", "memory_admitted")
            if not links[name]
        ]
    if decision.outcome == "action_selected_execution_pending":
        return [
            name
            for name in (
                "selected_action_materialized",
                "proof_opened",
                "proof_closed",
                "memory_admitted",
            )
            if not links[name]
        ]
    if links["proof_closed"]:
        # A terminal Referee grade is projected by RewardLedger.  SmartLoop
        # must not independently mutate a second local score accumulator.
        return ["memory_admitted"] if not links["memory_admitted"] else []
    if links["action_selected"]:
        return [
            name
            for name in (
                "selected_action_materialized",
                "proof_opened",
                "proof_closed",
                "memory_admitted",
            )
            if not links[name]
        ]
    if decision.tsar_gate and decision.tsar_gate.verdict == "no_action":
        return []
    return [
        name for name in ("candidate_generated", "tsar_arbitrated") if not links[name]
    ]


def _context_ts(context: ModemContext) -> float:
    try:
        return float(context.observed_ts or 0.0)
    except Exception:
        return 0.0


def _context_has_valid_proof_time(context: ModemContext) -> bool:
    return _context_ts(context) > 0.0


def smart_loop_objective_key(
    *,
    context: ModemContext,
    action: str,
    target_band: int,
    target_bands: Sequence[int] | None = None,
) -> str:
    bands = _normalize_band_tuple(target_bands or ())
    if not bands and int(target_band or 0) > 0:
        bands = (int(target_band),)
    return "|".join(
        (
            str(context.identity_code or "0x0000000000000000"),
            str(context.provider_plmn or ""),
            str(context.profile_id or ""),
            str(action or ""),
            f"B{int(target_band or 0)}",
            "+".join(f"B{band}" for band in bands),
        )
    )


def _objective_hold_from_proof(
    *, proof: ProofWindow, grade: ProofGrade
) -> ObjectiveHold | None:
    key = str(proof.objective_key or "").strip()
    if not key:
        return None
    hold_s = max(0.0, float(proof.failed_objective_hold_s or FAILED_OBJECTIVE_HOLD_S))
    if hold_s <= 0.0:
        return None
    opened_ts = float(proof.opened_ts or 0.0)
    until_ts = opened_ts + hold_s if opened_ts > 0.0 else hold_s
    return ObjectiveHold(
        key=key,
        action=proof.action,
        target_band=int(proof.target_band or 0),
        lever=proof.lever,
        target_shape=dict(proof.target_shape or {}),
        target_bands=_normalize_band_tuple(proof.target_bands)
        or (
            (int(proof.target_band),)
            if int(proof.target_band or 0) > 0
            else ()
        ),
        until_ts=until_ts,
        reason_code=str(grade.reason_code or "objective_feedback"),
        command_id=str(proof.command_id or ""),
        proof_id=str(proof.proof_id or ""),
    )


def _pending_proof_is_invalid(proof: ProofWindow) -> bool:
    return (
        float(proof.opened_ts or 0.0) <= 0.0
        or not str(proof.proof_id or "").strip()
        or not str(proof.command_id or "").strip()
        or not str(proof.context_key or "").strip()
        or not str(proof.lever or "").strip()
        or not dict(proof.target_shape or {})
    )


def _invalid_pending_proof_grade(proof: ProofWindow) -> ProofGrade:
    feedback = build_smart_loop_layered_feedback(
        label="invalid",
        reason="proof_timestamp_missing",
        evidence={
            "grade_evidence_valid": False,
            "grade_validity_reason": "proof_timestamp_missing",
            "grade_context_status": "invalid_proof_contract",
        },
    )
    return ProofGrade(
        "invalid",
        "proof_timestamp_missing",
        False,
        None,
        None,
        proof_id=proof.proof_id,
        command_id=proof.command_id,
        context_key=proof.context_key,
        reason_ref=proof.reason_ref,
        layered_feedback=feedback,
    )


def _availability_intent_for_plan(plan: ActionPlan) -> str:
    metadata = _mapping(plan.metadata)
    explicit = str(metadata.get("availability_intent") or "").strip()
    if explicit:
        return explicit
    reason = str(plan.reason_code or "").lower()
    if "restore_ca" in reason or "ca_recovery" in reason:
        return "restore_ca"
    return "explore"


class SmartLoopCore:
    """Single owner for clean-room self-learning decisions and proof ROI."""

    min_confidence = 0.50
    min_evidence = 2

    def generate_candidates(
        self,
        *,
        context: ModemContext,
        facts: LearningFacts,
        state: SmartLoopState,
    ) -> tuple[Candidate, ...]:
        if not context.link_valid:
            return ()
        rows: list[Candidate] = []
        current_roi_adjustment = _action_shape_roi_adjustment(
            facts.action_roi,
            context=context,
            target_band=context.current_pband,
        )
        current_capacity_adjustment = _measured_capacity_adjustment(
            facts.measured_capacity_fact,
            context=context,
            target_band=context.current_pband,
        )
        current_passive_adjustment = _passive_capacity_adjustment(
            facts.passive_dl_peak_fact,
            context=context,
            target_band=context.current_pband,
        )
        current_score = max(
            0.0,
            min(
                1.0,
                _score(facts.band_scores.get(context.current_pband))
                + current_roi_adjustment.adjustment
                + current_capacity_adjustment.adjustment
                + current_passive_adjustment.adjustment,
            ),
        )
        current_roi_penalty = max(0.0, current_roi_adjustment.penalty)
        rollback_target = int(
            current_roi_adjustment.bucket.get("last_bad_previous_pband", 0) or 0
        )
        opportunity_matrix = build_opportunity_matrix(
            context=context, facts=facts, state=state
        )
        configuration_space_candidate = self._configuration_space_probe_candidate(
            context=context,
            facts=facts,
            current_score=current_score,
            current_roi_adjustment=current_roi_adjustment,
        )
        if configuration_space_candidate is not None:
            rows.append(configuration_space_candidate)
        candidate_bands = {
            int(band) for band in opportunity_target_bands(opportunity_matrix)
        }
        for band in sorted(
            candidate_bands,
            key=lambda item: (-_score(facts.band_scores.get(item, 0.45)), int(item)),
        ):
            target = int(band)
            base_score = _score(facts.band_scores.get(target, 0.45))
            action = (
                "stay_current_anchor"
                if target == context.current_pband
                else RADIO_CONFIGURATION_ACTION
            )
            roi = 0.0
            action_roi_adjustment = _action_shape_roi_adjustment(
                facts.action_roi,
                context=context,
                target_band=target,
            )
            prediction_adjustment = _prediction_adjustment(
                facts.prediction_context,
                context=context,
                target_band=target,
            )
            recent_window_adjustment = _recent_window_adjustment(
                state,
                context=context,
                target_band=target,
            )
            measured_capacity_adjustment = _measured_capacity_adjustment(
                facts.measured_capacity_fact,
                context=context,
                target_band=target,
            )
            passive_capacity_adjustment = _passive_capacity_adjustment(
                facts.passive_dl_peak_fact,
                context=context,
                target_band=target,
            )
            metadata = dict(action_roi_adjustment.metadata)
            metadata.update(dict(prediction_adjustment.metadata))
            metadata.update(dict(recent_window_adjustment.metadata))
            metadata.update(dict(measured_capacity_adjustment.metadata))
            metadata.update(dict(passive_capacity_adjustment.metadata))
            opportunity = opportunity_for_target(opportunity_matrix, target)
            if opportunity:
                metadata["opportunity_matrix"] = {
                    "schema": OPPORTUNITY_MATRIX_SCHEMA,
                    "owner": OPPORTUNITY_MATRIX_OWNER,
                    "source": str(opportunity.get("source") or ""),
                    "reason_code": str(opportunity.get("reason_code") or ""),
                    "target_band": int(opportunity.get("target_band") or 0),
                    "target_bands": list(
                        _normalize_band_tuple(opportunity.get("target_bands"))
                    ),
                }
            reason_code = "learned_anchor_outperforms_current"
            reason = "self-learning has evidence that another anchor is more useful"
            if (
                prediction_adjustment.adjustment > 0.0
                and target != context.current_pband
            ):
                reason_code = "crystal_ball_horizon_predicts_better_anchor"
                reason = "self-learning sees Crystal Ball horizon facts that this anchor is likely to be more useful"
            elif (
                recent_window_adjustment.adjustment > 0.0
                and target != context.current_pband
            ):
                reason_code = "recent_window_context_predicts_better_anchor"
                reason = "self-learning recent windows show this anchor is more useful in the current context"
            if (
                action == RADIO_CONFIGURATION_ACTION
                and current_roi_penalty > 0.0
                and rollback_target > 0
                and target == rollback_target
            ):
                metadata["rollback_source_band"] = int(context.current_pband or 0)
                metadata["rollback_source_roi_penalty"] = round(
                    float(current_roi_penalty), 4
                )
                metadata["rollback_source_reason"] = str(
                    current_roi_adjustment.bucket.get("reason") or ""
                )
                reason_code = "learned_bad_pband_rollback_selected"
                reason = (
                    f"rollback candidate returns from B{context.current_pband} to previous B{target} "
                    "after learned negative PBAND ROI"
                )
            score = max(
                0.0,
                min(
                    1.0,
                    base_score
                    + roi
                    + action_roi_adjustment.adjustment
                    + prediction_adjustment.adjustment
                    + recent_window_adjustment.adjustment
                    + measured_capacity_adjustment.adjustment
                    + passive_capacity_adjustment.adjustment,
                ),
            )
            evidence = int(facts.evidence_counts.get(target, facts.sample_count) or 0)
            effective_evidence = max(
                _effective_learning_evidence(evidence, facts=facts),
                int(prediction_adjustment.evidence_count or 0),
                int(recent_window_adjustment.evidence_count or 0),
                int(measured_capacity_adjustment.evidence_count or 0),
                int(passive_capacity_adjustment.evidence_count or 0),
            )
            effective_confidence = max(
                _effective_learning_confidence(facts.profile_confidence, facts=facts),
                _score(prediction_adjustment.confidence),
                _score(recent_window_adjustment.confidence),
                _score(measured_capacity_adjustment.confidence),
                _score(passive_capacity_adjustment.confidence),
            )
            if facts.calibration_clean_for_learning:
                metadata["calibration_fact_package"] = _calibration_candidate_metadata(
                    facts
                )
                if effective_confidence > float(facts.profile_confidence or 0.0):
                    metadata["calibration_confidence_lift"] = round(
                        effective_confidence - float(facts.profile_confidence or 0.0),
                        4,
                    )
            if (
                score <= 0.35
                and effective_confidence >= self.min_confidence
                and effective_evidence >= self.min_evidence
            ):
                rows.append(
                    self._candidate(
                        action="avoid_band",
                        target_band=target,
                        context=context,
                        score=score,
                        confidence=effective_confidence,
                        evidence_count=effective_evidence,
                        reason_code="learned_anchor_underperforms",
                        reason="self-learning has evidence that this anchor is weak for this context",
                        roi_bias=roi
                        + action_roi_adjustment.adjustment
                        + prediction_adjustment.adjustment
                        + recent_window_adjustment.adjustment
                        + measured_capacity_adjustment.adjustment
                        + passive_capacity_adjustment.adjustment,
                        metadata=metadata,
                    )
                )
                continue
            if action == "stay_current_anchor" and action_roi_adjustment.penalty > 0.0:
                rows.append(
                    self._candidate(
                        action=action,
                        target_band=target,
                        context=context,
                        score=score,
                        confidence=effective_confidence,
                        evidence_count=effective_evidence,
                        reason_code="current_anchor_penalized_by_roi",
                        reason="self-learning has negative ROI evidence for the current anchor",
                        roi_bias=roi
                        + action_roi_adjustment.adjustment
                        + prediction_adjustment.adjustment
                        + recent_window_adjustment.adjustment
                        + measured_capacity_adjustment.adjustment
                        + passive_capacity_adjustment.adjustment,
                        metadata=metadata,
                    )
                )
                continue
            if action == "stay_current_anchor" and score >= 0.65:
                rows.append(
                    self._candidate(
                        action=action,
                        target_band=target,
                        context=context,
                        score=score,
                        confidence=effective_confidence,
                        evidence_count=effective_evidence,
                        reason_code="current_anchor_is_best_known",
                        reason="self-learning sees current anchor as the best useful choice",
                        roi_bias=roi
                        + action_roi_adjustment.adjustment
                        + prediction_adjustment.adjustment
                        + recent_window_adjustment.adjustment
                        + measured_capacity_adjustment.adjustment
                        + passive_capacity_adjustment.adjustment,
                        metadata=metadata,
                    )
                )
                continue
            should_try = (
                action == RADIO_CONFIGURATION_ACTION
                and score >= 0.65
                and (current_score < 0.65 or score >= current_score + 0.08)
            )
            prediction_try = (
                action == RADIO_CONFIGURATION_ACTION
                and prediction_adjustment.adjustment > 0.0
                and score >= 0.62
                and effective_confidence >= self.min_confidence
                and effective_evidence >= self.min_evidence
            )
            recent_window_try = (
                action == RADIO_CONFIGURATION_ACTION
                and recent_window_adjustment.adjustment > 0.0
                and score >= 0.62
                and effective_confidence >= self.min_confidence
                and effective_evidence >= self.min_evidence
            )
            avoid_bad_current = (
                action == RADIO_CONFIGURATION_ACTION
                and current_score <= 0.25
                and score >= 0.40
                and effective_evidence >= self.min_evidence
            )
            if should_try or prediction_try or recent_window_try or avoid_bad_current:
                rows.append(
                    self._candidate(
                        action=action,
                        target_band=target,
                        context=context,
                        score=score,
                        confidence=effective_confidence,
                        evidence_count=effective_evidence,
                        reason_code=(
                            "avoid_bad_current_anchor"
                            if avoid_bad_current and not should_try
                            else reason_code
                        ),
                        reason=(
                            "self-learning has evidence that current anchor is poor and this alternate is usable"
                            if avoid_bad_current and not should_try
                            else reason
                        ),
                        roi_bias=roi
                        + action_roi_adjustment.adjustment
                        + prediction_adjustment.adjustment
                        + recent_window_adjustment.adjustment
                        + measured_capacity_adjustment.adjustment
                        + passive_capacity_adjustment.adjustment,
                        target_bands=_target_configuration_bands(
                            context=context, target_band=target
                        ),
                        metadata=metadata,
                    )
                )
        if not any(
            candidate.executable and candidate.action == RADIO_CONFIGURATION_ACTION
            for candidate in rows
        ):
            rows.extend(
                self._recovery_candidates_from_negative_anchor_evidence(
                    context=context,
                    facts=facts,
                    current_score=current_score,
                    current_roi_adjustment=current_roi_adjustment,
                )
            )
        return tuple(
            sorted(
                _apply_objective_holds(
                    context=context,
                    state=state,
                    candidates=_dedupe_candidates(rows),
                ),
                key=lambda c: (
                    0 if c.executable else 1,
                    -c.score,
                    -c.confidence,
                    c.target_band,
                ),
            )
        )

    def candidate_gate(
        self, *, context: ModemContext, candidate: Candidate | None
    ) -> TsarGate:
        """Return the complete pre-Brain executability verdict for one candidate."""

        if candidate is None:
            return TsarGate("no_action", "no_candidate", "wait_for_next_sample")
        if candidate.action == HELD_RADIO_CONFIGURATION_ACTION:
            return TsarGate(
                "refused",
                "smart_loop_objective_hold",
                "wait_for_objective_feedback_window",
            )
        if context.safety_block:
            return TsarGate(
                "refused",
                f"safety_block:{context.safety_block}",
                "wait_for_safety_release",
            )
        if context.proof_pending:
            return TsarGate("refused", "proof_pending", "wait_for_referee_window")
        if context.cooldown_s > 0:
            return TsarGate("refused", "cooldown_active", "wait_for_cooldown")
        authority_bands = set(context.authority_bands)
        if not authority_bands:
            return TsarGate(
                "refused", "band_authority_unavailable", "wait_for_radio_authority"
            )
        if candidate.target_band not in authority_bands:
            return TsarGate(
                "refused",
                "target_band_outside_authority",
                "update_modem_profile_authority",
            )
        if candidate.confidence < self.min_confidence:
            return TsarGate(
                "refused", "learning_confidence_low", "collect_more_samples"
            )
        if candidate.evidence_count < self.min_evidence:
            return TsarGate(
                "refused", "learning_evidence_low", "collect_more_samples"
            )
        if candidate.action == "stay_current_anchor":
            return _stay_current_anchor_gate(candidate)
        if candidate.action == "avoid_band":
            if candidate.target_band == context.current_pband:
                return TsarGate(
                    "no_action",
                    "current_anchor_underperforms_no_better_candidate",
                    "generate_better_executable_candidate",
                )
            return TsarGate(
                "no_action", "advisory_avoid_band_recorded", "keep_observing"
            )
        if not candidate.executable:
            return TsarGate(
                "refused", "candidate_not_executable", "generate_executable_candidate"
            )
        if (
            candidate.action == RADIO_CONFIGURATION_ACTION
            and not _context_has_valid_proof_time(context)
        ):
            return TsarGate(
                "refused", "context_timestamp_missing", "wait_for_fresh_context"
            )
        target_pool = _normalize_band_tuple(candidate.effective_target_bands)
        if any(band not in authority_bands for band in target_pool):
            return TsarGate(
                "refused",
                "target_pool_outside_authority",
                "update_modem_profile_authority",
            )
        return TsarGate(
            "act",
            "tsar_approved",
            "materialize_selected_action",
        )

    def operationalize(
        self, *, context: ModemContext, candidate: Candidate | None
    ) -> tuple[ActionPlan | None, TsarGate]:
        gate = self.candidate_gate(context=context, candidate=candidate)
        if candidate is None or gate.verdict != "act":
            return None, gate
        target_pool = _normalize_band_tuple(candidate.effective_target_bands)
        target_signature = "+".join(
            f"B{band}" for band in target_pool or (candidate.target_band,)
        )
        objective_key = smart_loop_objective_key(
            context=context,
            action=candidate.action,
            target_band=candidate.target_band,
            target_bands=target_pool,
        )
        command_id = _stable_loop_id(
            "command",
            candidate.action,
            candidate.target_band,
            candidate.reason_ref,
            context.learning_context_key,
            target_bands=target_pool,
            generation_ref=_context_ts(context),
        )
        proof_id = _stable_loop_id(
            "proof",
            candidate.action,
            candidate.target_band,
            candidate.reason_ref,
            context.learning_context_key,
            target_bands=target_pool,
            generation_ref=_context_ts(context),
        )
        plan = ActionPlan(
            action=candidate.action,
            target_band=candidate.target_band,
            reason_ref=candidate.reason_ref,
            context_key=context.learning_context_key,
            command_id=command_id,
            proof_id=proof_id,
            reason_code=candidate.reason_code,
            reason=f"{candidate.reason}; target_configuration={target_signature}",
            target_bands=target_pool,
            objective_key=objective_key,
            metadata=candidate.metadata,
        )
        return plan, gate

    def open_proof(self, *, context: ModemContext, plan: ActionPlan) -> ProofWindow:
        opened_ts = _context_ts(context)
        if opened_ts <= 0.0:
            raise ValueError("smart_loop_proof_timestamp_missing")
        return ProofWindow(
            action=plan.action,
            target_band=plan.target_band,
            context_key=plan.context_key,
            reason_ref=plan.reason_ref,
            proof_id=plan.proof_id,
            command_id=plan.command_id,
            before_cq=context.cq,
            before_congestion_pct=context.congestion_pct,
            lever="PBAND",
            target_shape={
                "pband": int(plan.target_band),
                "ca_pool": list(plan.target_bands),
            },
            target_bands=plan.target_bands,
            objective_key=plan.objective_key
            or smart_loop_objective_key(
                context=context,
                action=plan.action,
                target_band=plan.target_band,
                target_bands=plan.target_bands,
            ),
            opened_ts=opened_ts,
        )

    def decide(
        self,
        *,
        context: ModemContext,
        facts: LearningFacts,
        state: SmartLoopState | None = None,
        proof_grade: ProofGrade | None = None,
    ) -> ClosedLoopDecision:
        current_state = (state or SmartLoopState()).with_observation(context)
        closed: ProofGrade | None = None
        if current_state.pending_proof is not None and proof_grade is not None:
            if not proof_grade.terminal:
                current_state = current_state.admit_interim_feedback(
                    current_state.pending_proof,
                    proof_grade,
                )
                gate = TsarGate(
                    "no_action",
                    f"proof_feedback_{int(proof_grade.horizon_s or 0)}s",
                    "wait_for_referee_window",
                )
                return ClosedLoopDecision(
                    schema=SCHEMA,
                    owner=OWNER,
                    outcome="proof_feedback_recorded",
                    reason_code=gate.reason_code,
                    required_next_step=gate.required_next_step,
                    candidates=self.generate_candidates(
                        context=context, facts=facts, state=current_state
                    ),
                    selected=None,
                    tsar_gate=gate,
                    opened_proof=current_state.pending_proof,
                    closed_proof=None,
                    state=current_state,
                    policy_update_applied=False,
                    future_policy_effect_status="",
                    proof_feedback=proof_grade,
                )
            closed = proof_grade
            current_state = current_state.admit_grade(
                current_state.pending_proof, closed
            )
        if closed is not None:
            gate = TsarGate(
                "no_action",
                closed.reason_code,
                "commit_learning_memory"
                if closed.memory_admitted
                else "inspect_proof_contract",
            )
            return ClosedLoopDecision(
                schema=SCHEMA,
                owner=OWNER,
                outcome=f"proof_closed_{closed.label}"
                if closed.memory_admitted
                else "proof_invalid",
                reason_code=gate.reason_code,
                required_next_step=gate.required_next_step,
                candidates=self.generate_candidates(
                    context=context, facts=facts, state=current_state
                ),
                selected=None,
                tsar_gate=gate,
                opened_proof=None,
                closed_proof=closed,
                state=current_state,
                policy_update_applied=bool(
                    closed.policy_update_applied
                ),
                future_policy_effect_status=str(
                    closed.future_policy_effect_status or ""
                ),
            )
        if current_state.pending_proof is not None and _pending_proof_is_invalid(
            current_state.pending_proof
        ):
            invalid_proof = current_state.pending_proof
            closed = _invalid_pending_proof_grade(invalid_proof)
            current_state = current_state.with_proof(None)
            gate = TsarGate("no_action", closed.reason_code, "repair_proof_contract")
            return ClosedLoopDecision(
                schema=SCHEMA,
                owner=OWNER,
                outcome="proof_invalid",
                reason_code=gate.reason_code,
                required_next_step=gate.required_next_step,
                candidates=self.generate_candidates(
                    context=context, facts=facts, state=current_state
                ),
                selected=None,
                tsar_gate=gate,
                opened_proof=None,
                closed_proof=closed,
                state=current_state,
                policy_update_applied=False,
                future_policy_effect_status="",
            )
        if current_state.pending_proof is not None:
            candidates = self.generate_candidates(
                context=context, facts=facts, state=current_state
            )
            gate = TsarGate("refused", "proof_pending", "wait_for_referee_window")
            return ClosedLoopDecision(
                schema=SCHEMA,
                owner=OWNER,
                outcome="proof_pending",
                reason_code=gate.reason_code,
                required_next_step=gate.required_next_step,
                candidates=candidates,
                selected=None,
                tsar_gate=gate,
                opened_proof=current_state.pending_proof,
                closed_proof=None,
                state=current_state,
            )
        if not context.link_valid:
            gate = TsarGate(
                "no_action", "live_context_unavailable", "wait_for_modem_live_context"
            )
            return ClosedLoopDecision(
                schema=SCHEMA,
                owner=OWNER,
                outcome="no_action_justified",
                reason_code=gate.reason_code,
                required_next_step=gate.required_next_step,
                candidates=(),
                selected=None,
                tsar_gate=gate,
                opened_proof=None,
                closed_proof=None,
                state=current_state,
            )
        candidates = self.generate_candidates(
            context=context, facts=facts, state=current_state
        )
        gate = TsarGate(
            "no_action",
            "brain_selection_required",
            "submit_complete_candidate_set_to_brain",
        )
        outcome = "candidate_set_ready"
        if not candidates and _should_have_candidate(context):
            outcome = "candidate_missing_counterfactual"
            gate = TsarGate(
                "no_action",
                "candidate_generation_failed",
                "improve_candidate_generation",
            )
        elif not candidates:
            outcome = "no_action_justified"
            gate = TsarGate("no_action", "no_candidate", "wait_for_next_sample")
        return ClosedLoopDecision(
            schema=SCHEMA,
            owner=OWNER,
            outcome=outcome,
            reason_code=gate.reason_code,
            required_next_step=gate.required_next_step,
            candidates=candidates,
            selected=None,
            tsar_gate=gate,
            opened_proof=None,
            closed_proof=closed,
            state=current_state,
        )

    def apply_authoritative_candidate(
        self,
        *,
        decision: ClosedLoopDecision,
        context: ModemContext,
        candidate: Candidate | None,
        action_opportunity_set: Mapping[str, object] | None = None,
        contextual_bandit_selection_receipt: Mapping[str, object] | None = None,
        brain_decision_receipt: Mapping[str, object] | None = None,
    ) -> ClosedLoopDecision:
        """Operationalize one candidate selected by the external Brain owner.

        Candidate generation and physical TSAR checks remain SmartLoop work.
        Choice among candidates is never made here: the caller must provide the
        candidate proven by ActionOpportunitySet, Contextual Bandit and the
        Brain decision receipt.
        """

        if decision.selected is not None:
            raise ValueError("smart_loop_decision_already_selected")
        if decision.outcome not in {"candidate_set_ready", "no_action_justified"}:
            return decision
        if candidate is None:
            next_step = _brain_do_nothing_next_step(
                decision=decision,
                context=context,
                action_opportunity_set=action_opportunity_set,
            )
            return replace(
                decision,
                outcome="no_action_justified",
                reason_code="brain_selected_do_nothing",
                required_next_step=next_step,
                tsar_gate=TsarGate(
                    "no_action", "brain_selected_do_nothing", next_step
                ),
                action_opportunity_set=dict(action_opportunity_set or {}),
                contextual_bandit_selection_receipt=dict(
                    contextual_bandit_selection_receipt or {}
                ),
                brain_decision_receipt=dict(brain_decision_receipt or {}),
            )
        plan, gate = self.operationalize(context=context, candidate=candidate)
        outcome = (
            "action_selected_execution_pending"
            if plan is not None
            else (
                "no_action_justified"
                if gate.verdict == "no_action"
                else "action_rejected"
            )
        )
        return replace(
            decision,
            outcome=outcome,
            reason_code=gate.reason_code,
            required_next_step=gate.required_next_step,
            selected=plan,
            tsar_gate=gate,
            action_opportunity_set=dict(action_opportunity_set or {}),
            contextual_bandit_selection_receipt=dict(
                contextual_bandit_selection_receipt or {}
            ),
            brain_decision_receipt=dict(brain_decision_receipt or {}),
        )
    def _candidate(
        self,
        *,
        action: str,
        target_band: int,
        context: ModemContext,
        score: float,
        confidence: float,
        evidence_count: int,
        reason_code: str,
        reason: str,
        roi_bias: float,
        target_bands: Sequence[int] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Candidate:
        normalized_target_bands = _normalize_band_tuple(target_bands or ())
        if not normalized_target_bands and int(target_band or 0) > 0:
            normalized_target_bands = (int(target_band),)
        material = (
            f"{action}|{target_band}|{'+'.join(str(band) for band in normalized_target_bands)}|"
            f"{round(score, 3)}|{round(confidence, 3)}|{evidence_count}|{reason_code}"
        )
        return Candidate(
            action=action,
            target_band=int(target_band),
            context_key=context.learning_context_key,
            score=round(float(score), 4),
            confidence=round(float(confidence), 4),
            evidence_count=int(evidence_count),
            reason_code=reason_code,
            reason=reason,
            reason_ref="0x"
            + blake2s(material.encode("utf-8"), digest_size=4).hexdigest(),
            roi_bias=round(float(roi_bias), 4),
            target_bands=normalized_target_bands,
            metadata=_candidate_metadata(context=context, metadata=metadata),
        )

    def _configuration_space_probe_candidate(
        self,
        *,
        context: ModemContext,
        facts: LearningFacts,
        current_score: float,
        current_roi_adjustment: _ActionShapeRoiAdjustment,
    ) -> Candidate | None:
        current = int(context.current_pband or 0)
        if current <= 0:
            return None
        authority_pool = _normalize_band_tuple(context.authority_bands)
        current_pool = _normalize_band_tuple(context.ca_pool)
        if not authority_pool or current not in authority_pool:
            return None
        if not current_pool:
            return None
        if set(authority_pool).issubset(set(current_pool)):
            return None
        if len(authority_pool) <= len(current_pool):
            return None
        if current_roi_adjustment.penalty > 0.0 and _has_viable_alternate_configuration(
            context=context,
            facts=facts,
        ):
            return None
        quality_pressure = (
            current_score < 0.65
            or current_roi_adjustment.penalty > 0.0
            or context.ca_active is False
            or _score(context.cq) < 0.55
            or (
                context.congestion_pct is not None
                and float(context.congestion_pct) >= 55.0
            )
        )
        if not quality_pressure:
            return None
        evidence = int(facts.evidence_counts.get(current, facts.sample_count) or 0)
        effective_evidence = _effective_learning_evidence(evidence, facts=facts)
        effective_confidence = _effective_learning_confidence(
            facts.profile_confidence, facts=facts
        )
        if (
            effective_confidence < self.min_confidence
            or effective_evidence < self.min_evidence
        ):
            return None
        missing = tuple(
            band for band in authority_pool if band not in set(current_pool)
        )
        metadata = dict(current_roi_adjustment.metadata)
        metadata.update(
            {
                "radio_configuration_hypothesis": True,
                "configuration_previous_bands": list(current_pool),
                "configuration_added_bands": list(missing),
                "configuration_authority_bands": list(authority_pool),
                "negative_anchor_score": round(float(current_score), 4),
            }
        )
        if current_roi_adjustment.penalty > 0.0:
            metadata["negative_anchor_roi_penalty"] = round(
                float(current_roi_adjustment.penalty), 4
            )
            metadata["negative_anchor_roi_reason"] = str(
                current_roi_adjustment.bucket.get("reason")
                or "negative_current_anchor_roi"
            )
        if facts.calibration_clean_for_learning:
            metadata["calibration_fact_package"] = _calibration_candidate_metadata(
                facts
            )
        score = max(
            0.62,
            min(
                0.78,
                float(current_score or 0.0)
                + 0.18
                - max(0.0, current_roi_adjustment.penalty),
            ),
        )
        return self._candidate(
            action=RADIO_CONFIGURATION_ACTION,
            target_band=current,
            target_bands=authority_pool,
            context=context,
            score=score,
            confidence=effective_confidence,
            evidence_count=effective_evidence,
            reason_code="radio_configuration_space_probe",
            reason="self-learning will test a legal radio configuration with better expected utility",
            roi_bias=max(0.0, current_roi_adjustment.benefit)
            - max(0.0, current_roi_adjustment.penalty),
            metadata=metadata,
        )

    def _recovery_candidates_from_negative_anchor_evidence(
        self,
        *,
        context: ModemContext,
        facts: LearningFacts,
        current_score: float,
        current_roi_adjustment: _ActionShapeRoiAdjustment,
    ) -> tuple[Candidate, ...]:
        if current_score > 0.35 and current_roi_adjustment.penalty <= 0.0:
            return ()
        effective_confidence = _effective_learning_confidence(
            facts.profile_confidence, facts=facts
        )
        if effective_confidence < self.min_confidence:
            return ()
        allowed = [
            int(band)
            for band in context.authority_bands
            if int(band) != int(context.current_pband)
        ]
        if not allowed:
            return ()
        candidates: list[Candidate] = []
        for target in allowed:
            score = _score(facts.band_scores.get(target))
            evidence = int(facts.evidence_counts.get(target, facts.sample_count) or 0)
            effective_evidence = _effective_learning_evidence(evidence, facts=facts)
            if effective_evidence < self.min_evidence:
                continue
            if score < 0.35 and target != int(
                current_roi_adjustment.bucket.get("last_bad_previous_pband", 0) or 0
            ):
                continue
            action_roi_adjustment = _action_shape_roi_adjustment(
                facts.action_roi,
                context=context,
                target_band=target,
            )
            adjusted_score = max(
                0.0, min(1.0, score + action_roi_adjustment.adjustment)
            )
            rollback_target = int(
                current_roi_adjustment.bucket.get("last_bad_previous_pband", 0) or 0
            )
            if (
                adjusted_score < 0.35
                and action_roi_adjustment.benefit <= 0.0
                and target != rollback_target
            ):
                continue
            metadata = dict(action_roi_adjustment.metadata)
            metadata["negative_anchor_source_band"] = int(context.current_pband or 0)
            metadata["negative_anchor_score"] = round(float(current_score), 4)
            if current_roi_adjustment.penalty > 0.0:
                metadata["negative_anchor_roi_penalty"] = round(
                    float(current_roi_adjustment.penalty), 4
                )
                metadata["negative_anchor_roi_reason"] = str(
                    current_roi_adjustment.bucket.get("reason")
                    or "negative_current_anchor_roi"
                )
            if facts.calibration_clean_for_learning:
                metadata["calibration_fact_package"] = _calibration_candidate_metadata(
                    facts
                )
                if effective_confidence > float(facts.profile_confidence or 0.0):
                    metadata["calibration_confidence_lift"] = round(
                        effective_confidence - float(facts.profile_confidence or 0.0),
                        4,
                    )
            candidates.append(
                self._candidate(
                    action=RADIO_CONFIGURATION_ACTION,
                    target_band=target,
                    target_bands=_target_configuration_bands(
                        context=context, target_band=target
                    ),
                    context=context,
                    score=adjusted_score,
                    confidence=effective_confidence,
                    evidence_count=effective_evidence,
                    reason_code="negative_current_anchor_recovery",
                    reason="self-learning has negative evidence for the current anchor and a legal alternate to try",
                    roi_bias=action_roi_adjustment.adjustment,
                    metadata=metadata,
                )
            )
        return tuple(candidates)


def _brain_do_nothing_next_step(
    *,
    decision: ClosedLoopDecision,
    context: ModemContext,
    action_opportunity_set: Mapping[str, object] | None,
) -> str:
    opportunity_set = _mapping(action_opportunity_set)
    opportunities = [
        _mapping(value)
        for value in opportunity_set.get("opportunities", [])
        if isinstance(value, Mapping)
    ]
    optimization = [
        row
        for row in opportunities
        if str(row.get("owner_class") or "") == "optimization"
    ]
    if opportunity_set.get("scope_admitted") is False:
        return "wait_for_fresh_context"
    if optimization and not any(
        row.get("legal") is True and row.get("executable") is True
        for row in optimization
    ):
        if any(
            isinstance(value, Candidate)
            and value.action == "avoid_band"
            and int(value.target_band or 0) == int(context.current_pband or 0)
            for value in decision.candidates
        ):
            return "generate_better_executable_candidate"
        availability = {
            str(reason)
            for row in optimization
            for reason in list(row.get("availability_reasons") or [])
            if str(reason)
        }
        if any(reason.startswith("capability_not_admitted:") for reason in availability):
            return "update_modem_profile_authority"
        return "generate_executable_candidate"
    return "keep_observing"


@dataclass(frozen=True)
class MatrixCase:
    name: str
    context: ModemContext
    facts: LearningFacts
    expected_outcome: str
    expected_reason: str = ""
    proof_grade: ProofGrade | None = None
    state: SmartLoopState = field(default_factory=SmartLoopState)


def default_smart_loop_matrix() -> tuple[MatrixCase, ...]:
    base = ModemContext(
        provider_plmn="22005",
        cell_id=2050,
        pci=255,
        earfcn=1795,
        hour=6,
        current_pband=1,
        allowed_bands=(1, 3, 20),
        cq=0.42,
        congestion_pct=74.0,
        observed_ts=1000.0,
    )
    core = SmartLoopCore()
    seed_plan = ActionPlan(
        RADIO_CONFIGURATION_ACTION,
        3,
        "0xseed",
        base.learning_context_key,
        command_id=_stable_loop_id(
            "command",
            RADIO_CONFIGURATION_ACTION,
            3,
            "0xseed",
            base.learning_context_key,
            target_bands=_target_configuration_bands(context=base, target_band=3),
        ),
        proof_id=_stable_loop_id(
            "proof",
            RADIO_CONFIGURATION_ACTION,
            3,
            "0xseed",
            base.learning_context_key,
            target_bands=_target_configuration_bands(context=base, target_band=3),
        ),
        target_bands=_target_configuration_bands(context=base, target_band=3),
    )
    # The matrix exercises the SmartLoop candidate/proof/TSAR boundary only.
    # Its pending-proof fixture is explicit: live candidate choice belongs to
    # ActionOpportunitySet -> Contextual Bandit -> Brain, never this module.
    executed_state = SmartLoopState().with_proof(
        core.open_proof(context=base, plan=seed_plan)
    )
    bad_state = executed_state.admit_grade(
        core.open_proof(context=base, plan=seed_plan),
        ProofGrade("bad", "post_action_regressed", True, -0.1, 12.0),
    )
    return (
        MatrixCase(
            name="useful_candidate_becomes_action",
            context=base,
            facts=LearningFacts(
                band_scores={3: 0.82, 1: 0.38},
                evidence_counts={3: 4},
                profile_confidence=0.9,
            ),
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="calibration_strengthens_existing_learning_candidate",
            context=replace(base, current_pband=3, cq=0.33, congestion_pct=82.0),
            facts=LearningFacts(
                band_scores={3: 0.3333, 1: 0.66},
                evidence_counts={3: 3, 1: 2},
                profile_confidence=0.3,
                calibration_fact_package={
                    "schema": "calibration_fact_package.v1",
                    "owner": "calibration_fact_confidence",
                    "confidence": 1.0,
                    "clean_for_learning": True,
                    "strong_enough_for_preemption_facts": True,
                    "self_learning_input_strength": 1.0,
                    "evidence_count": 30,
                },
            ),
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="recent_window_context_seeds_useful_candidate",
            context=replace(
                base,
                observed_ts=1000.0,
                current_pband=1,
                cq=0.31,
                congestion_pct=84.0,
                ca_active=False,
            ),
            facts=LearningFacts(
                band_scores={1: 0.46},
                evidence_counts={1: 3},
                profile_confidence=0.4,
            ),
            state=SmartLoopState(
                observations=(
                    SmartLoopObservation(
                        ts=650.0,
                        pband=3,
                        cq=0.78,
                        congestion_pct=22.0,
                        ca_active=True,
                        context_key=base.learning_context_key,
                    ),
                    SmartLoopObservation(
                        ts=760.0,
                        pband=3,
                        cq=0.76,
                        congestion_pct=24.0,
                        ca_active=True,
                        context_key=base.learning_context_key,
                    ),
                    SmartLoopObservation(
                        ts=880.0,
                        pband=3,
                        cq=0.80,
                        congestion_pct=26.0,
                        ca_active=True,
                        context_key=base.learning_context_key,
                    ),
                    SmartLoopObservation(
                        ts=930.0,
                        pband=1,
                        cq=0.32,
                        congestion_pct=82.0,
                        ca_active=False,
                        context_key=base.learning_context_key,
                    ),
                )
            ),
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="low_evidence_candidate_is_refused_with_learnable_reason",
            context=base,
            facts=LearningFacts(
                band_scores={3: 0.91}, evidence_counts={3: 1}, profile_confidence=0.9
            ),
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="safety_gate_blocks_action",
            context=replace(base, safety_block="manual_action_cooldown"),
            facts=LearningFacts(
                band_scores={3: 0.82}, evidence_counts={3: 4}, profile_confidence=0.9
            ),
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="missing_candidate_is_counterfactual_failure",
            context=replace(base, cq=0.25, congestion_pct=86.0),
            facts=LearningFacts(
                band_scores={}, evidence_counts={}, profile_confidence=0.0
            ),
            expected_outcome="candidate_missing_counterfactual",
            expected_reason="candidate_generation_failed",
        ),
        MatrixCase(
            name="good_referee_proof_closes_without_local_bias",
            context=base,
            facts=LearningFacts(
                band_scores={3: 0.82}, evidence_counts={3: 4}, profile_confidence=0.9
            ),
            proof_grade=ProofGrade(
                "good",
                "post_action_improved",
                True,
                0.13,
                -16.0,
                proof_id=executed_state.pending_proof.proof_id,
                command_id=executed_state.pending_proof.command_id,
                context_key=executed_state.pending_proof.context_key,
                reason_ref=executed_state.pending_proof.reason_ref,
                prediction_error_recorded=True,
                policy_update_applied=True,
                future_policy_effect_status="pending",
            ),
            state=executed_state,
            expected_outcome="proof_closed_good",
            expected_reason="post_action_improved",
        ),
        MatrixCase(
            name="bad_proof_hold_blocks_repeated_objective",
            context=base,
            facts=LearningFacts(
                band_scores={3: 0.68, 20: 0.66},
                evidence_counts={3: 4, 20: 4},
                profile_confidence=0.9,
            ),
            state=bad_state,
            expected_outcome="candidate_set_ready",
            expected_reason="brain_selection_required",
        ),
        MatrixCase(
            name="invalid_proof_does_not_mutate_roi",
            context=base,
            facts=LearningFacts(
                band_scores={3: 0.82}, evidence_counts={3: 4}, profile_confidence=0.9
            ),
            proof_grade=ProofGrade(
                "invalid",
                "proof_missing_quality",
                False,
                proof_id=executed_state.pending_proof.proof_id,
                command_id=executed_state.pending_proof.command_id,
                context_key=executed_state.pending_proof.context_key,
                reason_ref=executed_state.pending_proof.reason_ref,
            ),
            state=executed_state,
            expected_outcome="proof_invalid",
            expected_reason="proof_missing_quality",
        ),
    )


def run_smart_loop_matrix(
    cases: Sequence[MatrixCase] | None = None,
) -> tuple[tuple[str, ClosedLoopDecision], ...]:
    core = SmartLoopCore()
    rows: list[tuple[str, ClosedLoopDecision]] = []
    for case in cases or default_smart_loop_matrix():
        decision = core.decide(
            context=case.context,
            facts=case.facts,
            state=case.state,
            proof_grade=case.proof_grade,
        )
        rows.append((case.name, decision))
    return tuple(rows)


def _dedupe_candidates(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    best: dict[tuple[str, int, tuple[int, ...]], Candidate] = {}
    for candidate in candidates:
        key = (
            candidate.action,
            int(candidate.target_band),
            candidate.effective_target_bands,
        )
        old = best.get(key)
        if old is None or (
            candidate.executable,
            candidate.score,
            candidate.confidence,
            candidate.evidence_count,
        ) > (
            old.executable,
            old.score,
            old.confidence,
            old.evidence_count,
        ):
            best[key] = candidate
    return tuple(best.values())


def _apply_objective_holds(
    *,
    context: ModemContext,
    state: SmartLoopState,
    candidates: Sequence[Candidate],
) -> tuple[Candidate, ...]:
    rows: list[Candidate] = []
    for candidate in candidates:
        if candidate.action != RADIO_CONFIGURATION_ACTION:
            rows.append(candidate)
            continue
        hold = state.active_hold_for_candidate(context=context, candidate=candidate)
        if hold is None:
            rows.append(candidate)
            continue
        metadata = dict(candidate.metadata or {})
        metadata["objective_hold"] = hold.as_dict()
        rows.append(
            replace(
                candidate,
                action=HELD_RADIO_CONFIGURATION_ACTION,
                reason_code="smart_loop_objective_hold",
                reason="SmartLoop is still grading the previous result for this radio objective",
                roi_bias=min(float(candidate.roi_bias or 0.0), -0.25),
                metadata=metadata,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class _ActionShapeRoiAdjustment:
    adjustment: float = 0.0
    penalty: float = 0.0
    benefit: float = 0.0
    bucket: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _PredictionAdjustment:
    adjustment: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    target_band: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _RecentWindowAdjustment:
    adjustment: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    target_band: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _MeasuredCapacityAdjustment:
    adjustment: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    target_band: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)


def _prediction_target_bands(facts: LearningFacts) -> tuple[int, ...]:
    if not facts.prediction_clean_for_learning:
        return ()
    bands: list[int] = []
    for horizon in _mapping(facts.prediction_context.get("horizons")).values():
        row = _mapping(horizon)
        for source_key in ("best_alternate", "best_candidate"):
            band = _int(_mapping(row.get(source_key)).get("band"), 0)
            if band > 0 and band not in bands:
                bands.append(band)
    return tuple(bands)


def _recent_window_target_bands(state: SmartLoopState) -> tuple[int, ...]:
    bands: list[int] = []
    for item in state.observations:
        band = int(item.pband or 0)
        if band > 0 and band not in bands:
            bands.append(band)
    return tuple(bands)


def _recent_window_state_summary(
    observations: Sequence[SmartLoopObservation],
) -> dict[str, object]:
    if not observations:
        return {
            "owner": OWNER,
            "windows_s": list(RECENT_WINDOW_SECONDS),
            "observation_count": 0,
            "bands": {},
        }
    newest = max(float(item.ts or 0.0) for item in observations)
    bands: dict[str, dict[str, object]] = {}
    for band in sorted(
        {int(item.pband or 0) for item in observations if int(item.pband or 0) > 0}
    ):
        per_window: dict[str, object] = {}
        for seconds in RECENT_WINDOW_SECONDS:
            stats = _recent_window_stats(
                observations,
                band=band,
                newest_ts=newest,
                seconds=int(seconds),
            )
            per_window[_recent_window_label(int(seconds))] = (
                _public_recent_window_stats(stats)
            )
        bands[str(band)] = per_window
    return {
        "owner": OWNER,
        "windows_s": list(RECENT_WINDOW_SECONDS),
        "observation_count": len(observations),
        "newest_ts": round(float(newest), 3),
        "bands": bands,
    }


def _recent_window_label(seconds: int) -> str:
    if int(seconds) == 60:
        return "past_1m"
    if int(seconds) == 360:
        return "past_6m"
    if int(seconds) == 3600:
        return "past_60m"
    return f"past_{int(seconds)}s"


def _recent_window_stats(
    observations: Sequence[SmartLoopObservation],
    *,
    band: int,
    newest_ts: float,
    seconds: int,
) -> dict[str, object]:
    start_ts = float(newest_ts) - float(seconds)
    rows = [
        item
        for item in observations
        if int(item.pband or 0) == int(band)
        and start_ts <= float(item.ts or 0.0) <= float(newest_ts)
        and bool(item.link_valid)
    ]
    cq_values = [float(item.cq) for item in rows if item.cq is not None]
    congestion_values = [
        float(item.congestion_pct) for item in rows if item.congestion_pct is not None
    ]
    ca_values = [bool(item.ca_active) for item in rows if item.ca_active is not None]
    return {
        "band": int(band),
        "window_s": int(seconds),
        "sample_count": len(rows),
        "avg_cq": _avg(cq_values),
        "avg_congestion_pct": _avg(congestion_values),
        "ca_rate": (sum(1 for value in ca_values if value) / len(ca_values))
        if ca_values
        else None,
    }


def _public_recent_window_stats(stats: Mapping[str, object]) -> dict[str, object]:
    return {
        "sample_count": int(stats.get("sample_count") or 0),
        "avg_cq": _round_optional(stats.get("avg_cq")),
        "avg_congestion_pct": _round_optional(stats.get("avg_congestion_pct")),
        "ca_rate": _round_optional(stats.get("ca_rate")),
    }


def _recent_window_adjustment(
    state: SmartLoopState,
    *,
    context: ModemContext,
    target_band: int,
) -> _RecentWindowAdjustment:
    target = int(target_band or 0)
    current = int(context.current_pband or 0)
    if target <= 0 or target == current or not state.observations:
        return _RecentWindowAdjustment()
    newest = max(float(item.ts or 0.0) for item in state.observations)
    best: dict[str, object] = {}
    best_adjustment = 0.0
    best_confidence = 0.0
    best_evidence = 0
    for seconds in RECENT_WINDOW_SECONDS:
        target_stats = _recent_window_stats(
            state.observations,
            band=target,
            newest_ts=newest,
            seconds=int(seconds),
        )
        target_count = int(target_stats.get("sample_count") or 0)
        if target_count < 2:
            continue
        current_stats = _recent_window_stats(
            state.observations,
            band=current,
            newest_ts=newest,
            seconds=int(seconds),
        )
        cq_gain = _optional_delta(target_stats.get("avg_cq"), context.cq)
        congestion_gain = _optional_delta(
            context.congestion_pct, target_stats.get("avg_congestion_pct")
        )
        current_cq_gain = _optional_delta(
            target_stats.get("avg_cq"), current_stats.get("avg_cq")
        )
        current_congestion_gain = _optional_delta(
            current_stats.get("avg_congestion_pct"),
            target_stats.get("avg_congestion_pct"),
        )
        positive_quality = max(0.0, cq_gain or 0.0, current_cq_gain or 0.0)
        positive_congestion = max(
            0.0,
            (congestion_gain or 0.0) / 100.0,
            (current_congestion_gain or 0.0) / 100.0,
        )
        ca_rate = target_stats.get("ca_rate")
        ca_lift = 0.04 if ca_rate is not None and float(ca_rate) >= 0.75 else 0.0
        adjustment = min(
            0.24, (0.75 * positive_quality) + (0.55 * positive_congestion) + ca_lift
        )
        if adjustment <= 0.0:
            continue
        confidence = min(0.9, 0.45 + min(0.4, target_count / 20.0))
        if adjustment > best_adjustment:
            best_adjustment = adjustment
            best_confidence = confidence
            best_evidence = target_count
            best = {
                "schema": "smart_loop.recent_window_context.v1",
                "owner": OWNER,
                "window_s": int(seconds),
                "target_band": target,
                "sample_count": target_count,
                "confidence": round(float(confidence), 4),
                "adjustment": round(float(adjustment), 4),
                "target": _public_recent_window_stats(target_stats),
                "current": _public_recent_window_stats(current_stats),
                "reason": "recent_window_context_predicts_better_anchor",
            }
    if not best:
        return _RecentWindowAdjustment()
    return _RecentWindowAdjustment(
        adjustment=round(float(best_adjustment), 4),
        confidence=round(float(best_confidence), 4),
        evidence_count=int(best_evidence),
        target_band=target,
        metadata={"recent_window_context": best},
    )


def _measured_capacity_adjustment(
    measured_capacity_fact: Mapping[str, Any],
    *,
    context: ModemContext,
    target_band: int,
) -> _MeasuredCapacityAdjustment:
    fact = _mapping(measured_capacity_fact)
    if (
        fact.get("schema") != "measured_capacity_fact.v1"
        or fact.get("owner") != "arch4.smart_loop.capacity_probe"
        or not bool(fact.get("measured", False))
    ):
        return _MeasuredCapacityAdjustment()
    target = int(target_band or 0)
    measured_band = _int(fact.get("serving_band"), 0)
    current = int(context.current_pband or 0)
    if target <= 0 or measured_band <= 0 or measured_band != current:
        return _MeasuredCapacityAdjustment()
    dl_mbps = max(0.0, _float(fact.get("dl_mbps"), 0.0))
    confidence = _score(fact.get("confidence"))
    if dl_mbps <= 0.0 or confidence <= 0.0:
        return _MeasuredCapacityAdjustment()

    metadata = {
        "measured_capacity_fact": {
            "schema": "measured_capacity_fact.v1",
            "owner": "arch4.smart_loop.capacity_probe",
            "serving_band": measured_band,
            "target_bands": list(_normalize_band_tuple(fact.get("target_bands"))),
            "dl_mbps": round(float(dl_mbps), 3),
            "latency_ms": _round_optional(fact.get("latency_ms")),
            "confidence": round(float(confidence), 4),
            "context_key": str(fact.get("context_key") or ""),
            "sample_ts": _round_optional(fact.get("sample_ts")),
        }
    }
    # Target is the measured current anchor. A good quiet-window result is real
    # evidence to stay. A poor result is evidence against staying.
    if target == current:
        if dl_mbps >= 25.0:
            return _MeasuredCapacityAdjustment(
                adjustment=min(0.16, 0.04 + (dl_mbps / 500.0)),
                confidence=confidence,
                evidence_count=3,
                target_band=target,
                metadata=metadata,
            )
        if dl_mbps <= 5.0:
            return _MeasuredCapacityAdjustment(
                adjustment=-0.18,
                confidence=confidence,
                evidence_count=3,
                target_band=target,
                metadata=metadata,
            )
        return _MeasuredCapacityAdjustment(
            adjustment=0.02,
            confidence=confidence,
            evidence_count=2,
            target_band=target,
            metadata=metadata,
        )

    # Target is an alternate. If a controlled quiet probe says current anchor is
    # weak, alternates get a small pressure lift but still need their own
    # learning/prediction/recent-window evidence to become executable.
    if dl_mbps <= 5.0:
        return _MeasuredCapacityAdjustment(
            adjustment=0.08,
            confidence=confidence,
            evidence_count=2,
            target_band=target,
            metadata=metadata,
        )
    return _MeasuredCapacityAdjustment()


def _passive_capacity_adjustment(
    passive_dl_peak_fact: Mapping[str, Any],
    *,
    context: ModemContext,
    target_band: int,
) -> _MeasuredCapacityAdjustment:
    fact = _mapping(passive_dl_peak_fact)
    if (
        fact.get("schema") != "passive_dl_peak_fact.v1"
        or fact.get("owner") != "arch4.smart_loop.capacity_probe"
        or not bool(fact.get("observed", False))
    ):
        return _MeasuredCapacityAdjustment()
    target = int(target_band or 0)
    current = int(context.current_pband or 0)
    measured_band = _int(fact.get("serving_band"), 0)
    if target <= 0 or target != current or measured_band != current:
        return _MeasuredCapacityAdjustment()
    if str(fact.get("context_key") or "") != str(
        context.learning_context_key
    ):
        return _MeasuredCapacityAdjustment()
    peak_dl = max(0.0, _float(fact.get("peak_dl_mbps_lower_bound"), 0.0))
    if peak_dl < 25.0:
        return _MeasuredCapacityAdjustment()
    confidence = min(0.9, 0.45 + min(0.35, peak_dl / 200.0))
    metadata = {
        "passive_dl_peak_fact": {
            "schema": "passive_dl_peak_fact.v1",
            "owner": "arch4.smart_loop.capacity_probe",
            "serving_band": measured_band,
            "target_bands": list(_normalize_band_tuple(fact.get("target_bands"))),
            "peak_dl_mbps_lower_bound": round(float(peak_dl), 3),
            "context_key": str(fact.get("context_key") or ""),
            "sample_ts": _round_optional(fact.get("sample_ts")),
            "source": "runtime_traffic_register",
        }
    }
    return _MeasuredCapacityAdjustment(
        adjustment=min(0.12, 0.03 + (peak_dl / 600.0)),
        confidence=confidence,
        evidence_count=2,
        target_band=target,
        metadata=metadata,
    )


def _prediction_adjustment(
    prediction_context: Mapping[str, Any],
    *,
    context: ModemContext,
    target_band: int,
) -> _PredictionAdjustment:
    prediction = _mapping(prediction_context)
    if (
        prediction.get("schema") != "crystal_ball_horizon_context.v1"
        or not _owner_is_crystal_ball(prediction.get("owner"))
        or not bool(prediction.get("advisory_only", False))
        or bool(prediction.get("direct_modem_actuation", True))
    ):
        return _PredictionAdjustment()
    target = int(target_band or 0)
    if target <= 0:
        return _PredictionAdjustment()
    best: dict[str, object] = {}
    best_score = 0.0
    best_confidence = 0.0
    best_evidence = 0
    for label, raw_horizon in _mapping(prediction.get("horizons")).items():
        horizon = _mapping(raw_horizon)
        alternate = _mapping(horizon.get("best_alternate"))
        if _int(alternate.get("band"), 0) != target:
            continue
        delta = _mapping(horizon.get("expected_delta"))
        congestion_gain = max(0.0, _float(delta.get("congestion_improvement_pct"), 0.0))
        score_delta = max(0.0, _float(delta.get("score_delta"), 0.0))
        confidence = _score(horizon.get("confidence"))
        evidence = max(
            _int(horizon.get("sample_count"), 0),
            _int(alternate.get("sample_count"), 0),
            _int(alternate.get("confidence"), 0),
        )
        seconds = max(1, _int(horizon.get("horizon_s"), 0))
        horizon_weight = 1.0
        if seconds <= 90:
            horizon_weight = 0.75
        elif seconds >= 1800:
            horizon_weight = 0.85
        adjustment = min(
            0.28, horizon_weight * ((congestion_gain / 100.0) + (0.45 * score_delta))
        )
        if adjustment <= 0.0:
            continue
        if adjustment > best_score:
            best_score = adjustment
            best_confidence = confidence
            best_evidence = evidence
            best = {
                "schema": "crystal_ball_horizon_context.v1",
                "owner": "crystal_ball",
                "horizon": str(label),
                "horizon_s": seconds,
                "target_band": target,
                "confidence": round(float(confidence), 4),
                "evidence_count": int(evidence),
                "congestion_improvement_pct": round(float(congestion_gain), 4),
                "score_delta": round(float(score_delta), 4),
                "reason": "crystal_ball_future_horizon_predicts_better_anchor",
            }
    if not best:
        return _PredictionAdjustment()
    return _PredictionAdjustment(
        adjustment=round(float(best_score), 4),
        confidence=round(float(best_confidence), 4),
        evidence_count=max(0, int(best_evidence)),
        target_band=target,
        metadata={"prediction_context": best},
    )


def _action_shape_roi_adjustment(
    action_roi: Mapping[str, Any],
    *,
    context: ModemContext,
    target_band: int,
) -> _ActionShapeRoiAdjustment:
    roi = _mapping(action_roi)
    by_pband = _mapping(roi.get("by_pband"))
    bucket = _mapping(by_pband.get(str(int(target_band or 0))))
    if not bucket:
        return _ActionShapeRoiAdjustment()
    match, reason = _action_roi_context_match(bucket, context=context)
    metadata: dict[str, object] = {
        "action_shape_roi_context_match": bool(match),
    }
    if reason:
        metadata["action_shape_roi_context_reason"] = reason
    if not match:
        recent_bad_match, recent_bad_reason = _recent_bad_action_shape_match(
            bucket, context=context
        )
        if recent_bad_match:
            penalty = max(0.22, max(0.0, _float(bucket.get("penalty"), 0.0)))
            metadata["action_shape_roi_recent_bad_match"] = True
            metadata["action_shape_roi_recent_bad_reason"] = recent_bad_reason
            metadata["action_shape_roi_penalty"] = round(float(penalty), 4)
            metadata["action_shape_roi_reason"] = str(
                bucket.get("reason") or "recent_bad_action_shape"
            )
            return _ActionShapeRoiAdjustment(
                adjustment=round(-float(penalty), 4),
                penalty=round(float(penalty), 4),
                benefit=0.0,
                bucket=bucket,
                metadata=metadata,
            )
        return _ActionShapeRoiAdjustment(bucket=bucket, metadata=metadata)
    penalty = max(0.0, _float(bucket.get("penalty"), 0.0))
    benefit = max(0.0, _float(bucket.get("benefit"), 0.0))
    if penalty > 0.0:
        metadata["action_shape_roi_penalty"] = round(float(penalty), 4)
    if benefit > 0.0:
        metadata["action_shape_roi_benefit"] = round(float(benefit), 4)
    metadata["action_shape_roi_reason"] = str(bucket.get("reason") or "")
    return _ActionShapeRoiAdjustment(
        adjustment=round(float(benefit - penalty), 4),
        penalty=round(float(penalty), 4),
        benefit=round(float(benefit), 4),
        bucket=bucket,
        metadata=metadata,
    )


def _recent_bad_action_shape_match(
    bucket: Mapping[str, Any],
    *,
    context: ModemContext,
) -> tuple[bool, str]:
    if not any(str(key).startswith("last_bad_") for key in bucket):
        return False, ""
    for field_id, expected in (
        ("identity_code", str(context.identity_code or "")),
        ("provider_plmn", str(context.provider_plmn or "")),
        ("model", str(context.model or "")),
        ("profile_id", str(context.profile_id or "")),
        ("firmware", str(context.firmware or "")),
    ):
        recorded = str(bucket.get(f"last_bad_{field_id}") or "").strip()
        if recorded and expected and recorded.lower() != expected.lower():
            return False, f"{field_id}_mismatch"
    for field_id, expected in (
        ("cell_id", int(context.cell_id or 0)),
        ("pci", int(context.pci or 0)),
        ("earfcn", int(context.earfcn or 0)),
    ):
        recorded = _int(bucket.get(f"last_bad_{field_id}"), 0)
        if recorded > 0 and expected > 0 and recorded != expected:
            return False, f"{field_id}_mismatch"
    return True, "recent_bad_same_radio_shape"


def _action_roi_context_match(
    bucket: Mapping[str, Any],
    *,
    context: ModemContext,
) -> tuple[bool, str]:
    prefixes = ("last_bad", "last_good")
    for prefix in prefixes:
        if not any(str(key).startswith(f"{prefix}_") for key in bucket):
            continue
        for field_id, expected in (
            ("identity_code", str(context.identity_code or "")),
            ("provider_plmn", str(context.provider_plmn or "")),
            ("model", str(context.model or "")),
            ("profile_id", str(context.profile_id or "")),
            ("firmware", str(context.firmware or "")),
        ):
            recorded = str(bucket.get(f"{prefix}_{field_id}") or "").strip()
            if recorded and expected and recorded.lower() != expected.lower():
                return False, f"{field_id}_mismatch"
        for field_id, expected in (
            ("cell_id", int(context.cell_id or 0)),
            ("pci", int(context.pci or 0)),
            ("earfcn", int(context.earfcn or 0)),
            ("hour", int(context.hour or -1)),
        ):
            recorded = _int(
                bucket.get(f"{prefix}_{field_id}"), 0 if field_id != "hour" else -1
            )
            if recorded > 0 and expected > 0 and recorded != expected:
                return False, f"{field_id}_mismatch"
            if (
                field_id == "hour"
                and recorded >= 0
                and expected >= 0
                and recorded != expected
            ):
                return False, "hour_mismatch"
    return True, ""


def _candidate_metadata(
    *,
    context: ModemContext,
    metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    out = dict(metadata or {})
    out["context"] = _context_payload(context)
    return out


def _context_payload(context: ModemContext) -> dict[str, object]:
    return {
        "identity_code": str(context.identity_code or "0x0000000000000000"),
        "identity_status": str(context.identity_status or "unknown"),
        "model": str(context.model or ""),
        "provider_plmn": str(context.provider_plmn or ""),
        "cell_id": int(context.cell_id or 0),
        "pci": int(context.pci or 0),
        "earfcn": int(context.earfcn or 0),
        "nr_band": str(context.nr_band or ""),
        "nr_pci": int(context.nr_pci or 0),
        "nr_arfcn": int(context.nr_arfcn or 0),
        "nr_cell_id": str(context.nr_cell_id or ""),
        "hour": int(context.hour or 0),
        "current_pband": int(context.current_pband or 0),
        "ca_pool": [int(band) for band in context.ca_pool],
        "authority_bands": [int(band) for band in context.authority_bands],
        "cq": _round_optional(context.cq),
        "congestion_pct": _round_optional(context.congestion_pct),
        "ca_active": context.ca_active if context.ca_active is not None else None,
        "profile_id": str(context.profile_id or ""),
        "firmware": str(context.firmware or ""),
        "observed_ts": round(float(context.observed_ts or 0.0), 3),
    }


def _normalize_band_tuple(values: object) -> tuple[int, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raw_values: Sequence[object] = str(values).replace("+", ",").split(",")
    elif isinstance(values, Sequence):
        raw_values = values
    else:
        raw_values = (values,)
    bands: list[int] = []
    for item in raw_values:
        text = str(item or "").strip()
        if text.upper().startswith("B"):
            text = text[1:]
        band = _int(text, 0)
        if band > 0 and band not in bands:
            bands.append(band)
    return tuple(sorted(bands))


def _observation_context_matches(
    existing: SmartLoopObservation,
    current: SmartLoopObservation,
) -> bool:
    """Prevent recent-window evidence crossing modem/site/context boundaries."""

    existing_key = str(existing.context_key or "")
    current_key = str(current.context_key or "")
    if not existing_key or not current_key:
        return existing_key == current_key
    return existing_key == current_key


def _target_configuration_bands(
    *, context: ModemContext, target_band: int
) -> tuple[int, ...]:
    target = int(target_band or 0)
    authority = _normalize_band_tuple(context.authority_bands)
    if target <= 0:
        return authority
    if authority and target in authority:
        return authority
    return (target,)


def _has_viable_alternate_configuration(
    *, context: ModemContext, facts: LearningFacts
) -> bool:
    current = int(context.current_pband or 0)
    authority = set(_normalize_band_tuple(context.authority_bands))
    for band, raw_score in facts.band_scores.items():
        target = int(band or 0)
        if target <= 0 or target == current or (authority and target not in authority):
            continue
        evidence = int(facts.evidence_counts.get(target, facts.sample_count) or 0)
        if (
            _score(raw_score) >= 0.35
            and _effective_learning_evidence(evidence, facts=facts)
            >= SmartLoopCore.min_evidence
        ):
            return True
    for target in _prediction_target_bands(facts):
        if (
            int(target or 0) > 0
            and int(target) != current
            and (not authority or int(target) in authority)
        ):
            return True
    return False


def _stable_loop_id(
    kind: str,
    action: object,
    target_band: object,
    reason_ref: object,
    context_key: object,
    *,
    target_bands: Sequence[int] | None = None,
    generation_ref: object = "",
) -> str:
    band_signature = "+".join(
        str(band) for band in _normalize_band_tuple(target_bands or ())
    )
    raw = "|".join(
        (
            str(kind or "loop"),
            str(action or ""),
            str(int(_int(target_band, 0))),
            str(band_signature),
            str(reason_ref or ""),
            str(context_key or ""),
            str(generation_ref or ""),
        )
    )
    return (
        str(kind or "loop")
        + "_"
        + blake2s(raw.encode("utf-8"), digest_size=6).hexdigest()
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _proof_feedback_grade_score(grade: ProofGrade) -> int:
    feedback = _mapping(grade.layered_feedback)
    try:
        score = int(feedback.get("memory_grade_score", 0) or 0)
    except (TypeError, ValueError):
        score = 0
    if score:
        return max(-3, min(3, score))
    if not grade.memory_admitted:
        return 0
    if grade.label == "good":
        return 1
    if grade.label == "bad":
        return -1
    return 0


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ok", "supported"}:
        return True
    if text in {"0", "false", "no", "off", "unsupported"}:
        return False
    return bool(default)


def _owner_is_crystal_ball(value: object) -> bool:
    parts = str(value or "").strip().split(".")
    return parts[-3:] == ["arch4", "brain", "crystal_ball"]


def _score(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _avg(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(float(item) for item in values) / len(values)


def _round_optional(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_delta(after: object, before: object) -> float | None:
    if after is None or before is None:
        return None
    try:
        return float(after) - float(before)
    except (TypeError, ValueError):
        return None


def _effective_learning_confidence(
    profile_confidence: object, *, facts: LearningFacts
) -> float:
    base = _score(profile_confidence)
    if not facts.calibration_clean_for_learning:
        return base
    strength = facts.calibration_self_learning_strength
    if strength <= 0.0:
        return base
    return max(base, min(1.0, (0.65 * base) + (0.35 * strength), strength - 0.05))


def _calibration_candidate_metadata(facts: LearningFacts) -> dict[str, object]:
    package = _mapping(facts.calibration_fact_package)
    metadata: dict[str, object] = {
        "schema": "calibration_fact_package.v1",
        "confidence": _score(package.get("confidence")),
        "self_learning_input_strength": facts.calibration_self_learning_strength,
        "preemption_ready": facts.calibration_preemption_ready,
    }
    identity_rf = _mapping(package.get("identity_relative_rf"))
    if identity_rf:
        metadata["identity_relative_rf"] = {
            "identity_code": str(identity_rf.get("identity_code") or ""),
            "raw_sinr_db": _round_optional(identity_rf.get("raw_sinr_db")),
            "raw_rsrq_db": _round_optional(identity_rf.get("raw_rsrq_db")),
            "normalized_rf_score": _score(identity_rf.get("normalized_rf_score")),
            "confidence": _score(identity_rf.get("confidence")),
            "nr_ready": _bool(identity_rf.get("nr_ready"), False),
        }
    return metadata


def _effective_learning_evidence(raw_evidence: int, *, facts: LearningFacts) -> int:
    return max(0, int(raw_evidence or 0))


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return round(float(after) - float(before), 4)


def _should_have_candidate(context: ModemContext) -> bool:
    if not context.link_valid:
        return False
    bad_cq = context.cq is not None and context.cq < 0.40
    bad_congestion = (
        context.congestion_pct is not None and context.congestion_pct >= 70.0
    )
    return bad_cq or bad_congestion or context.ca_active is False
