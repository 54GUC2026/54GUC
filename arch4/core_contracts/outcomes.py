"""Customer cursor contract over Core command lifecycle truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from arch4.architecture_contracts.runtime_generation import (
        RuntimeGenerationNamespace,
    )
    from arch4.command_transactions import CommandTerminalReceipt


@dataclass(frozen=True, slots=True)
class CoreCommandOutcomeRecord:
    key: str
    receipt: "CommandTerminalReceipt"

    def __post_init__(self) -> None:
        from arch4.command_transactions import CommandTerminalReceipt

        if not isinstance(self.receipt, CommandTerminalReceipt):
            raise TypeError("core_command_outcome_typed_receipt_required")
        expected = (
            f"command-authority-receipt:{self.receipt.receipt_sequence}"
        )
        if self.key != expected:
            raise ValueError("core_command_outcome_record_key_mismatch")


class CoreCommandOutcomeClientPort(Protocol):
    def claim_next(
        self,
        client_id: str,
        *,
        runtime_namespace: "RuntimeGenerationNamespace",
    ) -> CoreCommandOutcomeRecord | None: ...

    def acknowledge(
        self,
        client_id: str,
        *,
        runtime_namespace: "RuntimeGenerationNamespace",
        key: str,
    ) -> None: ...

    def release(
        self,
        client_id: str,
        *,
        runtime_namespace: "RuntimeGenerationNamespace",
        key: str,
        error: BaseException,
    ) -> None: ...

    def status(self) -> dict[str, Any]: ...


__all__ = ("CoreCommandOutcomeClientPort", "CoreCommandOutcomeRecord")
