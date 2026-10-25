"""Generic customer/Core contract for one selected optimization opportunity.

ActionOpportunitySet owns the lever and parameter shape.  Contextual Bandit
ranks optimization opportunities and Brain selects exactly one.  This module
only binds that immutable selection to stable command/proof identities and a
generic proof template; it cannot rank, select, execute, grade or read a modem.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

from arch4.action_opportunity_set import action_opportunity_set_error
from arch4.action_selection_receipts import (
    brain_decision_receipt_error,
    contextual_bandit_selection_receipt_error,
)
from arch4.action_shape_legality import validate_action_shape
from arch4.interfaces import LeverType


OWNER = "arch4.customer_contracts.optimization_action"
PLAN_SCHEMA = "OptimizationActionPlan.v1"
PROOF_SCHEMA = "OptimizationProofTemplate.v1"
TARGET_CONFIRMATION_TIMEOUT_S = 180.0
FAILED_OBJECTIVE_HOLD_S = 900.0


@dataclass(frozen=True, slots=True)
class OptimizationActionPlan:
    command_id: str
    proof_id: str
    lever: str
    action: str
    target_shape: tuple[Any, ...]
    context_key: str
    reason_ref: str
    reason_code: str
    availability_intent: str
    objective_key: str
    opportunity_set_id: str
    opportunity_id: str
    decision_receipt_id: str
    created_ts: float
    before_cq: float | None = None
    before_congestion_pct: float | None = None
    target_confirmation_timeout_s: float = TARGET_CONFIRMATION_TIMEOUT_S
    failed_objective_hold_s: float = FAILED_OBJECTIVE_HOLD_S

    def target_shape_dict(self) -> dict[str, Any]:
        value = _thaw(self.target_shape)
        if not isinstance(value, dict):
            raise ValueError("optimization_action_plan_target_shape_invalid")
        return value

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": PLAN_SCHEMA,
            "owner": OWNER,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "lever": self.lever,
            "action": self.action,
            "target_shape": self.target_shape_dict(),
            "context_key": self.context_key,
            "reason_ref": self.reason_ref,
            "reason_code": self.reason_code,
            "availability_intent": self.availability_intent,
            "objective_key": self.objective_key,
            "opportunity_set_id": self.opportunity_set_id,
            "opportunity_id": self.opportunity_id,
            "decision_receipt_id": self.decision_receipt_id,
            "created_ts": round(float(self.created_ts), 6),
            "before_cq": self.before_cq,
            "before_congestion_pct": self.before_congestion_pct,
            "target_confirmation_timeout_s": round(
                float(self.target_confirmation_timeout_s), 3
            ),
            "failed_objective_hold_s": round(
                float(self.failed_objective_hold_s), 3
            ),
        }
        return {**content, "plan_id": _digest(content)}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OptimizationActionPlan":
        row = dict(raw or {})
        if row.get("schema") != PLAN_SCHEMA or row.get("owner") != OWNER:
            raise ValueError("optimization_action_plan_owner_schema_invalid")
        content = {key: value for key, value in row.items() if key != "plan_id"}
        if str(row.get("plan_id") or "") != _digest(content):
            raise ValueError("optimization_action_plan_checksum_invalid")
        target_shape = row.get("target_shape")
        if not isinstance(target_shape, Mapping):
            raise ValueError("optimization_action_plan_target_shape_invalid")
        value = cls(
            command_id=str(row.get("command_id") or ""),
            proof_id=str(row.get("proof_id") or ""),
            lever=str(row.get("lever") or ""),
            action=str(row.get("action") or ""),
            target_shape=_freeze(dict(target_shape)),
            context_key=str(row.get("context_key") or ""),
            reason_ref=str(row.get("reason_ref") or ""),
            reason_code=str(row.get("reason_code") or ""),
            availability_intent=str(row.get("availability_intent") or ""),
            objective_key=str(row.get("objective_key") or ""),
            opportunity_set_id=str(row.get("opportunity_set_id") or ""),
            opportunity_id=str(row.get("opportunity_id") or ""),
            decision_receipt_id=str(row.get("decision_receipt_id") or ""),
            created_ts=_float(row.get("created_ts"), 0.0),
            before_cq=_optional_float(row.get("before_cq")),
            before_congestion_pct=_optional_float(
                row.get("before_congestion_pct")
            ),
            target_confirmation_timeout_s=_float(
                row.get("target_confirmation_timeout_s"),
                TARGET_CONFIRMATION_TIMEOUT_S,
            ),
            failed_objective_hold_s=_float(
                row.get("failed_objective_hold_s"), FAILED_OBJECTIVE_HOLD_S
            ),
        )
        _validate_plan(value)
        return value


def build_optimization_action_plan(
    *,
    opportunity_set: Mapping[str, Any],
    brain_decision_receipt: Mapping[str, Any],
    contextual_bandit_selection_receipt: Mapping[str, Any],
    context: Any,
    now: float,
) -> OptimizationActionPlan:
    """Bind one exact Bandit/Brain selection without changing its shape."""

    set_row = dict(opportunity_set or {})
    set_error = action_opportunity_set_error(set_row)
    if set_error:
        raise ValueError(f"optimization_action_plan_opportunity_set_invalid:{set_error}")
    brain_row = dict(brain_decision_receipt or {})
    selected = _selected_opportunity(set_row, brain_row)
    if str(selected.get("owner_class") or "") != "optimization":
        raise ValueError("optimization_action_plan_owner_class_invalid")
    if selected.get("legal") is not True or selected.get("executable") is not True:
        raise ValueError("optimization_action_plan_opportunity_not_executable")
    opportunity_set_id = str(set_row.get("opportunity_set_id") or "")
    opportunity_id = str(selected.get("opportunity_id") or "")
    context_key = str(set_row.get("learning_context_key") or "")
    bandit_row = dict(contextual_bandit_selection_receipt or {})
    bandit_error = contextual_bandit_selection_receipt_error(
        bandit_row,
        expected_learning_context_key=context_key,
        expected_opportunity_set_id=opportunity_set_id,
    )
    if bandit_error:
        raise ValueError(f"optimization_action_plan_bandit_receipt_invalid:{bandit_error}")
    if str(bandit_row.get("selected_opportunity_id") or "") != opportunity_id:
        raise ValueError("optimization_action_plan_bandit_selection_mismatch")

    lever_name = str(selected.get("lever") or "").strip().upper()
    try:
        lever = LeverType[lever_name]
    except KeyError as exc:
        raise ValueError("optimization_action_plan_lever_invalid") from exc
    params = dict(selected.get("params") or {})
    shape = validate_action_shape(lever, params)
    if not shape.legal:
        raise ValueError(
            "optimization_action_plan_shape_invalid:"
            + str(shape.code or shape.reason or "invalid")
        )
    decision_receipt_id = str(brain_row.get("decision_receipt_id") or "")
    identity_material = "|".join(
        (opportunity_set_id, decision_receipt_id, opportunity_id, lever_name)
    )
    command_id = "selected:" + hashlib.sha256(
        ("command|" + identity_material).encode("utf-8")
    ).hexdigest()[:32]
    proof_id = "proof:selected:" + hashlib.sha256(
        ("proof|" + identity_material).encode("utf-8")
    ).hexdigest()[:32]
    target_digest = _digest({"lever": lever_name, "params": params})
    objective_key = "optimization:" + hashlib.sha256(
        (
            str(set_row.get("identity_code") or "")
            + "|"
            + str(set_row.get("session_scope_key") or "")
            + "|"
            + context_key
            + "|"
            + target_digest
        ).encode("utf-8")
    ).hexdigest()[:40]
    evidence = dict(selected.get("evidence") or {})
    reason_ref = _evidence_text(evidence, "smart_loop_reason_ref", "reason_ref")
    if not reason_ref:
        reason_ref = "opportunity:" + opportunity_id[:32]
    reason_code = _evidence_text(
        evidence, "smart_loop_reason_code", "reason_code", "behavior_id"
    ) or str(selected.get("action") or lever_name.lower())
    availability_intent = _evidence_text(evidence, "availability_intent")
    if not availability_intent:
        combined = (reason_code + " " + str(selected.get("action") or "")).lower()
        availability_intent = (
            "restore_ca"
            if "restore" in combined or "recovery" in combined
            else "explore"
        )
    value = OptimizationActionPlan(
        command_id=command_id,
        proof_id=proof_id,
        lever=lever_name,
        action=str(selected.get("action") or lever_name.lower()),
        target_shape=_freeze(params),
        context_key=context_key,
        reason_ref=reason_ref,
        reason_code=reason_code,
        availability_intent=availability_intent,
        objective_key=objective_key,
        opportunity_set_id=opportunity_set_id,
        opportunity_id=opportunity_id,
        decision_receipt_id=decision_receipt_id,
        created_ts=_float(now, 0.0),
        before_cq=_optional_float(getattr(context, "cq", None)),
        before_congestion_pct=_optional_float(
            getattr(context, "congestion_pct", None)
        ),
    )
    _validate_plan(value)
    return value


def optimization_proof_template(
    plan: OptimizationActionPlan | Mapping[str, Any],
) -> dict[str, Any]:
    value = (
        plan
        if isinstance(plan, OptimizationActionPlan)
        else OptimizationActionPlan.from_mapping(plan)
    )
    content = {
        "schema": PROOF_SCHEMA,
        "owner": OWNER,
        "command_id": value.command_id,
        "proof_id": value.proof_id,
        "lever": value.lever,
        "action": value.action,
        "target_shape": value.target_shape_dict(),
        "context_key": value.context_key,
        "reason_ref": value.reason_ref,
        "objective_key": value.objective_key,
        "opportunity_set_id": value.opportunity_set_id,
        "opportunity_id": value.opportunity_id,
        "decision_receipt_id": value.decision_receipt_id,
        "created_ts": round(float(value.created_ts), 6),
        "before_cq": value.before_cq,
        "before_congestion_pct": value.before_congestion_pct,
        "target_confirmation_timeout_s": round(
            float(value.target_confirmation_timeout_s), 3
        ),
        "failed_objective_hold_s": round(
            float(value.failed_objective_hold_s), 3
        ),
    }
    return {**content, "proof_template_id": _digest(content)}


def validate_optimization_proof_template(
    proof_template: Mapping[str, Any],
    *,
    selected_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = dict(proof_template or {})
    if row.get("schema") != PROOF_SCHEMA or row.get("owner") != OWNER:
        raise ValueError("optimization_proof_template_owner_schema_invalid")
    content = {
        key: value for key, value in row.items() if key != "proof_template_id"
    }
    if str(row.get("proof_template_id") or "") != _digest(content):
        raise ValueError("optimization_proof_template_checksum_invalid")
    for field in (
        "command_id",
        "proof_id",
        "lever",
        "action",
        "context_key",
        "reason_ref",
        "objective_key",
        "opportunity_set_id",
        "opportunity_id",
        "decision_receipt_id",
    ):
        if not str(row.get(field) or "").strip():
            raise ValueError(f"optimization_proof_template_{field}_missing")
    target_shape = row.get("target_shape")
    if not isinstance(target_shape, Mapping):
        raise ValueError("optimization_proof_template_target_shape_invalid")
    try:
        lever = LeverType[str(row.get("lever") or "").strip().upper()]
    except KeyError as exc:
        raise ValueError("optimization_proof_template_lever_invalid") from exc
    shape = validate_action_shape(lever, dict(target_shape))
    if not shape.legal:
        raise ValueError(
            "optimization_proof_template_shape_invalid:"
            + str(shape.code or shape.reason or "invalid")
        )
    if _float(row.get("created_ts"), 0.0) <= 0.0:
        raise ValueError("optimization_proof_template_created_ts_invalid")
    if _float(row.get("target_confirmation_timeout_s"), 0.0) <= 0.0:
        raise ValueError("optimization_proof_template_timeout_invalid")
    if _float(row.get("failed_objective_hold_s"), 0.0) <= 0.0:
        raise ValueError("optimization_proof_template_hold_invalid")
    if selected_action is not None:
        selected = dict(selected_action or {})
        for field in ("command_id", "proof_id", "lever", "opportunity_set_id", "opportunity_id", "decision_receipt_id"):
            if str(selected.get(field) or "") != str(row.get(field) or ""):
                raise ValueError(
                    f"optimization_proof_template_selected_{field}_mismatch"
                )
        if dict(selected.get("params") or {}) != dict(target_shape):
            raise ValueError("optimization_proof_template_selected_shape_mismatch")
    return row


def _selected_opportunity(
    opportunity_set: Mapping[str, Any], brain_receipt: Mapping[str, Any]
) -> dict[str, Any]:
    set_id = str(opportunity_set.get("opportunity_set_id") or "")
    selected_id = str(brain_receipt.get("selected_opportunity_id") or "")
    error = brain_decision_receipt_error(
        brain_receipt,
        expected_opportunity_set_id=set_id,
        expected_opportunity_id=selected_id,
    )
    if error:
        raise ValueError(f"optimization_action_plan_brain_receipt_invalid:{error}")
    if brain_receipt.get("do_nothing") is True:
        raise ValueError("optimization_action_plan_do_nothing_forbidden")
    selected = dict(brain_receipt.get("selected_opportunity") or {})
    ledger = next(
        (
            dict(value)
            for value in list(opportunity_set.get("opportunities") or [])
            if isinstance(value, Mapping)
            and str(value.get("opportunity_id") or "") == selected_id
        ),
        None,
    )
    if ledger is None or ledger != selected:
        raise ValueError("optimization_action_plan_selected_opportunity_not_in_ledger")
    return selected


def _validate_plan(value: OptimizationActionPlan) -> None:
    for field in (
        "command_id",
        "proof_id",
        "lever",
        "action",
        "context_key",
        "reason_ref",
        "objective_key",
        "opportunity_set_id",
        "opportunity_id",
        "decision_receipt_id",
    ):
        if not str(getattr(value, field) or "").strip():
            raise ValueError(f"optimization_action_plan_{field}_missing")
    if value.created_ts <= 0.0:
        raise ValueError("optimization_action_plan_created_ts_invalid")
    if value.target_confirmation_timeout_s <= 0.0:
        raise ValueError("optimization_action_plan_timeout_invalid")
    if value.failed_objective_hold_s <= 0.0:
        raise ValueError("optimization_action_plan_hold_invalid")
    try:
        lever = LeverType[value.lever]
    except KeyError as exc:
        raise ValueError("optimization_action_plan_lever_invalid") from exc
    shape = validate_action_shape(lever, value.target_shape_dict())
    if not shape.legal:
        raise ValueError(
            "optimization_action_plan_shape_invalid:"
            + str(shape.code or shape.reason or "invalid")
        )


def _evidence_text(evidence: Mapping[str, Any], *keys: str) -> str:
    rows = [dict(evidence or {})]
    rows.extend(
        dict(value)
        for value in list(dict(evidence or {}).get("packages") or [])
        if isinstance(value, Mapping)
    )
    for row in rows:
        for key in keys:
            text = str(row.get(key) or "").strip()
            if text:
                return text
    return ""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            "__optimization_mapping_v1__",
            tuple(sorted((str(key), _freeze(item)) for key, item in value.items())),
        )
    if isinstance(value, (list, tuple)):
        return (
            "__optimization_sequence_v1__",
            tuple(_freeze(item) for item in value),
        )
    return value


def _thaw(value: Any) -> Any:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] == "__optimization_mapping_v1__"
        and isinstance(value[1], tuple)
    ):
        return {item[0]: _thaw(item[1]) for item in value[1]}
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] == "__optimization_sequence_v1__"
        and isinstance(value[1], tuple)
    ):
        return [_thaw(item) for item in value[1]]
    return value


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value or {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


__all__ = (
    "FAILED_OBJECTIVE_HOLD_S",
    "OptimizationActionPlan",
    "OWNER",
    "PLAN_SCHEMA",
    "PROOF_SCHEMA",
    "TARGET_CONFIRMATION_TIMEOUT_S",
    "build_optimization_action_plan",
    "optimization_proof_template",
    "validate_optimization_proof_template",
)
