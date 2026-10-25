"""Canonical smartness reporting/proof projection owner.

This owner replaces the old Brain vector snapshot producer. Reporting is allowed
to copy owner-stamped smartness surfaces and publish derived reporting receipts;
it is not allowed to rebuild candidates from raw vectors or act as a second
learning/proof owner.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Mapping

from arch4.brain.tick_contract import ExecutionOutcome

from arch4.brain.decision_quality_owner import (
    build_decision_quality_from_strategy,
    publish_decision_quality_register,
)
from arch4.brain.action_proof_register import action_proof_surface_from_engine
from arch4.brain.feed_registers import publish_brain_feed_registers_status
from arch4.brain.learning_effect import build_brain_learning_effect, publish_brain_learning_effect_register
from arch4.brain.referee_feedback_quality import referee_feedback_quality_from_surfaces
from arch4.smart_loop.strategy_register import (
    SmartLoopStrategyUnavailable,
    read_smart_loop_strategy_register,
    smart_loop_strategy_fault,
    strategy_authority_projection,
    strategy_candidate_projection,
    strategy_tsar_plan_projection,
)

log = logging.getLogger("arch4.brain.smartness_reporting_owner")

SCHEMA = "brain_smartness_reporting_snapshot.v1"
OWNER = "arch4.brain.smartness_reporting_owner"
_COUNTDOWN_RE = re.compile(r"\b\d+(?:\.\d+)?s\b|\(\d+(?:\.\d+)?s(?:\s+left|\s+remaining)?\)")
_DEFAULT_SNAPSHOT_HEARTBEAT_S = 60.0


def publish_smartness_reporting_snapshot(
    engine: Any,
    *,
    extra: Mapping[str, Any],
    execution_outcome: ExecutionOutcome,
    decision_trace_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Publish one reporting snapshot from canonical owner feeds."""

    if not isinstance(execution_outcome, ExecutionOutcome):
        raise TypeError("smartness_reporting_requires_execution_outcome")

    now_ts = time.time() if now is None else float(now)
    strategy = _read_strategy(engine, now=now_ts)
    candidate_publication = strategy_candidate_projection(strategy)
    candidate_authority = strategy_authority_projection(strategy)
    tsar_plan = strategy_tsar_plan_projection(strategy)
    decision_quality = build_decision_quality_from_strategy(
        strategy=strategy,
        candidate_authority=candidate_authority,
        action_count=1 if execution_outcome.action_executed else 0,
    )
    counterfactual_audit = _counterfactual_audit_from_owner_feeds(
        strategy=strategy,
        candidate_authority=candidate_authority,
        decision_quality=decision_quality,
    )
    referee_feedback_quality = referee_feedback_quality_from_surfaces(
        self_learning_authority=strategy,
        brain_referee=_mapping(extra.get("brain_referee")),
        counterfactual_audit=counterfactual_audit,
    )
    opportunity_inventory = _opportunity_inventory_from_candidate_publication(candidate_publication)
    learning_effect = build_brain_learning_effect(
        tsar_plan=tsar_plan,
        referee_feedback_quality=referee_feedback_quality,
        decision_quality=decision_quality,
        self_learning_candidate_surfaces=(strategy,),
        action_proof_surface=action_proof_surface_from_engine(engine),
    )
    dq_receipt = publish_decision_quality_register(
        engine,
        decision_quality=decision_quality,
        now=now_ts,
    )
    effect_receipt = publish_brain_learning_effect_register(
        engine,
        learning_effect=learning_effect,
        now=now_ts,
    )
    payload = {
        "schema": SCHEMA,
        "owner": OWNER,
        "decision_trace_id": str(decision_trace_id or ""),
        "published_at": round(now_ts, 3),
        "action_present": execution_outcome.action_lever.lower() != "none",
        "action_suppressed": execution_outcome.action_suppressed,
        "action_origin": execution_outcome.action_origin,
        "operator_initiated": execution_outcome.operator_initiated,
        "smart_loop_candidate_projection": candidate_publication,
        "smart_loop_authority": candidate_authority,
        "opportunity_inventory": opportunity_inventory,
        "counterfactual_opportunity_audit": counterfactual_audit,
        "smart_loop_tsar_plan": tsar_plan,
        "self_learning_strategy": strategy,
        "decision_quality": decision_quality,
        "referee_feedback_quality": referee_feedback_quality,
        "brain_learning_effect": learning_effect,
        "decision_quality_register": dict(dq_receipt),
        "brain_learning_effect_register": dict(effect_receipt),
    }
    engine._last_smartness_reporting_snapshot = dict(payload)
    if dq_receipt.get("published") or effect_receipt.get("published"):
        try:
            publish_brain_feed_registers_status(engine, now=now_ts, refresh_fused=True)
        except Exception as exc:
            log.debug("smartness reporting feed status suppressed: %s", exc)
    if _should_emit(engine, payload, now=now_ts):
        diagnostics = getattr(engine, "diagnostics", None)
        log_event = getattr(diagnostics, "log_event", None)
        if callable(log_event):
            log_event("brain_smartness_reporting_snapshot", **payload)
    return payload


