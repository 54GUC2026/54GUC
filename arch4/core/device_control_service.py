"""The only Core owner for non-cellular device controls such as Wi-Fi radios."""

from __future__ import annotations

from pathlib import Path
from time import time
from typing import Any

from arch4.command_transactions import CommandRootState
from arch4.core.command_request_builder import build_core_command_request
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.core_contracts.command_targets import CoreWifiRadioTarget
from arch4.core_contracts.commands import CoreCommandOrigin
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.interfaces import LeverType
from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
from arch4.operational_service_contracts import (
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    WifiRadioControlRequest,
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
    require_product_operational_effect_permit,
)


OWNER = "54guc.core.device_control"


class CoreDeviceControlService:
    def __init__(self, core: Any, *, persistence_dir: str | Path) -> None:
        del persistence_dir
        if core is None:
            raise TypeError("core_device_control_owner_required")
        if not callable(getattr(getattr(core, "command_gateway", None), "submit", None)):
            raise TypeError("core_device_control_command_gateway_required")
        self._core = core

    def read_diagnostics(self, request: RouterReadDiagnosticRequest, *,
                         permit: ProductOperationalEffectPermit) -> OperationalServiceReceipt:
        require_product_operational_effect_permit(
            permit, request, expected_kind=OperationalKind.READ_DIAGNOSTICS)
        try:
            result = self._core.input_module.request_read_diagnostics(
                action=request.action, request_id=request.context.request_id,
                expires_at=request.context.expires_at)
        except ReadDiagnosticUnavailable as exc:
            result = {"message": str(exc), "reason": "read_diagnostics_unavailable"}
            disposition = OperationalDisposition.REJECTED
        else:
            disposition = OperationalDisposition.ACCEPTED
        return OperationalServiceReceipt(
            request_id=request.context.request_id, kind=OperationalKind.READ_DIAGNOSTICS,
            owner=OWNER, disposition=disposition,
            message=result["message"], occurred_at=time(),
            runtime_namespace=request.context.ingress.matrix_identity.namespace,
            matrix_identity=request.context.ingress.matrix_identity,
            detail=CoreFrozenPayload.from_mapping({**result, "action": request.action}))

    def inspect_network_settings(
        self, request: NetworkSettingsInspectionRequest, *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, NetworkSettingsInspectionRequest):
            raise TypeError("core_network_inspection_typed_request_required")
        require_product_operational_effect_permit(
            permit, request, expected_kind=OperationalKind.INSPECT_NETWORK_SETTINGS,
        )
        demand_id = self._core.input_module.request_network_settings_inspection(
            expires_at=request.context.expires_at,
        )
        if not isinstance(demand_id, str) or not demand_id:
            raise RuntimeError("modem_live_network_demand_receipt_required")
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.INSPECT_NETWORK_SETTINGS, owner=OWNER,
            disposition=OperationalDisposition.ACCEPTED,
            message="Network settings inspection requested.",
            occurred_at=time(),
            runtime_namespace=request.context.ingress.matrix_identity.namespace,
            matrix_identity=request.context.ingress.matrix_identity,
            detail=CoreFrozenPayload.from_mapping({"reason": "inspection_queued", "inspection_demand_id": demand_id}),
        )

    def set_wifi_radio(
        self,
        request: WifiRadioControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, WifiRadioControlRequest):
            raise TypeError("core_device_control_typed_wifi_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.WIFI_SET_RADIO,
        )
        now = time()
        command = self._wifi_command(request)
        result = dict(self._core.command_gateway.submit(command, now=now))
        submitted = result.get("submitted") is True
        reason = str(result.get("reason") or "")
        return self._receipt(
            request,
            (
                OperationalDisposition.ACCEPTED
                if submitted
                else OperationalDisposition.REJECTED
            ),
            (
                "Wi-Fi command accepted by Core."
                if submitted
                else f"Wi-Fi command rejected: {reason or 'unknown reason'}"
            ),
            {
                "command_root_id": str(result.get("root_id") or ""),
                "physical_action_root_id": str(
                    result.get("physical_action_root_id") or ""
                ),
                "reason": reason,
            },
            now=time(),
        )

    @staticmethod
    def _wifi_command(
        request: WifiRadioControlRequest,
    ):
        band = {
            "2.4": "2.4ghz",
            "5": "5ghz",
            "6": "6ghz",
        }[request.radio]
        return build_core_command_request(
            request.context.ingress,
            request_id=request.context.request_id,
            correlation_id=request.context.request_id,
            client_id=request.context.client_id,
            client_role=request.context.client_role,
            origin=CoreCommandOrigin.OPERATOR,
            command_kind=LeverType.WIFI_RADIO.name,
            target=CoreWifiRadioTarget(
                band=band,
                enabled=request.enabled,
            ),
            parameters={
                "reason": "Manual Wi-Fi radio control",
                "intent": "manual",
                "objective_id": request.context.request_id,
                "action_parameters": {},
            },
            capability_reference=lever_action_authority(
                LeverType.WIFI_RADIO
            ).capability_key,
            created_at=request.context.created_at,
            expires_at=request.context.expires_at,
            confirmed=True,
        )

    def reconcile_wifi_radio(
        self,
        request: WifiRadioControlRequest,
    ) -> OperationalServiceReceipt | None:
        if not isinstance(request, WifiRadioControlRequest):
            raise TypeError("core_device_control_typed_wifi_request_required")
        authority = self._core.command_authority
        root = authority.core_command_root(request.context.request_id)
        if root is None:
            return None
        if root.request != self._wifi_command(request):
            raise RuntimeError(
                "core_wifi_command_authority_request_conflict"
            )
        if root.state is CommandRootState.TERMINAL:
            return self._receipt(
                request,
                OperationalDisposition.REJECTED,
                (
                    "Wi-Fi command rejected: "
                    + root.terminal_reason
                ),
                {
                    "command_root_id": root.request_root_id,
                    "physical_action_root_id": (
                        root.physical_action_root_id
                    ),
                    "reason": root.terminal_reason,
                    "command_state": root.state.value,
                },
                now=time(),
            )
        if root.state is not CommandRootState.DISPATCHED:
            return None
        return self._receipt(
            request,
            OperationalDisposition.ACCEPTED,
            "Wi-Fi command recovered from CommandAuthority.",
            {
                "command_root_id": root.request_root_id,
                "physical_action_root_id": (
                    root.physical_action_root_id
                ),
                "reason": "command_authority_dispatch_retained",
                "command_state": root.state.value,
            },
            now=time(),
        )

    @staticmethod
    def _receipt(
        request: WifiRadioControlRequest,
        disposition: OperationalDisposition,
        message: str,
        detail: dict[str, Any],
        *,
        now: float,
    ) -> OperationalServiceReceipt:
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.WIFI_SET_RADIO,
            owner=OWNER,
            disposition=disposition,
            message=message,
            occurred_at=now,
            runtime_namespace=(
                request.context.ingress.matrix_identity.namespace
            ),
            matrix_identity=request.context.ingress.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(detail),
        )


__all__ = ("CoreDeviceControlService",)
