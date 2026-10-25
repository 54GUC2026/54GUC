"""Explicit customer-boundary builder for immutable Core command requests."""

from __future__ import annotations

from typing import Any, Mapping
from arch4.core_contracts.network_settings import network_target_from_mapping

from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.commands import (
    CoreCommandIngressContext,
    CoreCommandOrigin,
    CoreCommandRequest,
)
from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreBandAutoTarget,
    CoreMobileDataTarget,
    CoreBandSetTarget,
    CoreCellLockTarget,
    CoreCommandTarget,
    CoreNrBandTarget,
    CorePBandTarget,
    CoreRestartTarget,
    CoreTrafficPresenceTarget,
    CoreTrafficVolumeTarget,
    CoreUeCapabilityTarget,
    CoreWifiRadioTarget,
    CoreRadioModeTarget,
)
from arch4.core_contracts.payload import CoreFrozenPayload


_PHYSICAL_TARGET_VOCABULARY = frozenset(
    {
        "bands",
        "dl_bands",
        "ca_pool",
        "pband",
        "nr_band",
        "nr_bands",
        "domain",
        "pci",
        "earfcn",
        "band",
        "unlock",
        "emergency_fast",
        "keepalive_interval_s",
        "background_mbps",
        "sqm_limit_mbps",
        "dl_256qam",
        "ul_64qam",
        "restart",
        "enabled",
        "subscription_key", "roaming_enabled", "selection", "target_plmn", "apn",
        "mode", "expected_mode", "lte_bands", "band_selection_mode", "profile_definition_hash", "retry_delay_s",
    }
)

_TARGET_FIELDS_BY_KIND = {
    "RADIO_MODE": frozenset({"mode", "expected_mode", "lte_bands", "nr_bands", "band_selection_mode", "subscription_key", "profile_definition_hash", "retry_delay_s"}),
    "MOBILE_DATA": frozenset({"enabled"}),
    "BAND_SELECTION": frozenset({"bands"}),
    "BAND_LOCK": frozenset({"bands"}),
    "BAND_AUTO": frozenset({"domain"}),
    "PBAND": frozenset({"pband", "ca_pool"}),
    "CELL_LOCK": frozenset({"pci", "earfcn", "band", "unlock"}),
    "NR_BAND_LOCK": frozenset({"nr_bands"}),
    "RESTART": frozenset({"emergency_fast"}),
    "TRAFFIC_PRESENCE": frozenset(
        {"keepalive_interval_s", "background_mbps"}
    ),
    "TRAFFIC_VOLUME": frozenset({"background_mbps", "sqm_limit_mbps"}),
    "UE_CAPABILITY": frozenset({"dl_256qam", "ul_64qam", "restart"}),
    "WIFI_RADIO": frozenset({"band", "enabled"}),
    "NETWORK_SETTINGS": frozenset({"subscription_key", "roaming_enabled", "selection", "target_plmn", "apn"}),
}


def _reject_unclaimed_physical_semantics(
    kind: str,
    params: Mapping[str, Any],
) -> None:
    claimed = _TARGET_FIELDS_BY_KIND.get(kind, frozenset())
    unclaimed = sorted(
        (set(params) & _PHYSICAL_TARGET_VOCABULARY) - set(claimed)
    )
    if unclaimed:
        raise ValueError(
            "core_command_target_unclaimed_physical_fields:"
            + ",".join(unclaimed)
        )


