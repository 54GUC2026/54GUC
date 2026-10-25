"""Pure Brain-decision and Contextual-Bandit receipt contracts."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping


BRAIN_DECISION_SCHEMA = "BrainDecisionReceipt.v1"
BRAIN_DECISION_OWNER = "arch4.brain_decision_authority"
CONTEXTUAL_BANDIT_RECEIPT_SCHEMA = "ContextualBanditSelectionReceipt.v1"
CONTEXTUAL_BANDIT_OWNER = "arch4.contextual_bandit"


def contextual_bandit_selection_receipt_error(
    receipt: Mapping[str, Any] | None,
    *,
    expected_learning_context_key: str = "",
    expected_opportunity_set_id: str = "",
) -> str:
    row = _mapping(receipt)
    if (
        row.get("schema") != CONTEXTUAL_BANDIT_RECEIPT_SCHEMA
        or row.get("owner") != CONTEXTUAL_BANDIT_OWNER
    ):
        return "bandit_receipt_owner_schema_invalid"
    content = {
        key: value
        for key, value in row.items()
        if key != "selection_receipt_id"
    }
    if not str(row.get("selection_receipt_id") or "") or str(
        row.get("selection_receipt_id")
    ) != _digest(content):
        return "bandit_receipt_checksum_invalid"
    if row.get("selected") is not True:
        return str(row.get("reason") or "bandit_did_not_select")
    if "context_key" in row:
        return "bandit_ambiguous_context_forbidden"
    if not str(row.get("learning_context_key") or ""):
        return "bandit_learning_context_missing"
    if expected_learning_context_key and str(
        row.get("learning_context_key") or ""
    ) != str(
        expected_learning_context_key
    ):
        return "bandit_learning_context_mismatch"
    if expected_opportunity_set_id and str(
        row.get("opportunity_set_id") or ""
    ) != str(expected_opportunity_set_id):
        return "bandit_opportunity_set_mismatch"
    selected_id = str(row.get("selected_opportunity_id") or "")
    forecasts = [
        dict(value)
        for value in row.get("candidate_forecasts", [])
        if isinstance(value, Mapping)
    ]
    matching = [
        value
        for value in forecasts
        if value.get("opportunity_id") == selected_id
    ]
    if not selected_id or len(matching) != 1:
        return "bandit_selected_opportunity_unproven"
    propensities = [
        _float(value.get("selection_propensity"), -1.0)
        for value in forecasts
    ]
    propensity_sum = _float(row.get("propensity_sum"), -1.0)
    if (
        any(value <= 0.0 or value > 1.0 for value in propensities)
        or abs(sum(propensities) - 1.0) > 0.000001
        or abs(propensity_sum - 1.0) > 0.000001
    ):
        return "bandit_propensity_sum_invalid"
    selected_probability = _float(
        row.get("selection_probability"),
        -1.0,
    )
    if (
        abs(
            selected_probability
            - propensities[forecasts.index(matching[0])]
        )
        > 0.000001
    ):
        return "bandit_selected_probability_mismatch"
    return ""


def brain_decision_receipt_error(
    receipt: Mapping[str, Any] | None,
    *,
    expected_opportunity_set_id: str = "",
    expected_opportunity_id: str = "",
) -> str:
    row = _mapping(receipt)
    if (
        row.get("schema") != BRAIN_DECISION_SCHEMA
        or row.get("owner") != BRAIN_DECISION_OWNER
    ):
        return "brain_decision_receipt_owner_schema_invalid"
    content = {
        key: value
        for key, value in row.items()
        if key != "decision_receipt_id"
    }
    if not str(row.get("decision_receipt_id") or "") or str(
        row.get("decision_receipt_id")
    ) != _digest(content):
        return "brain_decision_receipt_checksum_invalid"
    if row.get("selected") is not True:
        return str(row.get("reason") or "brain_decision_not_selected")
    if "context_key" in row:
        return "brain_decision_ambiguous_context_forbidden"
    if not str(row.get("learning_context_key") or ""):
        return "brain_decision_learning_context_missing"
    if not str(row.get("action_context_key") or ""):
        return "brain_decision_action_context_missing"
    if not str(row.get("decision_id") or ""):
        return "brain_decision_id_missing"
    if expected_opportunity_set_id and str(
        row.get("opportunity_set_id") or ""
    ) != str(expected_opportunity_set_id):
        return "brain_decision_opportunity_set_mismatch"
    selected_id = str(row.get("selected_opportunity_id") or "")
    if expected_opportunity_id and selected_id != str(
        expected_opportunity_id
    ):
        return "brain_decision_opportunity_mismatch"
    selected = _mapping(row.get("selected_opportunity"))
    if (
        not selected_id
        or str(selected.get("opportunity_id") or "") != selected_id
    ):
        return "brain_decision_selected_opportunity_invalid"
    if str(row.get("selected_owner_class") or "") != str(
        selected.get("owner_class") or ""
    ):
        return "brain_decision_owner_class_mismatch"
    if str(row.get("selected_action") or "") != str(
        selected.get("action") or ""
    ):
        return "brain_decision_action_mismatch"
    considered = [
        dict(value)
        for value in row.get("considered", [])
        if isinstance(value, Mapping)
    ]
    matching = [
        value
        for value in considered
        if str(value.get("opportunity_id") or "") == selected_id
        and value.get("eligible") is True
    ]
    if len(matching) != 1:
        return "brain_decision_consideration_missing"
    selected_priority = _int(row.get("selected_priority"), -1)
    if selected_priority != _int(matching[0].get("priority"), -2):
        return "brain_decision_priority_mismatch"
    if any(
        value.get("eligible") is True
        and _int(value.get("priority"), -1) > selected_priority
        for value in considered
    ):
        return "brain_decision_higher_priority_opportunity_skipped"
    return ""


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


__all__ = [
    "BRAIN_DECISION_OWNER",
    "BRAIN_DECISION_SCHEMA",
    "CONTEXTUAL_BANDIT_OWNER",
    "CONTEXTUAL_BANDIT_RECEIPT_SCHEMA",
    "brain_decision_receipt_error",
    "contextual_bandit_selection_receipt_error",
]
from arch4.canonical_json import canonical_json_bytes
