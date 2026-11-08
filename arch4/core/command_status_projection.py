"""Read-only Core register projection of CommandAuthority physical truth."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import RLock
from typing import Any, Callable, Mapping

from arch4.command_transactions import (
    COMMAND_AUTHORITY_OWNER,
    CommandLifecycle,
    CommandLifecycleProjectionObligation,
    CommandLifecycleStage,
    CommandTerminalReceipt,
    CommandTerminalStatus,
)
from arch4.payload_budget import retained_payload_bytes
from arch4.runtime_registers import (
    REGISTER_ACTION,
    require_core_runtime_register_bank,
)


CORE_COMMAND_STATUS_SCHEMA = "CoreCommandStatus.v3"
CORE_COMMAND_STATUS_OWNER = COMMAND_AUTHORITY_OWNER
# Full checksummed five-stage APN receipts include the bounded protected
# credentials in each target. Keep one budget for lifecycle and terminal truth.
MAX_CORE_COMMAND_STATUS_BYTES = 384 * 1024
_WAKE_LISTENER_ATTR = "_core_command_status_wake_listener"
_LOCK_ATTR = "_core_command_status_projection_lock"
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandStatusProjectionReceipt:
    obligation_id: str
    authority_sequence: int
    transaction_root_id: str
    payload_fingerprint: str
    register_sequence: int
    register_signature: str
    projected_at: float
    listener_notified: bool


def _terminal_acceptance_is_success(receipt: CommandTerminalReceipt) -> bool:
    completion = receipt.transaction.cellular_completion
    return bool(
        completion is not None and completion.terminal_on_modem_acceptance
    )


def bind_core_command_status_wake_listener(
    core: Any,
    listener: Callable[[], None] | None,
) -> None:
    current = getattr(core, _WAKE_LISTENER_ATTR, None)
    if current is not None and listener is not None and current is not listener:
        raise RuntimeError("core command status wake listener is already bound")
    setattr(core, _WAKE_LISTENER_ATTR, listener)


def bind_core_command_status_listener(
    core: Any,
    listener: Callable[[Mapping[str, Any]], None] | None,
) -> None:
    del core, listener
    raise RuntimeError(
        "core command status mapping listener retired; "
        "bind payload-free command status wake listener"
    )


def _projection_lock(core: Any) -> RLock:
    lock = getattr(core, _LOCK_ATTR, None)
    if isinstance(lock, type(RLock())):
        return lock
    lock = RLock()
    setattr(core, _LOCK_ATTR, lock)
    return lock


def _base_status(core: Any, transaction: Any) -> dict[str, Any]:
    key = transaction.action_key
    if key is None:
        raise RuntimeError("core_command_status_action_key_missing")
    command_root_id = str(transaction.public_command_root_id or "").strip()
    if not command_root_id:
        raise RuntimeError("core_command_status_public_root_missing")
    return {
        "schema": CORE_COMMAND_STATUS_SCHEMA,
        "owner": CORE_COMMAND_STATUS_OWNER,
        "command_id": str(transaction.command_id or ""),
        "gui_action_id": str(key.correlation_id or ""),
        "gui_action_source": str(key.correlation_source or ""),
        "command_root_id": command_root_id,
        "physical_action_root_id": str(transaction.root_id or ""),
        "action_origin": str(transaction.origin_kind or ""),
        "operator_initiated": bool(key.operator_initiated),
        "command": str(transaction.command or ""),
        "lever": str(key.lever or ""),
        "matrix_identity": transaction.matrix_identity.as_dict(),
        "identity_key": str(transaction.identity_key or ""),
        "target": key.target.to_mapping(),
    }


def command_status_from_lifecycle(
    core: Any,
    lifecycle: CommandLifecycle,
) -> dict[str, Any]:
    if not isinstance(lifecycle, CommandLifecycle):
        raise TypeError("core_command_status_lifecycle_required")
    status = {
        CommandLifecycleStage.CLAIMED: "queued",
        CommandLifecycleStage.DISPATCHED: "executing",
        CommandLifecycleStage.ACCEPTED: "modem_accepted",
        CommandLifecycleStage.CONFIRMED: "target_confirmed",
    }[lifecycle.stage]
    value = {
        **_base_status(core, lifecycle.lease.transaction),
        "status": status,
        "reason": str(lifecycle.reason or status),
        "error": "",
        "action_pending": True,
        "published_ts": max(
            float(lifecycle.confirmed_ts or 0.0),
            float(lifecycle.accepted_ts or 0.0),
            float(lifecycle.lease.acquired_ts or 0.0),
        ),
        "accepted_ts": float(lifecycle.accepted_ts or 0.0),
        "confirmed_ts": float(lifecycle.confirmed_ts or 0.0),
        "terminal_ts": 0.0,
        "target_generation": int(lifecycle.target_generation or 0),
        "settle_generation": int(lifecycle.settle_generation or 0),
        "proof_ready": False,
        "command_authority_receipt": lifecycle.as_dict(),
    }
    retained_payload_bytes(
        value,
        max_bytes=MAX_CORE_COMMAND_STATUS_BYTES,
        label="core command status lifecycle projection",
    )
    return value


def command_status_from_terminal(
    core: Any,
    terminal: CommandTerminalReceipt,
) -> dict[str, Any]:
    if not isinstance(terminal, CommandTerminalReceipt):
        raise TypeError("core_command_status_terminal_required")
    acceptance_complete = _terminal_acceptance_is_success(terminal)
    if terminal.terminal_status is CommandTerminalStatus.SETTLED:
        status = "stream_settled"
        success = True
    elif (
        terminal.terminal_status
        in {CommandTerminalStatus.ACCEPTED, CommandTerminalStatus.CONFIRMED}
        and acceptance_complete
    ):
        status = "complete"
        success = True
    else:
        status = {
            CommandTerminalStatus.ACCEPTED: "modem_accepted",
            CommandTerminalStatus.CONFIRMED: "target_confirmed",
            CommandTerminalStatus.FAILED: "failed",
            CommandTerminalStatus.REJECTED: "rejected",
            CommandTerminalStatus.TIMED_OUT: "timed_out",
            CommandTerminalStatus.CANCELLED: "cancelled",
        }[terminal.terminal_status]
        success = False
    value = {
        **_base_status(core, terminal.transaction),
        "status": status,
        "reason": str(terminal.reason or terminal.terminal_status.value),
        "error": "" if success else str(terminal.reason or status),
        "success": success,
        "action_pending": False,
        "published_ts": float(terminal.terminal_ts),
        "accepted_ts": float(terminal.accepted_ts or 0.0),
        "confirmed_ts": float(terminal.confirmed_ts or 0.0),
        "terminal_ts": float(terminal.terminal_ts or 0.0),
        "target_generation": int(terminal.target_generation or 0),
        "settle_generation": int(terminal.settle_generation or 0),
        "proof_ready": bool(terminal.proof_ready),
        "command_authority_receipt": terminal.as_dict(),
    }
    retained_payload_bytes(
        value,
        max_bytes=MAX_CORE_COMMAND_STATUS_BYTES,
        label="core command status terminal projection",
    )
    return value


def project_core_command_status(
    core: Any,
    obligation: CommandLifecycleProjectionObligation,
) -> CommandStatusProjectionReceipt:
    if not isinstance(obligation, CommandLifecycleProjectionObligation):
        raise TypeError("core_command_projection_obligation_required")
    receipt = obligation.typed_receipt()
    if isinstance(receipt, CommandLifecycle):
        status = command_status_from_lifecycle(core, receipt)
        pending = True
    elif isinstance(receipt, CommandTerminalReceipt):
        status = command_status_from_terminal(core, receipt)
        pending = False
    else:
        raise TypeError("core_command_status_typed_receipt_required")
    status = {
        **status,
        "projection_obligation_id": obligation.obligation_id,
        "projection_authority_sequence": obligation.authority_sequence,
        "projection_payload_fingerprint": obligation.payload_fingerprint,
    }
    with _projection_lock(core):
        bank = require_core_runtime_register_bank(core)
        current = bank.read(REGISTER_ACTION)
        current_value = (
            current.value if isinstance(current.value, Mapping) else {}
        )
        current_status = current_value.get("core_command_status")
        if (
            isinstance(current_status, Mapping)
            and current_status.get("projection_obligation_id")
            == obligation.obligation_id
            and current_status.get("projection_payload_fingerprint")
            == obligation.payload_fingerprint
            and int(
                current_status.get("projection_authority_sequence", 0) or 0
            )
            == obligation.authority_sequence
        ):
            written = current
        else:
            written = bank.write_action(
                value={"core_command_status": dict(status)},
                source=CORE_COMMAND_STATUS_OWNER,
                updated_at=float(status["published_ts"]),
                valid=True,
                stale=False,
                dirty=pending,
                trust="transition" if pending else "derived",
                source_kind="derived",
                validity="dirty" if pending else "valid",
                transition=pending,
                force=True,
            )
        readback = bank.read(REGISTER_ACTION)
        readback_value = (
            readback.value if isinstance(readback.value, Mapping) else {}
        )
        readback_status = readback_value.get("core_command_status")
        if (
            not isinstance(readback_status, Mapping)
            or readback_status.get("projection_obligation_id")
            != obligation.obligation_id
            or readback_status.get("projection_payload_fingerprint")
            != obligation.payload_fingerprint
            or int(
                readback_status.get("projection_authority_sequence", 0) or 0
            )
            != obligation.authority_sequence
        ):
            raise RuntimeError(
                "core_command_status_projection_readback_mismatch"
            )
        if readback.sequence != written.sequence:
            raise RuntimeError(
                "core_command_status_projection_sequence_mismatch"
            )
    listener_notified = False
    listener = getattr(core, _WAKE_LISTENER_ATTR, None)
    if callable(listener):
        try:
            listener()
            listener_notified = True
        except Exception as exc:
            log.error(
                "Core command register committed but frame wake failed: %s: %s",
                type(exc).__name__,
                exc,
            )
    return CommandStatusProjectionReceipt(
        obligation_id=obligation.obligation_id,
        authority_sequence=obligation.authority_sequence,
        transaction_root_id=obligation.transaction_root_id,
        payload_fingerprint=obligation.payload_fingerprint,
        register_sequence=readback.sequence,
        register_signature=str(readback.signature or ""),
        projected_at=float(status["published_ts"]),
        listener_notified=listener_notified,
    )


def read_core_command_status(core: Any) -> dict[str, Any]:
    value = require_core_runtime_register_bank(core).read(REGISTER_ACTION).value
    status = value.get("core_command_status") if isinstance(value, Mapping) else None
    return dict(status) if isinstance(status, Mapping) else {}


__all__ = (
    "CORE_COMMAND_STATUS_OWNER",
    "CORE_COMMAND_STATUS_SCHEMA",
    "CommandStatusProjectionReceipt",
    "bind_core_command_status_listener",
    "bind_core_command_status_wake_listener",
    "command_status_from_lifecycle",
    "command_status_from_terminal",
    "project_core_command_status",
    "read_core_command_status",
)