def build_core_command_target(
    command_kind: str,
    parameters: Mapping[str, Any],
) -> CoreCommandTarget:
    """Build the validated target at the customer boundary."""

    kind = str(command_kind or "").strip().upper()
    params = dict(parameters or {})
    _reject_unclaimed_physical_semantics(kind, params)
    if kind == "RADIO_MODE":
        return CoreRadioModeTarget(**{key: params.get(key, 1800.0 if key == "retry_delay_s" else None) for key in _TARGET_FIELDS_BY_KIND[kind]})
    if kind == "MOBILE_DATA":
        return CoreMobileDataTarget(enabled=params.get("enabled"))
    if kind in {"BAND_SELECTION", "BAND_LOCK"}:
        return CoreBandSetTarget(
            bands=tuple(params.get("bands") or ()),
            command_kind=kind,
        )
    if kind == "BAND_AUTO":
        return CoreBandAutoTarget(domain=str(params.get("domain") or ""))
    if kind == "PBAND":
        return CorePBandTarget(
            pband=params.get("pband", 0),
            ca_pool=tuple(params.get("ca_pool") or ()),
        )
    if kind == "CELL_LOCK":
        return CoreCellLockTarget(
            pci=params.get("pci", 0),
            earfcn=params.get("earfcn", 0),
            band=params.get("band"),
            unlock=params.get("unlock", False),
        )
    if kind == "NR_BAND_LOCK":
        return CoreNrBandTarget(
            nr_bands=tuple(params.get("nr_bands") or ()),
        )
    if kind == "RESTART":
        return CoreRestartTarget(
            emergency_fast=params.get("emergency_fast", False),
        )
    if kind == "TRAFFIC_PRESENCE":
        return CoreTrafficPresenceTarget(
            keepalive_interval_s=params.get("keepalive_interval_s", 0.0),
            background_mbps=params.get("background_mbps", 0.0),
        )
    if kind == "TRAFFIC_VOLUME":
        return CoreTrafficVolumeTarget(
            background_mbps=params.get("background_mbps"),
            sqm_limit_mbps=params.get("sqm_limit_mbps"),
        )
    if kind == "UE_CAPABILITY":
        return CoreUeCapabilityTarget(
            dl_256qam=params.get("dl_256qam"),
            ul_64qam=params.get("ul_64qam"),
            restart=params.get("restart"),
        )
    if kind == "NETWORK_SETTINGS":
        return network_target_from_mapping({"lever": kind, **{key: params.get(key) for key in _TARGET_FIELDS_BY_KIND[kind]}})
    if kind == "WIFI_RADIO":
        return CoreWifiRadioTarget(
            band=str(params.get("band") or ""),
            enabled=params.get("enabled"),
        )
    raise ValueError(f"core_command_target_kind_invalid:{kind or 'missing'}")


def build_core_command_request(
    context: CoreCommandIngressContext,
    *,
    request_id: str,
    correlation_id: str,
    client_id: str,
    client_role: CoreClientRole,
    origin: CoreCommandOrigin,
    command_kind: str,
    target: CoreCommandTarget,
    parameters: Mapping[str, Any],
    capability_reference: str,
    created_at: float,
    expires_at: float,
    confirmed: bool,
    safety_context: Mapping[str, Any] | None = None,
    autonomous_lease: str = "",
) -> CoreCommandRequest:
    """Bind customer intent to the accepted Core generation."""

    if not isinstance(context, CoreCommandIngressContext):
        raise TypeError("core_command_builder_ingress_context_required")
    command_name = str(command_kind or "").strip().upper()
    if not isinstance(target, CORE_COMMAND_TARGET_TYPES):
        raise TypeError("core_command_builder_typed_target_required")
    if target.command_kind != command_name:
        raise ValueError("core_command_builder_target_lever_mismatch")
    safety = dict(safety_context or {})
    return CoreCommandRequest(
        request_id=request_id,
        correlation_id=correlation_id,
        client_id=client_id,
        client_role=client_role,
        origin=origin,
        matrix_identity=context.matrix_identity,
        identity_code=context.identity_code,
        command_kind=command_name,
        target=target,
        parameters=CoreFrozenPayload.from_mapping(dict(parameters or {})),
        capability_reference=capability_reference,
        created_at=created_at,
        expires_at=expires_at,
        confirmed=confirmed,
        safety_context=CoreFrozenPayload.from_mapping(safety),
        autonomous_lease=autonomous_lease,
    )


__all__ = ("build_core_command_request", "build_core_command_target")
