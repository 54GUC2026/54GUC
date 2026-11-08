"""Sole selected-action to ``LeverAction`` materialization boundary.

The complete opportunity ledger and Brain receipt describe the selected
intent.  This module is the only normal-path owner allowed to turn that
selection into a mutable runtime ``LeverAction``.  It does not select, run
Bandit, evaluate Bouncer, acquire a cellular lease, execute a modem command or
grade the result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from arch4.action_opportunity_set import action_opportunity_set_error
from arch4.action_selection_receipts import (
    brain_decision_receipt_error,
    contextual_bandit_selection_receipt_error,
)
from arch4.action_shape_legality import validate_action_shape
from arch4.action_transaction import (
    bind_core_command_target,
    bound_core_command_target_from_action,
)
from arch4.core.command_request_builder import build_core_command_target
from arch4.customer_contracts.optimization_action import (
    OptimizationActionPlan,
    OWNER as OPTIMIZATION_CONTRACT_OWNER,
)
from arch4.interfaces import LeverAction, LeverType


OWNER = "arch4.customer_contracts.selected_action"
SCHEMA = "SelectedAction.v1"
_MAP_TAG = "__selected_action_mapping__"
_SEQUENCE_TAG = "__selected_action_sequence__"
_OWNER_CLASSES = frozenset(
    {"operator", "recovery", "maintenance", "policy", "service", "optimization"}
)
_ACTION_ATTRIBUTE_NAMES = (
    "action_kind",
    "effect",
    "capability_key",
    "expected_readback",
    "mutation_window",
    "requires_modem_write",
    "requires_quiet_window",
    "allowed_while_mutating",
    "allowed_while_degraded",
    "created_ts",
    "created_at",
    "expires_after_s",
    "expires_at_ts",
    "dequeued_ts",
    "queue_wait_s",
    "queue_path",
    "dedupe_key",
    "replay_policy",
    "risk_level",
    "backend_family",
    "family",
    "family_label",
    "family_support_text",
    "allowed_backend_families",
    "profile",
    "profile_name",
    "policy_family",
    "provider_plmn",
    "provider_name",
    "modem_backend",
    "_gui_action_id",
    "_gui_action_source",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            _MAP_TAG,
            tuple(
                sorted(
                    ((str(key), _freeze(item)) for key, item in value.items()),
                    key=lambda row: row[0],
                )
            ),
        )
    if isinstance(value, (set, frozenset)):
        return (
            _SEQUENCE_TAG,
            tuple(sorted((_freeze(item) for item in value), key=repr)),
        )
    if isinstance(value, (list, tuple)):
        return (_SEQUENCE_TAG, tuple(_freeze(item) for item in value))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        if len(value) == 2 and value[0] == _MAP_TAG:
            return {item[0]: _thaw(item[1]) for item in value[1]}
        if len(value) == 2 and value[0] == _SEQUENCE_TAG:
            return [_thaw(item) for item in value[1]]
        if all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {item[0]: _thaw(item[1]) for item in value}
        return [_thaw(item) for item in value]
    return value


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedActionContract:
    """Immutable post-Brain action description and receipt bundle."""

    command_id: str
    proof_id: str
    lever: str
    params: tuple[Any, ...]
    reason: str
    intent: str
    objective_id: str
    source: str
    owner_class: str
    created_ts: float
    expires_at_ts: float
    opportunity_set_id: str
    opportunity_id: str
    decision_id: str
    decision_receipt_id: str
    brain_decision_receipt: tuple[Any, ...]
    contextual_bandit_selection_receipt: tuple[Any, ...]
    parameter_metadata: tuple[Any, ...] = ()
    action_attributes: tuple[Any, ...] = ()
    optimization_metadata: tuple[Any, ...] = ()

    @property
    def optimization(self) -> bool:
        return self.owner_class == "optimization"

    def params_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.params))

    def brain_receipt_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.brain_decision_receipt))

    def bandit_receipt_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.contextual_bandit_selection_receipt))

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": SCHEMA,
            "owner": OWNER,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "lever": self.lever,
            "params": self.params_dict(),
            "reason": self.reason,
            "intent": self.intent,
            "objective_id": self.objective_id,
            "source": self.source,
            "owner_class": self.owner_class,
            "optimization": self.optimization,
            "created_ts": round(float(self.created_ts), 6),
            "expires_at_ts": round(float(self.expires_at_ts), 6),
            "opportunity_set_id": self.opportunity_set_id,
            "opportunity_id": self.opportunity_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "brain_decision_receipt": self.brain_receipt_dict(),
            "contextual_bandit_selection_receipt": self.bandit_receipt_dict(),
            "parameter_metadata": dict(_thaw(self.parameter_metadata)),
            "action_attributes": dict(_thaw(self.action_attributes)),
            "optimization_metadata": dict(_thaw(self.optimization_metadata)),
        }
        return {**content, "selected_action_id": _digest(content)}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SelectedActionContract":
        row = dict(raw or {})
        content = {
            key: value for key, value in row.items() if key != "selected_action_id"
        }
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("selected_action_owner_schema_invalid")
        if str(row.get("selected_action_id") or "") != _digest(content):
            raise ValueError("selected_action_checksum_invalid")
        value = cls(
            command_id=str(row.get("command_id") or ""),
            proof_id=str(row.get("proof_id") or ""),
            lever=str(row.get("lever") or ""),
            params=_freeze(dict(row.get("params") or {})),
            reason=str(row.get("reason") or ""),
            intent=str(row.get("intent") or ""),
            objective_id=str(row.get("objective_id") or ""),
            source=str(row.get("source") or ""),
            owner_class=str(row.get("owner_class") or ""),
            created_ts=float(row.get("created_ts", 0.0) or 0.0),
            expires_at_ts=float(row.get("expires_at_ts", 0.0) or 0.0),
            opportunity_set_id=str(row.get("opportunity_set_id") or ""),
            opportunity_id=str(row.get("opportunity_id") or ""),
            decision_id=str(row.get("decision_id") or ""),
            decision_receipt_id=str(row.get("decision_receipt_id") or ""),
            brain_decision_receipt=_freeze(
                dict(row.get("brain_decision_receipt") or {})
            ),
            contextual_bandit_selection_receipt=_freeze(
                dict(row.get("contextual_bandit_selection_receipt") or {})
            ),
            parameter_metadata=_freeze(
                dict(row.get("parameter_metadata") or {})
            ),
            action_attributes=_freeze(dict(row.get("action_attributes") or {})),
            optimization_metadata=_freeze(
                dict(row.get("optimization_metadata") or {})
            ),
        )
        _validate_selected_action(value)
        if bool(row.get("optimization")) is not value.optimization:
            raise ValueError("selected_action_optimization_projection_invalid")
        return value

    def to_lever_action(self) -> LeverAction:
        """Construct the runtime action only from this validated record."""

        _validate_selected_action(self)
        params = self.params_dict()
        params.update(dict(_thaw(self.parameter_metadata)))
        params.update(
            {
                "_selected_action_command": True,
                "_selected_action_command_id": self.command_id,
                "_selected_action_proof_id": self.proof_id,
                "_selected_action_owner_class": self.owner_class,
                "_opportunity_set_id": self.opportunity_set_id,
                "_opportunity_id": self.opportunity_id,
                "_brain_decision_receipt": self.brain_receipt_dict(),
                "_contextual_bandit_selection_receipt": self.bandit_receipt_dict(),
            }
        )
        if self.optimization:
            optimization = dict(_thaw(self.optimization_metadata))
            params.update(
                {
                    "_smart_loop_optimization": True,
                    "_smart_loop_reason_ref": str(
                        optimization.get("reason_ref") or ""
                    ),
                    "_smart_loop_reason_code": str(
                        optimization.get("reason_code") or ""
                    ),
                    "_smart_loop_availability_intent": str(
                        optimization.get("availability_intent") or ""
                    ),
                    "_smart_loop_context_key": str(
                        optimization.get("context_key") or ""
                    ),
                }
            )
        action = LeverAction(
            lever=LeverType[self.lever],
            params=params,
            reason=self.reason,
            intent=self.intent,
            objective_id=self.objective_id,
        )
        bind_core_command_target(
            action,
            build_core_command_target(self.lever, self.params_dict()),
        )
        setattr(action, "command_id", self.command_id)
        setattr(action, "source", self.source)
        for name, value in dict(_thaw(self.action_attributes)).items():
            if name in _ACTION_ATTRIBUTE_NAMES:
                setattr(action, name, value)
        return action


def materialize_selected_action(
    *,
    opportunity_set: Mapping[str, Any],
    brain_decision_receipt: Mapping[str, Any],
    contextual_bandit_selection_receipt: Mapping[str, Any] | None,
    now: float,
    action_template: LeverAction | None = None,
    source_context: Mapping[str, Any] | None = None,
    parameter_metadata: Mapping[str, Any] | None = None,
    optimization_plan: Mapping[str, Any] | None = None,
) -> SelectedActionContract:
    """Validate one Brain selection and construct its immutable action record."""

    set_row = dict(opportunity_set or {})
    set_error = action_opportunity_set_error(set_row)
    if set_error:
        raise ValueError(f"selected_action_opportunity_set_invalid:{set_error}")
    brain_row = dict(brain_decision_receipt or {})
    opportunity_set_id = str(set_row.get("opportunity_set_id") or "")
    opportunity_id = str(brain_row.get("selected_opportunity_id") or "")
    brain_error = brain_decision_receipt_error(
        brain_row,
        expected_opportunity_set_id=opportunity_set_id,
        expected_opportunity_id=opportunity_id,
    )
    if brain_error:
        raise ValueError(f"selected_action_brain_receipt_invalid:{brain_error}")
    if brain_row.get("do_nothing") is True:
        raise ValueError("selected_action_do_nothing_cannot_materialize")
    selected = dict(brain_row.get("selected_opportunity") or {})
    ledger_selected = next(
        (
            dict(value)
            for value in list(set_row.get("opportunities") or [])
            if isinstance(value, Mapping)
            and str(value.get("opportunity_id") or "") == opportunity_id
        ),
        None,
    )
    if ledger_selected is None or ledger_selected != selected:
        raise ValueError("selected_action_selected_opportunity_not_in_ledger")
    if selected.get("legal") is not True or selected.get("executable") is not True:
        raise ValueError("selected_action_selected_opportunity_not_executable")
    owner_class = str(selected.get("owner_class") or "")
    if owner_class not in _OWNER_CLASSES:
        raise ValueError("selected_action_owner_class_invalid")
    try:
        lever = LeverType[str(selected.get("lever") or "")]
    except KeyError as exc:
        raise ValueError("selected_action_lever_invalid") from exc
    selected_params = dict(selected.get("params") or {})
    verdict = validate_action_shape(lever, selected_params)
    if not verdict.legal:
        raise ValueError(
            "selected_action_shape_invalid:"
            + str(verdict.code or verdict.reason or "invalid")
        )

    bandit_row = dict(contextual_bandit_selection_receipt or {})
    if owner_class == "optimization":
        if not bandit_row:
            raise ValueError("selected_action_optimization_bandit_receipt_missing")
        bandit_error = contextual_bandit_selection_receipt_error(
            bandit_row,
            expected_learning_context_key=str(
                set_row.get("learning_context_key") or ""
            ),
            expected_opportunity_set_id=opportunity_set_id,
        )
        if bandit_error:
            raise ValueError(
                f"selected_action_bandit_receipt_invalid:{bandit_error}"
            )
        if str(bandit_row.get("selected_opportunity_id") or "") != opportunity_id:
            raise ValueError("selected_action_bandit_selection_mismatch")
        if not isinstance(optimization_plan, Mapping):
            raise ValueError("selected_action_optimization_plan_missing")
    elif optimization_plan:
        raise ValueError("selected_action_non_optimization_plan_forbidden")
    elif bandit_row:
        bandit_error = contextual_bandit_selection_receipt_error(
            bandit_row,
            expected_learning_context_key=str(
                set_row.get("learning_context_key") or ""
            ),
            expected_opportunity_set_id=opportunity_set_id,
        )
        if bandit_error:
            raise ValueError(
                f"selected_action_bandit_receipt_invalid:{bandit_error}"
            )

    context = dict(source_context or {})
    action_params = selected_params
    attributes: dict[str, Any] = {}
    if action_template is not None:
        if not isinstance(action_template, LeverAction):
            raise TypeError("selected_action_template_requires_lever_action")
        if action_template.lever is not lever:
            raise ValueError("selected_action_template_lever_mismatch")
        template_shape = bound_core_command_target_from_action(
            action_template
        ).to_mapping()
        template_shape.pop("lever", None)
        if _freeze(template_shape) != _freeze(selected_params):
            raise ValueError("selected_action_template_shape_mismatch")
        action_params = dict(action_template.params or {})
        for name in _ACTION_ATTRIBUTE_NAMES:
            if hasattr(action_template, name):
                attributes[name] = getattr(action_template, name)

    plan = dict(optimization_plan or {})
    parsed_plan: OptimizationActionPlan | None = None
    if plan:
        parsed_plan = _validate_optimization_plan(
            plan,
            lever=lever,
            selected_params=selected_params,
        )
        if parsed_plan.opportunity_set_id != opportunity_set_id:
            raise ValueError(
                "selected_action_optimization_opportunity_set_mismatch"
            )
        if parsed_plan.opportunity_id != opportunity_id:
            raise ValueError(
                "selected_action_optimization_opportunity_mismatch"
            )
        if parsed_plan.decision_receipt_id != str(
            brain_row.get("decision_receipt_id") or ""
        ):
            raise ValueError(
                "selected_action_optimization_decision_receipt_mismatch"
            )
        if parsed_plan.context_key != str(
            set_row.get("learning_context_key") or ""
        ):
            raise ValueError("selected_action_optimization_context_mismatch")
    evidence = dict(selected.get("evidence") or {})
    command_id = str(
        plan.get("command_id")
        or context.get("command_id")
        or getattr(action_template, "command_id", "")
        or ""
    ).strip()
    if not command_id:
        command_id = (
            "selected:"
            + hashlib.sha256(
                (
                    str(brain_row.get("decision_receipt_id") or "")
                    + "|"
                    + opportunity_id
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
    proof_id = str(plan.get("proof_id") or context.get("proof_id") or "").strip()
    if not proof_id:
        proof_id = (
            "proof:selected:"
            + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:32]
        )
    created_ts = float(now)
    expires_at_ts = float(
        context.get("expires_at_ts")
        or getattr(action_template, "expires_at_ts", 0.0)
        or created_ts + max(1.0, float(context.get("expires_after_s", 300.0) or 300.0))
    )
    value = SelectedActionContract(
        command_id=command_id,
        proof_id=proof_id,
        lever=lever.name,
        params=_freeze(action_params),
        reason=str(
            context.get("reason")
            or (
                _optimization_runtime_reason(plan, lever=lever)
                if plan
                else ""
            )
            or getattr(action_template, "reason", "")
            or selected.get("reason")
            or selected.get("action")
            or lever.name
        ),
        intent=str(
            context.get("intent")
            or ("smart_loop_candidate" if plan else "")
            or getattr(action_template, "intent", "")
            or evidence.get("intent")
            or selected.get("action")
            or lever.name.lower()
        ),
        objective_id=str(
            context.get("objective_id")
            or (
                _optimization_runtime_objective(plan)
                if plan
                else ""
            )
            or getattr(action_template, "objective_id", "")
            or evidence.get("objective_id")
            or f"selected_opportunity:{opportunity_id}"
        ),
        source=str(
            context.get("source")
            or (OPTIMIZATION_CONTRACT_OWNER if plan else "")
            or getattr(action_template, "source", "")
            or selected.get("source")
            or OWNER
        ),
        owner_class=owner_class,
        created_ts=created_ts,
        expires_at_ts=expires_at_ts,
        opportunity_set_id=opportunity_set_id,
        opportunity_id=opportunity_id,
        decision_id=str(brain_row.get("decision_id") or ""),
        decision_receipt_id=str(brain_row.get("decision_receipt_id") or ""),
        brain_decision_receipt=_freeze(brain_row),
        contextual_bandit_selection_receipt=_freeze(bandit_row),
        parameter_metadata=_freeze(dict(parameter_metadata or {})),
        action_attributes=_freeze(attributes),
        optimization_metadata=_freeze(
            {
                "reason_ref": str(plan.get("reason_ref") or ""),
                "reason_code": str(plan.get("reason_code") or ""),
                "availability_intent": str(
                    plan.get("availability_intent") or ""
                ),
                "context_key": str(
                    plan.get("context_key")
                    or set_row.get("learning_context_key")
                    or ""
                ),
                "lever": str(plan.get("lever") or lever.name),
                "target_shape": dict(plan.get("target_shape") or {}),
                "objective_key": str(plan.get("objective_key") or ""),
            }
            if plan
            else {}
        ),
    )
    _validate_selected_action(value)
    return value


def _validate_selected_action(value: SelectedActionContract) -> None:
    for name in (
        "command_id",
        "proof_id",
        "lever",
        "source",
        "owner_class",
        "opportunity_set_id",
        "opportunity_id",
        "decision_id",
        "decision_receipt_id",
    ):
        if not str(getattr(value, name) or "").strip():
            raise ValueError(f"selected_action_{name}_missing")
    if value.owner_class not in _OWNER_CLASSES:
        raise ValueError("selected_action_owner_class_invalid")
    if value.created_ts <= 0.0 or value.expires_at_ts <= value.created_ts:
        raise ValueError("selected_action_expiry_invalid")
    try:
        lever = LeverType[value.lever]
    except KeyError as exc:
        raise ValueError("selected_action_lever_invalid") from exc
    verdict = validate_action_shape(lever, value.params_dict())
    if not verdict.legal:
        raise ValueError(
            "selected_action_shape_invalid:"
            + str(verdict.code or verdict.reason or "invalid")
        )
    brain_row = value.brain_receipt_dict()
    error = brain_decision_receipt_error(
        brain_row,
        expected_opportunity_set_id=value.opportunity_set_id,
        expected_opportunity_id=value.opportunity_id,
    )
    if error:
        raise ValueError(f"selected_action_brain_receipt_invalid:{error}")
    if brain_row.get("do_nothing") is True:
        raise ValueError("selected_action_do_nothing_cannot_materialize")
    selected = dict(brain_row.get("selected_opportunity") or {})
    if str(selected.get("lever") or "") != value.lever:
        raise ValueError("selected_action_selected_lever_mismatch")
    selected_params = dict(selected.get("params") or {})
    target = build_core_command_target(lever.name, value.params_dict())
    selected_target = build_core_command_target(lever.name, selected_params)
    if target != selected_target:
        raise ValueError("selected_action_selected_params_mismatch")
    bandit_row = value.bandit_receipt_dict()
    if value.optimization:
        error = contextual_bandit_selection_receipt_error(
            bandit_row,
            expected_learning_context_key=str(
                brain_row.get("learning_context_key") or ""
            ),
            expected_opportunity_set_id=value.opportunity_set_id,
        )
        if error:
            raise ValueError(f"selected_action_bandit_receipt_invalid:{error}")
        if str(bandit_row.get("selected_opportunity_id") or "") != value.opportunity_id:
            raise ValueError("selected_action_bandit_selection_mismatch")
        optimization = dict(_thaw(value.optimization_metadata))
        if str(optimization.get("lever") or "") != value.lever:
            raise ValueError("selected_action_optimization_lever_mismatch")
        if _freeze(dict(optimization.get("target_shape") or {})) != _freeze(
            value.params_dict()
        ):
            raise ValueError("selected_action_optimization_shape_mismatch")
        if not str(optimization.get("objective_key") or ""):
            raise ValueError("selected_action_optimization_objective_missing")


def _validate_optimization_plan(
    plan: Mapping[str, Any],
    *,
    lever: LeverType,
    selected_params: Mapping[str, Any],
) -> OptimizationActionPlan:
    value = OptimizationActionPlan.from_mapping(plan)
    if value.lever != lever.name:
        raise ValueError("selected_action_optimization_lever_mismatch")
    if _freeze(value.target_shape_dict()) != _freeze(dict(selected_params or {})):
        raise ValueError("selected_action_optimization_shape_mismatch")
    return value


def _optimization_runtime_reason(
    plan: Mapping[str, Any],
    *,
    lever: LeverType,
) -> str:
    reason_ref = str(plan.get("reason_ref") or "")
    return (
        f"SmartLoop selected {lever.name} target "
        f"{dict(plan.get('target_shape') or {})}; reason_ref={reason_ref}"
    )


def _optimization_runtime_objective(plan: Mapping[str, Any]) -> str:
    return str(plan.get("objective_key") or "")


__all__ = (
    "SelectedActionContract",
    "OWNER",
    "SCHEMA",
    "materialize_selected_action",
)
