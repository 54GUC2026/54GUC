"""
Typed tick/decision contract objects.

Phase A1 starts with DecisionTrace as the narrowest explicit contract slice.
The existing dict/event payloads remain the public compatibility surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from arch4.interfaces import LeverAction


def _thaw_frozen_mapping(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {item[0]: _thaw_frozen_mapping(item[1]) for item in value}
        return [_thaw_frozen_mapping(item) for item in value]
    return value


def _freeze_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _freeze_mapping(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_mapping(item) for item in value)
    return value


@dataclass(frozen=True)
class DecisionTraceStep:
    gate: str
    result: str
    changed: bool = False
    reason: str = ""
    skipped: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecisionTraceStep":
        extras = {
            str(k): v
            for k, v in raw.items()
            if k not in {"gate", "result", "changed", "reason", "skipped"}
        }
        return cls(
            gate=str(raw.get("gate", "") or ""),
            result=str(raw.get("result", "") or ""),
            changed=bool(raw.get("changed", False)),
            reason=str(raw.get("reason", "") or ""),
            skipped=bool(raw.get("skipped", False)),
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        row = {
            "gate": self.gate,
            "result": self.result,
        }
        if self.changed:
            row["changed"] = True
        if self.reason:
            row["reason"] = self.reason
        if self.skipped:
            row["skipped"] = True
        row.update(self.extras)
        return row


@dataclass(frozen=True)
class DecisionTrace:
    decision_trace_id: str
    source: str
    state: str
    proposed: str
    final: str
    passed: int
    blocked: int
    tsar_verdict: str = ""
    tsar_reason: str = ""
    tsar_summary: str = ""
    primary_blocker_gate: str = ""
    primary_blocker_reason: str = ""
    also_blocked_by: tuple[str, ...] = ()
    steps: tuple[DecisionTraceStep, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecisionTrace":
        steps_raw = raw.get("steps", ())
        if not isinstance(steps_raw, (list, tuple)):
            steps_raw = ()
        return cls(
            decision_trace_id=str(raw.get("decision_trace_id", "") or ""),
            source=str(raw.get("source", "") or ""),
            state=str(raw.get("state", "") or ""),
            proposed=str(raw.get("proposed", "") or ""),
            final=str(raw.get("final", "") or ""),
            passed=int(raw.get("passed", 0) or 0),
            blocked=int(raw.get("blocked", 0) or 0),
            tsar_verdict=str(raw.get("tsar_verdict", "") or ""),
            tsar_reason=str(raw.get("tsar_reason", "") or ""),
            tsar_summary=str(raw.get("tsar_summary", "") or ""),
            primary_blocker_gate=str(raw.get("primary_blocker_gate", "") or ""),
            primary_blocker_reason=str(raw.get("primary_blocker_reason", "") or ""),
            also_blocked_by=tuple(str(x) for x in (raw.get("also_blocked_by", ()) or ())),
            steps=tuple(
                DecisionTraceStep.from_mapping(step)
                for step in steps_raw
                if isinstance(step, Mapping)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "source": self.source,
            "state": self.state,
            "proposed": self.proposed,
            "final": self.final,
            "passed": int(self.passed),
            "blocked": int(self.blocked),
            "tsar_verdict": self.tsar_verdict,
            "tsar_reason": self.tsar_reason,
            "tsar_summary": self.tsar_summary,
            "primary_blocker_gate": self.primary_blocker_gate,
            "primary_blocker_reason": self.primary_blocker_reason,
            "also_blocked_by": list(self.also_blocked_by),
            "steps": [step.to_dict() for step in self.steps],
        }

    def to_event_payload(self) -> dict[str, Any]:
        # Current runtime event shape intentionally matches the old dict contract.
        return self.to_dict()

    def to_compact_proof(self) -> dict[str, Any]:
        """Small RCA surface for status/log review.

        This is a projection of the existing decision trace, not a second
        authority. It names the owner/source, the primary blocker if any, and
        which gate evidence was present or missing.
        """
        evidence_used: list[str] = []
        evidence_missing: list[str] = []
        for step in self.steps:
            gate = str(step.gate or "").strip()
            if not gate:
                continue
            result = str(step.result or "").strip().lower()
            if result in {"pass", "passed", "ok"}:
                evidence_used.append(gate)
            elif result in {"blocked", "missing", "fail", "failed"} or step.skipped:
                evidence_missing.append(gate)
        blocker = str(self.primary_blocker_gate or "").strip()
        blocker_reason = str(self.primary_blocker_reason or "").strip()
        if blocker:
            proof_level = "blocked"
        elif self.final:
            proof_level = "proven"
        elif evidence_missing:
            proof_level = "unproven"
        else:
            proof_level = "unknown"
        return {
            "decision_trace_id": self.decision_trace_id,
            "owner": f"arch4.brain.decision_pipeline:{self.source or 'unknown'}",
            "proof_level": proof_level,
            "final": self.final,
            "blocker": blocker,
            "blocker_reason": blocker_reason,
            "evidence_used": evidence_used[:8],
            "evidence_missing": evidence_missing[:8],
        }


@dataclass(frozen=True)
class DecisionActionSummary:
    brief: str
    lever: str
    intent: str
    objective_id: str
    reason: str
    availability_intent: str = ""
    action_root_id: str = ""
    command_id: str = ""
    proof_id: str = ""
    session_scope_key: str = ""
    identity_key: str = ""
    source_generation: int = 0
    target_shape: tuple[tuple[str, Any], ...] = ()
    opportunity_id: str = ""
    opportunity_set_id: str = ""
    decision_id: str = ""
    decision_receipt_id: str = ""
    bandit_selection_receipt_id: str = ""
    brain_decision_receipt: tuple[tuple[str, Any], ...] = ()
    bandit_selection_receipt: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_action(
        cls,
        action: LeverAction | None,
        *,
        brief: str = "",
    ) -> "DecisionActionSummary | None":
        if action is None:
            return None
        params = getattr(action, "params", {}) or {}
        availability_intent = ""
        if isinstance(params, Mapping):
            availability_intent = str(params.get("_smart_loop_availability_intent") or "")
        from arch4.action_transaction import action_transaction_key_from_action

        action_key = action_transaction_key_from_action(action)
        return cls(
            brief=str(brief or ""),
            lever=str(getattr(action.lever, "name", "") or ""),
            intent=str(getattr(action, "intent", "") or ""),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            reason=str(getattr(action, "reason", "") or ""),
            availability_intent=availability_intent,
            action_root_id=str(action_key.root_id if action_key else ""),
            command_id=str(
                action_key.command_id
                if action_key
                else getattr(action, "command_id", "") or ""
            ),
            proof_id=str(action_key.proof_id if action_key else ""),
            session_scope_key=str(
                action_key.matrix_identity.session_scope_key
                if action_key
                else ""
            ),
            identity_key=str(action_key.identity_key if action_key else ""),
            source_generation=int(
                action_key.matrix_identity.matrix_sequence
                if action_key
                else 0
            ),
            target_shape=(
                _freeze_mapping(action_key.target.to_mapping())
                if action_key
                else ()
            ),
            opportunity_id=str(action_key.opportunity_id if action_key else ""),
            opportunity_set_id=str(action_key.opportunity_set_id if action_key else ""),
            decision_id=str(action_key.decision_id if action_key else ""),
            decision_receipt_id=str(
                action_key.decision_receipt_id if action_key else ""
            ),
            bandit_selection_receipt_id=str(
                action_key.bandit_selection_receipt_id if action_key else ""
            ),
            brain_decision_receipt=(
                action_key.brain_decision_receipt if action_key else ()
            ),
            bandit_selection_receipt=(
                action_key.bandit_selection_receipt if action_key else ()
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief": self.brief,
            "lever": self.lever,
            "intent": self.intent,
            "objective_id": self.objective_id,
            "reason": self.reason,
            "availability_intent": self.availability_intent,
            "action_root_id": self.action_root_id,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "session_scope_key": self.session_scope_key,
            "identity_key": self.identity_key,
            "source_generation": int(self.source_generation),
            "target_shape": _thaw_frozen_mapping(self.target_shape),
            "opportunity_id": self.opportunity_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "bandit_selection_receipt_id": self.bandit_selection_receipt_id,
            "brain_decision_receipt": _thaw_frozen_mapping(
                self.brain_decision_receipt
            ),
            "bandit_selection_receipt": _thaw_frozen_mapping(
                self.bandit_selection_receipt
            ),
        }


@dataclass(frozen=True)
class DecisionResult:
    decision_trace_id: str
    source: str
    state: str
    proposed_action: DecisionActionSummary | None
    final_action: DecisionActionSummary | None
    passed: int
    blocked: int
    tsar_verdict: str = ""
    tsar_reason: str = ""
    primary_blocker_gate: str = ""
    primary_blocker_reason: str = ""

    @property
    def action_selected(self) -> bool:
        return self.final_action is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "source": self.source,
            "state": self.state,
            "proposed_action": (
                self.proposed_action.to_dict() if self.proposed_action is not None else None
            ),
            "final_action": (
                self.final_action.to_dict() if self.final_action is not None else None
            ),
            "passed": int(self.passed),
            "blocked": int(self.blocked),
            "tsar_verdict": self.tsar_verdict,
            "tsar_reason": self.tsar_reason,
            "primary_blocker_gate": self.primary_blocker_gate,
            "primary_blocker_reason": self.primary_blocker_reason,
            "action_selected": bool(self.action_selected),
        }


@dataclass(frozen=True)
class DecisionContext:
    decision_trace_id: str
    state: str
    fault_class: str = ""
    congestion_kind: str = ""
    warning_class: str = ""
    warning_source: str = ""
    warning_mechanism: str = ""
    warning_reason: str = ""
    health_stage: str = ""
    health_action_hint: str = ""
    incident_class: str = ""
    incident_next_step: str = ""
    incident_suppress_exploration: bool = False
    recovered_lane_active: bool = False
    recovered_lane_bands: tuple = ()
    recovered_lane_from_fault: str = ""
    recovered_lane_remaining_s: int = 0
    recovered_lane_evidence_multiplier: float = 1.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecisionContext":
        return cls(
            decision_trace_id=str(raw.get("decision_trace_id", "") or ""),
            state=str(raw.get("state", "") or ""),
            fault_class=str(raw.get("fault_class", "") or ""),
            congestion_kind=str(raw.get("congestion_kind", "") or ""),
            warning_class=str(raw.get("warning_class", "") or ""),
            warning_source=str(raw.get("warning_source", "") or ""),
            warning_mechanism=str(raw.get("warning_mechanism", "") or ""),
            warning_reason=str(raw.get("warning_reason", "") or ""),
            health_stage=str(raw.get("health_stage", "") or ""),
            health_action_hint=str(raw.get("health_action_hint", "") or ""),
            incident_class=str(raw.get("incident_class", "") or ""),
            incident_next_step=str(raw.get("incident_next_step", "") or ""),
            incident_suppress_exploration=bool(raw.get("incident_suppress_exploration", False)),
            recovered_lane_active=bool(raw.get("recovered_lane_active", False)),
            recovered_lane_bands=tuple(raw.get("recovered_lane_bands", ()) or ()),
            recovered_lane_from_fault=str(raw.get("recovered_lane_from_fault", "") or ""),
            recovered_lane_remaining_s=int(raw.get("recovered_lane_remaining_s", 0) or 0),
            recovered_lane_evidence_multiplier=float(raw.get("recovered_lane_evidence_multiplier", 1.0) or 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "state": self.state,
            "fault_class": self.fault_class,
            "congestion_kind": self.congestion_kind,
            "warning_class": self.warning_class,
            "warning_source": self.warning_source,
            "warning_mechanism": self.warning_mechanism,
            "warning_reason": self.warning_reason,
            "health_stage": self.health_stage,
            "health_action_hint": self.health_action_hint,
            "incident_class": self.incident_class,
            "incident_next_step": self.incident_next_step,
            "incident_suppress_exploration": bool(self.incident_suppress_exploration),
            "recovered_lane_active": bool(self.recovered_lane_active),
            "recovered_lane_bands": list(self.recovered_lane_bands),
            "recovered_lane_from_fault": self.recovered_lane_from_fault,
            "recovered_lane_remaining_s": int(self.recovered_lane_remaining_s),
            "recovered_lane_evidence_multiplier": float(self.recovered_lane_evidence_multiplier),
        }


@dataclass(frozen=True)
class ProposalOutcome:
    decision_trace_id: str
    lever: str
    intent: str
    objective_id: str
    reason: str = ""
    score: float = 0.0
    action_origin: str = ""
    stage: str = "proposal"
    authority: str = "brain"
    triggered_by: str = "engine"

    @classmethod
    def from_runtime(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
    ) -> "ProposalOutcome":
        if action is not None:
            from arch4.action_admission import selected_action_context_from_action

            action_origin = selected_action_context_from_action(action).origin.kind.value
        else:
            action_origin = ""
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            intent=str(getattr(action, "intent", "") or ""),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            reason=str(getattr(action, "reason", "") or ""),
            score=float(getattr(action, "score", 0.0) or 0.0),
            action_origin=action_origin,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "lever": self.lever,
            "intent": self.intent,
            "objective_id": self.objective_id,
            "reason": self.reason,
            "score": float(self.score),
            "action_origin": self.action_origin,
            "stage": self.stage,
            "authority": self.authority,
            "triggered_by": self.triggered_by,
        }


@dataclass(frozen=True)
class IntentVerdictOutcome:
    decision_trace_id: str
    lever: str
    intent: str
    objective_id: str
    level: str = ""
    code: str = ""
    message: str = ""
    stage: str = "intent_verdict"
    authority: str = "bouncer"
    triggered_by: str = "engine"

    @classmethod
    def from_runtime(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
        verdict: Mapping[str, Any] | None = None,
    ) -> "IntentVerdictOutcome":
        verdict = dict(verdict or {})
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            intent=str(getattr(action, "intent", "") or ""),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            level=str(verdict.get("level", "") or ""),
            code=str(verdict.get("code", "") or ""),
            message=str(verdict.get("message", "") or ""),
            stage=str(verdict.get("stage", "intent_verdict") or "intent_verdict"),
            authority=str(verdict.get("authority", "bouncer") or "bouncer"),
            triggered_by=str(verdict.get("triggered_by", "engine") or "engine"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "lever": self.lever,
            "intent": self.intent,
            "objective_id": self.objective_id,
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "authority": self.authority,
            "triggered_by": self.triggered_by,
        }


@dataclass(frozen=True)
class OutcomeVerdictOutcome:
    decision_trace_id: str
    lever: str
    intent: str
    objective_id: str
    level: str = ""
    code: str = ""
    message: str = ""
    stage: str = "outcome_verdict"
    authority: str = "referee"
    triggered_by: str = "engine"

    @classmethod
    def from_runtime(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
        verdict: Mapping[str, Any] | None = None,
    ) -> "OutcomeVerdictOutcome":
        verdict = dict(verdict or {})
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            intent=str(getattr(action, "intent", "") or ""),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            level=str(verdict.get("level", "") or ""),
            code=str(verdict.get("code", "") or ""),
            message=str(verdict.get("message", "") or ""),
            stage=str(verdict.get("stage", "outcome_verdict") or "outcome_verdict"),
            authority=str(verdict.get("authority", "referee") or "referee"),
            triggered_by=str(verdict.get("triggered_by", "engine") or "engine"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "lever": self.lever,
            "intent": self.intent,
            "objective_id": self.objective_id,
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "authority": self.authority,
            "triggered_by": self.triggered_by,
        }

# ── Canonical verification status categories ──
# These are the normalized categories for verification results.
# Consumers can rely on these values being stable.
VERIFICATION_VERIFIED = "verified"          # action confirmed successful
VERIFICATION_NO_EFFECT = "no_effect"        # action didn't take effect (wrong bands/cell/radio down)
VERIFICATION_TIMEOUT = "timeout"            # ran out of time before confirmation
VERIFICATION_NOT_VERIFIED = "not_verified"  # verification not performed (unsupported backend)
VERIFICATION_NOT_REQUIRED = "not_required"  # non-mutating action, no verification needed
VERIFICATION_UNKNOWN = "unknown"            # couldn't determine status


def classify_verification_status(verified: bool | None, note: str) -> str:
    """Map a (verified, note) pair to a canonical verification status.

    The note comes from verify_mutating_action_settle's last_reason string.
    This function preserves the full detail in the note field while giving
    consumers a stable category to branch on.
    """
    if verified is None:
        return VERIFICATION_UNKNOWN
    if verified is True:
        note_lower = str(note or "").lower()
        if "non_mikrotik" in note_lower or "not_verified" in note_lower:
            return VERIFICATION_NOT_VERIFIED
        return VERIFICATION_VERIFIED
    # verified is False — distinguish timeout from no_effect
    note_lower = str(note or "").lower()
    # Timeout family: never got usable data, or session too young at deadline
    if any(tag in note_lower for tag in ("no_sample", "read_failed", "session_warmup")):
        return VERIFICATION_TIMEOUT
    # No-effect family: modem is readable but action didn't apply
    if any(tag in note_lower for tag in ("bands_not_applied", "cell_lock_not_set",
                                          "lte1_disabled", "radio_not_connected")):
        return VERIFICATION_NO_EFFECT
    # Unrecognized failure note — default to no_effect (we timed out AND
    # the last read showed the action hadn't taken effect)
    return VERIFICATION_NO_EFFECT


@dataclass(frozen=True)
class VerificationOutcome:
    decision_trace_id: str
    lever: str
    objective_id: str
    mutating: bool
    status: str
    note: str = ""
    stage: str = "verification_result"
    authority: str = "engine_verifier"
    triggered_by: str = "engine"

    @classmethod
    def from_runtime(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
        mutating: bool,
        verified: bool | None,
        note: str = "",
    ) -> "VerificationOutcome":
        if not bool(mutating):
            status = VERIFICATION_NOT_REQUIRED
        else:
            status = classify_verification_status(verified, note)
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            mutating=bool(mutating),
            status=status,
            note=str(note or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "lever": self.lever,
            "objective_id": self.objective_id,
            "mutating": bool(self.mutating),
            "status": self.status,
            "note": self.note,
            "stage": self.stage,
            "authority": self.authority,
            "triggered_by": self.triggered_by,
        }


@dataclass(frozen=True)
class LearningCommitOutcome:
    decision_trace_id: str
    lever: str
    objective_id: str
    commit_status: str
    learning_ltm_applied: bool = True
    learning_policy_reason: str = ""
    proposal_reason: str = ""
    proposal_intent: str = ""
    proposal_objective_id: str = ""
    proposal_score: float = 0.0
    proposal_action_origin: str = ""
    proposal_stage: str = "proposal"
    proposal_authority: str = "brain"
    proposal_triggered_by: str = "engine"
    outcome_label: str = ""
    hard_fail: bool = False
    execution_status: str = ""
    execution_error: str = ""
    execution_stage: str = "execution_result"
    execution_authority: str = "engine_executor"
    execution_triggered_by: str = "engine"
    verification_status: str = ""
    verification_note: str = ""
    verification_stage: str = "verification_result"
    verification_authority: str = "engine_verifier"
    verification_triggered_by: str = "engine"
    bouncer_intent_level: str = ""
    bouncer_intent_code: str = ""
    intent_verdict_stage: str = "intent_verdict"
    intent_verdict_authority: str = "bouncer"
    intent_verdict_triggered_by: str = "engine"
    referee_outcome_level: str = ""
    referee_outcome_code: str = ""
    outcome_verdict_stage: str = "outcome_verdict"
    outcome_verdict_authority: str = "referee"
    outcome_verdict_triggered_by: str = "engine"
    incident_class: str = ""
    stage: str = "learning_commit"
    authority: str = "memory_store"
    triggered_by: str = "engine"

    @classmethod
    def from_action_record(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
        action_record: Any = None,
        commit_status: str = "recorded",
    ) -> "LearningCommitOutcome":
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            commit_status=str(commit_status or "recorded"),
            learning_ltm_applied=bool(getattr(action_record, "learning_ltm_applied", True)) if action_record is not None else True,
            learning_policy_reason=str(getattr(action_record, "learning_policy_reason", "") or "") if action_record is not None else "",
            proposal_reason=str(getattr(action_record, "proposal_reason", str(getattr(action, "reason", "") or "")) or str(getattr(action, "reason", "") or "")) if action_record is not None else str(getattr(action, "reason", "") or ""),
            proposal_intent=str(getattr(action_record, "proposal_intent", str(getattr(action, "intent", "") or "")) or str(getattr(action, "intent", "") or "")) if action_record is not None else str(getattr(action, "intent", "") or ""),
            proposal_objective_id=str(getattr(action_record, "proposal_objective_id", str(getattr(action, "objective_id", "") or "")) or str(getattr(action, "objective_id", "") or "")) if action_record is not None else str(getattr(action, "objective_id", "") or ""),
            proposal_score=float(getattr(action_record, "proposal_score", float(getattr(action, "score", 0.0) or 0.0)) or float(getattr(action, "score", 0.0) or 0.0)) if action_record is not None else float(getattr(action, "score", 0.0) or 0.0),
            proposal_action_origin=str(
                getattr(action_record, "proposal_action_origin", "") or ""
            ) if action_record is not None else "",
            proposal_stage=str(getattr(action_record, "proposal_stage", "proposal") or "proposal") if action_record is not None else "proposal",
            proposal_authority=str(getattr(action_record, "proposal_authority", "brain") or "brain") if action_record is not None else "brain",
            proposal_triggered_by=str(getattr(action_record, "proposal_triggered_by", "engine") or "engine") if action_record is not None else "engine",
            outcome_label=str(getattr(action_record, "outcome_label", "") or "") if action_record is not None else "",
            hard_fail=bool(getattr(action_record, "hard_fail", False)) if action_record is not None else False,
            execution_status=str(getattr(action_record, "execution_status", "") or "") if action_record is not None else "",
            execution_error=str(getattr(action_record, "execution_error", "") or "") if action_record is not None else "",
            execution_stage=str(getattr(action_record, "execution_stage", "execution_result") or "execution_result") if action_record is not None else "execution_result",
            execution_authority=str(getattr(action_record, "execution_authority", "engine_executor") or "engine_executor") if action_record is not None else "engine_executor",
            execution_triggered_by=str(getattr(action_record, "execution_triggered_by", "engine") or "engine") if action_record is not None else "engine",
            verification_status=str(getattr(action_record, "verification_status", "") or "") if action_record is not None else "",
            verification_note=str(getattr(action_record, "verification_note", "") or "") if action_record is not None else "",
            verification_stage=str(getattr(action_record, "verification_stage", "verification_result") or "verification_result") if action_record is not None else "verification_result",
            verification_authority=str(getattr(action_record, "verification_authority", "engine_verifier") or "engine_verifier") if action_record is not None else "engine_verifier",
            verification_triggered_by=str(getattr(action_record, "verification_triggered_by", "engine") or "engine") if action_record is not None else "engine",
            bouncer_intent_level=str(getattr(action_record, "bouncer_intent_level", "") or "") if action_record is not None else "",
            bouncer_intent_code=str(getattr(action_record, "bouncer_intent_code", "") or "") if action_record is not None else "",
            intent_verdict_stage=str(getattr(action_record, "intent_verdict_stage", "intent_verdict") or "intent_verdict") if action_record is not None else "intent_verdict",
            intent_verdict_authority=str(getattr(action_record, "intent_verdict_authority", "bouncer") or "bouncer") if action_record is not None else "bouncer",
            intent_verdict_triggered_by=str(getattr(action_record, "intent_verdict_triggered_by", "engine") or "engine") if action_record is not None else "engine",
            referee_outcome_level=str(getattr(action_record, "referee_outcome_level", "") or "") if action_record is not None else "",
            referee_outcome_code=str(getattr(action_record, "referee_outcome_code", "") or "") if action_record is not None else "",
            outcome_verdict_stage=str(getattr(action_record, "outcome_verdict_stage", "outcome_verdict") or "outcome_verdict") if action_record is not None else "outcome_verdict",
            outcome_verdict_authority=str(getattr(action_record, "outcome_verdict_authority", "referee") or "referee") if action_record is not None else "referee",
            outcome_verdict_triggered_by=str(getattr(action_record, "outcome_verdict_triggered_by", "engine") or "engine") if action_record is not None else "engine",
            incident_class=str(getattr(action_record, "incident_class", "") or "") if action_record is not None else "",
            stage=str(getattr(action_record, "learning_commit_stage", "learning_commit") or "learning_commit") if action_record is not None else "learning_commit",
            authority=str(getattr(action_record, "learning_commit_authority", "memory_store") or "memory_store") if action_record is not None else "memory_store",
            triggered_by=str(getattr(action_record, "learning_commit_triggered_by", "engine") or "engine") if action_record is not None else "engine",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "lever": self.lever,
            "objective_id": self.objective_id,
            "commit_status": self.commit_status,
            "learning_ltm_applied": bool(self.learning_ltm_applied),
            "learning_policy_reason": self.learning_policy_reason,
            "proposal_reason": self.proposal_reason,
            "proposal_intent": self.proposal_intent,
            "proposal_objective_id": self.proposal_objective_id,
            "proposal_score": float(self.proposal_score),
            "proposal_action_origin": self.proposal_action_origin,
            "proposal_stage": self.proposal_stage,
            "proposal_authority": self.proposal_authority,
            "proposal_triggered_by": self.proposal_triggered_by,
            "outcome_label": self.outcome_label,
            "hard_fail": bool(self.hard_fail),
            "execution_status": self.execution_status,
            "execution_error": self.execution_error,
            "execution_stage": self.execution_stage,
            "execution_authority": self.execution_authority,
            "execution_triggered_by": self.execution_triggered_by,
            "verification_status": self.verification_status,
            "verification_note": self.verification_note,
            "verification_stage": self.verification_stage,
            "verification_authority": self.verification_authority,
            "verification_triggered_by": self.verification_triggered_by,
            "bouncer_intent_level": self.bouncer_intent_level,
            "bouncer_intent_code": self.bouncer_intent_code,
            "intent_verdict_stage": self.intent_verdict_stage,
            "intent_verdict_authority": self.intent_verdict_authority,
            "intent_verdict_triggered_by": self.intent_verdict_triggered_by,
            "referee_outcome_level": self.referee_outcome_level,
            "referee_outcome_code": self.referee_outcome_code,
            "outcome_verdict_stage": self.outcome_verdict_stage,
            "outcome_verdict_authority": self.outcome_verdict_authority,
            "outcome_verdict_triggered_by": self.outcome_verdict_triggered_by,
            "incident_class": self.incident_class,
            "stage": self.stage,
            "authority": self.authority,
            "triggered_by": self.triggered_by,
        }

@dataclass(frozen=True)
class ExecutionOutcome:
    decision_trace_id: str
    action_lever: str
    action_intent: str
    action_reason: str
    objective_id: str
    action_suppressed: bool
    action_executed: bool
    action_origin: str
    action_success: bool | None
    action_error: str = ""
    radio_drop_detected: bool = False
    # Why the action was suppressed. One of:
    #   "probe_mode"  — Probe/Test Mode blocked autonomous actuation
    #   "observe_only" — observe-only mode blocked all actuation
    #   ""            — not suppressed (action executed or no action)
    suppressed_reason: str = ""
    bouncer_intent_level: str = ""
    bouncer_intent_code: str = ""
    bouncer_outcome_level: str = ""
    bouncer_outcome_code: str = ""
    action_root_id: str = ""
    command_id: str = ""
    proof_id: str = ""
    session_scope_key: str = ""
    identity_key: str = ""
    source_generation: int = 0
    target_shape: tuple[tuple[str, Any], ...] = ()
    opportunity_id: str = ""
    opportunity_set_id: str = ""
    decision_id: str = ""
    decision_receipt_id: str = ""
    bandit_selection_receipt_id: str = ""
    brain_decision_receipt: tuple[tuple[str, Any], ...] = ()
    bandit_selection_receipt: tuple[tuple[str, Any], ...] = ()

    @property
    def operator_initiated(self) -> bool:
        """Presentation projection derived only from the typed action origin."""

        from arch4.action_origin import is_human_operator_origin

        return bool(
            self.action_origin and is_human_operator_origin(self.action_origin)
        )

    @classmethod
    def from_runtime(
        cls,
        *,
        decision_trace_id: str,
        action: LeverAction | None,
        action_suppressed: bool,
        action_result: Any = None,
        suppressed_reason: str = "",
        bouncer_intent: Mapping[str, Any] | None = None,
        bouncer_outcome: Mapping[str, Any] | None = None,
    ) -> "ExecutionOutcome":
        from arch4.action_transaction import action_transaction_key_from_action
        from arch4.action_admission import (
            SelectedActionContext,
            selected_action_context_from_action,
        )

        action_key = action_transaction_key_from_action(action)
        selected_context = None
        if action is not None:
            selected_context = selected_action_context_from_action(action)
        return cls(
            decision_trace_id=str(decision_trace_id or ""),
            action_lever=str(getattr(getattr(action, "lever", None), "name", "") or ("none" if action is None else "")),
            action_intent=str(getattr(action, "intent", "") or ""),
            action_reason=str(getattr(action, "reason", "") or ""),
            objective_id=str(getattr(action, "objective_id", "") or ""),
            action_suppressed=bool(action_suppressed),
            action_executed=bool(action is not None and not action_suppressed and action_result is not None),
            action_origin=(
                selected_context.origin.kind.value
                if isinstance(selected_context, SelectedActionContext)
                else ""
            ),
            action_success=(
                None
                if action_result is None
                else bool(getattr(action_result, "success", False))
            ),
            action_error=str(getattr(action_result, "error", "") or "") if action_result is not None else "",
            radio_drop_detected=bool(getattr(action_result, "radio_drop_detected", False)) if action_result is not None else False,
            suppressed_reason=str(suppressed_reason or ""),
            bouncer_intent_level=str((bouncer_intent or {}).get("level", "") or ""),
            bouncer_intent_code=str((bouncer_intent or {}).get("code", "") or ""),
            bouncer_outcome_level=str((bouncer_outcome or {}).get("level", "") or ""),
            bouncer_outcome_code=str((bouncer_outcome or {}).get("code", "") or ""),
            action_root_id=str(action_key.root_id if action_key else ""),
            command_id=str(action_key.command_id if action_key else ""),
            proof_id=str(action_key.proof_id if action_key else ""),
            session_scope_key=str(
                action_key.matrix_identity.session_scope_key
                if action_key
                else ""
            ),
            identity_key=str(action_key.identity_key if action_key else ""),
            source_generation=int(
                action_key.matrix_identity.matrix_sequence
                if action_key
                else 0
            ),
            target_shape=(
                _freeze_mapping(action_key.target.to_mapping())
                if action_key
                else ()
            ),
            opportunity_id=str(action_key.opportunity_id if action_key else ""),
            opportunity_set_id=str(action_key.opportunity_set_id if action_key else ""),
            decision_id=str(action_key.decision_id if action_key else ""),
            decision_receipt_id=str(
                action_key.decision_receipt_id if action_key else ""
            ),
            bandit_selection_receipt_id=str(
                action_key.bandit_selection_receipt_id if action_key else ""
            ),
            brain_decision_receipt=(
                action_key.brain_decision_receipt if action_key else ()
            ),
            bandit_selection_receipt=(
                action_key.bandit_selection_receipt if action_key else ()
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_trace_id": self.decision_trace_id,
            "action_lever": self.action_lever,
            "action_intent": self.action_intent,
            "action_reason": self.action_reason,
            "objective_id": self.objective_id,
            "action_suppressed": bool(self.action_suppressed),
            "suppressed_reason": self.suppressed_reason,
            "action_executed": bool(self.action_executed),
            "action_origin": self.action_origin,
            "action_success": self.action_success,
            "action_error": self.action_error,
            "radio_drop_detected": bool(self.radio_drop_detected),
            "bouncer_intent_level": self.bouncer_intent_level,
            "bouncer_intent_code": self.bouncer_intent_code,
            "bouncer_outcome_level": self.bouncer_outcome_level,
            "bouncer_outcome_code": self.bouncer_outcome_code,
            "action_root_id": self.action_root_id,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "session_scope_key": self.session_scope_key,
            "identity_key": self.identity_key,
            "source_generation": int(self.source_generation),
            "target_shape": _thaw_frozen_mapping(self.target_shape),
            "opportunity_id": self.opportunity_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "bandit_selection_receipt_id": self.bandit_selection_receipt_id,
            "brain_decision_receipt": _thaw_frozen_mapping(
                self.brain_decision_receipt
            ),
            "bandit_selection_receipt": _thaw_frozen_mapping(
                self.bandit_selection_receipt
            ),
        }