def _read_strategy(engine: Any, *, now: float) -> dict[str, Any]:
    try:
        return read_smart_loop_strategy_register(engine, now=now)
    except SmartLoopStrategyUnavailable as exc:
        return smart_loop_strategy_fault(
            reason=str(exc or "smart_loop_strategy_missing"),
            required_next_step="publish_smart_loop_strategy_register",
        )


def _should_emit(engine: Any, payload: Mapping[str, Any], *, now: float) -> bool:
    signature = _snapshot_signature(payload)
    if not signature:
        return False
    last_signature = str(getattr(engine, "_last_smartness_reporting_signature", "") or "")
    last_emit = float(getattr(engine, "_last_smartness_reporting_emit_monotonic", 0.0) or 0.0)
    heartbeat = float(getattr(engine, "_smartness_reporting_snapshot_heartbeat_s", _DEFAULT_SNAPSHOT_HEARTBEAT_S) or 0.0)
    monotonic_now = time.monotonic()
    if signature == last_signature and heartbeat > 0.0 and monotonic_now - last_emit < heartbeat:
        return False
    engine._last_smartness_reporting_signature = signature
    engine._last_smartness_reporting_emit_monotonic = monotonic_now
    return True


def _snapshot_signature(payload: Mapping[str, Any]) -> str:
    strategy = _mapping(payload.get("self_learning_strategy"))
    tsar_plan = _mapping(payload.get("smart_loop_tsar_plan"))
    arbitration = _mapping(tsar_plan.get("arbitration_outcome"))
    decision_quality = _mapping(payload.get("decision_quality"))
    effect = _mapping(payload.get("brain_learning_effect"))
    candidate_authority = _mapping(payload.get("smart_loop_authority"))
    signature = {
        "strategy": {
            "outcome": strategy.get("outcome", ""),
            "reason": _normalise_text(strategy.get("reason", "")),
            "required_next_step": strategy.get("required_next_step", ""),
        },
        "tsar": {
            "status": tsar_plan.get("status", ""),
            "reason": tsar_plan.get("reason", ""),
            "verdict": arbitration.get("verdict", ""),
            "reason_code": arbitration.get("reason_code", ""),
        },
        "decision_quality": {
            "bucket": decision_quality.get("bucket", ""),
            "reason": _normalise_text(decision_quality.get("decision_quality_reason", "")),
            "should_have_acted": bool(decision_quality.get("should_have_acted", False)),
        },
        "learning_effect": {
            "bucket": effect.get("decision_quality_bucket", ""),
            "learning_signal": bool(effect.get("learning_signal", False)),
            "diagnostic_signal": bool(effect.get("diagnostic_signal", False)),
        },
        "candidate": {
            "status": candidate_authority.get("status", ""),
            "reason": _normalise_text(candidate_authority.get("reason", "")),
            "required_next_step": candidate_authority.get("required_next_step", ""),
        },
    }
    return json.dumps(signature, sort_keys=True, separators=(",", ":"), default=str)


def _opportunity_inventory_from_candidate_publication(publication: Mapping[str, Any]) -> dict[str, Any]:
    inventory = _mapping(publication.get("opportunity_inventory"))
    if inventory:
        return inventory
    authority = _mapping(publication.get("authority"))
    candidates = authority.get("top_candidates")
    if not isinstance(candidates, list):
        candidates = []
    return {
        "schema": "brain_opportunity_inventory.v1",
        "owner": OWNER,
        "source_owner": str(authority.get("owner") or publication.get("owner") or ""),
        "candidates": [dict(row) for row in candidates if isinstance(row, Mapping)],
    }


def _counterfactual_audit_from_owner_feeds(
    *,
    strategy: Mapping[str, Any],
    candidate_authority: Mapping[str, Any],
    decision_quality: Mapping[str, Any],
) -> dict[str, Any]:
    outcome = _normalise_text(strategy.get("outcome"))
    reason = (
        _normalise_text(strategy.get("reason"))
        or _normalise_text(decision_quality.get("decision_quality_reason"))
        or _normalise_text(candidate_authority.get("reason"))
    )
    should_have = bool(decision_quality.get("should_have_acted", False))
    if outcome == "candidate_missing_counterfactual":
        status = "needs_action_candidate"
        candidate_generation_failed = True
        should_have = True
    elif outcome == "candidate_present_but_not_operational" or should_have:
        status = "candidate_present"
        candidate_generation_failed = False
        should_have = True
    elif outcome in {"no_action_justified", "action_rejected", "safety_wait", "recorded_advisory"}:
        status = "ok"
        candidate_generation_failed = False
    else:
        status = "insufficient_evidence"
        candidate_generation_failed = False
    return {
        "schema": "brain_counterfactual_opportunity_audit.v1",
        "owner": "arch4.brain.counterfactual_opportunity_audit",
        "source_owner": OWNER,
        "status": status,
        "reason": reason or status,
        "should_have_candidate": should_have,
        "candidate_generation_failed": candidate_generation_failed,
        "strategy_outcome": outcome,
    }


def _normalise_text(value: Any) -> str:
    return _COUNTDOWN_RE.sub("<duration>", str(value or "").strip())


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "OWNER",
    "SCHEMA",
    "publish_smartness_reporting_snapshot",
]
