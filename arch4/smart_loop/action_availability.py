"""Self-learning action availability associate.

This module owns pre-decision mechanical availability for the clean smart loop.
It is pure: no widgets, no modem I/O, no decision-pipeline calls.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from arch4.policy.health_action_policy import health_hold_reason, should_hold_action_for_health
from arch4.policy.incident_action_policy import incident_hold_reason, should_hold_action_for_incident
from arch4.runtime_decision_projection import (
    BOUNCER_SAFETY_SCHEMA,
    BOUNCER_SOURCE_OWNER,
    OWNER as DECISION_PROJECTION_OWNER,
    SCHEMA as DECISION_PROJECTION_SCHEMA,
)


SCHEMA = "smart_loop.action_availability.v1"
OWNER = "arch4.smart_loop.action_availability"
SMART_LOOP_ROI_OBSERVATION_COOLDOWN = "smart_loop_radio_action_observation"


def build_action_availability(
    *,
    decision: Mapping[str, Any] | None = None,
    mutation: Mapping[str, Any] | None = None,
    health: Mapping[str, Any] | None = None,
    runtime_settle: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the self-learning-owned availability verdict for smart actions."""

    now_ts = _float(now, time.time())
    decision_row = _mapping(decision)
    if decision_row and (
        str(decision_row.get("schema") or "") != DECISION_PROJECTION_SCHEMA
        or str(decision_row.get("owner") or "") != DECISION_PROJECTION_OWNER
    ):
        raise ValueError("runtime_decision_projection_required")
    decision_availability = _mapping(decision_row.get("action_availability"))
    bouncer_safety = _mapping(
        decision_availability.get("bouncer_safety")
    )
    if bouncer_safety and (
        str(bouncer_safety.get("schema") or "") != BOUNCER_SAFETY_SCHEMA
        or str(bouncer_safety.get("owner") or "")
        != DECISION_PROJECTION_OWNER
        or str(bouncer_safety.get("source_owner") or "")
        != BOUNCER_SOURCE_OWNER
    ):
        raise ValueError("runtime_bouncer_safety_projection_required")
    mutation_row = _mapping(mutation)
    health_row = _mapping(health)
    settle_row = _mapping(runtime_settle)
    normal_safety = _mapping(
        decision_availability.get("normal_policy_safety")
    )

    reasons: list[str] = []
    cooldown_s = 0.0
    cooldown_until_ts = 0.0
    safety_block = ""
    settle_projection = dict(settle_row)

    if _bool(bouncer_safety.get("hard_stop_active"), False):
        bouncer_reason = str(
            bouncer_safety.get("trip_reason")
            or "durable_bouncer_hard_stop"
        )
        reasons.append("bouncer_hard_stop:" + bouncer_reason)
        safety_block = "bouncer_hard_stop:" + bouncer_reason

    if _bool(settle_row.get("context_unready_active"), False):
        context_reason = str(settle_row.get("context_unready_reason") or "live_context_unavailable")
        reasons.append(context_reason)
        safety_block = f"runtime_settle:{context_reason}"

    for key in ("startup_grace", "connect_settle", "radio_change_settle"):
        active_key = f"{key}_active"
        remaining_key = f"{key}_remaining_s"
        until_key = f"{key}_until_ts"
        remaining_s = _remaining_from_until(
            settle_row,
            until_key=until_key,
            remaining_key=remaining_key,
            now=now_ts,
        )
        active = _bool(settle_row.get(active_key), False) or remaining_s > 0.0
        settle_projection[active_key] = bool(active and remaining_s > 0.0)
        settle_projection[remaining_key] = round(float(max(0.0, remaining_s)), 3)
        settle_projection[until_key] = _until_from_remaining(now_ts, remaining_s, settle_row.get(until_key))
        if active and remaining_s > 0.0:
            reasons.append(key)
            cooldown_s = max(cooldown_s, remaining_s)
            cooldown_until_ts = max(
                cooldown_until_ts,
                _until_from_remaining(now_ts, remaining_s, settle_row.get(until_key)),
            )
            safety_block = f"runtime_settle:{key}"

    stability_remaining = _float(
        decision_availability.get("radio_stability_hold_remaining_s"),
        0.0,
    )
    stability_active = _bool(
        decision_availability.get("radio_stability_hold_active"),
        False,
    )
    if stability_active and stability_remaining > 0.0:
        reasons.append("radio_stability_hold")
        cooldown_s = max(cooldown_s, stability_remaining)
        cooldown_until_ts = max(
            cooldown_until_ts,
            _until_from_remaining(now_ts, stability_remaining, 0.0),
        )

    roi_remaining = _float(
        decision_availability.get("smart_loop_roi_observation_remaining_s"),
        0.0,
    )
    roi_active = _bool(
        decision_availability.get("smart_loop_roi_observation_active"),
        False,
    )
    if roi_active and roi_remaining > 0.0:
        reasons.append("smart_loop_roi_observation")
        cooldown_s = max(cooldown_s, roi_remaining)
        cooldown_until_ts = max(
            cooldown_until_ts,
            _until_from_remaining(now_ts, roi_remaining, 0.0),
        )

    mutation_active = _bool(mutation_row.get("active"), False)
    mutation_learning_allowed = _bool(mutation_row.get("learning_allowed"), not mutation_active)
    if mutation_active and not mutation_learning_allowed:
        mutation_reason = str(mutation_row.get("reason") or mutation_row.get("state") or "radio_mutation_window")
        reasons.append(mutation_reason)
        safety_block = f"radio_mutation_window:{mutation_reason}"

    health_hold = _bool(
        health_row.get("action_hold")
        if "action_hold" in health_row
        else health_row.get("hold_action"),
        False,
    )
    if health_hold:
        health_reason = str(health_row.get("reason") or health_row.get("stage") or "health_hold")
        reasons.append(health_reason)
        safety_block = f"health_hold:{health_reason}"

    if _bool(normal_safety.get("throughput_hold"), False):
        reasons.append("user_throughput_hold")
        safety_block = "normal_policy:user_throughput_hold"

    incident_class = str(normal_safety.get("incident_class") or "")
    if incident_class == "post_action_instability":
        reasons.append("post_action_instability")
        safety_block = "normal_policy:post_action_instability"

    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "computed_at_ts": round(float(now_ts), 3),
        "decision_allowed": not reasons,
        "cooldown_s": round(float(cooldown_s), 3),
        "cooldown_until_ts": round(float(cooldown_until_ts), 3) if cooldown_until_ts > 0.0 else 0.0,
        "safety_block": safety_block,
        "reasons": reasons,
        "user_throughput_hold": bool(
            _bool(normal_safety.get("throughput_hold"), False)
        ),
        "incident_class": incident_class,
        "radio_stability_hold_active": bool(
            stability_active and stability_remaining > 0.0
        ),
        "radio_stability_hold_remaining_s": round(
            float(max(0.0, stability_remaining)), 3
        ),
        "smart_loop_roi_observation_active": bool(roi_active and roi_remaining > 0.0),
        "smart_loop_roi_observation_remaining_s": round(float(max(0.0, roi_remaining)), 3),
        "startup_grace_active": bool(_bool(settle_projection.get("startup_grace_active"), False)),
        "startup_grace_remaining_s": round(
            float(max(0.0, _float(settle_projection.get("startup_grace_remaining_s"), 0.0))),
            3,
        ),
        "startup_grace_until_ts": _float(settle_projection.get("startup_grace_until_ts"), 0.0),
        "connect_settle_active": bool(_bool(settle_projection.get("connect_settle_active"), False)),
        "connect_settle_remaining_s": round(
            float(max(0.0, _float(settle_projection.get("connect_settle_remaining_s"), 0.0))),
            3,
        ),
        "connect_settle_until_ts": _float(settle_projection.get("connect_settle_until_ts"), 0.0),
        "radio_change_settle_active": bool(_bool(settle_projection.get("radio_change_settle_active"), False)),
        "radio_change_settle_remaining_s": round(
            float(max(0.0, _float(settle_projection.get("radio_change_settle_remaining_s"), 0.0))),
            3,
        ),
        "radio_change_settle_until_ts": _float(settle_projection.get("radio_change_settle_until_ts"), 0.0),
        "context_unready_active": bool(_bool(settle_row.get("context_unready_active"), False)),
        "context_unready_reason": str(settle_row.get("context_unready_reason") or ""),
        "runtime_settle": settle_projection,
        "bouncer_safety": dict(bouncer_safety),
    }


