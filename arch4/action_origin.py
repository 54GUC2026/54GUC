"""Sole typed-origin and origin-authority contract for every 54GUC action."""

from __future__ import annotations

from enum import Enum
from typing import Any


OWNER = "arch4.action_origin"


class ActionOriginKind(str, Enum):
    SMART_LOOP = "smart_loop"
    EXPLORE = "explore"
    FAULT_RECOVERY = "fault_recovery"
    OPERATOR_NGUI = "operator_ngui"
    OPERATOR_SMS = "operator_sms"
    SCHEDULE = "schedule"
    WATCHDOG = "watchdog"
    LEASE_EXPIRY = "lease_expiry"


class ActionAuthority(str, Enum):
    AUTONOMOUS_CANDIDATE = "autonomous_candidate_authority"
    EMERGENCY_RECOVERY = "emergency_recovery_planner"
    OPERATOR = "operator_action_authority"
    SCHEDULE_POLICY = "schedule_policy_authority"
    WATCHDOG_POLICY = "watchdog_policy_authority"
    LEASE_SAFETY = "lease_safety_authority"


_ORIGIN_AUTHORITY = {
    ActionOriginKind.SMART_LOOP: ActionAuthority.AUTONOMOUS_CANDIDATE,
    ActionOriginKind.EXPLORE: ActionAuthority.AUTONOMOUS_CANDIDATE,
    ActionOriginKind.FAULT_RECOVERY: ActionAuthority.EMERGENCY_RECOVERY,
    ActionOriginKind.OPERATOR_NGUI: ActionAuthority.OPERATOR,
    ActionOriginKind.OPERATOR_SMS: ActionAuthority.OPERATOR,
    ActionOriginKind.SCHEDULE: ActionAuthority.SCHEDULE_POLICY,
    ActionOriginKind.WATCHDOG: ActionAuthority.WATCHDOG_POLICY,
    ActionOriginKind.LEASE_EXPIRY: ActionAuthority.LEASE_SAFETY,
}
HUMAN_OPERATOR_ORIGINS = frozenset(
    {ActionOriginKind.OPERATOR_NGUI, ActionOriginKind.OPERATOR_SMS}
)
USER_AUTHORIZED_BAND_ORIGINS = frozenset(
    {
        *HUMAN_OPERATOR_ORIGINS,
        ActionOriginKind.SCHEDULE,
        ActionOriginKind.EXPLORE,
    }
)
LEGAL_ACTION_ORIGIN_NAMES = frozenset(item.value for item in ActionOriginKind)
HUMAN_OPERATOR_ORIGIN_NAMES = frozenset(
    item.value for item in HUMAN_OPERATOR_ORIGINS
)
USER_AUTHORIZED_BAND_ORIGIN_NAMES = frozenset(
    item.value for item in USER_AUTHORIZED_BAND_ORIGINS
)
AUTONOMOUS_INTELLIGENCE_ORIGIN_NAMES = frozenset(
    {
        ActionOriginKind.SMART_LOOP.value,
        ActionOriginKind.EXPLORE.value,
        ActionOriginKind.FAULT_RECOVERY.value,
    }
)
BRAIN_DECISION_ORIGINS = frozenset(
    {
        ActionOriginKind.SMART_LOOP,
        ActionOriginKind.EXPLORE,
    }
)
BRAIN_DECISION_ORIGIN_NAMES = frozenset(
    item.value for item in BRAIN_DECISION_ORIGINS
)


def action_authority_for_origin(value: ActionOriginKind) -> ActionAuthority:
    if not isinstance(value, ActionOriginKind):
        raise TypeError("typed_action_origin_required")
    return _ORIGIN_AUTHORITY[value]


def require_action_origin_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    name = str(raw or "").strip()
    if name not in LEGAL_ACTION_ORIGIN_NAMES:
        raise ValueError(f"action_origin_kind_invalid:{name or 'missing'}")
    return name


def is_human_operator_origin(value: Any) -> bool:
    return require_action_origin_name(value) in HUMAN_OPERATOR_ORIGIN_NAMES


def is_user_authorized_band_origin(value: Any) -> bool:
    return require_action_origin_name(value) in USER_AUTHORIZED_BAND_ORIGIN_NAMES


def is_brain_decision_origin(value: Any) -> bool:
    """Return whether one concrete action origin represents a Brain choice."""

    raw = getattr(value, "value", value)
    return str(raw or "").strip() in BRAIN_DECISION_ORIGIN_NAMES


__all__ = (
    "ActionAuthority",
    "ActionOriginKind",
    "AUTONOMOUS_INTELLIGENCE_ORIGIN_NAMES",
    "BRAIN_DECISION_ORIGINS",
    "BRAIN_DECISION_ORIGIN_NAMES",
    "HUMAN_OPERATOR_ORIGINS",
    "HUMAN_OPERATOR_ORIGIN_NAMES",
    "LEGAL_ACTION_ORIGIN_NAMES",
    "OWNER",
    "USER_AUTHORIZED_BAND_ORIGINS",
    "USER_AUTHORIZED_BAND_ORIGIN_NAMES",
    "action_authority_for_origin",
    "is_brain_decision_origin",
    "is_human_operator_origin",
    "is_user_authorized_band_origin",
    "require_action_origin_name",
)
