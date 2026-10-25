"""Foundation-owned operator automation configuration service."""

from __future__ import annotations

from time import time
from pathlib import Path
from typing import Any, Mapping

from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.configuration import (
    CoreConfigDisposition,
    CoreConfigProposal,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.foundation.modem_watchdog import FoundationModemWatchdog
from arch4.operational_service_contracts import (
    FoundationOperatorRequestContext,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    SmsCommandControlConfigRequest,
    require_product_operational_effect_permit,
)
from arch4.sms_receiver import SmsRuntimeBoundary


OWNER = "54guc.foundation.automation"


class FoundationAutomationService:
    def __init__(
        self,
        core: CoreRuntimePort,
        *,
        sms_runtime: SmsRuntimeBoundary,
        persistence_dir: str | Path | None = None,
    ) -> None:
        if core is None:
            raise TypeError("foundation_automation_core_port_required")
        configuration = getattr(core, "active_configuration", None)
        if not callable(getattr(configuration, "active_snapshot", None)) or not callable(
            getattr(configuration, "submit_proposal", None)
        ):
            raise TypeError("foundation_automation_configuration_port_required")
        if not isinstance(sms_runtime, SmsRuntimeBoundary):
            raise TypeError("foundation_automation_sms_runtime_required")
        self._core = core
        self._configuration = configuration
        self._sms_runtime = sms_runtime
        self._modem_watchdog = FoundationModemWatchdog(
            core,
            persistence_dir=persistence_dir,
        )

    def service_modem_watchdog(self, *, now: float) -> dict[str, Any]:
        return self._modem_watchdog.service_once(now=now)

    def configure_sms_command_control(
        self,
        request: SmsCommandControlConfigRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, SmsCommandControlConfigRequest):
            raise TypeError("foundation_sms_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=(
                OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL
            ),
        )
        rejection = self._commit_sms_config(request)
        if rejection:
            return self._rejected(
                request.context,
                OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL,
                rejection,
            )
        active = self._configuration.active_snapshot()
        self._sms_runtime.bind_config_generation(
            self._core.cfg,
            generation_id=active.generation_id,
            config_fingerprint=active.config_fingerprint,
        )
        status = self._sms_runtime.snapshot()
        return self._confirmed(
            request.context,
            OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL,
            "SMS command control updated.",
            status,
        )

    def reconcile_sms_command_control(
        self,
        request: SmsCommandControlConfigRequest,
    ) -> OperationalServiceReceipt | None:
        expected = {
            "sms_command_enabled": request.enabled,
            "sms_command_delete_processed": request.delete_processed,
            "sms_command_code_word": request.code_word,
            "sms_inbox_poll_interval_s": request.poll_interval_s,
            "sms_command_approved_senders": list(
                request.approved_senders
            ),
        }
        committed = self._committed_for(
            request.context.request_id,
            expected_values=expected,
        )
        if not committed:
            return None
        active = self._configuration.active_snapshot()
        self._sms_runtime.bind_config_generation(
            self._core.cfg,
            generation_id=active.generation_id,
            config_fingerprint=active.config_fingerprint,
        )
        status = self._sms_runtime.snapshot()
        status.update(
            config_generation_id=str(
                committed.get("generation_id") or ""
            ),
            config_fingerprint=str(
                committed.get("config_fingerprint") or ""
            ),
            active_config_generation_id=active.generation_id,
            active_config_fingerprint=active.config_fingerprint,
        )
        return self._confirmed(
            request.context,
            OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL,
            "SMS command configuration recovered from durable Core truth.",
            status,
        )

    def _committed_for(
        self,
        request_id: str,
        *,
        expected_values: Mapping[str, Any],
    ) -> dict[str, Any]:
        reader = getattr(
            self._configuration,
            "committed_generation_for_operational_request",
            None,
        )
        if not callable(reader):
            raise RuntimeError(
                "foundation_automation_configuration_evidence_unavailable"
            )
        receipt = dict(reader(request_id) or {})
        if not receipt:
            return {}
        provenance = dict(receipt.get("provenance") or {})
        if provenance.get("client_request_id") != request_id:
            return {}
        effective = dict(receipt.get("effective_config_values") or {})
        if not all(
            effective.get(key) == value
            for key, value in expected_values.items()
        ):
            return {}
        return receipt

    def _commit_sms_config(self, request: SmsCommandControlConfigRequest) -> str:
        return self._submit_operator_config(
            proposal_id=f"foundation-sms:{request.context.request_id}",
            values={
                "sms_command_enabled": request.enabled,
                "sms_command_delete_processed": request.delete_processed,
                "sms_command_code_word": request.code_word,
                "sms_inbox_poll_interval_s": request.poll_interval_s,
                "sms_command_approved_senders": list(request.approved_senders),
            },
            runtime_policy_state={
                "sms_command_control": {
                    "enabled": request.enabled,
                    "approved_sender_count": len(request.approved_senders),
                    "poll_interval_s": request.poll_interval_s,
                }
            },
            reason="sms_command_control_configuration",
            context=request.context,
        )

    def _submit_operator_config(
        self,
        *,
        proposal_id: str,
        values: Mapping[str, Any],
        runtime_policy_state: Mapping[str, Any],
        reason: str,
        context: FoundationOperatorRequestContext,
    ) -> str:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_automation_typed_context_required")
        service = self._configuration
        proposal = CoreConfigProposal(
            proposal_id=proposal_id,
            client_id=OWNER,
            client_role=CoreClientRole.OPERATOR_AUTOMATION,
            parent_generation_id=service.active_snapshot().generation_id,
            changes=CoreFrozenPayload.from_mapping(
                {
                    "source_kind": "operator_runtime",
                    "set_config_values": dict(values),
                    "runtime_policy_state": dict(runtime_policy_state),
                }
            ),
            evidence=CoreFrozenPayload.from_mapping(
                {
                    "provenance": {
                        "application_owner": OWNER,
                        "application_reason": reason,
                        "client_request_id": context.request_id,
                        "runtime_namespace": (
                            context.runtime_namespace.as_dict()
                        ),
                        "matrix_identity": (
                            context.matrix_identity.as_dict()
                        ),
                    }
                }
            ),
            created_at=time(),
        )
        receipt = service.submit_proposal(proposal)
        if receipt.disposition is CoreConfigDisposition.ACCEPTED:
            return ""
        return receipt.reason

    @staticmethod
    def _rejected(
        context: FoundationOperatorRequestContext,
        kind: OperationalKind,
        reason: str,
    ) -> OperationalServiceReceipt:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_automation_typed_context_required")
        return OperationalServiceReceipt(
            request_id=context.request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.REJECTED,
            message=f"Automation configuration rejected: {reason}",
            occurred_at=time(),
            runtime_namespace=context.runtime_namespace,
            matrix_identity=context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping({"reason": reason}),
        )

    @staticmethod
    def _confirmed(
        context: FoundationOperatorRequestContext,
        kind: OperationalKind,
        message: str,
        detail: Mapping[str, Any],
    ) -> OperationalServiceReceipt:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_automation_typed_context_required")
        return OperationalServiceReceipt(
            request_id=context.request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=message,
            occurred_at=time(),
            runtime_namespace=context.runtime_namespace,
            matrix_identity=context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(dict(detail or {})),
        )


__all__ = ("FoundationAutomationService",)