def _remaining_from_until(
    row: Mapping[str, Any],
    *,
    until_key: str,
    remaining_key: str,
    now: float,
) -> float:
    until_ts = _float(row.get(until_key), 0.0)
    if until_ts > 0.0:
        return max(0.0, until_ts - float(now))
    return max(0.0, _float(row.get(remaining_key), 0.0))


def _until_from_remaining(now: float, remaining_s: float, current_until: Any) -> float:
    remaining = max(0.0, _float(remaining_s, 0.0))
    if remaining <= 0.0:
        return 0.0
    until_ts = _float(current_until, 0.0)
    if until_ts > 0.0:
        return round(float(until_ts), 3)
    return round(float(now) + remaining, 3)


def evaluate_action_availability(
    *,
    action: Any,
    now: float,
    state_name: str = "",
    is_fault_state: bool = False,
    is_emergency_state: bool = False,
    constraint: Mapping[str, Any] | None = None,
    radio_stability_hold_until: float = 0.0,
    cooldowns: Any = None,
    objective_feedback_keys: Iterable[str] = (),
    objective_feedback_reason: Mapping[str, str] | None = None,
    scalar_cooldowns: Iterable[Mapping[str, Any]] = (),
    routeros_explore_block: Mapping[str, Any] | None = None,
    user_traffic_hold: bool = False,
) -> dict[str, Any]:
    """Return the single autonomous-action availability verdict.

    Cooldowns, objective feedback, health, and incident state are facts. This
    owner is the only code that turns those facts into an allow/deny verdict.
    """

    action_brief = _action_brief(action)
    if action is None:
        return _verdict(
            allowed=False,
            reason_code="no_action",
            reason="no action supplied",
            now=now,
            state_name=state_name,
            action=action_brief,
            learnable=False,
        )

    reasons = _action_tokens(action)
    constraint_row = _mapping(constraint)
    objective_reasons = _mapping(objective_feedback_reason)
    ts = float(now)

    if (
        _is_smart_loop_full_radio_action(action)
        and not bool(is_fault_state or is_emergency_state)
        and bool(user_traffic_hold)
    ):
        return _verdict(
            allowed=False,
            reason_code="user_traffic_active",
            reason="active user throughput hold protects SmartLoop full-radio action",
            now=ts,
            state_name=state_name,
            action=action_brief,
            metadata={"traffic_policy": "adaptive_throughput_hold"},
            learnable=False,
        )

    if (
        _is_radio_mutation_action(action)
        and not bool(
            is_fault_state
            or is_emergency_state
            or str(getattr(action, "intent", "") or "").strip()
            == "emergency_radio_down_recover"
        )
        and ts < float(radio_stability_hold_until or 0.0)
    ):
        remaining_s = max(
            0.0, float(radio_stability_hold_until or 0.0) - ts
        )
        return _verdict(
            allowed=False,
            reason_code="radio_stability_hold",
            reason=(
                "radio stability hold active "
                f"({int(round(remaining_s))}s left)"
            ),
            now=ts,
            state_name=state_name,
            action=action_brief,
            cooldown_s=remaining_s,
        )

    if _is_radio_mutation_action(action):
        auto_lockout = _bool(constraint_row.get("auto_action_lockout_active"), False)
        planned_quiet = _bool(constraint_row.get("planned_radio_quiet_active"), False)
        if auto_lockout or planned_quiet:
            remaining_s = _float(
                constraint_row.get("auto_action_lockout_remaining_s")
                if auto_lockout
                else constraint_row.get("planned_radio_quiet_remaining_s"),
                0.0,
            )
            return _verdict(
                allowed=False,
                reason_code="action_cooldown",
                reason="radio action cooldown active",
                now=ts,
                state_name=state_name,
                action=action_brief,
                cooldown_s=remaining_s,
                metadata={"constraint": "auto_action_lockout" if auto_lockout else "planned_radio_quiet"},
            )

    if not bool(is_fault_state or is_emergency_state):
        post_fault_remaining_s = _float(constraint_row.get("post_fault_cooldown_remaining_s"), 0.0)
        if _is_radio_mutation_action(action) and post_fault_remaining_s > 0.0 and "restore_ca" not in reasons:
            return _verdict(
                allowed=False,
                reason_code="post_fault_cooldown",
                reason="post-fault radio action cooldown active",
                now=ts,
                state_name=state_name,
                action=action_brief,
                cooldown_s=post_fault_remaining_s,
                metadata={"post_fault_class": str(constraint_row.get("post_fault_class") or "")},
            )
        rf_cliff_remaining_s = _float(constraint_row.get("rf_cliff_cooldown_remaining_s"), 0.0)
        if _is_radio_mutation_action(action) and rf_cliff_remaining_s > 0.0 and "restore_ca" not in reasons:
            return _verdict(
                allowed=False,
                reason_code="rf_cliff_cooldown",
                reason="RF-cliff radio action cooldown active",
                now=ts,
                state_name=state_name,
                action=action_brief,
                cooldown_s=rf_cliff_remaining_s,
                metadata={"rf_cliff_reason": str(constraint_row.get("rf_cliff_reason") or "")},
            )

        if not (
            _is_optimization_selected_action(action) and "restore_ca" in reasons
        ):
            health_status = _mapping(constraint_row.get("health_status"))
            if should_hold_action_for_health(status=health_status, action=action):
                return _verdict(
                    allowed=False,
                    reason_code="health_hold",
                    reason=health_hold_reason(status=health_status, action=action),
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                )

            incident_assessment = _mapping(constraint_row.get("incident_assessment"))
            if should_hold_action_for_incident(assessment=incident_assessment, action=action):
                return _verdict(
                    allowed=False,
                    reason_code="incident_hold",
                    reason=incident_hold_reason(assessment=incident_assessment, action=action),
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                )

    if cooldowns is not None:
        for cooldown in list(scalar_cooldowns or []):
            row = _mapping(cooldown)
            name = str(row.get("name", "") or "")
            if not name:
                continue
            until = _scalar_cooldown_until(cooldowns, name)
            if ts < until:
                return _verdict(
                    allowed=False,
                    reason_code=str(row.get("reason_code") or name),
                    reason=str(row.get("reason") or f"{name} cooldown active"),
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                    cooldown_s=max(0.0, until - ts),
                    metadata={"cooldown": name, **_mapping(row.get("metadata"))},
                )

        for key in [str(k or "") for k in objective_feedback_keys if str(k or "")]:
            until = _cooldown_until(cooldowns, "objective_feedback", key)
            if ts < until:
                return _verdict(
                    allowed=False,
                    reason_code="objective_feedback",
                    reason=str(objective_reasons.get(key) or "objective feedback hold active"),
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                    cooldown_s=max(0.0, until - ts),
                    metadata={"feedback_key": key},
                )

        objective_id = str(getattr(action, "objective_id", "") or "")
        intent = str(getattr(action, "intent", "") or "")
        if objective_id and intent in {"explore_cell_lock", "anchor_lock", "degraded_cell_lock", "congested_cell_lock", "cell_lock_renew"}:
            until = _cooldown_until(cooldowns, "cell_lock_retry", objective_id)
            if ts < until:
                return _verdict(
                    allowed=False,
                    reason_code="cell_lock_retry",
                    reason="cell-lock retry hold",
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                    cooldown_s=max(0.0, until - ts),
                    metadata={"objective_id": objective_id},
                )
        if objective_id and intent in {"emergency_fast_restart", "emergency_restart"}:
            until = _cooldown_until(cooldowns, "restart_retry", objective_id)
            if ts < until:
                return _verdict(
                    allowed=False,
                    reason_code="restart_retry",
                    reason="restart retry hold",
                    now=ts,
                    state_name=state_name,
                    action=action_brief,
                    cooldown_s=max(0.0, until - ts),
                    metadata={"objective_id": objective_id},
                )
        if "restore_ca" in reasons:
            band_sig = _band_signature(action)
            if objective_id:
                until = _cooldown_until(cooldowns, "restore_ca_retry", objective_id)
                if ts < until:
                    return _verdict(
                        allowed=False,
                        reason_code="restore_ca_retry",
                        reason="restore_ca retry cooldown active",
                        now=ts,
                        state_name=state_name,
                        action=action_brief,
                        cooldown_s=max(0.0, until - ts),
                        metadata={"objective_id": objective_id, "band_sig": band_sig},
                    )
            if band_sig:
                until = _cooldown_until(cooldowns, "restore_ca_repeat", band_sig)
                if ts < until:
                    return _verdict(
                        allowed=False,
                        reason_code="restore_ca_repeat",
                        reason="restore_ca repeat cooldown active",
                        now=ts,
                        state_name=state_name,
                        action=action_brief,
                        cooldown_s=max(0.0, until - ts),
                        metadata={"objective_id": objective_id, "band_sig": band_sig},
                    )
        if "explore" in reasons:
            band_sig = _band_signature(action)
            if band_sig:
                until = _cooldown_until(cooldowns, "explore_repeat", band_sig)
                if ts < until:
                    return _verdict(
                        allowed=False,
                        reason_code="explore_repeat",
                        reason="explore repeat cooldown active",
                        now=ts,
                        state_name=state_name,
                        action=action_brief,
                        cooldown_s=max(0.0, until - ts),
                        metadata={"band_sig": band_sig},
                    )

    routeros = _mapping(routeros_explore_block)
    if bool(routeros.get("blocked")):
        return _verdict(
            allowed=False,
            reason_code="routeros_explore_retry",
            reason=str(routeros.get("reason") or "RouterOS explore objective hold"),
            now=ts,
            state_name=state_name,
            action=action_brief,
            metadata={"objective_id": str(getattr(action, "objective_id", "") or "")},
        )

    return _verdict(
        allowed=True,
        reason_code="available",
        reason="action available",
        now=ts,
        state_name=state_name,
        action=action_brief,
        learnable=False,
    )


