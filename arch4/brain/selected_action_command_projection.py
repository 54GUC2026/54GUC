"""Explicit boundary from CommandAuthority truth to Brain selection handoff."""

from __future__ import annotations

from typing import Any

from arch4.core.selected_action_register import SelectedActionStage
from arch4.action_transaction_authority import require_action_transaction_authority
from arch4.core.selected_action_terminal_router import (
    route_selected_action_terminal,
)
from arch4.command_transactions import (
    CommandAuthority,
    CommandTerminalReceipt,
    RadioMutationLease,
)


def handoff_selected_action_command(
    owner: Any,
    *,
    lease: RadioMutationLease,
    receipt: dict[str, Any] | None = None,
) -> bool:
    """Bind a claimed Brain selection to its one physical command root."""

    authority = require_action_transaction_authority(owner)
    active = dict(authority.selection_status().get("active") or {})
    if not active:
        return False
    if str(active.get("command_id") or "") != lease.transaction.command_id:
        return False
    stage = str(active.get("stage") or "")
    if stage not in {
        SelectedActionStage.CLAIMED.value,
        SelectedActionStage.HANDED_OFF.value,
    }:
        return False
    action_key = lease.transaction.action_key
    if action_key is None:
        raise RuntimeError("selected_action_command_action_key_missing")
    authority.handoff_to_command_authority(
        command_id=lease.transaction.command_id,
        action_key=action_key,
        receipt={
            "owner": "command_authority_projection",
            "command_root_id": lease.root_id,
            **dict(receipt or {}),
        },
    )
    return True


def project_command_terminal_receipt(
    owner: Any,
    receipt: CommandTerminalReceipt,
) -> bool:
    """Project one exact terminal command receipt when it owns the selection."""

    if not isinstance(receipt, CommandTerminalReceipt):
        raise TypeError("command_terminal_receipt_required")
    authority = require_action_transaction_authority(owner)
    active = dict(authority.selection_status().get("active") or {})
    if not active:
        return False
    if str(active.get("command_id") or "") != receipt.transaction.command_id:
        return False
    stage = str(active.get("stage") or "")
    if stage == SelectedActionStage.CLAIMED.value:
        key = receipt.action_key
        if key is None:
            raise RuntimeError(
                "selected_action_terminal_action_key_missing"
            )
        authority.handoff_to_command_authority(
            command_id=receipt.transaction.command_id,
            action_key=key,
            receipt={
                "owner": "command_authority_projection",
                "recovered_from_terminal_receipt": True,
            },
            now=receipt.terminal_ts,
        )
    elif stage not in {
        SelectedActionStage.HANDED_OFF.value,
        SelectedActionStage.TERMINAL.value,
    }:
        return False
    selected = authority.accept_selection_terminal(
        command_id=receipt.transaction.command_id,
        terminal_receipt=receipt,
    )
    if selected is None:
        return False
    route_selected_action_terminal(
        authority,
        now=receipt.terminal_ts,
    )
    return True


def reconcile_selected_action_command(
    owner: Any,
    *,
    command_authority: CommandAuthority | None = None,
) -> bool:
    """Close crash gaps without creating another lifecycle owner."""

    authority = require_action_transaction_authority(owner)
    active = dict(authority.selection_status().get("active") or {})
    if not active:
        return False
    stage = str(active.get("stage") or "")
    if stage in {
        SelectedActionStage.REJECTED.value,
        SelectedActionStage.TERMINAL.value,
    }:
        return route_selected_action_terminal(authority).routed
    if stage == SelectedActionStage.PENDING.value:
        return False
    command_owner = command_authority
    if command_owner is None:
        command_owner = getattr(
            getattr(
                getattr(owner, "controller", None),
                "runtime",
                None,
            ),
            "command_authority",
            None,
        )
    if not isinstance(command_owner, CommandAuthority):
        raise RuntimeError("command_authority_unavailable")
    command_id = str(active.get("command_id") or "")
    lifecycle = command_owner.active_lifecycle()
    if (
        lifecycle is not None
        and lifecycle.lease.transaction.command_id == command_id
    ):
        handoff_selected_action_command(
            owner,
            lease=lifecycle.lease,
            receipt={"reconciled_active_command": True},
        )
        return True
    for receipt in reversed(command_owner.terminal_receipts()):
        if receipt.transaction.command_id == command_id:
            return project_command_terminal_receipt(owner, receipt)
    if stage == SelectedActionStage.HANDED_OFF.value:
        raise RuntimeError(
            "selected_action_handed_off_without_command_authority_root"
        )
    return False


__all__ = (
    "handoff_selected_action_command",
    "project_command_terminal_receipt",
    "reconcile_selected_action_command",
)
