"""
Family-packaged runtime state helpers.

These helpers keep vendor/model-shaped state serialization logic out of the
shared decision pipeline while preserving backward compatibility with the
legacy attribute layout used by the existing engine and policy modules.
"""

from __future__ import annotations

from typing import Any, Dict


def get_hilink_radio_drop_state(engine: Any) -> dict:
    getter = getattr(engine, "_get_hilink_radio_drop_state", None)
    if callable(getter):
        state = getter() or {}
        if isinstance(state, dict):
            return state
    state = getattr(engine, "_hilink_radio_drop_state", None)
    if isinstance(state, dict):
        return state
    state = getattr(engine, "_a1_big_dip", None)
    if isinstance(state, dict):
        return state
    state = {"day": "", "active": False, "step": 0, "last_action_at": 0.0, "hold_until": 0.0}
    try:
        engine._hilink_radio_drop_state = state
        engine._a1_big_dip = state
    except Exception:
        pass
    return state


def export_hilink_radio_drop_state(engine: Any) -> Dict[str, float | int | bool | str]:
    state = get_hilink_radio_drop_state(engine)
    return {
        "day": str(state.get("day", "") or ""),
        "active": bool(state.get("active", False)),
        "step": int(state.get("step", 0) or 0),
        "last_action_at": float(state.get("last_action_at", 0.0) or 0.0),
        "hold_until": float(state.get("hold_until", 0.0) or 0.0),
    }


def restore_hilink_radio_drop_state(engine: Any, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    state = get_hilink_radio_drop_state(engine)
    state["day"] = str(payload.get("day", "") or "")
    state["active"] = bool(payload.get("active", False))
    state["step"] = int(payload.get("step", 0) or 0)
    state["last_action_at"] = float(payload.get("last_action_at", 0.0) or 0.0)
    state["hold_until"] = float(payload.get("hold_until", 0.0) or 0.0)
    try:
        engine._hilink_radio_drop_state = state
        engine._a1_big_dip = state
    except Exception:
        pass


def export_routeros_rotate_state(engine: Any) -> Dict[str, Any]:
    getter = getattr(engine, "_get_routeros_rotate_success_quiet_until", None)
    quiet_until = float(getter() if callable(getter) else getattr(engine, "_routeros_rotate_success_quiet_until", 0.0) or 0.0)
    return {
        "success_quiet_until": quiet_until,
        "last_attempt_at": float(getattr(engine, "_mikrotik_rotate_last_attempt_at", 0.0) or 0.0),
        "attempt_times": [float(ts) for ts in (getattr(engine, "_mikrotik_rotate_attempt_times", []) or [])],
    }


def restore_routeros_rotate_state(engine: Any, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    quiet_until = float(payload.get("success_quiet_until", 0.0) or 0.0)
    setter = getattr(engine, "_set_routeros_rotate_success_quiet_until", None)
    if callable(setter):
        setter(quiet_until)
    else:
        try:
            engine._routeros_rotate_success_quiet_until = quiet_until
            engine._mikrotik_rotate_success_quiet_until = quiet_until
        except Exception:
            pass
    try:
        engine._mikrotik_rotate_last_attempt_at = float(payload.get("last_attempt_at", 0.0) or 0.0)
        engine._mikrotik_rotate_attempt_times = [
            float(ts) for ts in (payload.get("attempt_times") or [])
        ]
    except Exception:
        pass
