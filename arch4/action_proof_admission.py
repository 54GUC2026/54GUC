"""Single admission point for action records that may become learning proof."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.brain.memory_action_history_owner import (
    record_action_history as memory_record_action_history,
)

OWNER = "arch4.action_proof_admission"


class ActionProofAdmissionError(RuntimeError):
    """Raised when action proof cannot be admitted into the owned store."""


def record_action_for_proof(engine: Any, *, source: str, **kwargs: Any) -> Any:
    """Record an action outcome through the only live proof-admission API.

    Memory owns storage. This module owns the production admission boundary so
    action paths do not call Memory directly from multiple places.
    """

    controller = getattr(engine, "controller", None)
    brain = getattr(controller, "brain", None)
    memory = getattr(brain, "memory", None)
    if memory is None:
        raise ActionProofAdmissionError(
            "controller_brain_memory_owner_unavailable"
        )
    params = dict(kwargs)
    if "proof_observed_at" in params:
        raise ActionProofAdmissionError(
            "action_proof_timestamp_caller_override_illegal"
        )
    action_origin = str(params.get("proposal_action_origin") or "").strip()
    if not action_origin:
        raise ActionProofAdmissionError("action_proof_typed_origin_required")
    learning_gate = params.get("learning_gate")
    if isinstance(learning_gate, Mapping):
        gate = dict(learning_gate)
    else:
        gate = {}
    gate.setdefault("proof_admission_owner", OWNER)
    gate.setdefault("proof_admission_source", str(source or ""))
    params["learning_gate"] = gate
    params["proof_observed_at"] = _proof_observed_at(params)
    return memory_record_action_history(memory, **params)


def _proof_observed_at(params: Mapping[str, Any]) -> float:
    """Return the accepted evidence time owned by the proof boundary."""

    success = bool(params.get("success", False))
    outcome = params.get("modem_after")
    if success and outcome is None:
        raise ActionProofAdmissionError(
            "successful_action_proof_requires_outcome_generation"
        )
    evidence = outcome if outcome is not None else params.get("modem_before")
    generation = getattr(evidence, "generation", None)
    try:
        observed_at = float(getattr(generation, "sample_ts", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise ActionProofAdmissionError(
            "action_proof_evidence_timestamp_invalid"
        ) from exc
    if observed_at <= 0.0:
        raise ActionProofAdmissionError(
            "action_proof_evidence_timestamp_required"
        )
    return observed_at


__all__ = ["ActionProofAdmissionError", "OWNER", "record_action_for_proof"]
