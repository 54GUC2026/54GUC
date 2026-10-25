"""Typed NGUI login lifecycle shared by its owner and renderer."""

from __future__ import annotations

from typing import Mapping


LOGIN_LIFECYCLE_SCHEMA = "NGUILoginLifecycle.v1"
LOGIN_LIFECYCLE_OWNER = "54guc.ngui.login_lifecycle"
LOGIN_LIFECYCLE_STATES = frozenset(
    {
        "draft",
        "connecting",
        "starting",
        "identity_required",
        "waiting_for_data",
        "confirming_modem",
        "reconnecting",
        "connected",
        "failed",
        "stopping",
        "stopped",
    }
)


def login_lifecycle(
    state: str,
    *,
    message: str,
    connected: bool = False,
) -> dict[str, object]:
    canonical_state = str(state)
    if canonical_state not in LOGIN_LIFECYCLE_STATES:
        raise ValueError(f"undeclared NGUI login lifecycle state: {canonical_state}")
    return {
        "schema": LOGIN_LIFECYCLE_SCHEMA,
        "owner": LOGIN_LIFECYCLE_OWNER,
        "state": canonical_state,
        "connected": bool(connected),
        "message": str(message),
    }


def login_lifecycle_state(value: object) -> str:
    row: Mapping[str, object] = value if isinstance(value, Mapping) else {}
    if (
        row.get("schema") != LOGIN_LIFECYCLE_SCHEMA
        or row.get("owner") != LOGIN_LIFECYCLE_OWNER
    ):
        return ""
    state = str(row.get("state") or "")
    return state if state in LOGIN_LIFECYCLE_STATES else ""


__all__ = (
    "LOGIN_LIFECYCLE_OWNER",
    "LOGIN_LIFECYCLE_SCHEMA",
    "LOGIN_LIFECYCLE_STATES",
    "login_lifecycle",
    "login_lifecycle_state",
)
