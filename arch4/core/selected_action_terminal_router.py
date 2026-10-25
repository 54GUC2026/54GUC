"""Route final customer-selected results without keeping another state owner.
Non-optimization commands are archived only after a pre-handoff rejection or a saved
domain result. Optimization results remain with SmartLoop until save-before-ack is
complete.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Protocol, runtime_checkable

from arch4.customer_contracts.selected_action import SelectedActionContract
from arch4.core.selected_action_register import (
    SelectedActionTransaction,
    SelectedActionStage,
    SelectedActionDomainResultReceipt,
    SelectedActionTerminalRouteReceipt,
)


@dataclass(frozen=True, slots=True)
class SelectedActionTerminalRoutingOutcome:
    command_id: str
    owner_class: str
    routed: bool
    reason: str


@runtime_checkable
class SelectedActionTerminalAuthorityPort(Protocol):
    """Minimum lifecycle-owner boundary required by the stateless router."""

    def accept_selected_domain_result(
        self,
        receipt: SelectedActionDomainResultReceipt,
    ) -> SelectedActionTransaction: ...

    def read_terminal(self) -> SelectedActionTransaction | None: ...

    def read_selected_transaction(
        self,
    ) -> SelectedActionTransaction | None: ...

    def acknowledge_non_optimization_terminal(
        self,
        *,
        route_receipt: SelectedActionTerminalRouteReceipt,
    ) -> SelectedActionTransaction: ...


def _require_selected_action_terminal_authority_port(
    owner: Any,
) -> SelectedActionTerminalAuthorityPort:
    if isinstance(owner, SelectedActionTerminalAuthorityPort):
        return owner
    raise TypeError("selected_action_terminal_authority_port_required")


def route_selected_action_terminal(
    authority: SelectedActionTerminalAuthorityPort,
    *,
    domain_result_receipt: SelectedActionDomainResultReceipt | None = None,
    now: float | None = None,
) -> SelectedActionTerminalRoutingOutcome:
    """Route one terminal without retaining state or grading the action."""

    authority = _require_selected_action_terminal_authority_port(authority)
    if domain_result_receipt is not None:
        authority.accept_selected_domain_result(domain_result_receipt)
    terminal = authority.read_terminal()
    if terminal is None:
        selected = authority.read_selected_transaction()
        return SelectedActionTerminalRoutingOutcome(
            command_id=(selected.command_id if selected is not None else ""),
            owner_class=(
                str(selected.selected_action.get("owner_class") or "")
                if selected is not None
                else ""
            ),
            routed=False,
            reason="selected_action_not_terminal",
        )
    selected = SelectedActionContract.from_mapping(terminal.selected_action)
    if selected.optimization:
        return SelectedActionTerminalRoutingOutcome(
            command_id=terminal.command_id,
            owner_class=selected.owner_class,
            routed=False,
            reason="optimization_terminal_owned_by_smart_loop",
        )
    domain_receipt_id = ""
    if terminal.stage is SelectedActionStage.TERMINAL:
        retained = dict(terminal.domain_result_receipt or {})
        if not retained:
            return SelectedActionTerminalRoutingOutcome(
                command_id=terminal.command_id,
                owner_class=selected.owner_class,
                routed=False,
                reason="awaiting_domain_result_persistence",
            )
        domain_receipt_id = (
            SelectedActionDomainResultReceipt.from_mapping(retained).receipt_id
        )
    route_receipt = SelectedActionTerminalRouteReceipt(
        command_id=terminal.command_id,
        terminal_stage=terminal.stage,
        routed_ts=float(time.time() if now is None else now),
        domain_result_receipt_id=domain_receipt_id,
    )
    authority.acknowledge_non_optimization_terminal(
        route_receipt=route_receipt
    )
    return SelectedActionTerminalRoutingOutcome(
        command_id=terminal.command_id,
        owner_class=selected.owner_class,
        routed=True,
        reason=(
            "pre_handoff_rejection_archived"
            if terminal.stage is SelectedActionStage.REJECTED
            else "domain_result_persisted_and_archived"
        ),
    )


def record_selected_action_domain_result(
    owner: Any,
    *,
    action: Any,
    success: bool,
    result_owner: str,
    result_key: str = "",
    now: float | None = None,
) -> SelectedActionTerminalRoutingOutcome:
    """Persist the result boundary receipt, then route if command is terminal."""

    authority = _require_selected_action_terminal_authority_port(owner)
    transaction = authority.read_selected_transaction()
    if transaction is None:
        return SelectedActionTerminalRoutingOutcome("", "", False, "no_selection")
    selected = SelectedActionContract.from_mapping(transaction.selected_action)
    if selected.optimization:
        return SelectedActionTerminalRoutingOutcome(
            selected.command_id,
            selected.owner_class,
            False,
            "optimization_result_owned_by_smart_loop",
        )
    params = dict(getattr(action, "params", {}) or {})
    action_command_id = str(
        getattr(action, "command_id", "")
        or params.get("_selected_action_command_id")
        or ""
    )
    if action_command_id != selected.command_id:
        raise RuntimeError("selected_action_domain_result_command_mismatch")
    if transaction.stage not in {
        SelectedActionStage.HANDED_OFF,
        SelectedActionStage.TERMINAL,
    }:
        raise RuntimeError(
            "selected_action_domain_result_before_command_handoff:"
            + transaction.stage.value
        )
    if transaction.action_key is None:
        raise RuntimeError("selected_action_domain_result_action_key_missing")
    timestamp = float(time.time() if now is None else now)
    wanted_result_owner = str(result_owner or "")
    wanted_result_status = "succeeded" if bool(success) else "failed"
    wanted_result_key = str(result_key or transaction.action_key.root_id)
    retained = dict(transaction.domain_result_receipt or {})
    if retained:
        receipt = SelectedActionDomainResultReceipt.from_mapping(retained)
        if (
            receipt.command_id != selected.command_id
            or receipt.transaction_root_id != transaction.action_key.root_id
            or receipt.owner_class != selected.owner_class
            or receipt.result_owner != wanted_result_owner
            or receipt.result_status != wanted_result_status
            or receipt.result_key != wanted_result_key
        ):
            raise RuntimeError("selected_action_domain_result_replay_mismatch")
        return route_selected_action_terminal(authority, now=timestamp)
    receipt = SelectedActionDomainResultReceipt(
        command_id=selected.command_id,
        transaction_root_id=transaction.action_key.root_id,
        owner_class=selected.owner_class,
        result_owner=wanted_result_owner,
        result_status=wanted_result_status,
        persisted_ts=timestamp,
        result_key=wanted_result_key,
    )
    authority.accept_selected_domain_result(receipt)
    return route_selected_action_terminal(authority, now=timestamp)


__all__ = (
    "SelectedActionTerminalAuthorityPort",
    "SelectedActionTerminalRoutingOutcome",
    "record_selected_action_domain_result",
    "route_selected_action_terminal",
)
