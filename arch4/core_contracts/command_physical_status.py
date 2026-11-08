"""Typed Core view of one command's exact modem dispatch evidence."""

from __future__ import annotations

from dataclasses import dataclass

from arch4.core_contracts.physical_commands import PhysicalCommandReceipt


@dataclass(frozen=True, slots=True)
class CoreCommandPhysicalStatus:
    """Read-only correlation of a Core root with Modem Live evidence."""

    request_id: str
    command_root_id: str
    physical_action_root_id: str
    disposition: str
    receipt: PhysicalCommandReceipt | None = None
    terminal_reason: str = ""
    schema: str = "CoreCommandPhysicalStatus.v1"
    owner: str = "54guc.core.command_lifecycle_service"

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "command_root_id",
            "physical_action_root_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"core_command_physical_status_{name}_required")
        if self.disposition not in {
            "pending_physical_dispatch",
            "physical_accepted",
            "physical_rejected",
            "terminal_without_physical_receipt",
        }:
            raise ValueError("core_command_physical_status_disposition_invalid")
        if not isinstance(self.terminal_reason, str):
            raise TypeError("core_command_physical_status_terminal_reason_invalid")
        has_receipt = self.receipt is not None
        expects_receipt = self.disposition in {
            "physical_accepted",
            "physical_rejected",
        }
        if has_receipt is not expects_receipt:
            raise ValueError("core_command_physical_status_receipt_mismatch")
        if self.receipt is not None:
            if (
                self.receipt.request_id != self.request_id
                or self.receipt.transaction_root_id
                != self.physical_action_root_id
            ):
                raise ValueError("core_command_physical_status_identity_mismatch")
            if self.receipt.accepted is not (
                self.disposition == "physical_accepted"
            ):
                raise ValueError("core_command_physical_status_result_mismatch")
        if self.disposition == "terminal_without_physical_receipt":
            if not self.terminal_reason:
                raise ValueError(
                    "core_command_physical_status_terminal_reason_required"
                )
        elif self.terminal_reason:
            raise ValueError(
                "core_command_physical_status_early_terminal_reason_forbidden"
            )
        if self.schema != "CoreCommandPhysicalStatus.v1":
            raise ValueError("core_command_physical_status_schema_invalid")
        if self.owner != "54guc.core.command_lifecycle_service":
            raise ValueError("core_command_physical_status_owner_invalid")


__all__ = ("CoreCommandPhysicalStatus",)
