"""Immutable identity shared by one cellular action from admission to reward.

The key is description, not mutable lifecycle state.  Owners may append their
own receipts, but every receipt must carry this exact key and root id.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

from arch4.action_selection_receipts import (
    brain_decision_receipt_error,
    contextual_bandit_selection_receipt_error,
)
from arch4.interfaces import LeverAction
from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreCommandTarget,
    core_command_target_from_mapping,
    core_command_target_from_payload,
    core_command_target_payload,
)
from arch4.action_origin import (
    is_human_operator_origin,
    require_action_origin_name,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)


SCHEMA = "action_transaction_key.v4"
OWNER = "arch4.action_transaction"
ACTION_KEY_PARAM = "_action_transaction_key"
CORE_TARGET_PARAM = "_core_command_target"
STANDARD_REWARD_HORIZON_S = 600
EMERGENCY_RESTART_REWARD_HORIZON_S = 90
_MAP_TAG = "__action_transaction_mapping__"
_SEQUENCE_TAG = "__action_transaction_sequence__"
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
        if len(value) == 2 and value[0] == _MAP_TAG and isinstance(value[1], tuple):
            return {item[0]: _thaw(item[1]) for item in value[1]}
        if (
            len(value) == 2
            and value[0] == _SEQUENCE_TAG
            and isinstance(value[1], tuple)
        ):
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


@dataclass(frozen=True)
class ActionTransactionKey:
    root_id: str
    command_id: str
    proof_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_key: str
    source: str
    origin_kind: str
    correlation_id: str
    correlation_source: str
    lever: str
    target: CoreCommandTarget
    opportunity_id: str = ""
    opportunity_set_id: str = ""
    decision_id: str = ""
    decision_receipt_id: str = ""
    bandit_selection_receipt_id: str = ""
    brain_decision_receipt: tuple[tuple[str, Any], ...] = ()
    bandit_selection_receipt: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "root_id",
            "command_id",
            "proof_id",
            "identity_key",
            "source",
            "origin_kind",
            "correlation_id",
            "correlation_source",
            "lever",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"action_transaction_{name}_missing")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("action_transaction_matrix_identity_required")
        require_action_origin_name(self.origin_kind)
        if not isinstance(self.target, CORE_COMMAND_TARGET_TYPES):
            raise TypeError("action_transaction_typed_target_required")
        if self.target.command_kind != self.lever:
            raise ValueError("action_transaction_target_lever_mismatch")
        brain_receipt_present = bool(_thaw(self.brain_decision_receipt))
        bandit_receipt_present = bool(_thaw(self.bandit_selection_receipt))
        bandit_identity_present = bool(
            self.opportunity_id
            or self.opportunity_set_id
            or self.decision_id
            or self.decision_receipt_id
            or self.bandit_selection_receipt_id
            or brain_receipt_present
            or bandit_receipt_present
        )
        if bandit_identity_present and not all(
            (
                self.opportunity_id,
                self.opportunity_set_id,
                self.decision_id,
                self.decision_receipt_id,
                brain_receipt_present,
            )
        ):
            raise ValueError("action_transaction_brain_decision_identity_incomplete")
        if bool(self.bandit_selection_receipt_id) != bandit_receipt_present:
            raise ValueError("action_transaction_bandit_identity_incomplete")

    @property
    def operator_initiated(self) -> bool:
        """Read projection derived only from the typed origin."""
        return is_human_operator_origin(self.origin_kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "root_id": self.root_id,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_key": self.identity_key,
            "source": self.source,
            "origin_kind": self.origin_kind,
            "correlation_id": self.correlation_id,
            "correlation_source": self.correlation_source,
            "lever": self.lever,
            "target": core_command_target_payload(self.target),
            "opportunity_id": self.opportunity_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "bandit_selection_receipt_id": self.bandit_selection_receipt_id,
            "brain_decision_receipt": dict(_thaw(self.brain_decision_receipt)),
            "bandit_selection_receipt": dict(_thaw(self.bandit_selection_receipt)),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ActionTransactionKey":
        row = dict(raw or {})
        expected = {
            "schema",
            "owner",
            "root_id",
            "command_id",
            "proof_id",
            "matrix_identity",
            "identity_key",
            "source",
            "origin_kind",
            "correlation_id",
            "correlation_source",
            "lever",
            "target",
            "opportunity_id",
            "opportunity_set_id",
            "decision_id",
            "decision_receipt_id",
            "bandit_selection_receipt_id",
            "brain_decision_receipt",
            "bandit_selection_receipt",
        }
        if set(row) != expected:
            raise ValueError("action_transaction_key_fields_invalid")
        if "manual" in row or "operator_initiated" in row:
            raise ValueError("action_transaction_boolean_origin_deleted")
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("action_transaction_key_contract_invalid")
        matrix_identity = row.get("matrix_identity")
        if not isinstance(matrix_identity, Mapping):
            raise TypeError("action_transaction_matrix_identity_mapping_required")
        for field in (
            "root_id",
            "command_id",
            "proof_id",
            "identity_key",
            "source",
            "origin_kind",
            "correlation_id",
            "correlation_source",
            "lever",
            "opportunity_id",
            "opportunity_set_id",
            "decision_id",
            "decision_receipt_id",
            "bandit_selection_receipt_id",
        ):
            if not isinstance(row.get(field), str):
                raise TypeError(f"action_transaction_{field}_invalid")
        if not isinstance(row.get("brain_decision_receipt"), Mapping):
            raise TypeError("action_transaction_brain_receipt_invalid")
        if not isinstance(row.get("bandit_selection_receipt"), Mapping):
            raise TypeError("action_transaction_bandit_receipt_invalid")
        return cls(
            root_id=row["root_id"],
            command_id=row["command_id"],
            proof_id=row["proof_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                matrix_identity
            ),
            identity_key=row["identity_key"],
            source=row["source"],
            origin_kind=row["origin_kind"],
            correlation_id=row["correlation_id"],
            correlation_source=row["correlation_source"],
            lever=row["lever"],
            target=core_command_target_from_payload(row.get("target")),
            opportunity_id=row["opportunity_id"],
            opportunity_set_id=row["opportunity_set_id"],
            decision_id=row["decision_id"],
            decision_receipt_id=row["decision_receipt_id"],
            bandit_selection_receipt_id=row["bandit_selection_receipt_id"],
            brain_decision_receipt=_freeze(
                dict(row.get("brain_decision_receipt") or {})
            ),
            bandit_selection_receipt=_freeze(
                dict(row.get("bandit_selection_receipt") or {})
            ),
        )


def target_shape_from_action(action: LeverAction) -> dict[str, Any]:
    raise RuntimeError(
        "action_target_reconstruction_illegal_use_bound_core_target"
    )


def bind_core_command_target(
    action: LeverAction,
    target: CoreCommandTarget,
) -> LeverAction:
    """Bind the exact typed target once before semantic selection/admission."""

    if not isinstance(action, LeverAction):
        raise TypeError("core_command_target_binding_requires_lever_action")
    if not isinstance(target, CORE_COMMAND_TARGET_TYPES):
        raise TypeError("core_command_typed_target_required")
    if target.command_kind != action.lever.name:
        raise ValueError("core_command_target_binding_lever_mismatch")
    params = dict(action.params or {})
    payload = core_command_target_payload(target)
    existing = params.get(CORE_TARGET_PARAM)
    if existing is not None:
        retained = core_command_target_from_payload(existing)
        if retained != target:
            raise RuntimeError("core_command_target_binding_conflict")
        return action
    projected = target.to_mapping()
    projected.pop("lever", None)
    for key, expected in projected.items():
        if key in params and _freeze(params[key]) != _freeze(expected):
            raise RuntimeError(
                "core_command_target_binding_projection_conflict:" + key
            )
        params[key] = expected
    params[CORE_TARGET_PARAM] = payload
    action.params = params
    return action


def bound_core_command_target_from_action(
    action: LeverAction,
) -> CoreCommandTarget:
    """Read the one typed target; target reconstruction is prohibited."""

    if not isinstance(action, LeverAction):
        raise TypeError("bound_core_command_target_requires_lever_action")
    target = core_command_target_from_payload(
        dict(action.params or {}).get(CORE_TARGET_PARAM)
    )
    if target.command_kind != action.lever.name:
        raise ValueError("action_transaction_bound_target_lever_mismatch")
    params = dict(action.params or {})
    projected = target.to_mapping()
    projected.pop("lever", None)
    forbidden_aliases: set[str] = set()
    if target.command_kind in {"BAND_SELECTION", "BAND_LOCK"}:
        forbidden_aliases.add("resolved_target_bands")
    elif target.command_kind == "PBAND":
        forbidden_aliases.update({"dl_bands", "bands", "nr_band"})
    elif target.command_kind == "NR_BAND_LOCK":
        forbidden_aliases.add("bands")
    present_aliases = forbidden_aliases.intersection(params)
    if present_aliases:
        raise RuntimeError(
            "action_alternate_target_projection_forbidden:"
            + ",".join(sorted(present_aliases))
        )
    for key, expected in projected.items():
        if key not in params or _freeze(params.get(key)) != _freeze(expected):
            raise RuntimeError(
                "action_bound_target_projection_mismatch:" + key
            )
    return target


def build_action_transaction_key(
    action: LeverAction,
    *,
    matrix_identity: MatrixGenerationIdentity,
    identity_key: str,
    source: str,
    origin_kind: str,
) -> ActionTransactionKey:
    command_id = str(getattr(action, "command_id", "") or "").strip()
    if not command_id:
        raise ValueError("action_transaction_command_id_missing")
    if not isinstance(matrix_identity, MatrixGenerationIdentity):
        raise TypeError("action_transaction_matrix_identity_required")
    params = dict(getattr(action, "params", {}) or {})
    target = bound_core_command_target_from_action(action)
    target_shape = target.to_mapping()
    if target.command_kind != action.lever.name:
        raise ValueError("action_transaction_bound_target_lever_mismatch")
    selection_identity = _decision_selection_identity(params)
    _validate_selected_opportunity_materialization(
        action=action,
        target_shape=target_shape,
        selection_identity=selection_identity,
    )
    material = {
        "command_id": command_id,
        "matrix_identity": matrix_identity.as_dict(),
        "identity_key": str(identity_key or ""),
        "source": str(source or ""),
        "origin_kind": str(origin_kind or ""),
        "correlation_id": str(
            getattr(action, "_gui_action_id", "") or command_id
        ),
        "correlation_source": str(
            getattr(action, "_gui_action_source", "")
            or getattr(action, "source", "")
            or source
        ),
        "lever": action.lever.name,
        "target_shape": target_shape,
        **selection_identity,
    }
    encoded = canonical_json_bytes(material)
    root_id = f"action:{hashlib.sha256(encoded).hexdigest()[:32]}"
    proof_id = (
        str(params.get("_selected_action_proof_id") or "").strip()
        or f"proof:{root_id}"
    )
    return ActionTransactionKey(
        root_id=root_id,
        command_id=command_id,
        proof_id=proof_id,
        matrix_identity=matrix_identity,
        identity_key=str(identity_key or ""),
        source=str(source or ""),
        origin_kind=str(origin_kind or ""),
        correlation_id=str(
            getattr(action, "_gui_action_id", "") or command_id
        ),
        correlation_source=str(
            getattr(action, "_gui_action_source", "")
            or getattr(action, "source", "")
            or source
        ),
        lever=action.lever.name,
        target=target,
        opportunity_id=str(selection_identity["opportunity_id"]),
        opportunity_set_id=str(selection_identity["opportunity_set_id"]),
        decision_id=str(selection_identity["decision_id"]),
        decision_receipt_id=str(selection_identity["decision_receipt_id"]),
        bandit_selection_receipt_id=str(
            selection_identity["bandit_selection_receipt_id"]
        ),
        brain_decision_receipt=_freeze(selection_identity["brain_decision_receipt"]),
        bandit_selection_receipt=_freeze(
            selection_identity["bandit_selection_receipt"]
        ),
    )


def _decision_selection_identity(params: Mapping[str, Any]) -> dict[str, Any]:
    decision_raw = params.get("_brain_decision_receipt")
    if not isinstance(decision_raw, Mapping):
        decision_raw = params.get("brain_decision_receipt")
    decision = dict(decision_raw) if isinstance(decision_raw, Mapping) else {}
    selected_owner = str(decision.get("selected_owner_class") or "")
    receipt_raw = params.get("_contextual_bandit_selection_receipt")
    if not isinstance(receipt_raw, Mapping):
        receipt_raw = params.get("contextual_bandit_selection_receipt")
    receipt = (
        dict(receipt_raw)
        if selected_owner == "optimization" and isinstance(receipt_raw, Mapping)
        else (
            dict(decision.get("bandit_receipt") or {})
            if selected_owner == "optimization"
            else {}
        )
    )
    explicit_opportunity = str(
        params.get("_opportunity_id") or params.get("opportunity_id") or ""
    )
    decision_opportunity = str(decision.get("selected_opportunity_id") or "")
    selected_opportunity = str(receipt.get("selected_opportunity_id") or "")
    opportunity_id = explicit_opportunity or decision_opportunity or selected_opportunity
    decision_id = str(decision.get("decision_id") or params.get("_brain_decision_id") or "")
    opportunity_set_id = str(
        decision.get("opportunity_set_id")
        or params.get("_opportunity_set_id")
        or ""
    )
    decision_receipt_id = str(
        decision.get("decision_receipt_id")
        or params.get("_brain_decision_receipt_id")
        or ""
    )
    selection_receipt_id = str(
        receipt.get("selection_receipt_id")
        or params.get("_contextual_bandit_selection_receipt_id")
        or ""
    )
    if decision:
        error = brain_decision_receipt_error(
            decision,
            expected_opportunity_set_id=opportunity_set_id,
            expected_opportunity_id=opportunity_id,
        )
        if error:
            raise ValueError(f"action_transaction_{error}")
    if receipt:
        error = contextual_bandit_selection_receipt_error(
            receipt,
            expected_learning_context_key=str(
                decision.get("learning_context_key") or ""
            ),
            expected_opportunity_set_id=opportunity_set_id,
        )
        if error:
            raise ValueError(f"action_transaction_{error}")
    if any(
        value and value != opportunity_id
        for value in (explicit_opportunity, decision_opportunity, selected_opportunity)
    ):
        raise ValueError("action_transaction_opportunity_selection_mismatch")
    if selected_owner == "optimization" and not receipt:
        raise ValueError("action_transaction_optimization_bandit_receipt_missing")
    if opportunity_id or decision or receipt:
        if (
            not opportunity_id
            or not opportunity_set_id
            or not decision
            or not decision_id
            or not decision_receipt_id
        ):
            raise ValueError("action_transaction_brain_decision_identity_incomplete")
        if receipt and not selection_receipt_id:
            raise ValueError("action_transaction_bandit_identity_incomplete")
    return {
        "opportunity_id": opportunity_id,
        "opportunity_set_id": opportunity_set_id,
        "decision_id": decision_id,
        "decision_receipt_id": decision_receipt_id,
        "bandit_selection_receipt_id": selection_receipt_id,
        "brain_decision_receipt": decision,
        "bandit_selection_receipt": receipt,
    }


def _validate_selected_opportunity_materialization(
    *,
    action: LeverAction,
    target_shape: Mapping[str, Any],
    selection_identity: Mapping[str, Any],
) -> None:
    decision = dict(selection_identity.get("brain_decision_receipt") or {})
    if not decision:
        return
    if decision.get("do_nothing") is True:
        raise ValueError("action_transaction_do_nothing_cannot_materialize_mutation")
    selected = dict(decision.get("selected_opportunity") or {})
    if str(selected.get("lever") or "") != action.lever.name:
        raise ValueError("action_transaction_selected_lever_mismatch")
    selected_params = dict(selected.get("params") or {})
    for key, expected in selected_params.items():
        if key not in target_shape or _freeze(target_shape.get(key)) != _freeze(expected):
            raise ValueError("action_transaction_selected_params_mismatch")


def bind_action_transaction_key(
    action: LeverAction, key: ActionTransactionKey
) -> LeverAction:
    if key.command_id != str(getattr(action, "command_id", "") or ""):
        raise ValueError("action_transaction_command_mismatch")
    if key.lever != action.lever.name:
        raise ValueError("action_transaction_lever_mismatch")
    params = dict(getattr(action, "params", {}) or {})
    existing = params.get(ACTION_KEY_PARAM)
    if existing is not None:
        retained = ActionTransactionKey.from_mapping(dict(existing or {}))
        if retained != key:
            raise ValueError("action_transaction_key_rebind")
        return action
    params[ACTION_KEY_PARAM] = key.as_dict()
    action.params = params
    return action


def action_transaction_key_from_action(action: Any) -> ActionTransactionKey | None:
    if not isinstance(action, LeverAction):
        return None
    params = getattr(action, "params", {}) or {}
    raw = params.get(ACTION_KEY_PARAM) if isinstance(params, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    return ActionTransactionKey.from_mapping(raw)


def terminal_reward_horizon_s(key: ActionTransactionKey) -> int:
    """Return the only terminal reward horizon legal for this action shape."""

    target = key.target.to_mapping()
    emergency_restart = bool(
        key.lever == "RESTART" and target.get("emergency_fast") is True
    )
    return (
        EMERGENCY_RESTART_REWARD_HORIZON_S
        if emergency_restart
        else STANDARD_REWARD_HORIZON_S
    )


__all__ = [
    "ACTION_KEY_PARAM",
    "CORE_TARGET_PARAM",
    "EMERGENCY_RESTART_REWARD_HORIZON_S",
    "STANDARD_REWARD_HORIZON_S",
    "ActionTransactionKey",
    "action_transaction_key_from_action",
    "bind_core_command_target",
    "bind_action_transaction_key",
    "bound_core_command_target_from_action",
    "build_action_transaction_key",
    "target_shape_from_action",
    "terminal_reward_horizon_s",
]
from arch4.canonical_json import canonical_json_bytes
