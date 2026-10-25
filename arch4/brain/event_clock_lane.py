"""
Brain event-inbox runtime-clock lane owner.

Runtime clock triggers this lane. Brain owns draining due events, publishing the
event feed into the Brain register bank, refreshing the fused register, and
running due staged ROI proof evaluation from that fused register.
"""

from __future__ import annotations

from typing import Any

from arch4.brain.action_proof_register import action_proof_surface_from_engine
from arch4.brain.feed_registers import (
    FEED_BRAIN_EVENTS,
    OWNER_BRAIN_EVENT_INBOX,
    publish_brain_feed_registers_status,
    publish_brain_feed_registers_status_failure,
    require_brain_feed_register_bank,
)
from arch4.brain.proof_referee_due_owner import evaluate_due_staged_roi_from_brain_fused_register
from arch4.action_transaction_authority import require_action_transaction_authority
from arch4.runtime_clock import BRAIN_EVENT_INBOX_DRAIN_MAX_ITEMS, BRAIN_EVENT_INBOX_JOB_KEY, ClockedJobOutcome
from arch4.intelligence.evidence_recorder import (
    require_intelligence_evidence_recorder,
)

BRAIN_EVENT_FEED_HEARTBEAT_S = 1.0


def run_brain_event_inbox_clock_lane(engine: Any, *, now: float) -> ClockedJobOutcome:
    controller = getattr(engine, "controller", None)
    brain = getattr(controller, "brain", None)
    inbox = getattr(brain, "event_inbox", None)
    if inbox is None or not callable(getattr(inbox, "drain_due", None)):
        raise RuntimeError("controller brain event inbox is not composed")
    try:
        result = dict(
            inbox.drain_due(
                now=now,
                max_events=BRAIN_EVENT_INBOX_DRAIN_MAX_ITEMS,
            )
            or {}
        )
    except Exception as exc:
        return ClockedJobOutcome(
            ran=False,
            last_error=str(exc or ""),
            backpressure_reason="brain_event_inbox_drain_failed",
        )

    engine._brain_event_inbox_last_drain = result
    if callable(getattr(inbox, "status", None)):
        try:
            engine._brain_event_inbox_status = dict(inbox.status(now=now) or {})
        except Exception as exc:
            engine._brain_event_inbox_status = {
                "last_error": str(exc or ""),
                "status_failed": True,
            }

    pending = int(result.get("pending", 0) or 0)
    failed = int(result.get("failed", 0) or 0)
    feed_bank = require_brain_feed_register_bank(engine)
    publish_signature = (
        int(result.get("drained", 0) or 0),
        pending,
        failed,
        str(result.get("last_error", "") or ""),
        int(_status_value(getattr(engine, "_brain_event_inbox_status", {}), "dropped")),
    )
    last_publish_ts = float(getattr(engine, "_brain_event_inbox_feed_last_publish_ts", 0.0) or 0.0)
    last_signature = getattr(engine, "_brain_event_inbox_feed_signature", None)
    should_publish = (
        publish_signature != last_signature
        or max(0.0, float(now) - last_publish_ts) >= BRAIN_EVENT_FEED_HEARTBEAT_S
    )
    if should_publish:
        try:
            feed_bank.write_feeder(
                FEED_BRAIN_EVENTS,
                owner=OWNER_BRAIN_EVENT_INBOX,
                payload={
                    "event_inbox_status": dict(getattr(engine, "_brain_event_inbox_status", {}) or {}),
                    "last_drain": dict(result or {}),
                    "action_proof_closures": action_proof_surface_from_engine(engine),
                },
                source=BRAIN_EVENT_INBOX_JOB_KEY,
                updated_at=now,
                valid=failed <= 0,
                stale_after_s=1.0,
                error=str(result.get("last_error", "") or ""),
            )
            publish_brain_feed_registers_status(engine, now=now, refresh_fused=True)
            fused = feed_bank.fused()
            engine._brain_event_inbox_feed_signature = publish_signature
            engine._brain_event_inbox_feed_last_publish_ts = float(now)
            last_roi_eval = float(getattr(engine, "_brain_staged_roi_last_eval_ts", 0.0) or 0.0)
            if now - last_roi_eval >= 1.0:
                staged = evaluate_due_staged_roi_from_brain_fused_register(
                    logger=require_intelligence_evidence_recorder(engine),
                    memory=brain.memory,
                    action_transaction_authority=require_action_transaction_authority(
                        engine
                    ),
                    historical=brain.historical,
                    crystal_ball=brain.crystal_ball,
                    brain_event_inbox=inbox,
                    bandit_state_store=brain.contextual_bandit_state_store,
                    fused_register=fused,
                    now=now,
                )
                engine._brain_staged_roi_clock_updates = list(staged or [])
                engine._brain_staged_roi_last_eval_ts = float(now)
        except Exception as exc:
            publish_brain_feed_registers_status_failure(engine, last_error=str(exc or ""))

    return ClockedJobOutcome(
        ran=True,
        backlog=pending,
        last_error=str(result.get("last_error", "") or ""),
        backpressure_reason=(
            "brain_event_handler_failed"
            if failed > 0
            else ("brain_event_backlog_remaining" if pending > 0 else "")
        ),
    )


def _status_value(status: Any, key: str) -> int:
    if not isinstance(status, dict):
        return 0
    try:
        return int(status.get(key, 0) or 0)
    except Exception:
        return 0


__all__ = ["run_brain_event_inbox_clock_lane"]
