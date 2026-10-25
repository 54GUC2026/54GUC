"""Foundation SMS command adapter to the only Core command gateway."""

from __future__ import annotations

from typing import Any, Iterable

from arch4.core.command_context import require_core_command_ingress_context
from arch4.core.command_gateway import require_core_command_gateway
from arch4.core.command_request_builder import (
    build_core_command_request,
    build_core_command_target,
)
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.commands import CoreCommandOrigin
from arch4.command_transactions import CommandTerminalReceipt
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.sms_receiver import SmsCommand, SmsRuntimeBoundary


FOUNDATION_SMS_OUTCOME_CLIENT_ID = "54guc.foundation.sms.outcome"


def submit_sms_core_commands(
    core: CoreRuntimePort,
    *,
    commands: Iterable[SmsCommand],
    sms_runtime: SmsRuntimeBoundary,
    now: float,
) -> int:
    """Submit each SMS command and let its final receipt report completion."""

    if not isinstance(sms_runtime, SmsRuntimeBoundary):
        raise TypeError("foundation_sms_runtime_boundary_required")
    context = require_core_command_ingress_context(core)
    gateway = require_core_command_gateway(core)
    submitted_count = 0
    for command in commands:
        if not isinstance(command, SmsCommand):
            raise TypeError("foundation_sms_typed_command_required")
        sms_runtime.require_consumed(command)
        command_id = str(command.command_id or "")
        kind = str(command.kind or "").strip().upper()
        params = dict(command.params or {})
        raw = str(command.raw or kind)
        if not command_id:
            raise RuntimeError("sms_command_identity_missing")
        if kind == "REBOOT":
            lever = "RESTART"
            target_parameters = {"emergency_fast": False}
        elif kind == "PBAND":
            lever = "PBAND"
            target_parameters = {
                "pband": int(params.get("pband", 0) or 0),
                "ca_pool": list(params.get("ca_pool") or []),
            }
        elif kind == "BAND":
            lever = "BAND_SELECTION"
            target_parameters = {
                "bands": list(params.get("bands") or []),
            }
        else:
            raise RuntimeError(f"sms_command_kind_unsupported:{kind}")
        target = build_core_command_target(lever, target_parameters)
        received_ts = float(command.received_ts or 0.0)
        expires_ts = max(float(now) + 0.1, received_ts + (10.0 * 60.0))
        request = build_core_command_request(
            context,
            request_id=command_id,
            correlation_id=command_id,
            client_id="foundation.sms_command",
            client_role=CoreClientRole.OPERATOR_AUTOMATION,
            origin=CoreCommandOrigin.OPERATOR_AUTOMATION,
            command_kind=lever,
            target=target,
            parameters={
                "automation_source": "sms",
                "intent": "sms_remote_command",
                "reason": f"SMS command: {raw}",
                "objective_id": command_id,
                "action_parameters": {"source": "sms_runtime"},
                "sms_evidence": {
                    "command_id": command_id,
                    "message_id": str(command.message_id or ""),
                    "sender": str(command.sender or ""),
                    "timestamp": str(command.timestamp or ""),
                    "protocol": str(command.protocol or ""),
                    "received_ts": received_ts,
                    "raw": raw,
                },
            },
            capability_reference=lever_action_authority(lever).capability_key,
            created_at=float(now),
            expires_at=expires_ts,
            confirmed=True,
        )
        result = gateway.submit(request, now=float(now))
        submitted = bool(result.get("submitted", False))
        sms_runtime.record_command_submission(
            command,
            submitted=submitted,
            duplicate=str(result.get("reason") or "")
            == "core_command_replay_rejected",
            reason=str(result.get("reason") or ""),
        )
        submitted_count += int(submitted)
    return submitted_count


def drain_sms_core_command_outcomes(
    core: CoreRuntimePort,
    *,
    sms_runtime: SmsRuntimeBoundary,
) -> int:
    if not isinstance(sms_runtime, SmsRuntimeBoundary):
        raise TypeError("foundation_sms_runtime_boundary_required")
    stream = getattr(core, "command_outcome_stream", None)
    if not all(
        callable(getattr(stream, name, None))
        for name in ("claim_next", "acknowledge", "release")
    ):
        raise TypeError("foundation_sms_core_command_outcome_port_required")
    runtime_namespace = require_core_command_ingress_context(
        core
    ).matrix_identity.namespace
    consumed = 0
    while True:
        record = stream.claim_next(
            FOUNDATION_SMS_OUTCOME_CLIENT_ID,
            runtime_namespace=runtime_namespace,
        )
        if record is None:
            return consumed
        try:
            receipt = record.receipt
            if not isinstance(receipt, CommandTerminalReceipt):
                raise TypeError("foundation_sms_typed_terminal_receipt_required")
            sms_runtime.observe_command_terminal(
                command_id=receipt.transaction.command_id,
                state=receipt.terminal_status.value,
                reason=receipt.reason,
            )
            stream.acknowledge(
                FOUNDATION_SMS_OUTCOME_CLIENT_ID,
                runtime_namespace=runtime_namespace,
                key=record.key,
            )
            consumed += 1
        except Exception as exc:
            stream.release(
                FOUNDATION_SMS_OUTCOME_CLIENT_ID,
                runtime_namespace=runtime_namespace,
                key=record.key,
                error=exc,
            )
            raise


__all__ = (
    "FOUNDATION_SMS_OUTCOME_CLIENT_ID",
    "drain_sms_core_command_outcomes",
    "submit_sms_core_commands",
)
