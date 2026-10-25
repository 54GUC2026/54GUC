"""Counterfactual audit for Brain candidate generation.

This module owns one narrow production question:

Given current RF/congestion/CA evidence, should candidate generation have
produced at least one action candidate?

It does not authorize actions, execute actions, or replace TSAR/bouncer
policy. It only exposes whether the candidate generator left an opportunity
unrepresented.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.coerce import safe_bool, safe_float, safe_int
from arch4.interfaces import parse_band_signature

SCHEMA = "brain_counterfactual_opportunity_audit.v1"
OWNER = "arch4.brain.counterfactual_opportunity_audit"

GOVERNOR_BLOCKERS: frozenset[str] = frozenset(
    {
        "startup_grace",
        "connect_settle",
        "radio_change_settle",
        "min_action_interval",
        "radio_stability_hold",
        "planned_radio_quiet",
        "radio_mutation_window",
        "mutating_action_lock",
        "action_runtime_busy",
        "health_observe_hold",
    }
)

ACTION_CANDIDATE_WORDS: tuple[str, ...] = (
    "band",
    "ca",
    "cell",
    "pband",
    "lock",
    "selection",
    "explore",
    "avoid",
    "prefer",
    "restore",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _bool(value: Any, default: bool = False) -> bool:
    return safe_bool(value, default)


def _float(value: Any, default: float = 0.0) -> float:
    return safe_float(value, default)


def _int(value: Any, default: int = 0) -> int:
    return safe_int(value, default)


def _merge_runtime_registers(status: Mapping[str, Any], registers: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(status or {})
    for name in ("radio", "traffic", "latency", "quality", "ca", "mutation", "health"):
        register = _mapping(registers.get(name))
        value = _mapping(register.get("value"))
        for key, item in value.items():
            merged.setdefault(key, item)
        if name == "mutation":
            merged.setdefault("runtime_mutation_active", _bool(register.get("dirty")) or _bool(value.get("active")))
            merged.setdefault("runtime_mutation_validity", _text(register.get("validity")))
        if register.get("stale") is True:
            merged.setdefault(f"{name}_register_stale", True)
    return merged


def _bands_from_any(value: Any) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        out: list[int] = []
        for item in value:
            band = _int(item, 0)
            if band > 0 and band not in out:
                out.append(band)
        out.sort()
        return out
    return parse_band_signature(value)


def _first_bands(status: Mapping[str, Any], keys: tuple[str, ...]) -> list[int]:
    for key in keys:
        bands = _bands_from_any(status.get(key))
        if bands:
            return bands
    return []


def _candidate_rows(opportunity_inventory: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = opportunity_inventory.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


def _candidate_mentions_action(candidate: Mapping[str, Any]) -> bool:
    text = " ".join(
        _lower(candidate.get(key))
        for key in ("action", "lever", "intent", "reason")
    )
    return any(word in text for word in ACTION_CANDIDATE_WORDS)


def _declared_blockers(
    *,
    decision_trace: Mapping[str, Any],
    vectors: Mapping[str, Any],
    opportunity_inventory: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    for key in ("primary_blocker_gate", "primary_blocker_reason", "tsar_reason"):
        value = _text(decision_trace.get(key))
        if value and value not in blockers:
            blockers.append(value)
    for vector_name in ("calibration_vector", "learning_vector", "prediction_vector", "constraint_vector"):
        vector = _mapping(vectors.get(vector_name))
        raw = vector.get("blockers")
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple, set)):
            values = [str(item or "") for item in raw]
        else:
            values = []
        for value in values:
            text = _text(value)
            if text and text not in blockers:
                blockers.append(text)
    for candidate in _candidate_rows(opportunity_inventory):
        for reason in candidate.get("block_reasons") or []:
            text = _text(reason)
            if text and text not in blockers:
                blockers.append(text)
    return blockers[:12]


def _hard_governor_blocker(decision_trace: Mapping[str, Any], vectors: Mapping[str, Any]) -> str:
    gate = _lower(decision_trace.get("primary_blocker_gate"))
    if gate in GOVERNOR_BLOCKERS:
        return gate
    reason = _lower(decision_trace.get("primary_blocker_reason") or decision_trace.get("tsar_reason"))
    for blocker in GOVERNOR_BLOCKERS:
        if blocker in reason:
            return blocker
    constraint = _mapping(vectors.get("constraint_vector"))
    if _bool(constraint.get("observe_only")):
        return "observe_only"
    if _bool(constraint.get("planned_radio_quiet_active")):
        return "planned_radio_quiet"
    if _bool(constraint.get("incident_suppress_exploration")):
        return "incident_suppress_exploration"
    bouncer = _mapping(constraint.get("bouncer"))
    if _lower(bouncer.get("state")) == "blocked":
        return _text(bouncer.get("trip_reason")) or "bouncer_blocked"
    budget = _mapping(constraint.get("action_budget"))
    if _lower(budget.get("mode")) not in {"", "available"}:
        return _text(budget.get("blocker") or budget.get("reason") or budget.get("mode"))
    return ""


def _missing_evidence(status: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if "radio_connected" not in status and "packet_session_connected" not in status:
        missing.append("radio_connected")
    if not any(key in status for key in ("congestion_pct", "congestion_pressure_high", "cq_cell_display")):
        missing.append("quality_or_congestion")
    if not any(key in status for key in ("configured_band_pool", "selected_band_set", "configured_band_pool_signature", "selected_band_set_signature")):
        missing.append("configured_band_pool")
    if not any(key in status for key in ("active_band_set", "active_band_set_signature", "serving_band", "serving_band_signature", "ca_signature")):
        missing.append("active_band_state")
    return missing


def _status_is_stale(status: Mapping[str, Any]) -> bool:
    if status.get("sample_fresh") is False:
        return True
    for key in (
        "stale",
        "sample_stale",
        "radio_register_stale",
        "traffic_register_stale",
        "latency_register_stale",
        "quality_register_stale",
        "ca_register_stale",
    ):
        if _bool(status.get(key), False):
            return True
    return False


def _problem_reasons(status: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    congestion_valid = _bool(status.get("congestion_pct_valid"), "congestion_pct" in status)
    congestion_pct = _float(status.get("congestion_pct"), 0.0)
    if congestion_valid and congestion_pct >= 65.0:
        reasons.append("congestion_pct_high")
    if _bool(status.get("congestion_pressure_high")):
        reasons.append("congestion_pressure_high")
    cq_valid = _bool(status.get("cq_cell_display_valid"), _bool(status.get("cq_cell_valid"), False))
    cq_value = _float(status.get("cq_cell_display", status.get("cq_cell")), 0.0)
    if cq_valid and 0.0 < cq_value <= 0.45:
        reasons.append("cq_poor")
    rsrq_valid = _bool(status.get("rsrq_delta_display_valid"), _bool(status.get("rsrq_delta_valid"), False))
    rsrq_delta = _float(status.get("rsrq_delta_display_db", status.get("rsrq_delta")), 0.0)
    if rsrq_valid and rsrq_delta >= 4.0:
        reasons.append("rsrq_delta_high")
    rtt_valid = _bool(status.get("rtt_delta_display_valid"), _bool(status.get("rtt_delta_valid"), False))
    rtt_delta = _float(status.get("rtt_delta_display_ms", status.get("rtt_delta")), 0.0)
    if rtt_valid and rtt_delta >= 60.0:
        reasons.append("rtt_delta_high")
    has_traffic = _bool(status.get("has_traffic")) or _bool(status.get("traffic_active"))
    if not has_traffic:
        has_traffic = _float(status.get("dl_mbps"), 0.0) >= 0.5 or _float(status.get("ul_mbps"), 0.0) >= 0.2
    ca_flips = max(_float(status.get("ca_change_per_hour"), 0.0), _float(status.get("ca_ld_change_per_hour"), 0.0))
    if has_traffic and ca_flips >= 12.0 and reasons:
        reasons.append("ca_churn_under_load")
    return reasons


def _evidence(status: Mapping[str, Any]) -> dict[str, Any]:
    configured = _first_bands(
        status,
        (
            "configured_band_pool",
            "selected_band_set",
            "configured_band_pool_signature",
            "selected_band_set_signature",
            "band_set_signature",
        ),
    )
    active = _first_bands(
        status,
        (
            "active_band_set",
            "active_band_set_signature",
            "ca_component_band_set",
            "ca_component_band_signature",
            "ca_signature",
            "ca_band_signature",
            "serving_band_signature",
        ),
    )
    serving_band = _int(status.get("serving_band"), 0)
    if serving_band > 0 and serving_band not in active:
        active = sorted([*active, serving_band])
    alternatives = [band for band in configured if band not in set(active)]
    ca_active = _bool(status.get("ca_active"), len(active) > 1)
    radio_connected = _bool(
        status.get("radio_connected"),
        _bool(status.get("packet_session_connected"), True if active or serving_band > 0 else False),
    )
    return {
        "radio_connected": radio_connected,
        "sample_fresh": not _status_is_stale(status),
        "state": _text(status.get("state") or status.get("brain_state") or status.get("tsar_state")),
        "configured_bands": configured,
        "active_bands": active,
        "serving_band": serving_band,
        "alternative_bands": alternatives,
        "ca_active": ca_active,
        "ca_change_per_hour": _float(status.get("ca_change_per_hour"), 0.0),
        "ca_ld_change_per_hour": _float(status.get("ca_ld_change_per_hour"), 0.0),
        "congestion_pct": _float(status.get("congestion_pct"), 0.0),
        "congestion_pct_valid": _bool(status.get("congestion_pct_valid"), "congestion_pct" in status),
        "cq_cell_display": _float(status.get("cq_cell_display", status.get("cq_cell")), 0.0),
        "cq_cell_display_valid": _bool(status.get("cq_cell_display_valid"), _bool(status.get("cq_cell_valid"), False)),
        "rsrq_delta_display_db": _float(status.get("rsrq_delta_display_db", status.get("rsrq_delta")), 0.0),
        "rsrq_delta_display_valid": _bool(status.get("rsrq_delta_display_valid"), _bool(status.get("rsrq_delta_valid"), False)),
        "rtt_delta_display_ms": _float(status.get("rtt_delta_display_ms", status.get("rtt_delta")), 0.0),
        "rtt_delta_display_valid": _bool(status.get("rtt_delta_display_valid"), _bool(status.get("rtt_delta_valid"), False)),
        "has_traffic": _bool(status.get("has_traffic")) or _bool(status.get("traffic_active")),
        "dl_mbps": _float(status.get("dl_mbps"), 0.0),
        "ul_mbps": _float(status.get("ul_mbps"), 0.0),
    }


def build_counterfactual_opportunity_audit(
    *,
    status: Mapping[str, Any] | None = None,
    runtime_registers: Mapping[str, Any] | None = None,
    vectors: Mapping[str, Any] | None = None,
    opportunity_inventory: Mapping[str, Any] | None = None,
    decision_trace: Mapping[str, Any] | None = None,
    action_present: bool = False,
    action_suppressed: bool = False,
) -> dict[str, Any]:
    """Return the canonical missed-candidate audit for one decision snapshot."""

    safe_vectors = _mapping(vectors)
    safe_inventory = _mapping(opportunity_inventory)
    safe_trace = _mapping(decision_trace)
    merged_status = _merge_runtime_registers(_mapping(status), _mapping(runtime_registers))
    missing = _missing_evidence(merged_status)
    evidence = _evidence(merged_status)
    problems = _problem_reasons(merged_status)
    configured = evidence["configured_bands"]
    alternatives = evidence["alternative_bands"]
    ca_opportunity = bool(len(configured) > 1 and (alternatives or not evidence["ca_active"]))
    hard_blocker = _hard_governor_blocker(safe_trace, safe_vectors)
    candidates = _candidate_rows(safe_inventory)
    candidate_present = bool(candidates)
    action_candidate_present = any(_candidate_mentions_action(candidate) for candidate in candidates)
    actionable_candidate_present = safe_int(safe_inventory.get("actionable_count"), 0) > 0
    blocked_candidate_present = safe_int(safe_inventory.get("blocked_count"), 0) > 0

    should_have_candidate = bool(
        evidence["radio_connected"]
        and evidence["sample_fresh"]
        and ca_opportunity
        and problems
        and not action_present
    )
    candidate_generation_failed = bool(
        should_have_candidate
        and not hard_blocker
        and not action_candidate_present
        and not actionable_candidate_present
        and not action_suppressed
    )

    if action_present:
        status_text = "action_present"
        severity = "none"
        reason = "action_surface_already_present"
    elif missing:
        status_text = "insufficient_evidence"
        severity = "watch"
        reason = "counterfactual_inputs_missing"
    elif hard_blocker and should_have_candidate:
        status_text = "blocked_by_governor"
        severity = "none"
        reason = hard_blocker
    elif candidate_generation_failed:
        status_text = "needs_action_candidate"
        severity = "warning"
        reason = "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
    elif candidate_present or action_candidate_present:
        status_text = "candidate_present"
        severity = "none" if actionable_candidate_present else "watch"
        reason = "candidate_generation_reported_opportunity"
    else:
        status_text = "ok"
        severity = "none"
        reason = "no_counterfactual_action_need_detected"

    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "status": status_text,
        "severity": severity,
        "reason": reason,
        "should_have_candidate": should_have_candidate,
        "candidate_generation_failed": candidate_generation_failed,
        "action_candidate_present": action_candidate_present,
        "actionable_candidate_present": actionable_candidate_present,
        "blocked_candidate_present": blocked_candidate_present,
        "missed_action_family": "band_or_ca_exploration" if should_have_candidate else "",
        "problem_reasons": problems,
        "declared_blockers": _declared_blockers(
            decision_trace=safe_trace,
            vectors=safe_vectors,
            opportunity_inventory=safe_inventory,
        ),
        "governor_blocker": hard_blocker,
        "missing_evidence": missing,
        "evidence": evidence,
    }


def apply_counterfactual_audit_to_inventory(
    opportunity_inventory: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach audit result to the legacy inventory surface without changing owner."""

    inventory = dict(opportunity_inventory or {})
    status_text = _text(audit.get("status"))
    if status_text == "ok":
        proven = True
        table_status = "proven"
    elif status_text == "blocked_by_governor":
        proven = False
        table_status = "blocked_by_governor"
    elif _bool(audit.get("candidate_generation_failed")):
        proven = False
        table_status = "missed_counterfactual_candidate"
    elif status_text == "insufficient_evidence":
        proven = False
        table_status = "insufficient_evidence"
    elif status_text == "candidate_present":
        proven = False
        table_status = "candidate_present"
    else:
        proven = False
        table_status = status_text or "not_proven"
    inventory["nothing_left_on_table_proven"] = proven
    inventory["nothing_left_on_table_status"] = table_status
    inventory["nothing_left_on_table_reason"] = _text(audit.get("reason"))
    return inventory
