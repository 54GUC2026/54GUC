"""Canonical final-selection adapter for autonomous cellular proposals.

Only SmartLoop and fault-recovery intelligence may enter this boundary.  Core
operator, SMS, schedule, watchdog and lease-expiry commands enter through
``CoreCommandGateway`` and fail loudly here.  This boundary turns an autonomous
proposal plus mandatory do-nothing into one ``ActionOpportunitySet`` and
requires ``BrainDecisionReceipt`` before the action may reach Core Bouncer or
mechanical admission.  Optimization proposals additionally require the durable
Contextual Bandit owner.

The adapter does not execute, grade, acquire a lease, inspect Modem Live, or
change the proposed action shape.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Any, Mapping

from arch4.action_origin import ActionOriginKind
from arch4.action_admission import (
    SelectedActionContext,
    action_descriptor,
    bind_selected_action_context,
)
from arch4.action_opportunity_set import build_action_opportunity_set
from arch4.action_opportunity_sources import source_snapshot_from_accepted_frame
from arch4.action_selection_receipts import brain_decision_receipt_error
from arch4.action_session_scope import (
    current_action_context_key,
    current_session_action_scope_from_generation,
)
from arch4.action_transaction import (
    bind_core_command_target,
    bound_core_command_target_from_action,
)
from arch4.core.command_request_builder import build_core_command_target
from arch4.action_transaction_authority import (
    require_action_transaction_authority,
)
from arch4.customer_contracts.selected_action import (
    materialize_selected_action,
)
from arch4.customer_contracts.optimization_action import (
    build_optimization_action_plan,
)
from arch4.brain_decision_authority import select_brain_decision
from arch4.contextual_bandit import select_contextual_bandit_candidate
from arch4.contextual_bandit_state_store import ContextualBanditStateStore
from arch4.evidence_scope import FormulaVector, evidence_scope_from_mapping
from arch4.interfaces import LeverAction


OWNER = "arch4.action_selection_boundary"
SCHEMA = "CanonicalActionSelection.v1"
_FORMULA_VECTOR = FormulaVector.build(
    {"action_final_selection": "canonical_immediate_proposal_v1"}
)


@dataclass(frozen=True, slots=True)
class CanonicalActionSelection:
    action: LeverAction | None
    opportunity_set: Mapping[str, Any]
    brain_decision_receipt: Mapping[str, Any]
    bandit_selection_receipt: Mapping[str, Any]
    reason: str
    context: SelectedActionContext | None = None

    @property
    def selected(self) -> bool:
        return self.action is not None

    def as_dict(self) -> dict[str, Any]:
        opportunity_set_id = str(
            self.opportunity_set.get("opportunity_set_id")
            or self.brain_decision_receipt.get("opportunity_set_id")
            or ""
        )
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "selected": self.selected,
            "reason": str(self.reason or ""),
            "opportunity_set_id": opportunity_set_id,
            "opportunity_id": str(
                self.brain_decision_receipt.get("selected_opportunity_id") or ""
            ),
            "decision_id": str(
                self.brain_decision_receipt.get("decision_id") or ""
            ),
            "decision_receipt_id": str(
                self.brain_decision_receipt.get("decision_receipt_id") or ""
            ),
            "bandit_selection_receipt_id": str(
                self.bandit_selection_receipt.get("selection_receipt_id") or ""
            ),
        }


def select_immediate_action(
    *,
    engine: Any,
    action: LeverAction,
    origin: ActionOriginKind,
    accepted_frame: Any,
    now: float,
) -> CanonicalActionSelection:
    """Return the one Brain-selected immediate action, or explicit do-nothing."""

    if origin not in {
        ActionOriginKind.SMART_LOOP,
        ActionOriginKind.FAULT_RECOVERY,
    }:
        raise RuntimeError(
            "non_autonomous_cellular_selection_requires_core_command_gateway"
        )

    bind_core_command_target(
        action,
        build_core_command_target(action.lever.name, action.params),
    )
    existing = _existing_selection(action)
    if existing is not None:
        context = bind_selected_action_context(
            action,
            origin_kind=origin,
            selection_evidence={
                "opportunity_set": dict(existing.opportunity_set),
                "brain_decision_receipt": dict(existing.brain_decision_receipt),
                "bandit_selection_receipt": dict(existing.bandit_selection_receipt),
            },
            opportunity_id=str(
                existing.brain_decision_receipt.get("selected_opportunity_id") or ""
            ),
            now=now,
        )
        _record_selected_lifecycle(
            engine=engine,
            action=action,
            context=context,
            opportunity_set=existing.opportunity_set,
            brain_receipt=existing.brain_decision_receipt,
            bandit_receipt=existing.bandit_selection_receipt,
            now=now,
        )
        return replace(existing, context=context)

    target_shape = bound_core_command_target_from_action(action).to_mapping()
    params = {key: value for key, value in target_shape.items() if key != "lever"}
    scope = _accepted_scope(accepted_frame, now=now)
    action_context_key = _context_key(accepted_frame, scope=scope)
    owner_class, evidence, schedule = _origin_evidence(
        origin,
        action=action,
        now=now,
    )
    source_snapshot = source_snapshot_from_accepted_frame(
        engine=engine,
        accepted_frame=accepted_frame,
        requested_actions=(
            {
                "lever": action.lever.name,
                "action": str(action.intent or action.lever.name.lower()),
                "params": params,
                "owner_class": owner_class,
                "source": str(
                    getattr(action, "source", "") or origin.value or OWNER
                ),
                "reason": str(action.reason or action.intent or action.lever.name),
                "disruption_cost": _disruption_cost(action),
                "uncertainty": 0.0 if owner_class != "optimization" else 0.5,
                "evidence": evidence,
                "schedule": schedule,
            },
        ),
    )
    opportunity_set = build_action_opportunity_set(
        learning_context_key=action_context_key,
        action_context_key=action_context_key,
        evidence_scope=scope,
        source_snapshot=source_snapshot,
    )
    bandit_receipt: dict[str, Any] = {}
    if owner_class == "optimization":
        store = getattr(
            getattr(getattr(engine, "controller", None), "brain", None),
            "contextual_bandit_state_store",
            None,
        )
        if isinstance(store, ContextualBanditStateStore):
            bandit_receipt = select_contextual_bandit_candidate(
                opportunity_set=opportunity_set,
                state=store.state(),
                decision_seed=(
                    "immediate_action_opportunity_set:"
                    + str(opportunity_set.get("opportunity_set_id") or "")
                ),
                future_context={},
            )
    decision_id = "brain:" + hashlib.sha256(
        (
            str(opportunity_set.get("opportunity_set_id") or "")
            + "|"
            + str(getattr(action, "command_id", "") or "")
            + "|"
            + str(bandit_receipt.get("selection_receipt_id") or "")
        ).encode("utf-8")
    ).hexdigest()[:32]
    brain_receipt = select_brain_decision(
        opportunity_set=opportunity_set,
        bandit_receipt=bandit_receipt or None,
        decision_id=decision_id,
        decision_ts=now,
    )
    error = brain_decision_receipt_error(
        brain_receipt,
        expected_opportunity_set_id=str(
            opportunity_set.get("opportunity_set_id") or ""
        ),
        expected_opportunity_id=str(
            brain_receipt.get("selected_opportunity_id") or ""
        ),
    )
    if error:
        return CanonicalActionSelection(
            None,
            opportunity_set,
            brain_receipt,
            bandit_receipt,
            f"brain_decision_invalid:{error}",
        )
    if brain_receipt.get("do_nothing") is True:
        return CanonicalActionSelection(
            None,
            opportunity_set,
            brain_receipt,
            bandit_receipt,
            _do_nothing_reason(opportunity_set, brain_receipt),
        )
    selected = dict(brain_receipt.get("selected_opportunity") or {})
    if str(selected.get("lever") or "") != action.lever.name or dict(
        selected.get("params") or {}
    ) != params:
        return CanonicalActionSelection(
            None,
            opportunity_set,
            brain_receipt,
            bandit_receipt,
            "selected_action_shape_mismatch",
        )
    materialized = materialize_selected_action(
        opportunity_set=opportunity_set,
        brain_decision_receipt=brain_receipt,
        contextual_bandit_selection_receipt=bandit_receipt,
        now=now,
        action_template=action,
        source_context=(
            {"intent": "manual"}
            if owner_class == "operator"
            and not str(action.intent or "").strip()
            else None
        ),
        optimization_plan=(
            build_optimization_action_plan(
                opportunity_set=opportunity_set,
                brain_decision_receipt=brain_receipt,
                contextual_bandit_selection_receipt=bandit_receipt,
                context=accepted_frame,
                now=now,
            ).as_dict()
            if owner_class == "optimization"
            else None
        ),
    )
    selected_action = materialized.to_lever_action()
    context = bind_selected_action_context(
        selected_action,
        origin_kind=origin,
        selection_evidence={
            "opportunity_set": dict(opportunity_set),
            "brain_decision_receipt": dict(brain_receipt),
            "bandit_selection_receipt": dict(bandit_receipt),
        },
        opportunity_id=str(brain_receipt.get("selected_opportunity_id") or ""),
        now=now,
    )
    _record_selected_lifecycle(
        engine=engine,
        action=selected_action,
        context=context,
        opportunity_set=opportunity_set,
        brain_receipt=brain_receipt,
        bandit_receipt=bandit_receipt,
        now=now,
    )
    return CanonicalActionSelection(
        selected_action,
        opportunity_set,
        brain_receipt,
        bandit_receipt,
        "selected_action",
        context,
    )


def _record_selected_lifecycle(
    *,
    engine: Any,
    action: LeverAction,
    context: SelectedActionContext,
    opportunity_set: Mapping[str, Any],
    brain_receipt: Mapping[str, Any],
    bandit_receipt: Mapping[str, Any],
    now: float,
) -> None:
    """Project canonical selection into the sole lifecycle owner."""

    require_action_transaction_authority(engine).record_selected(
        command_id=context.origin.command_id,
        origin_kind=context.origin.kind.value,
        action_kind=action.lever.name,
        selection_receipt={
            "owner": OWNER,
            "opportunity_set_id": str(
                opportunity_set.get("opportunity_set_id") or ""
            ),
            "opportunity_id": str(
                brain_receipt.get("selected_opportunity_id") or ""
            ),
            "decision_id": str(brain_receipt.get("decision_id") or ""),
            "decision_receipt_id": str(
                brain_receipt.get("decision_receipt_id") or ""
            ),
            "bandit_selection_receipt_id": str(
                bandit_receipt.get("selection_receipt_id") or ""
            ),
            "learning_context_key": str(
                opportunity_set.get("learning_context_key")
                or brain_receipt.get("learning_context_key")
                or ""
            ),
            "action_context_key": str(
                opportunity_set.get("action_context_key")
                or brain_receipt.get("action_context_key")
                or ""
            ),
            "identity_key": str(
                opportunity_set.get("identity_key")
                or opportunity_set.get("identity_code")
                or ""
            ),
            "session_scope_key": str(
                opportunity_set.get("session_scope_key") or ""
            ),
            "source_epoch": str(opportunity_set.get("source_epoch") or ""),
            "source_sequence": int(
                opportunity_set.get("source_sequence") or 0
            ),
        },
        now=float(now),
    )


def _existing_selection(action: LeverAction) -> CanonicalActionSelection | None:
    params = dict(action.params or {})
    receipt = params.get("_brain_decision_receipt")
    if not isinstance(receipt, Mapping):
        return None
    brain_receipt = dict(receipt)
    error = brain_decision_receipt_error(
        brain_receipt,
        expected_opportunity_set_id=str(params.get("_opportunity_set_id") or ""),
        expected_opportunity_id=str(params.get("_opportunity_id") or ""),
    )
    selected = dict(brain_receipt.get("selected_opportunity") or {})
    target = bound_core_command_target_from_action(action).to_mapping()
    expected = {key: value for key, value in target.items() if key != "lever"}
    if (
        error
        or brain_receipt.get("do_nothing") is True
        or str(selected.get("lever") or "") != action.lever.name
        or not _selected_params_match(
            dict(selected.get("params") or {}), expected
        )
    ):
        return CanonicalActionSelection(
            None,
            {},
            brain_receipt,
            {},
            f"existing_brain_decision_invalid:{error or 'action_shape_mismatch'}",
        )
    bandit = params.get("_contextual_bandit_selection_receipt")
    return CanonicalActionSelection(
        action,
        {},
        brain_receipt,
        dict(bandit) if isinstance(bandit, Mapping) else {},
        "existing_brain_decision_valid",
    )


def _accepted_scope(frame: Any, *, now: float) -> dict[str, Any]:
    observed_at = _float(
        getattr(frame.generation, 'sample_ts', 0.0) or getattr(frame.generation, 'accepted_ts', 0.0),
        0.0,
    )
    raw = {
        "identity_code": str(getattr(frame.identity, 'identity_code', "") or ""),
        "identity_status": str(getattr(frame.identity, 'identity_status', "") or ""),
        "identity_catalog_version": str(
            getattr(frame.identity, 'identity_catalog_version', "") or ""
        ),
        "identity_catalog_checksum": str(
            getattr(frame.identity, 'identity_catalog_checksum', "") or ""
        ),
        "session_scope_key": str(
            getattr(frame.generation, 'session_scope_key', "") or ""
        ),
        "source_epoch": str(getattr(frame.generation, 'source_epoch', "") or ""),
        "source_sequence": _int(getattr(frame.generation, 'generation', 0), 0),
        "sample_ts": observed_at,
        "sample_age_s": max(0.0, float(now) - observed_at)
        if observed_at > 0.0
        else 0.0,
        "confidence": 1.0,
        "evidence_count": 1,
        "provider_plmn": str(getattr(frame.identity, 'provider_plmn', "") or ""),
        "serving_band": _int(getattr(frame.cellular, 'serving_band', 0), 0),
    }
    try:
        return evidence_scope_from_mapping(
            raw,
            formula_vector=_FORMULA_VECTOR,
            observed_at=observed_at if observed_at > 0.0 else None,
        ).as_dict()
    except (TypeError, ValueError):
        try:
            return current_session_action_scope_from_generation(
                frame,
                now=now,
            ).as_dict()
        except (TypeError, ValueError):
            return {}


def _context_key(frame: Any, *, scope: Mapping[str, Any]) -> str:
    if not scope or not str(scope.get("session_scope_key") or ""):
        return "|".join(
            (
                "unadmitted_identity",
                str(getattr(frame.identity, "provider_plmn", "") or ""),
                str(_int(getattr(frame.cellular, "cell_id", 0), 0)),
                str(_int(getattr(frame.cellular, "pci", 0), 0)),
                str(_int(getattr(frame.cellular, "earfcn_dl", 0), 0)),
                "unadmitted_session",
            )
        )
    session_scope = current_session_action_scope_from_generation(
        frame,
        now=float(
            getattr(frame.generation, "accepted_ts", 0.0)
            or getattr(frame.generation, "sample_ts", 0.0)
            or 0.0
        ),
    )
    if str(scope.get("identity_code") or "") != session_scope.identity_code:
        raise ValueError("action_selection_context_identity_scope_mismatch")
    if str(scope.get("session_scope_key") or "") != session_scope.session_scope_key:
        raise ValueError("action_selection_context_session_scope_mismatch")
    return current_action_context_key(frame, scope=session_scope)


def _origin_evidence(
    origin: ActionOriginKind,
    *,
    action: LeverAction | None,
    now: float,
    command_id: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    command_id = str(
        command_id or getattr(action, "command_id", "") or ""
    )
    if origin is ActionOriginKind.FAULT_RECOVERY:
        return (
            "recovery",
            {
                "packages": [
                    {
                        "required": True,
                        "origin": origin.value,
                        "command_id": command_id,
                    }
                ]
            },
            {},
        )
    if origin is not ActionOriginKind.SMART_LOOP:
        raise RuntimeError(
            "non_autonomous_cellular_selection_requires_core_command_gateway"
        )
    return (
        "optimization",
        {
            "confidence": 0.5,
            "evidence_count": 1,
            "expected_referee_grade": 0.0,
            "origin": origin.value,
            "command_id": command_id,
        },
        {},
    )


def _disruption_cost(action: LeverAction) -> float:
    descriptor = action_descriptor(action.lever)
    return max(0.0, min(1.0, float(descriptor.settling_s or 0.0) / 120.0))


def _do_nothing_reason(
    opportunity_set: Mapping[str, Any], receipt: Mapping[str, Any]
) -> str:
    for opportunity in list(opportunity_set.get("opportunities") or []):
        if not isinstance(opportunity, Mapping) or opportunity.get("action") == "do_nothing":
            continue
        reasons = [str(value) for value in list(opportunity.get("availability_reasons") or []) if str(value)]
        if reasons:
            return "brain_selected_do_nothing:" + reasons[0]
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        for considered in list(receipt.get("considered") or []):
            if (
                isinstance(considered, Mapping)
                and str(considered.get("opportunity_id") or "") == opportunity_id
                and str(considered.get("reason") or "")
            ):
                return "brain_selected_do_nothing:" + str(considered.get("reason"))
    return "brain_selected_do_nothing"


def _selected_params_match(
    selected: Mapping[str, Any], actual: Mapping[str, Any]
) -> bool:
    return all(key in actual and actual.get(key) == value for key, value in selected.items())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "CanonicalActionSelection",
    "OWNER",
    "SCHEMA",
    "select_immediate_action",
]
