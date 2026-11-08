"""
MikroTik objective retry/cooldown helpers.

These helpers keep MikroTik-specific explore and cell-lock retry lifecycles out
of the generic decision pipeline mixin.
"""

from __future__ import annotations

from typing import Any

from arch4.interfaces import LeverAction
from arch4.operator_profiles import render_action_alias_template


def _soft_or_cfg_float(engine: Any, soft_key: str, cfg_name: str, default: float) -> float:
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    raw = soft.get(soft_key, getattr(engine.cfg, cfg_name, default))
    if raw is None:
        return float(default)
    return float(raw)


def _soft_or_cfg_int(engine: Any, soft_key: str, cfg_name: str, default: int) -> int:
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    raw = soft.get(soft_key, getattr(engine.cfg, cfg_name, default))
    if raw is None:
        return int(default)
    return int(raw)




def _render_routeros_alias(action_id: str, *, objective_id: str) -> dict[str, object]:
    return render_action_alias_template(action_id, objective_id=str(objective_id or ""))


def _with_alias_payload(payload: dict[str, object], alias_data: dict[str, object]) -> dict[str, object]:
    out = dict(payload)
    for key in (
        "policy_family",
        "reason_code",
        "intent_alias",
        "legacy_intent",
        "objective_alias_id",
        "legacy_objective_id",
    ):
        val = alias_data.get(key)
        if val not in (None, ""):
            out[key] = val
    return out

MIKROTIK_CELL_LOCK_INTENTS = {
    "explore_cell_lock",
    "anchor_lock",
    "degraded_cell_lock",
    "congested_cell_lock",
}


def is_mikrotik_cell_lock_objective(action: LeverAction) -> bool:
    return bool(action.intent in MIKROTIK_CELL_LOCK_INTENTS)


def check_mikrotik_explore_objective_block(
    engine: Any,
    *,
    action: LeverAction,
    now: float,
) -> tuple[bool, str]:
    objective_id = str(action.objective_id or "")
    if not objective_id:
        return False, ""
    until = engine.cooldowns.get_keyed_until("mikrotik_explore_retry", objective_id)
    if float(now) < until:
        alias_data = _render_routeros_alias("routeros_explore_retry", objective_id=objective_id)
        reason = "mikrotik explore retry hold"
        engine._record_objective_lifecycle_event(
            stage="objective_guard",
            status="blocked",
            action=action,
            reason=reason,
            until_ts=until,
            metadata=_with_alias_payload({}, alias_data),
        )
        return True, reason
    if until > 0.0:
        engine.cooldowns.clear_keyed("mikrotik_explore_retry", objective_id)
    return False, ""


def note_mikrotik_explore_result(
    engine: Any,
    *,
    action: LeverAction,
    attached: bool,
    now: float,
    target_sig: str,
) -> None:
    objective_id = action.objective_id or engine._objective_id_for_bands(
        "explore", action.params.get("bands", [])
    )
    if not objective_id:
        return

    retry_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "mikrotik_explore_retry_after_s",
            "mikrotik_explore_retry_after_s",
            300.0,
        ),
    )
    fail_limit = max(
        1,
        _soft_or_cfg_int(
            engine,
            "mikrotik_explore_fail_limit",
            "mikrotik_explore_fail_limit",
            2,
        ),
    )
    fail_cooldown_s = max(
        retry_s,
        _soft_or_cfg_float(
            engine,
            "mikrotik_explore_fail_cooldown_s",
            "mikrotik_explore_fail_cooldown_s",
            1200.0,
        ),
    )

    if attached:
        engine._mikrotik_explore_fail_count[objective_id] = 0
        engine.cooldowns.set_keyed("mikrotik_explore_retry", objective_id, hold_s=retry_s, now=now)
        alias_data = _render_routeros_alias("routeros_explore_retry", objective_id=objective_id)
        engine._record_objective_lifecycle_event(
            stage="mikrotik_explore",
            status="retry_scheduled",
            action=action,
            objective_id=objective_id,
            reason="attached_wait_before_next",
            until_ts=float(now) + retry_s,
            metadata=_with_alias_payload({"retry_after_s": int(retry_s), "fail_count": 0}, alias_data),
        )
        event_name = str((alias_data.get("event_aliases") or ["mikrotik_explore_retry_scheduled"])[0])
        engine._emit_runtime_event(
            event_name,
            **_with_alias_payload({
                "objective_id": objective_id,
                "target": target_sig,
                "retry_after_s": int(retry_s),
                "fail_count": 0,
                "outcome": "attached",
            }, alias_data),
        )
        return

    fails = int(engine._mikrotik_explore_fail_count.get(objective_id, 0)) + 1
    if fails >= fail_limit:
        engine._mikrotik_explore_fail_count[objective_id] = 0
        engine.cooldowns.set_keyed("mikrotik_explore_retry", objective_id, hold_s=fail_cooldown_s, now=now)
        alias_data = _render_routeros_alias("routeros_explore_retry", objective_id=objective_id)
        engine._record_objective_lifecycle_event(
            stage="mikrotik_explore",
            status="cooldown_scheduled",
            action=action,
            objective_id=objective_id,
            reason="fail_limit_reached",
            until_ts=float(now) + fail_cooldown_s,
            metadata=_with_alias_payload({
                "fail_limit": int(fail_limit),
                "cooldown_s": int(fail_cooldown_s),
            }, alias_data),
        )
        event_name = str((alias_data.get("hold_event_aliases") or ["mikrotik_explore_cooldown_started"])[0])
        engine._emit_runtime_event(
            event_name,
            **_with_alias_payload({
                "objective_id": objective_id,
                "target": target_sig,
                "fail_limit": int(fail_limit),
                "cooldown_s": int(fail_cooldown_s),
            }, alias_data),
        )
        return

    engine._mikrotik_explore_fail_count[objective_id] = fails
    engine.cooldowns.set_keyed("mikrotik_explore_retry", objective_id, hold_s=retry_s, now=now)
    alias_data = _render_routeros_alias("routeros_explore_retry", objective_id=objective_id)
    engine._record_objective_lifecycle_event(
        stage="mikrotik_explore",
        status="retry_scheduled",
        action=action,
        objective_id=objective_id,
        reason="failed",
        until_ts=float(now) + retry_s,
        metadata=_with_alias_payload({"retry_after_s": int(retry_s), "fail_count": int(fails)}, alias_data),
    )
    event_name = str((alias_data.get("event_aliases") or ["mikrotik_explore_retry_scheduled"])[0])
    engine._emit_runtime_event(
        event_name,
        **_with_alias_payload({
            "objective_id": objective_id,
            "target": target_sig,
            "retry_after_s": int(retry_s),
            "fail_count": int(fails),
            "outcome": "failed",
        }, alias_data),
    )


