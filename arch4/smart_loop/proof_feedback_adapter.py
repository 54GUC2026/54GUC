"""SmartLoop boundary adapter for layered proof feedback.

SmartLoop owns action selection, not proof-feedback formulae. This adapter is
the only allowed SmartLoop-side import boundary to the Proof/Referee feedback
owner.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.brain.proof_feedback_owner import build_layered_action_feedback


OWNER = "arch4.smart_loop.proof_feedback_adapter"


def build_smart_loop_layered_feedback(
    *,
    label: str,
    reason: str,
    cq_delta: float | None = None,
    congestion_delta: float | None = None,
    evidence: Mapping[str, Any] | None = None,
    horizon_s: int = 0,
) -> dict[str, Any]:
    return build_layered_action_feedback(
        label=label,
        reason=reason,
        cq_delta=cq_delta,
        congestion_delta=congestion_delta,
        evidence=evidence,
        horizon_s=horizon_s,
    )


__all__ = ["OWNER", "build_smart_loop_layered_feedback"]
