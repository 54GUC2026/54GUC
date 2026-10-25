"""
Incident-assessment action policy helpers.
"""

from __future__ import annotations

from typing import Any, Dict

from arch4.interfaces import LeverAction, LeverType


RISKY_INCIDENT_HOLD_INTENTS = {
    "explore",
    "explore_cell_lock",
    "cell_optimization",
    "cell_optimization_hook",
    "cell_optimization_bounce",
}
RISKY_INCIDENT_HOLD_INTENT_PREFIXES = ("explore", "cell_optimization")
RISKY_INCIDENT_HOLD_LEVERS = {LeverType.BAND_SELECTION, LeverType.PBAND, LeverType.CELL_LOCK}
IMMEDIATE_RECOVERY_NEXT_STEPS = {
    "recover_radio",
    "emergency_recovery",
    "emergency_restart",
    "emergency_fast_restart",
    "emergency_radio_down_recover",
}


def incident_class_name(assessment: Dict[str, Any] | None) -> str:
    return str((assessment or {}).get("incident_class", "healthy") or "healthy")


def incident_next_step(assessment: Dict[str, Any] | None) -> str:
    return str((assessment or {}).get("recommended_next_step", "none") or "none")


def incident_suppresses_exploration(assessment: Dict[str, Any] | None) -> bool:
    return bool((assessment or {}).get("suppress_exploration", False))


def incident_requires_immediate_recovery(assessment: Dict[str, Any] | None) -> bool:
    incident = dict(assessment or {})
    if str(incident.get("severity", "") or "").lower() != "critical":
        return False
    return incident_next_step(incident) in IMMEDIATE_RECOVERY_NEXT_STEPS


def should_hold_action_for_incident(
    *,
    assessment: Dict[str, Any] | None,
    action: LeverAction | None,
) -> bool:
    if action is None:
        return False
    if (
        action.lever == LeverType.TRAFFIC_PRESENCE
        and incident_requires_immediate_recovery(assessment)
    ):
        return True
    if not incident_suppresses_exploration(assessment):
        return False
    if action.lever not in RISKY_INCIDENT_HOLD_LEVERS:
        return False
    params = getattr(action, "params", {}) or {}
    intent = str(params.get("_smart_loop_availability_intent") or getattr(action, "intent", "") or "")
    if intent in RISKY_INCIDENT_HOLD_INTENTS:
        return True
    return any(intent.startswith(prefix) for prefix in RISKY_INCIDENT_HOLD_INTENT_PREFIXES)


def incident_hold_reason(
    *,
    assessment: Dict[str, Any] | None,
    action: LeverAction | None,
) -> str:
    incident_class = incident_class_name(assessment)
    next_step = incident_next_step(assessment)
    self_induced = bool((assessment or {}).get("self_induced", False))
    lever = getattr(getattr(action, "lever", None), "name", "")
    intent = str(getattr(action, "intent", "") or "")
    return (
        f"incident hold class={incident_class} next={next_step} self_induced={self_induced}"
        f" suppresses risky exploration lever={lever} intent={intent}"
    )