def note_mikrotik_cell_lock_retry(
    engine: Any,
    *,
    action: LeverAction,
    objective_id: str,
    requested_pci: int,
    requested_earfcn: int,
    now: float,
) -> None:
    retry_s = max(
        0.0,
        _soft_or_cfg_float(
            engine,
            "cell_lock_retry_after_s",
            "cell_lock_retry_after_s",
            600.0,
        ),
    )
    fails = int(engine._mikrotik_cell_lock_fail_count.get(objective_id, 0)) + 1
    fail_limit = max(
        1,
        _soft_or_cfg_int(
            engine,
            "mikrotik_cell_lock_fail_limit",
            "mikrotik_cell_lock_fail_limit",
            2,
        ),
    )
    fail_cooldown_s = max(
        retry_s,
        _soft_or_cfg_float(
            engine,
            "mikrotik_cell_lock_fail_cooldown_s",
            "mikrotik_cell_lock_fail_cooldown_s",
            1800.0,
        ),
    )

    if fails >= fail_limit:
        engine._mikrotik_cell_lock_fail_count[objective_id] = 0
        engine.cooldowns.set_keyed("cell_lock_retry", objective_id, hold_s=fail_cooldown_s, now=now)
        alias_data = _render_routeros_alias("routeros_cell_lock_retry", objective_id=objective_id)
        engine._record_objective_lifecycle_event(
            stage="cell_lock",
            status="cooldown_scheduled",
            action=action,
            objective_id=objective_id,
            reason="fail_limit_reached",
            until_ts=float(now) + fail_cooldown_s,
            metadata=_with_alias_payload({
                "pci": int(requested_pci),
                "earfcn": int(requested_earfcn),
                "fail_limit": int(fail_limit),
                "cooldown_s": int(fail_cooldown_s),
            }, alias_data),
        )
        event_name = str((alias_data.get("hold_event_aliases") or ["mikrotik_cell_lock_cooldown_started"])[0])
        engine._emit_runtime_event(
            event_name,
            **_with_alias_payload({
                "objective_id": objective_id,
                "pci": int(requested_pci),
                "earfcn": int(requested_earfcn),
                "fail_limit": int(fail_limit),
                "cooldown_s": int(fail_cooldown_s),
            }, alias_data),
        )
        return

    engine._mikrotik_cell_lock_fail_count[objective_id] = fails
    engine.cooldowns.set_keyed("cell_lock_retry", objective_id, hold_s=retry_s, now=now)
    alias_data = _render_routeros_alias("routeros_cell_lock_retry", objective_id=objective_id)
    engine._record_objective_lifecycle_event(
        stage="cell_lock",
        status="retry_scheduled",
        action=action,
        objective_id=objective_id,
        reason="lock_not_verified",
        until_ts=float(now) + retry_s,
        metadata=_with_alias_payload({
            "pci": int(requested_pci),
            "earfcn": int(requested_earfcn),
            "retry_after_s": int(retry_s),
            "fail_count": int(fails),
        }, alias_data),
    )
    event_name = str((alias_data.get("event_aliases") or ["cell_lock_objective_retry_scheduled"])[0])
    engine._emit_runtime_event(
        event_name,
        **_with_alias_payload({
            "objective_id": objective_id,
            "pci": int(requested_pci),
            "earfcn": int(requested_earfcn),
            "retry_after_s": int(retry_s),
            "fail_count": int(fails),
        }, alias_data),
    )