def _verdict(
    *,
    allowed: bool,
    reason_code: str,
    reason: str,
    now: float,
    state_name: str,
    action: Mapping[str, Any],
    cooldown_s: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
    learnable: bool = True,
) -> dict[str, Any]:
    row = {
        "schema": SCHEMA,
        "owner": OWNER,
        "allowed": bool(allowed),
        "reason_code": str(reason_code or ""),
        "reason": str(reason or ""),
        "blocking_owner": "" if allowed else OWNER,
        "cooldown_s": round(float(max(0.0, cooldown_s)), 3),
        "learnable": bool(learnable),
        "now": float(now),
        "state": str(state_name or ""),
        "action": dict(action or {}),
        "metadata": dict(metadata or {}),
    }
    return row


def _action_brief(action: Any) -> dict[str, Any]:
    if action is None:
        return {}
    return {
        "lever": str(getattr(getattr(action, "lever", None), "name", getattr(action, "lever", "")) or ""),
        "intent": str(getattr(action, "intent", "") or ""),
        "objective_id": str(getattr(action, "objective_id", "") or ""),
        "params": _mapping(getattr(action, "params", None)),
    }


def _action_tokens(action: Any) -> set[str]:
    brief = _action_brief(action)
    params = _mapping(brief.get("params"))
    tokens = {
        str(brief.get("lever", "") or "").lower(),
        str(brief.get("intent", "") or "").lower(),
        str(brief.get("objective_id", "") or "").lower(),
    }
    smart_loop_intent = str(params.get("_smart_loop_availability_intent") or "").strip().lower()
    if smart_loop_intent:
        tokens.add(smart_loop_intent)
    elif _is_optimization_selected_action(action):
        tokens.add("explore")
    return {token for token in tokens if token}


