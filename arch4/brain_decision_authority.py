"""Sole final-selection owner over one complete action opportunity set.

SmartLoop ranks optimization opportunities.  Operator, recovery, maintenance,
policy, and service owners submit explicit active opportunities.  This module
selects exactly one opportunity or do-nothing and publishes the reason; it does
not evaluate physical safety, acquire a mutation lease, execute, or grade.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from arch4.action_opportunity_set import action_opportunity_set_error
from arch4.action_selection_receipts import (
    BRAIN_DECISION_OWNER as OWNER,
    BRAIN_DECISION_SCHEMA as SCHEMA,
    contextual_bandit_selection_receipt_error as _contextual_bandit_selection_receipt_error,
)


OPPORTUNITY_SET_SCHEMA = "ActionOpportunitySet.v1"

_OWNER_PRIORITY = {
    "operator": 600,
    "recovery": 500,
    "maintenance": 400,
    "policy": 300,
    "service": 250,
    "optimization": 100,
}


def select_brain_decision(
    *,
    opportunity_set: Mapping[str, Any],
    bandit_receipt: Mapping[str, Any] | None,
    decision_id: str,
    decision_ts: float,
) -> dict[str, Any]:
    set_row = dict(opportunity_set or {})
    set_error = _opportunity_set_error(set_row)
    if set_error:
        return _rejected(decision_id=decision_id, reason=set_error)
    decision_key = str(decision_id or "").strip()
    if not decision_key:
        return _rejected(decision_id="", reason="decision_id_required")
    try:
        ts = float(decision_ts)
    except (TypeError, ValueError):
        return _rejected(decision_id=decision_key, reason="decision_ts_invalid")
    if ts <= 0.0:
        return _rejected(decision_id=decision_key, reason="decision_ts_invalid")

    opportunities = [
        dict(row)
        for row in set_row.get("opportunities", [])
        if isinstance(row, Mapping)
    ]
    do_nothing = next(
        (row for row in opportunities if row.get("action") == "do_nothing"),
        None,
    )
    if do_nothing is None:
        return _rejected(decision_id=decision_key, reason="do_nothing_arm_missing")

    selected_bandit_id, bandit_error = _selected_bandit_opportunity(
        bandit_receipt,
        learning_context_key=str(
            set_row.get("learning_context_key") or ""
        ),
        opportunity_set_id=str(set_row.get("opportunity_set_id") or ""),
    )
    considered: list[dict[str, Any]] = []
    eligible: list[tuple[int, str, dict[str, Any]]] = []
    for opportunity in opportunities:
        admitted, reason = _opportunity_eligibility(
            opportunity,
            selected_bandit_id=selected_bandit_id,
            bandit_error=bandit_error,
            decision_ts=ts,
        )
        owner_class = str(opportunity.get("owner_class") or "")
        priority = _OWNER_PRIORITY.get(owner_class, 0)
        if opportunity.get("action") == "do_nothing":
            priority = 0
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        considered.append(
            {
                "opportunity_id": opportunity_id,
                "action": str(opportunity.get("action") or ""),
                "owner_class": owner_class,
                "eligible": bool(admitted),
                "priority": priority,
                "reason": reason,
            }
        )
        if admitted:
            eligible.append((priority, opportunity_id, opportunity))

    if not eligible:
        return _rejected(
            decision_id=decision_key,
            reason="no_eligible_opportunity_including_do_nothing",
            considered=considered,
        )
    eligible.sort(key=lambda item: (-item[0], item[1]))
    priority, selected_id, selected = eligible[0]
    selected_is_noop = selected.get("action") == "do_nothing"
    content = {
        "schema": SCHEMA,
        "owner": OWNER,
        "decision_id": decision_key,
        "decision_ts": ts,
        "opportunity_set_id": str(set_row.get("opportunity_set_id") or ""),
        "learning_context_key": str(
            set_row.get("learning_context_key") or ""
        ),
        "action_context_key": str(set_row.get("action_context_key") or ""),
        "identity_code": str(set_row.get("identity_code") or ""),
        "session_scope_key": str(set_row.get("session_scope_key") or ""),
        "source_epoch": str(set_row.get("source_epoch") or ""),
        "source_sequence": int(set_row.get("source_sequence") or 0),
        "selected": True,
        "selected_opportunity_id": selected_id,
        "selected_owner_class": str(selected.get("owner_class") or ""),
        "selected_priority": int(priority),
        "selected_action": str(selected.get("action") or ""),
        "selected_opportunity": selected,
        "do_nothing": bool(selected_is_noop),
        "reason": (
            "do_nothing_is_highest_eligible_opportunity"
            if selected_is_noop
            else "highest_priority_active_opportunity_selected"
        ),
        "bandit_receipt": dict(bandit_receipt or {}),
        "bandit_rejection_reason": bandit_error,
        "considered": considered,
    }
    return {**content, "decision_receipt_id": _digest(content)}


def _opportunity_set_error(row: Mapping[str, Any]) -> str:
    return action_opportunity_set_error(row)


def _selected_bandit_opportunity(
    receipt: Mapping[str, Any] | None,
    *,
    learning_context_key: str,
    opportunity_set_id: str,
) -> tuple[str, str]:
    row = dict(receipt or {})
    if not row:
        return "", "bandit_receipt_missing"
    error = _contextual_bandit_selection_receipt_error(
        row,
        expected_learning_context_key=learning_context_key,
        expected_opportunity_set_id=opportunity_set_id,
    )
    return (
        "" if error else str(row.get("selected_opportunity_id") or ""),
        error,
    )


def _opportunity_eligibility(
    opportunity: Mapping[str, Any],
    *,
    selected_bandit_id: str,
    bandit_error: str,
    decision_ts: float,
) -> tuple[bool, str]:
    if opportunity.get("action") == "do_nothing":
        return True, "mandatory_do_nothing"
    if opportunity.get("legal") is not True:
        return False, "opportunity_not_legal"
    if opportunity.get("executable") is not True:
        return False, "opportunity_not_executable"
    owner_class = str(opportunity.get("owner_class") or "")
    evidence = _mapping(opportunity.get("evidence"))
    schedule = _mapping(opportunity.get("schedule"))
    if owner_class == "optimization":
        if bandit_error:
            return False, bandit_error
        if str(opportunity.get("opportunity_id") or "") != selected_bandit_id:
            return False, "optimization_not_selected_by_bandit"
        return True, "optimization_selected_by_bandit"
    if owner_class == "operator":
        if not _operator_confirmation_present(evidence, decision_ts=decision_ts):
            return False, "operator_confirmation_required"
        return True, "confirmed_operator_intent"
    if owner_class == "recovery":
        if not any(
            row.get("required") is True for row in _evidence_packages(evidence)
        ):
            return False, "recovery_requirement_missing"
        return True, "required_recovery_intent"
    if owner_class == "maintenance":
        due_at = _float(schedule.get("due_at"), 0.0)
        if schedule and due_at > decision_ts:
            return False, "maintenance_not_due"
        return True, "maintenance_due"
    if owner_class in {"policy", "service"}:
        return True, f"active_{owner_class}_intent"
    return False, "opportunity_owner_class_invalid"


def _rejected(
    *,
    decision_id: str,
    reason: str,
    considered: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "decision_id": str(decision_id or ""),
        "selected": False,
        "reason": str(reason),
        "considered": [dict(row) for row in considered],
    }


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_packages(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    row = dict(evidence or {})
    packages = [
        dict(value)
        for value in row.get("packages", [])
        if isinstance(value, Mapping)
    ]
    return packages or [row]


def _operator_confirmation_present(
    evidence: Mapping[str, Any], *, decision_ts: float
) -> bool:
    for row in _evidence_packages(evidence):
        if row.get("confirmed") is not True or not str(
            row.get("confirmation_id") or ""
        ):
            continue
        expires_at = _float(row.get("expires_at"), 0.0)
        if expires_at > 0.0 and expires_at < decision_ts:
            continue
        return True
    return False


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "OWNER",
    "SCHEMA",
    "select_brain_decision",
]
