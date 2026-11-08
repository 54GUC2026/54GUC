from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Mapping, Optional

def drain_runtime_event_records(
    logger: Any,
    events: Iterable[Mapping[str, Any]] | None,
    *,
    default_kind: str,
    defaults: Mapping[str, Any] | None = None,
) -> None:
    if not logger or not events:
        return
    base_defaults = dict(defaults or {})
    for raw in events:
        event = dict(raw or {})
        kind = event.pop("kind", default_kind)
        for key, value in base_defaults.items():
            event.setdefault(str(key), value)
        logger.log_event(kind, **event)


def build_auto_action_lockout_event(*, reason: str, previous_until: float, new_until: float, now: Optional[float] = None) -> Dict[str, Any] | None:
    now_ts = float(time.time() if now is None else now)
    if float(new_until or 0.0) <= (float(previous_until or 0.0) + 0.5):
        return None
    return {
        "kind": "auto_action_lockout_updated",
        "reason": str(reason or ""),
        "previous_remaining_s": max(0, int(round(float(previous_until or 0.0) - now_ts))),
        "new_remaining_s": max(0, int(round(float(new_until or 0.0) - now_ts))),
    }


def arm_mutating_action_lock_state(*, current_until: float, lever: Any, lock_seconds: float, now: Optional[float] = None) -> tuple[float, str, Dict[str, Any]]:
    from arch4.core.mutation_policy import compute_mutating_action_lock
    new_until, lever_name, remaining_s = compute_mutating_action_lock(
        current_until=float(current_until or 0.0),
        lever=lever,
        lock_seconds=float(lock_seconds or 0.0),
        now=now,
    )
    return new_until, lever_name, {
        "kind": "mutating_action_lock_updated",
        "reason": "arm",
        "lever": lever_name,
        "remaining_s": remaining_s,
    }


def clear_mutating_action_lock_runtime(*, current_until: float, current_lever: str, reason: str, now: Optional[float] = None) -> tuple[bool, float, str, Dict[str, Any] | None]:
    from arch4.core.mutation_policy import clear_mutating_action_lock_state
    changed, new_until, new_lever = clear_mutating_action_lock_state(
        current_until=float(current_until or 0.0),
        current_lever=str(current_lever or ""),
        now=now,
    )
    if not changed:
        return False, new_until, new_lever, None
    return True, new_until, new_lever, {
        "kind": "mutating_action_lock_updated",
        "reason": str(reason or ""),
        "lever": str(current_lever or ""),
        "remaining_s": 0,
    }


def build_manual_bouncer_feedback(*, lever: str, code: str = "", message: str = "") -> Dict[str, Any]:
    return {
        "lever": str(lever or ""),
        "code": str(code or ""),
        "message": str(message or "Bouncer blocked action"),
    }


def build_manual_inflight_feedback(*, previous_lever: str, until_ts: float, current_lever: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    now_ts = float(time.time() if now is None else now)
    return {
        "lever": str(current_lever or ""),
        "previous_lever": str(previous_lever or "unknown"),
        "remaining_s": max(1, int(round(float(until_ts or 0.0) - now_ts))),
    }


def build_mutating_action_verification_event(*, action: Any, decision_trace_id: str = "", mutating: bool, success: bool, note: str, status: str = "") -> Dict[str, Any]:
    return {
        "kind": "mutating_action_verification",
        "decision_trace_id": str(decision_trace_id or ""),
        "objective_id": str(getattr(action, "objective_id", "") or ""),
        "lever": str(getattr(getattr(action, "lever", None), "name", "") or ""),
        "mutating": bool(mutating),
        "success": bool(success),
        "note": str(note or ""),
        "status": str(status or ""),
        "stage": "verification_result",
        "authority": "engine_verifier",
    }


def inject_runtime_guard_payloads(
    *,
    extra: Mapping[str, Any] | None,
    runtime_recovery_payload: Mapping[str, Any] | None = None,
    planned_radio_quiet_payload: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    out = dict(extra or {})
    runtime_recovery = dict(runtime_recovery_payload or {})
    if runtime_recovery:
        out["runtime_recovery"] = runtime_recovery
    else:
        out.pop("runtime_recovery", None)
    brain = dict(out.get("brain") or {})
    planned_quiet = dict(planned_radio_quiet_payload or {})
    brain["planned_radio_quiet"] = planned_quiet
    brain["planned_radio_quiet_active"] = bool(planned_quiet.get("active", False))
    out["brain"] = brain
    return out
