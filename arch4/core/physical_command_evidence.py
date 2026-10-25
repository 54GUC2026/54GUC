"""Strict decoder for the Modem Live physical command receipt."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.core_contracts.command_targets import CoreCommandTarget
from arch4.core_contracts.physical_commands import PhysicalCommandReceipt
from arch4.interfaces import LeverAction, LeverResult


def physical_command_receipt_from_result(
    action: LeverAction,
    result: LeverResult,
    *,
    request_id: str,
    transaction_root_id: str,
    target: CoreCommandTarget,
    execution_started_at: float,
    transaction_completed_at: float,
) -> PhysicalCommandReceipt:
    """Decode executor evidence. Generic LeverResult success is not enough."""

    if not isinstance(action, LeverAction):
        raise TypeError("physical_command_action_required")
    if not isinstance(result, LeverResult):
        raise TypeError("physical_command_lever_result_required")
    details = result.details
    if not isinstance(details, Mapping):
        raise TypeError("physical_command_result_details_mapping_required")
    raw = details.get("physical_command_receipt")
    if not isinstance(raw, Mapping):
        raise RuntimeError("modem_live_exact_physical_receipt_missing")
    receipt = PhysicalCommandReceipt.from_mapping(raw)
    if receipt.request_id != str(request_id or ""):
        raise RuntimeError("physical_command_receipt_request_mismatch")
    if receipt.transaction_root_id != str(transaction_root_id or ""):
        raise RuntimeError("physical_command_receipt_root_mismatch")
    if receipt.target != target:
        raise RuntimeError("physical_command_receipt_target_mismatch")
    if receipt.execution_started_at < float(execution_started_at):
        raise RuntimeError("physical_command_receipt_started_before_execution")
    if receipt.transaction_completed_at > float(transaction_completed_at):
        raise RuntimeError("physical_command_receipt_completed_after_execution")
    if bool(result.success) != receipt.accepted:
        raise RuntimeError("physical_command_receipt_result_mismatch")
    return receipt


def physical_command_summary_from_result(*_: Any, **__: Any) -> None:
    raise RuntimeError("generic_physical_command_summary_illegal")


def physical_command_stages_from_result(*_: Any, **__: Any) -> None:
    raise RuntimeError("reduced_physical_command_stage_path_illegal")


__all__ = ("physical_command_receipt_from_result",)
