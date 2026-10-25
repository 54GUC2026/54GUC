"""Clean-room self-learning closed-loop core."""

from arch4.smart_loop.closed_loop import (
    ActionPlan,
    Candidate,
    ClosedLoopDecision,
    LearningFacts,
    ModemContext,
    ProofGrade,
    ProofWindow,
    SmartLoopCore,
    SmartLoopState,
    TsarGate,
    default_smart_loop_matrix,
    run_smart_loop_matrix,
)
from arch4.smart_loop.register_adapter import smart_loop_inputs_from_registers

__all__ = [
    "ActionPlan",
    "Candidate",
    "ClosedLoopDecision",
    "LearningFacts",
    "ModemContext",
    "ProofGrade",
    "ProofWindow",
    "SmartLoopCore",
    "SmartLoopState",
    "TsarGate",
    "default_smart_loop_matrix",
    "run_smart_loop_matrix",
    "smart_loop_inputs_from_registers",
]
