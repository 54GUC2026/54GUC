"""
arch4.mutation_authority

The only production path that may execute a LeverAction against modem live.
Every action must first pass the canonical ActionAdmission owner and hold the
physical CommandAuthority lease. Runtime helpers and queues can only propose.
"""

from __future__ import annotations

from typing import Any, Optional

from arch4.action_admission import consume_action_admission_authority
from arch4.core.lever_action_adapter import validate_lever_action_command_authority
from arch4.interfaces import LeverAction, LeverResult, LeverType
from arch4.command_transactions import RadioMutationLease


def validate_action_execution_authority(
    engine: Any,
    action: Optional[LeverAction],
) -> tuple[bool, str, dict]:
    if not isinstance(action, LeverAction):
        return False, "action_not_lever_action", {}
    command_ok, command_reason, command_detail = validate_lever_action_command_authority(action)
    if not command_ok:
        return False, command_reason, {"detail": command_detail, "authority": "command_action_envelope"}
    return consume_action_admission_authority(engine, action)


def execute_admitted_modem_action(
    engine: Any,
    action: LeverAction,
    *,
    lease: RadioMutationLease,
    event_write_token: str,
    network_progress=None,
) -> LeverResult:
    """Execute one canonically admitted action through the sole mutation boundary."""

    ok, reason, record = validate_action_execution_authority(engine, action)
    if not ok:
        return LeverResult(
            success=False,
            lever=action.lever if isinstance(action, LeverAction) else LeverType.BAND_SELECTION,
            error=reason,
            details={"authority": "action_admission", "record": record},
        )
    command_authority = getattr(engine, "command_authority", None)
    validate_execution = getattr(command_authority, "validate_cellular_execution", None)
    if not callable(validate_execution):
        return LeverResult(
            success=False,
            lever=action.lever,
            error="cellular_command_authority_unavailable",
            details={"authority": "command_authority"},
        )
    lease_ok, lease_reason, retained_lease = validate_execution(action, lease)
    if not lease_ok:
        return LeverResult(
            success=False,
            lever=action.lever,
            error=str(lease_reason or "cellular_mutation_lease_rejected"),
            details={
                "authority": "command_authority",
                "lease_owner": str(getattr(retained_lease, "owner", "") or ""),
            },
        )
    live_module = getattr(engine, "input_module", None)
    execute = getattr(live_module, "execute_lever", None) if live_module is not None else None
    if not callable(execute):
        raise RuntimeError("Modem live module missing required method: execute_lever")
    key = lease.transaction.action_key
    if key is None:
        raise RuntimeError("cellular_execution_typed_target_missing")
    write_token = str(event_write_token or "").strip()
    if not write_token:
        raise RuntimeError("modem_live_event_write_token_required")
    progress_argument = {}
    if action.lever is LeverType.NETWORK_SETTINGS:
        if not callable(network_progress):
            raise RuntimeError("network_progress_owner_required")
        progress_argument["network_progress"] = network_progress
    return execute(
        action,
        command_target=key.target,
        transaction_root_id=lease.root_id,
        request_id=lease.transaction.command_id,
        event_write_token=write_token,
        **progress_argument,
    )


__all__ = [
    "execute_admitted_modem_action",
    "validate_action_execution_authority",
]
