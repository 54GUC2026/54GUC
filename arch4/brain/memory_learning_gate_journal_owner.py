"""Memory learning-gate journal owner."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from arch4.learning_gate_contract import LearningGateDecision

OWNER = "arch4.brain.memory_learning_gate_journal_owner"


def store_learning_gate_allowed(
    memory: Any,
    learning_gate: Dict[str, Any],
    *,
    surface: str,
    now: Optional[float] = None,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    if not isinstance(learning_gate, dict):
        gate_decision = LearningGateDecision(
            learning_allowed=False,
            block_reason="learning_gate_missing",
            gate_input_complete=False,
            missing_inputs=("learning_gate",),
        )
    else:
        gate_decision = LearningGateDecision.from_mapping(learning_gate)
    if gate_decision.learning_allowed:
        return True
    memory._append_journal_event(
        {
            "kind": f"{surface}_learning_gate_blocked",
            "ts": float(time.time() if now is None else now),
            "surface": str(surface or "memory"),
            "learning_gate": gate_decision.as_dict(),
            "context": dict(context or {}),
        }
    )
    memory._mark_dirty(events=1)
    return False


__all__ = ["OWNER", "store_learning_gate_allowed"]