def _is_radio_mutation_action(action: Any) -> bool:
    lever = str(getattr(getattr(action, "lever", None), "name", getattr(action, "lever", "")) or "")
    return lever in {"BAND_SELECTION", "BAND_LOCK", "PBAND", "CELL_LOCK"}


def _is_optimization_selected_action(action: Any) -> bool:
    params = _mapping(getattr(action, "params", None))
    return bool(
        _bool(params.get("_selected_action_command"), False)
        and str(params.get("_selected_action_owner_class") or "") == "optimization"
    )


def _is_smart_loop_full_radio_action(action: Any) -> bool:
    lever = str(getattr(getattr(action, "lever", None), "name", getattr(action, "lever", "")) or "")
    return lever == "PBAND" and _is_optimization_selected_action(action)


def _band_signature(action: Any) -> str:
    params = _mapping(getattr(action, "params", None))
    candidates = params.get("bands")
    if not candidates and params.get("ca_pool"):
        candidates = params.get("ca_pool")
    if not candidates and params.get("dl_bands"):
        candidates = params.get("dl_bands")
    bands: list[int] = []
    seen: set[int] = set()
    for raw in list(candidates or []):
        try:
            band = int(raw)
        except (TypeError, ValueError):
            continue
        if band <= 0 or band in seen:
            continue
        seen.add(band)
        bands.append(band)
    return "+".join(str(band) for band in sorted(bands))


def _cooldown_until(cooldowns: Any, namespace: str, key: str) -> float:
    try:
        return float(cooldowns.get_keyed_until(namespace, key) or 0.0)
    except Exception:
        return 0.0


def _scalar_cooldown_until(cooldowns: Any, namespace: str) -> float:
    try:
        return float(cooldowns.get_until(namespace) or 0.0)
    except Exception:
        return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "OWNER",
    "SCHEMA",
    "SMART_LOOP_ROI_OBSERVATION_COOLDOWN",
    "build_action_availability",
    "evaluate_action_availability",
]
