"""
Health-stage action policy helpers.

These helpers keep staged health semantics out of the core decision pipeline
body while making the current V4 rule explicit:

- health degradation first suppresses risky exploration
- it does not yet invent or force recovery actions by itself
"""

from __future__ import annotations

from typing import Any, Dict

from arch4.interfaces import LeverAction, LeverType


HEALTH_HOLD_STAGES = {"warning", "degraded_hold", "degraded_persist"}
RISKY_HEALTH_HOLD_INTENTS = {"explore", "explore_cell_lock"}
RISKY_HEALTH_HOLD_LEVERS = {LeverType.BAND_SELECTION, LeverType.PBAND, LeverType.CELL_LOCK}


def health_stage(status: Dict[str, Any] | None) -> str:
    return str((status or {}).get("stage", "healthy") or "healthy")


def health_action_hint(status: Dict[str, Any] | None) -> str:
    return str((status or {}).get("action_hint", "none") or "none")


def hold_risky_exploration(status: Dict[str, Any] | None) -> bool:
    return health_stage(status) in HEALTH_HOLD_STAGES


def is_risky_exploration_action(action: LeverAction | None) -> bool:
    if action is None:
        return False
    if action.lever not in RISKY_HEALTH_HOLD_LEVERS:
        return False
    params = getattr(action, "params", {}) or {}
    intent = str(params.get("_smart_loop_availability_intent") or getattr(action, "intent", "") or "")
    return intent in RISKY_HEALTH_HOLD_INTENTS


def should_hold_action_for_health(
    *,
    status: Dict[str, Any] | None,
    action: LeverAction | None,
) -> bool:
    if action is None:
        return False
    if not hold_risky_exploration(status):
        return False
    return is_risky_exploration_action(action)


def health_hold_reason(
    *,
    status: Dict[str, Any] | None,
    action: LeverAction | None,
) -> str:
    stage = health_stage(status)
    hint = health_action_hint(status)
    lever = getattr(getattr(action, "lever", None), "name", "")
    intent = str(getattr(action, "intent", "") or "")
    return (
        f"health hold stage={stage} hint={hint} suppresses risky exploration"
        f" lever={lever} intent={intent}"
    )
