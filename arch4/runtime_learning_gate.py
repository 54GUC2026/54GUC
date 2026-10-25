"""Runtime-register adapter for learning-gate admission.

The runtime register fabric owns the promoted learning gate. Decision and
learning consumers must not recompute it from raw tick/base-status fragments.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.learning_gate_contract import LearningGateDecision
from arch4.runtime_registers import REGISTER_LEARNING, RuntimeRegisterBank


OWNER = "arch4.runtime_learning_gate"


def learning_gate_from_runtime_registers(
    runtime_registers: Mapping[str, Any] | None,
) -> tuple[LearningGateDecision, str]:
    """Return the canonical learning gate from runtime registers.

    Missing or malformed register payloads fail closed. This function is the
    adapter boundary from register truth to Brain/learning consumers.
    """

    register = _register_row(runtime_registers)
    if not register:
        return (
            LearningGateDecision.from_mapping(
                {
                    "learning_allowed": False,
                    "block_reason": "runtime_learning_register_missing",
                    "gate_input_complete": False,
                    "missing_inputs": ["runtime_registers.learning"],
                }
            ),
            "runtime_learning_register_missing",
        )
    if register.get("valid") is False:
        return (
            LearningGateDecision.from_mapping(
                {
                    "learning_allowed": False,
                    "block_reason": "runtime_learning_register_invalid",
                    "block_detail": str(register.get("error") or ""),
                    "gate_input_complete": False,
                    "missing_inputs": ["runtime_registers.learning.valid"],
                }
            ),
            "runtime_learning_register_invalid",
        )
    value = register.get("value") if isinstance(register.get("value"), Mapping) else {}
    if not value:
        return (
            LearningGateDecision.from_mapping(
                {
                    "learning_allowed": False,
                    "block_reason": "runtime_learning_register_value_missing",
                    "gate_input_complete": False,
                    "missing_inputs": ["runtime_registers.learning.value"],
                }
            ),
            "runtime_learning_register_value_missing",
        )
    gate = LearningGateDecision.from_mapping(value)
    source = str(value.get("learning_gate_source") or value.get("source") or "runtime_registers.learning")
    return gate, source


def learning_gate_from_register_bank(
    bank: RuntimeRegisterBank,
) -> tuple[LearningGateDecision, str]:
    if not isinstance(bank, RuntimeRegisterBank):
        raise TypeError("learning gate requires RuntimeRegisterBank")
    return learning_gate_from_runtime_registers(bank.snapshot((REGISTER_LEARNING,)))


def learning_gate_register_value(bank: RuntimeRegisterBank) -> dict[str, Any]:
    if not isinstance(bank, RuntimeRegisterBank):
        raise TypeError("learning gate requires RuntimeRegisterBank")
    row = bank.snapshot((REGISTER_LEARNING,)).get(REGISTER_LEARNING, {})
    value = row.get("value") if isinstance(row, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _register_row(runtime_registers: Mapping[str, Any] | None) -> dict[str, Any]:
    registers = runtime_registers if isinstance(runtime_registers, Mapping) else {}
    row = registers.get(REGISTER_LEARNING)
    if isinstance(row, Mapping):
        return dict(row)
    return {}


__all__ = [
    "OWNER",
    "learning_gate_from_register_bank",
    "learning_gate_register_value",
    "learning_gate_from_runtime_registers",
]
