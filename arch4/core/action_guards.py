"""The only Core Bouncer receipt consumption and action-admission boundary."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

from arch4.core_contracts.accepted_runtime_view import (
    CORE_ACTION_READBACK_TO_CAPABILITY,
    CoreAcceptedRuntimeView,
)
from arch4.action_admission import (
    HUMAN_OPERATOR_ORIGINS,
    ActionAdmission,
    ActionAdmissionContext,
    ActionAdmissionDecision,
    ActionExecutionBoundary,
    authorize_admitted_action,
    current_engine_identity,
    execution_lease_active,
    selected_action_context_from_action,
)
from arch4.interfaces import LeverAction
from arch4.action_origin import ActionOriginKind
from arch4.core_contracts.command_safety import CoreCommandOpinion
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyRestartPermit,
)
from arch4.core.action_source_state_authority import (
    require_action_source_state_authority,
)
from arch4.action_transaction import (
    ActionTransactionKey,
    action_transaction_key_from_action,
    bind_action_transaction_key,
    build_action_transaction_key,
)


log = logging.getLogger(__name__)


def prepare_core_action_root(
    *,
    engine: Any,
    action: LeverAction,
    accepted_view: CoreAcceptedRuntimeView,
    now: float | None = None,
) -> ActionTransactionKey:
    """Bind the immutable action root before Core admission."""

    if not isinstance(accepted_view, CoreAcceptedRuntimeView):
        raise RuntimeError(
            "core_action_root_requires_accepted_runtime_view"
        )
    current = float(time.time() if now is None else now)
    identity = current_engine_identity(
        engine,
        now=current,
        accepted_view=accepted_view,
    )
    selected_context = selected_action_context_from_action(action)
    kind = selected_context.origin.kind
    source = str(
        getattr(action, "source", "")
        or dict(getattr(action, "params", {}) or {}).get("source")
        or kind.value
    )
    key = build_action_transaction_key(
        action,
        matrix_identity=accepted_view.matrix_identity,
        identity_key=str(identity.get("identity_key") or ""),
        source=source,
        origin_kind=selected_context.origin.kind.value,
    )
    bind_action_transaction_key(action, key)
    return key


def apply_canonical_action_admission(
    *,
    engine: Any,
    action: Optional[LeverAction],
    decision_trace_id: str,
    accepted_view: CoreAcceptedRuntimeView | None = None,
    command_safety_evidence: Any = None,
    autopilot_mode: bool,
    safety_checks_complete: bool = True,
    hard_stop_reason: str = "",
    lanes: Mapping[str, Any] | None = None,
    emergency_restart_permit: CoreEmergencyRestartPermit | None,
    now: float | None = None,
) -> tuple[Optional[LeverAction], bool, ActionAdmissionDecision]:
    """Consume one safety receipt and attach permission to execute."""

    current = float(time.time() if now is None else now)
    if type(autopilot_mode) is not bool:
        raise TypeError("action_admission_autopilot_mode_required")
    if action is None:
        decision = ActionAdmissionDecision(False, "no_action", None, None)
        return None, False, decision
    if not isinstance(accepted_view, CoreAcceptedRuntimeView):
        raise RuntimeError(
            "action_admission_requires_accepted_runtime_view"
        )
    identity = current_engine_identity(
        engine,
        now=current,
        accepted_view=accepted_view,
    )
    selected_context = selected_action_context_from_action(action)
    origin = selected_context.origin
    lease_active = execution_lease_active(engine)
    action_key = action_transaction_key_from_action(action)
    if not isinstance(action_key, ActionTransactionKey):
        raise RuntimeError("action_admission_requires_bound_action_key")
    if action_key.matrix_identity != accepted_view.matrix_identity:
        raise RuntimeError("action_admission_action_key_matrix_mismatch")
    bouncer = getattr(engine, "bouncer", None)
    band_policy_decision = require_action_source_state_authority(
        engine
    ).band_action_policy_decision(
        lever=action.lever.name,
        params=action.params,
        origin=origin.kind.value,
        intent=action.intent,
        now=current,
    )
    command_protection_reason = ""
    lifecycle_service = getattr(engine, "command_lifecycle", None)
    admission_status = getattr(lifecycle_service, "admission_status", None)
    if callable(admission_status):
        try:
            status = admission_status()
            if isinstance(status, Mapping) and status.get(
                "command_protection_active"
            ) is True:
                command_protection_reason = str(
                    status.get("command_protection_reason")
                    or status.get("reason")
                    or "command_protection_active"
                )
        except Exception as exc:
            log.error("Command protection status failed closed: %s", exc)
            command_protection_reason = "command_protection_status_unavailable"
    effective_hard_stop = str(hard_stop_reason or command_protection_reason or "")
    if (not accepted_view.cellular_automatic_actions_allowed
            and origin.kind not in HUMAN_OPERATOR_ORIGINS):
        effective_hard_stop = "mobile_data_off_or_unconfirmed"
    action_readback = accepted_view.action_readback.to_mapping()
    action_capabilities = action_readback.get("action_capabilities")
    action_capabilities = (
        action_capabilities if isinstance(action_capabilities, Mapping) else {}
    )
    actions = action_capabilities.get("actions")
    actions = actions if isinstance(actions, Mapping) else {}
    descriptor = selected_context.descriptor
    action_readback_name = next(
        (
            action_name
            for action_name, capability_key in CORE_ACTION_READBACK_TO_CAPABILITY
            if capability_key == descriptor.required_capability
        ),
        descriptor.required_capability,
    )
    capability_row = actions.get(action_readback_name)
    capability_row = capability_row if isinstance(capability_row, Mapping) else {}
    physical_capability_enabled = capability_row.get("enabled")
    if type(physical_capability_enabled) is not bool:
        physical_capability_enabled = None
    context = ActionAdmissionContext(
        now_ts=current,
        identity_key=str(identity.get("identity_key") or ""),
        identity_verified_at_ts=float(
            identity.get("verified_at_ts", 0.0) or 0.0
        ),
        identity_source=str(identity.get("identity_source") or ""),
        execution_boundary=ActionExecutionBoundary.CELLULAR_COMMAND,
        mutation_active=bool(
            float(getattr(engine, "_mutating_action_lock_until", 0.0) or 0.0)
            > current
        ),
        execution_lease_active=(
            bool(lease_active) if lease_active is not None else True
        ),
        profile_id=str(accepted_view.capability_facts.to_mapping().get("profile_id") or ""),
        physical_capability_enabled=physical_capability_enabled,
        physical_capability_reason=str(
            capability_row.get("reason")
            or f"action_capability_unavailable:{descriptor.required_capability}"
        ),
        band_policy_decision=band_policy_decision,
        hard_stop_reason=effective_hard_stop,
        safety_checks_complete=bool(
            safety_checks_complete and lease_active is not None
        ),
        command_safety_evidence=command_safety_evidence,
        matrix_identity=accepted_view.matrix_identity,
        emergency_restart_permit=emergency_restart_permit,
    )
    decision = ActionAdmission.evaluate(action, origin=origin, context=context)
    if autopilot_mode:
        evaluate_opinion = getattr(bouncer, "evaluate_core_opinion", None)
        if not callable(evaluate_opinion):
            raise RuntimeError("core_bouncer_opinion_port_unavailable")
        safety_state = str(
            getattr(command_safety_evidence, "modem_health_state", "") or ""
        )
        opinion = CoreCommandOpinion(
            request_id=action_key.command_id,
            matrix_identity=action_key.matrix_identity,
            allowed=decision.accepted,
            reason=decision.reason,
            observed_at=current,
            critical=bool(
                effective_hard_stop
                or not safety_state
                or (
                    safety_state != "ok"
                    and origin.kind
                    not in {
                        ActionOriginKind.FAULT_RECOVERY,
                        ActionOriginKind.WATCHDOG,
                        ActionOriginKind.LEASE_EXPIRY,
                    }
                )
            ),
        )
        final_verdict = evaluate_opinion(opinion, action_key=action_key)
        disposition = str(
            getattr(getattr(final_verdict, "disposition", None), "value", "")
        )
        if disposition == "HARD_STOP":
            return None, True, ActionAdmissionDecision(
                False,
                "autopilot_bouncer_stop:" + decision.reason,
                decision.descriptor,
                decision.origin,
            )
        if disposition != "PASS":
            raise RuntimeError("core_bouncer_emergency_verdict_invalid")
    if not decision.accepted:
        return None, True, decision
    authorize_admitted_action(
        engine,
        action,
        decision=decision,
        decision_trace_id=decision_trace_id,
        lanes=lanes,
    )
    return action, False, decision


__all__ = (
    "apply_canonical_action_admission",
    "prepare_core_action_root",
)
