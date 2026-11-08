"""Immutable end-to-end cellular intelligence transaction shell."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping

from arch4.brain.tick_contract import (
    DecisionActionSummary,
    DecisionTrace,
    ExecutionOutcome,
)
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.interfaces import LeverAction
from arch4.action_transaction import (
    ActionTransactionKey,
    action_transaction_key_from_action,
)


class BrainTransactionStage(str, Enum):
    FACTS = "facts"
    DECIDED = "decided"
    EXECUTED = "executed"
    PROVEN = "proven"
    CLOSED = "closed"


def _frozen_mapping(values: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(((str(key), value) for key, value in (values or {}).items()), key=lambda row: row[0]))


@dataclass(frozen=True)
class BrainTransaction:
    transaction_id: str
    generation: int
    facts: CellularIntelligenceGeneration | None
    stage: BrainTransactionStage = BrainTransactionStage.FACTS
    candidates: tuple[DecisionActionSummary, ...] = ()
    selection: DecisionActionSummary | None = None
    no_action_reason: str = ""
    arbitration: DecisionTrace | None = None
    execution: ExecutionOutcome | None = None
    readback: tuple[tuple[str, Any], ...] = ()
    proof: tuple[tuple[str, Any], ...] = ()
    grade: str = ""
    learning_admission: str = "pending"
    action_key: ActionTransactionKey | None = None

    @classmethod
    def from_facts(cls, facts: CellularIntelligenceGeneration) -> "BrainTransaction":
        if not isinstance(facts, CellularIntelligenceGeneration):
            raise TypeError("brain_transaction_requires_cellular_intelligence")
        generation = int(facts.generation.generation)
        transaction_id = (
            f"controller:{generation}:"
            f"{int(float(facts.generation.sample_ts or 0.0) * 1000.0)}"
        )
        return cls(
            transaction_id=transaction_id,
            generation=generation,
            facts=facts,
        )

    @classmethod
    def from_event_payload(cls, payload: Mapping[str, Any]) -> "BrainTransaction":
        """Restore the durable transaction shell without inventing modem facts.

        Executed transactions never return to decision/execution after restart;
        they only wait for terminal Referee correlation.  The protected input
        frame therefore remains unavailable instead of being reconstructed
        from false defaults.
        """

        row = dict(payload or {})
        stage = BrainTransactionStage(str(row.get("stage") or ""))
        candidates_raw = row.get("candidates") if isinstance(row.get("candidates"), list) else []
        selection_raw = row.get("selection") if isinstance(row.get("selection"), Mapping) else None
        arbitration_raw = row.get("arbitration") if isinstance(row.get("arbitration"), Mapping) else None
        execution_raw = row.get("execution") if isinstance(row.get("execution"), Mapping) else None
        transaction = cls(
            transaction_id=str(row.get("transaction_id") or ""),
            generation=int(row.get("generation", 0) or 0),
            facts=None,
            stage=stage,
            candidates=tuple(_action_summary_from_mapping(item) for item in candidates_raw if isinstance(item, Mapping)),
            selection=_action_summary_from_mapping(selection_raw) if selection_raw is not None else None,
            no_action_reason=str(row.get("no_action_reason") or ""),
            arbitration=DecisionTrace.from_mapping(arbitration_raw) if arbitration_raw is not None else None,
            execution=_execution_from_mapping(execution_raw) if execution_raw is not None else None,
            readback=_frozen_mapping(row.get("readback") if isinstance(row.get("readback"), Mapping) else {}),
            proof=_frozen_mapping(row.get("proof") if isinstance(row.get("proof"), Mapping) else {}),
            grade=str(row.get("grade") or ""),
            learning_admission=str(row.get("learning_admission") or "pending"),
            action_key=(
                ActionTransactionKey.from_mapping(
                    dict(row.get("action_transaction") or {})
                )
                if isinstance(row.get("action_transaction"), Mapping)
                and row.get("action_transaction")
                else None
            ),
        )
        if not transaction.transaction_id or transaction.generation <= 0:
            raise ValueError("brain_transaction_durable_identity_invalid")
        return transaction

    def with_decision(
        self,
        *,
        candidate_action: LeverAction | None,
        selected_action: LeverAction | None,
        trace: DecisionTrace | None,
        no_action_reason: str = "",
    ) -> "BrainTransaction":
        candidate = DecisionActionSummary.from_action(candidate_action)
        selection = DecisionActionSummary.from_action(selected_action)
        action_key = action_transaction_key_from_action(selected_action)
        candidates = (candidate,) if candidate is not None else ()
        reason = str(no_action_reason or "")
        if selection is None and not reason and trace is not None:
            reason = str(trace.primary_blocker_gate or trace.primary_blocker_reason or "no_candidate")
        return replace(
            self,
            transaction_id=(action_key.root_id if action_key is not None else self.transaction_id),
            stage=BrainTransactionStage.DECIDED,
            candidates=candidates,
            selection=selection,
            no_action_reason=reason,
            arbitration=trace,
            action_key=action_key,
        )

    def with_execution(
        self,
        *,
        execution: ExecutionOutcome,
        readback: Mapping[str, Any] | None = None,
    ) -> "BrainTransaction":
        if self.stage is not BrainTransactionStage.DECIDED:
            raise RuntimeError(f"brain_transaction_execution_from_{self.stage.value}")
        if execution.action_executed and self.action_key is None:
            raise RuntimeError("brain_transaction_executed_action_key_missing")
        if self.action_key is not None and execution.action_root_id != self.action_key.root_id:
            raise RuntimeError("brain_transaction_execution_root_mismatch")
        return replace(
            self,
            stage=BrainTransactionStage.EXECUTED,
            execution=execution,
            readback=_frozen_mapping(readback),
        )

    def with_proof(
        self,
        *,
        proof: Mapping[str, Any] | None,
        grade: str,
        learning_admission: str,
    ) -> "BrainTransaction":
        if self.stage is not BrainTransactionStage.EXECUTED:
            raise RuntimeError(f"brain_transaction_proof_from_{self.stage.value}")
        return replace(
            self,
            stage=BrainTransactionStage.PROVEN,
            proof=_frozen_mapping(proof),
            grade=str(grade or "ungraded"),
            learning_admission=str(learning_admission or "blocked"),
        )

    def with_readback_update(
        self, readback: Mapping[str, Any]
    ) -> "BrainTransaction":
        if self.stage is not BrainTransactionStage.EXECUTED:
            raise RuntimeError(f"brain_transaction_readback_from_{self.stage.value}")
        merged = dict(self.readback)
        merged.update(dict(readback or {}))
        return replace(self, readback=_frozen_mapping(merged))

    def close(self) -> "BrainTransaction":
        if self.stage is not BrainTransactionStage.PROVEN:
            raise RuntimeError(f"brain_transaction_close_from_{self.stage.value}")
        return replace(self, stage=BrainTransactionStage.CLOSED)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "generation": self.generation,
            "stage": self.stage.value,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selection": self.selection.to_dict() if self.selection is not None else None,
            "no_action_reason": self.no_action_reason,
            "arbitration": self.arbitration.to_dict() if self.arbitration is not None else None,
            "execution": self.execution.to_dict() if self.execution is not None else None,
            "readback": dict(self.readback),
            "proof": dict(self.proof),
            "grade": self.grade,
            "learning_admission": self.learning_admission,
            "action_transaction": self.action_key.as_dict() if self.action_key else {},
        }


def _action_summary_from_mapping(raw: Mapping[str, Any]) -> DecisionActionSummary:
    row = dict(raw or {})
    return DecisionActionSummary(
        brief=str(row.get("brief") or ""),
        lever=str(row.get("lever") or ""),
        intent=str(row.get("intent") or ""),
        objective_id=str(row.get("objective_id") or ""),
        reason=str(row.get("reason") or ""),
        availability_intent=str(row.get("availability_intent") or ""),
        action_root_id=str(row.get("action_root_id") or ""),
        command_id=str(row.get("command_id") or ""),
        proof_id=str(row.get("proof_id") or ""),
        session_scope_key=str(row.get("session_scope_key") or ""),
        identity_key=str(row.get("identity_key") or ""),
        source_generation=int(row.get("source_generation", 0) or 0),
        target_shape=_frozen_mapping(
            row.get("target_shape")
            if isinstance(row.get("target_shape"), Mapping)
            else {}
        ),
        opportunity_id=str(row.get("opportunity_id") or ""),
        opportunity_set_id=str(row.get("opportunity_set_id") or ""),
        decision_id=str(row.get("decision_id") or ""),
        decision_receipt_id=str(row.get("decision_receipt_id") or ""),
        bandit_selection_receipt_id=str(
            row.get("bandit_selection_receipt_id") or ""
        ),
        brain_decision_receipt=_frozen_mapping(
            row.get("brain_decision_receipt")
            if isinstance(row.get("brain_decision_receipt"), Mapping)
            else {}
        ),
        bandit_selection_receipt=_frozen_mapping(
            row.get("bandit_selection_receipt")
            if isinstance(row.get("bandit_selection_receipt"), Mapping)
            else {}
        ),
    )


def _execution_from_mapping(raw: Mapping[str, Any]) -> ExecutionOutcome:
    row = dict(raw or {})
    if "manual_action" in row or "operator_initiated" in row:
        raise ValueError("brain_transaction_rejects_boolean_action_origin")
    return ExecutionOutcome(
        decision_trace_id=str(row.get("decision_trace_id") or ""),
        action_lever=str(row.get("action_lever") or ""),
        action_intent=str(row.get("action_intent") or ""),
        action_reason=str(row.get("action_reason") or ""),
        objective_id=str(row.get("objective_id") or ""),
        action_suppressed=bool(row.get("action_suppressed", False)),
        action_executed=bool(row.get("action_executed", False)),
        action_origin=str(row.get("action_origin") or ""),
        action_success=(
            None if row.get("action_success") is None else bool(row.get("action_success"))
        ),
        action_error=str(row.get("action_error") or ""),
        radio_drop_detected=bool(row.get("radio_drop_detected", False)),
        suppressed_reason=str(row.get("suppressed_reason") or ""),
        bouncer_intent_level=str(row.get("bouncer_intent_level") or ""),
        bouncer_intent_code=str(row.get("bouncer_intent_code") or ""),
        bouncer_outcome_level=str(row.get("bouncer_outcome_level") or ""),
        bouncer_outcome_code=str(row.get("bouncer_outcome_code") or ""),
        action_root_id=str(row.get("action_root_id") or ""),
        command_id=str(row.get("command_id") or ""),
        proof_id=str(row.get("proof_id") or ""),
        session_scope_key=str(row.get("session_scope_key") or ""),
        identity_key=str(row.get("identity_key") or ""),
        source_generation=int(row.get("source_generation", 0) or 0),
        target_shape=_frozen_mapping(
            row.get("target_shape")
            if isinstance(row.get("target_shape"), Mapping)
            else {}
        ),
        opportunity_id=str(row.get("opportunity_id") or ""),
        opportunity_set_id=str(row.get("opportunity_set_id") or ""),
        decision_id=str(row.get("decision_id") or ""),
        decision_receipt_id=str(row.get("decision_receipt_id") or ""),
        bandit_selection_receipt_id=str(
            row.get("bandit_selection_receipt_id") or ""
        ),
        brain_decision_receipt=_frozen_mapping(
            row.get("brain_decision_receipt")
            if isinstance(row.get("brain_decision_receipt"), Mapping)
            else {}
        ),
        bandit_selection_receipt=_frozen_mapping(
            row.get("bandit_selection_receipt")
            if isinstance(row.get("bandit_selection_receipt"), Mapping)
            else {}
        ),
    )


__all__ = ["BrainTransaction", "BrainTransactionStage"]
