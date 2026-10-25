from __future__ import annotations

from typing import Any

from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.interfaces import normalize_band_list, parse_band_signature


def normalize_runtime_band_values(values: Any) -> list[int]:
    if values in (None, "", [], {}):
        return []
    if isinstance(values, str):
        return normalize_band_list(parse_band_signature(values))
    if isinstance(values, int):
        return [values] if int(values) > 0 else []
    try:
        return normalize_band_list(list(values))
    except Exception:
        return []


def _primary_band(
    primary_band: Any,
    serving_band: Any,
    status: dict[str, Any],
) -> list[int]:
    for value in (
        primary_band,
        serving_band,
        status.get("primary_band"),
        status.get("serving_band"),
    ):
        try:
            band = int(value or 0)
        except Exception:
            band = 0
        if band > 0:
            return [band]
    return []


def _current_observed_bands(
    *,
    band_set: Any,
    active_band_set: Any,
    ca_band_signature: Any,
    primary_band: Any,
    serving_band: Any,
    status: dict[str, Any],
) -> list[int]:
    for candidate in (
        band_set,
        active_band_set,
        status.get("band_set"),
        status.get("active_band_set"),
        status.get("band_set_signature"),
        status.get("active_band_set_signature"),
        ca_band_signature,
        status.get("ca_band_signature"),
    ):
        bands = normalize_runtime_band_values(candidate)
        if bands:
            return bands
    return _primary_band(primary_band, serving_band, status)


def remembered_runtime_bands(memory: Any) -> list[int]:
    """Return no learned bands; runtime scope is not a learning owner."""
    _ = memory
    return []


def resolve_available_band_values(
    *,
    selected_band_set: Any,
    band_set: Any,
    active_band_set: Any,
    ca_band_signature: Any,
    primary_band: Any,
    serving_band: Any,
    memory: Any = None,
    status: dict[str, Any] | None = None,
) -> list[int]:
    """Resolve bands from explicit lower-boundary primitive values."""

    live_status: dict[str, Any] = {}
    if isinstance(status, dict):
        live_status = dict(status)

    for candidate in (
        selected_band_set,
        live_status.get("selected_band_set"),
        live_status.get("allowed_bands"),
    ):
        bands = normalize_runtime_band_values(candidate)
        if bands:
            return bands

    remembered = remembered_runtime_bands(memory)
    current = _current_observed_bands(
        band_set=band_set,
        active_band_set=active_band_set,
        ca_band_signature=ca_band_signature,
        primary_band=primary_band,
        serving_band=serving_band,
        status=live_status,
    )
    if remembered:
        return normalize_band_list(list(remembered) + list(current))

    if current:
        return current

    return []


def resolve_runtime_available_bands(
    *,
    modem: CellularIntelligenceGeneration | None = None,
    memory: Any = None,
    status: dict[str, Any] | None = None,
) -> list[int]:
    """Resolve controller-visible bands from immutable accepted facts."""

    if modem is not None and not isinstance(
        modem, CellularIntelligenceGeneration
    ):
        raise TypeError(
            "runtime_band_scope_requires_cellular_intelligence_generation"
        )
    cellular = modem.cellular if modem is not None else None
    return resolve_available_band_values(
        selected_band_set=getattr(cellular, "selected_band_set", ()),
        band_set=getattr(cellular, "band_set", ()),
        active_band_set=getattr(cellular, "active_band_set", ()),
        ca_band_signature=getattr(cellular, "ca_band_signature", ""),
        primary_band=getattr(cellular, "primary_band", 0),
        serving_band=getattr(cellular, "serving_band", 0),
        memory=memory,
        status=status,
    )
