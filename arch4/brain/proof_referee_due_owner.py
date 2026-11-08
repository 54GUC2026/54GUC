"""Due delayed-ROI proof/referee evaluation owner."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, Optional

from arch4.brain.proof_referee_due_close_owner import close_due_staged_roi_window
from arch4.brain.proof_referee_due_scan_owner import (
    due_smart_loop_feedback_windows,
    due_staged_roi_windows,
)
from arch4.brain.proof_referee_evidence import staged_roi_runtime_ready
from arch4.brain.proof_referee_interim_feedback_owner import (
    record_due_smart_loop_feedback,
    resume_pending_smart_loop_feedback,
)
from arch4.brain.proof_referee_projection_outbox_owner import resume_reward_projection_outbox
from arch4.brain.proof_referee_runtime_input import staged_roi_inputs_from_brain_fused_register


OWNER = "arch4.brain.proof_referee_due_owner"


def evaluate_due_staged_roi(
    *,
    logger: Any,
    memory: Any,
    action_transaction_authority: Any,
    historical: Any = None,
    crystal_ball: Any = None,
    brain_event_inbox: Any = None,
    bandit_state_store: Any = None,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    extra: dict[str, Any],
    incident_assessment: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    if memory is None:
        return []
    ts = time.time() if now is None else float(now)
    emitted: list[dict[str, Any]] = resume_pending_smart_loop_feedback(
        memory=memory,
        action_transaction_authority=action_transaction_authority,
        brain_event_inbox=brain_event_inbox,
        ts=ts,
    )
    emitted.extend(resume_reward_projection_outbox(
        memory=memory,
        historical=historical,
        crystal_ball=crystal_ball,
        brain_event_inbox=brain_event_inbox,
        logger=logger,
        action_transaction_authority=action_transaction_authority,
        bandit_state_store=bandit_state_store,
        now=ts,
    ))
    runtime_ready, runtime_reason = staged_roi_runtime_ready(
        modem_now=modem_now,
        computed_now=computed_now,
        extra=extra,
        incident_assessment=incident_assessment,
    )
    if not runtime_ready:
        del runtime_reason
        return emitted
    for due_feedback in due_smart_loop_feedback_windows(memory=memory, now=ts):
        emitted.append(
            record_due_smart_loop_feedback(
                memory=memory,
                action_transaction_authority=action_transaction_authority,
                brain_event_inbox=brain_event_inbox,
                original=due_feedback.original,
                meta=due_feedback.meta,
                done=due_feedback.done,
                horizon=due_feedback.horizon,
                modem_now=modem_now,
                computed_now=computed_now,
                extra=extra,
                incident_assessment=incident_assessment,
                ts=ts,
            )
        )
    for due_window in due_staged_roi_windows(memory=memory, now=ts):
        result = close_due_staged_roi_window(
            logger=logger,
            memory=memory,
            action_transaction_authority=action_transaction_authority,
            historical=historical,
            crystal_ball=crystal_ball,
            brain_event_inbox=brain_event_inbox,
            bandit_state_store=bandit_state_store,
            original=due_window.original,
            meta=due_window.meta,
            done=due_window.done,
            horizon=due_window.horizon,
            modem_now=modem_now,
            computed_now=computed_now,
            extra=extra,
            incident_assessment=incident_assessment,
            ts=ts,
        )
        emitted.append(result)
    return emitted


def evaluate_due_staged_roi_from_brain_fused_register(
    *,
    logger: Any,
    memory: Any,
    action_transaction_authority: Any,
    historical: Any = None,
    crystal_ball: Any = None,
    brain_event_inbox: Any = None,
    bandit_state_store: Any = None,
    fused_register: Any,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    inputs = staged_roi_inputs_from_brain_fused_register(fused_register)
    if not inputs:
        return []
    return evaluate_due_staged_roi(
        logger=logger,
        memory=memory,
        action_transaction_authority=action_transaction_authority,
        historical=historical,
        crystal_ball=crystal_ball,
        brain_event_inbox=brain_event_inbox,
        bandit_state_store=bandit_state_store,
        modem_now=inputs["modem_now"],
        computed_now=inputs["computed_now"],
        extra=inputs["extra"],
        incident_assessment=inputs["incident_assessment"],
        now=now,
    )


__all__ = [
    "OWNER",
    "evaluate_due_staged_roi",
    "evaluate_due_staged_roi_from_brain_fused_register",
]
