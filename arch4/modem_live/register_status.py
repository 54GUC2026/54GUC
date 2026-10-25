"""Build public status dictionaries for Modem Live registers.

This module has no ``arch4.*`` imports. Acquisition supplies the current
snapshot, contract, profile, worker state, and audit data.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def live_register_stale_cause(register_status: Dict[str, Any], *, valid: bool, modem: Optional[Any]) -> str:
    if modem is None:
        return "no_live_register_sample"
    if not valid:
        return "invalid_live_register_sample"
    last_reason = str(register_status.get("last_reason") or "").strip()
    refreshing = bool(register_status.get("refreshing"))
    worker_alive = bool(register_status.get("worker_alive"))
    if last_reason:
        if refreshing:
            return f"{last_reason}_refreshing"
        return last_reason
    if refreshing:
        return "refresh_in_progress"
    if not worker_alive:
        return "worker_not_alive"
    return "sample_age_exceeded"


def _contract_list(contract: Any, name: str) -> list[Any]:
    value = getattr(contract, name, ()) or ()
    if callable(value):
        value = value()
    return list(value or ())


def _call_bool(contract: Any, name: str) -> bool:
    value = getattr(contract, name, None)
    if callable(value):
        return bool(value())
    return bool(value)


def _access_profile_dict(access_profile: Any) -> dict[str, Any]:
    as_dict = getattr(access_profile, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict() or {})
    if isinstance(access_profile, dict):
        return dict(access_profile)
    return {}


def build_lane_contract_status(
    *,
    contract: Any,
    live_config_overrides: Dict[str, Any],
    hard_rules: Dict[str, Any],
    hard_rule_clamps: Dict[str, Any],
) -> dict[str, Any]:
    return {
        "fast_modem_read_groups": _contract_list(contract, "effective_fast_modem_read_groups"),
        "slow_modem_read_groups": _contract_list(contract, "effective_slow_modem_read_groups"),
        "fast_publish_fields": list(getattr(contract, "fast_publish_fields", ()) or ()),
        "slow_publish_fields": list(getattr(contract, "slow_publish_fields", ()) or ()),
        "fast_read_groups": list(getattr(contract, "fast_read_groups", ()) or ()),
        "slow_read_groups": list(getattr(contract, "slow_read_groups", ()) or ()),
        "static_capability_groups": list(getattr(contract, "static_capability_groups", ()) or ()),
        "control_write_groups": list(getattr(contract, "control_write_groups", ()) or ()),
        "export_groups": list(getattr(contract, "export_groups", ()) or ()),
        "backend_io_parallel_safe": bool(getattr(contract, "backend_io_parallel_safe", False)),
        "io_serial_required": (
            None
            if getattr(contract, "io_serial_required", None) is None
            else bool(getattr(contract, "io_serial_required", False))
        ),
        "effective_io_serial_required": _call_bool(contract, "effective_io_serial_required"),
        "startup_parallel_allowed": (
            None
            if getattr(contract, "startup_parallel_allowed", None) is None
            else bool(getattr(contract, "startup_parallel_allowed", False))
        ),
        "effective_startup_parallel_allowed": _call_bool(contract, "effective_startup_parallel_allowed"),
        "maintenance_holds_io_lock": bool(getattr(contract, "maintenance_holds_io_lock", False)),
        "runtime_action_requires_io_lock": (
            None
            if getattr(contract, "runtime_action_requires_io_lock", None) is None
            else bool(getattr(contract, "runtime_action_requires_io_lock", True))
        ),
        "effective_runtime_action_requires_io_lock": _call_bool(
            contract,
            "effective_runtime_action_requires_io_lock",
        ),
        "live_config_overrides": dict(live_config_overrides or {}),
        "hard_rules": dict(hard_rules or {}),
        "hard_rule_clamps": dict(hard_rule_clamps or {}),
        "static_capability_refresh_policy": str(
            getattr(
                contract,
                "static_capability_refresh_policy",
                "identity_or_operator_refresh_only",
            )
            or "identity_or_operator_refresh_only"
        ),
    }


def build_live_register_status(
    *,
    enabled: bool,
    current_status: Any,
    now: float,
    contract: Any,
    access_profile: Any,
    clock_ms: int,
    cadence_cycles: int,
    interval_s: float,
    stale_after_s: float,
    refreshing: bool,
    worker_alive: bool,
    last_attempt_ts: float,
    last_miss_ts: float,
    miss_count: int,
    last_reason: str,
    connectivity: Dict[str, Any],
    clock: Dict[str, Any],
    modem_io: Dict[str, Any],
    live_config_overrides: Dict[str, Any],
    hard_rules: Dict[str, Any],
    hard_rule_clamps: Dict[str, Any],
) -> dict[str, Any]:
    updated_at = float(
        getattr(current_status, "sampled_at", 0.0) or 0.0
    )
    age_s = max(0.0, float(now - updated_at)) if updated_at > 0.0 else None
    valid = bool(getattr(current_status, "available", False))
    modem = True if valid else None
    stale = bool((not valid) or age_s is None or age_s > stale_after_s)
    stale_context = {
        "last_reason": str(last_reason or ""),
        "refreshing": bool(refreshing),
        "worker_alive": bool(worker_alive),
    }
    stale_reason = live_register_stale_cause(
        stale_context,
        valid=valid,
        modem=modem,
    ) if stale else ""
    return {
        "enabled": bool(enabled),
        "owner": "modem_live",
        "source": "modem_live_register",
        "enabled_by": "54guc_internal_live_register",
        "profile_name": str(getattr(contract, "profile_name", "") or ""),
        "access_profile": _access_profile_dict(access_profile),
        "lane_contract": build_lane_contract_status(
            contract=contract,
            live_config_overrides=live_config_overrides,
            hard_rules=hard_rules,
            hard_rule_clamps=hard_rule_clamps,
        ),
        "clock_ms": int(clock_ms),
        "cadence_cycles": int(cadence_cycles),
        "interval_s": round(float(interval_s), 3),
        "stale_after_s": round(float(stale_after_s), 3),
        "sequence": int(
            getattr(current_status, "sequence", 0) or 0
        ),
        "history_depth": 0,
        "history": [],
        "valid": valid,
        "stale": bool(stale),
        "stale_cause": stale_reason,
        "age_s": None if age_s is None else round(age_s, 3),
        "refreshing": bool(refreshing),
        "worker_alive": bool(worker_alive),
        "last_attempt_ts": float(last_attempt_ts or 0.0),
        "last_miss_ts": float(last_miss_ts or 0.0),
        "miss_count": int(miss_count or 0),
        "last_reason": str(last_reason or ""),
        "snapshot_source": str(
            getattr(current_status, "source", "") or ""
        ),
        "connectivity": dict(connectivity or {}),
        "clock": dict(clock or {}),
        "modem_io": dict(modem_io or {}),
    }
