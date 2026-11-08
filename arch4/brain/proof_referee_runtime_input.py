"""Runtime-register input adapter for delayed proof/referee closure."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from arch4.brain.proof_referee_context_owner import provider_identity
from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)

OWNER = "arch4.brain.proof_referee_runtime_input"


def staged_roi_inputs_from_brain_fused_register(fused_register: Any) -> dict[str, Any]:
    """Extract delayed-ROI inputs from the Brain fused register."""

    payload = _fused_payload(fused_register)
    if not payload:
        return {}
    tick_payload = payload.get("modem_tick") if isinstance(payload.get("modem_tick"), dict) else {}
    modem_now = tick_payload.get("cellular_intelligence")
    computed_now = tick_payload.get("derived_signals")
    if not isinstance(modem_now, CellularIntelligenceGeneration):
        raise TypeError(
            "brain_fused_register_requires_cellular_intelligence_generation"
        )
    if not isinstance(computed_now, DerivedSignalFrames):
        raise TypeError("brain_fused_register_requires_derived_signal_frames")
    extra_data = dict(tick_payload.get("extra") or {}) if isinstance(tick_payload.get("extra"), dict) else {}
    incident = dict(tick_payload.get("incident_assessment") or {}) if isinstance(tick_payload.get("incident_assessment"), dict) else {}
    if not incident and isinstance(extra_data.get("incident_assessment"), dict):
        incident = dict(extra_data.get("incident_assessment") or {})

    runtime_registers = _mapping(payload.get("runtime_registers"))
    radio_register = _register_value(runtime_registers, "radio")
    ca_register = _register_value(runtime_registers, "ca")
    modem_now = _apply_runtime_context(
        modem_now,
        radio_register=radio_register,
        ca_register=ca_register,
    )

    extra = {
        "health_stage": str(extra_data.get("health_stage", "") or ""),
        "sample_stale": bool(extra_data.get("sample_stale", False)),
        "live_register_stale": bool(extra_data.get("live_register_stale", False)),
        "runtime_health": dict(extra_data.get("runtime_health") or {}) if isinstance(extra_data.get("runtime_health"), dict) else {},
        "health_checks": dict(extra_data.get("health_checks") or {}) if isinstance(extra_data.get("health_checks"), dict) else {},
        "incident_assessment": incident,
    }
    return {
        "modem_now": modem_now,
        "computed_now": computed_now,
        "extra": extra,
        "incident_assessment": incident,
    }


def _apply_runtime_context(
    modem: CellularIntelligenceGeneration,
    *,
    radio_register: Mapping[str, Any],
    ca_register: Mapping[str, Any],
) -> CellularIntelligenceGeneration:
    identity_changes: dict[str, Any] = {}
    cellular_changes: dict[str, Any] = {}

    provider_plmn = radio_register.get("provider_plmn")
    if (
        not provider_identity(modem.identity.provider_plmn)
        and provider_identity(provider_plmn)
    ):
        identity_changes["provider_plmn"] = str(provider_plmn or "")
    provider_name = radio_register.get("provider_name")
    if (
        not provider_identity(modem.identity.provider_name)
        and provider_identity(provider_name)
    ):
        identity_changes["provider_name"] = str(provider_name or "")

    pool_signature = str(
        radio_register.get("configured_band_pool_signature")
        or ca_register.get("configured_band_pool_signature")
        or ""
    )
    configured_pool = _bands(
        radio_register.get("configured_band_pool")
        or ca_register.get("configured_band_pool")
        or pool_signature
    )
    if not modem.identity.configured_band_pool and configured_pool:
        identity_changes["configured_band_pool"] = tuple(configured_pool)
    if (
        not str(modem.identity.configured_band_pool_signature or "")
        and pool_signature
    ):
        identity_changes["configured_band_pool_signature"] = pool_signature

    serving_band = _int(radio_register.get("serving_band"))
    if int(modem.cellular.primary_band or 0) <= 0 and serving_band > 0:
        cellular_changes["primary_band"] = serving_band
    if not modem.cellular.selected_band_set and configured_pool:
        cellular_changes["selected_band_set"] = tuple(configured_pool)
    if (
        not str(modem.cellular.selected_band_set_signature or "")
        and pool_signature
    ):
        cellular_changes["selected_band_set_signature"] = pool_signature

    active_signature = str(
        radio_register.get("active_band_set_signature")
        or ca_register.get("active_band_set_signature")
        or ca_register.get("ca_component_band_signature")
        or ""
    )
    if not str(modem.cellular.ca_band_signature or "") and active_signature:
        cellular_changes["ca_band_signature"] = active_signature

    identity = (
        replace(modem.identity, **identity_changes)
        if identity_changes
        else modem.identity
    )
    cellular = (
        replace(modem.cellular, **cellular_changes)
        if cellular_changes
        else modem.cellular
    )
    if identity is modem.identity and cellular is modem.cellular:
        return modem
    return replace(
        modem,
        identity=identity,
        cellular=cellular,
    )


def _bands(value: Any) -> list[int]:
    raw = value
    if isinstance(raw, str):
        raw = (
            raw.replace("B", "")
            .replace("b", "")
            .replace("+", ",")
            .split(",")
        )
    if isinstance(raw, (int, float)):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    out: list[int] = []
    for item in raw:
        band = _int(item)
        if band > 0 and band not in out:
            out.append(band)
    return out


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _fused_payload(fused_register: Any) -> dict[str, Any]:
    fused_ready = getattr(fused_register, "ready", None)
    fused_payload = getattr(fused_register, "payload", None)
    if fused_ready is not None or fused_payload is not None:
        if not bool(fused_ready):
            return {}
        return dict(fused_payload) if isinstance(fused_payload, dict) else {}
    fused = fused_register if isinstance(fused_register, dict) else {}
    if not isinstance(fused, dict) or not bool(fused.get("ready", False)):
        return {}
    payload = fused.get("payload") if isinstance(fused.get("payload"), dict) else {}
    return dict(payload) if isinstance(payload, dict) else {}


def _register_value(registers: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    row = registers.get(name) if isinstance(registers, Mapping) else {}
    value = row.get("value") if isinstance(row, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["OWNER", "staged_roi_inputs_from_brain_fused_register"]
