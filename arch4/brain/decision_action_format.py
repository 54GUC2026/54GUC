"""Formatting helpers for decision actions and trace keys."""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import List, Optional

from arch4.interfaces import LeverAction, LeverType


def normalize_band_list(values) -> List[int]:
    bands: List[int] = []
    for raw_band in values or []:
        try:
            band = int(raw_band or 0)
        except (TypeError, ValueError):
            continue
        if band > 0:
            bands.append(band)
    return sorted(set(bands))


def action_brief(action: Optional[LeverAction]) -> str:
    if action is None:
        return "none"
    if action.lever in {LeverType.BAND_SELECTION, LeverType.BAND_LOCK}:
        bands = normalize_band_list(action.params.get("bands", []) or [])
        sig = "+".join(f"B{band}" for band in bands)
        escape_tag = "/escape_probe" if bool((action.params or {}).get("escape_probe", False)) else ""
        lever_name = "BAND_LOCK" if action.lever == LeverType.BAND_LOCK else "BAND_SELECTION"
        return f"{lever_name}[{sig}]/{action.intent or '-'}{escape_tag}"
    if action.lever == LeverType.PBAND:
        pband = _safe_positive_int((action.params or {}).get("pband", 0))
        raw_pool = list((action.params or {}).get("ca_pool") or [])
        if raw_pool:
            pool = normalize_band_list([*raw_pool, pband] if pband > 0 else raw_pool)
        else:
            dl_bands = (action.params or {}).get("dl_bands", []) or []
            pool = normalize_band_list([pband, *dl_bands] if pband > 0 else dl_bands)
        sig = "+".join(f"B{band}" for band in pool)
        anchor = f"B{pband}" if pband > 0 else "-"
        return f"PBAND[{anchor}->{sig}]/{action.intent or '-'}"
    if action.lever == LeverType.CELL_LOCK:
        return f"CELL_LOCK/{action.intent or '-'}"
    return f"{action.lever.name}/{action.intent or '-'}"


def band_sig_from_action(action: LeverAction) -> str:
    if action.lever == LeverType.PBAND:
        pband = _safe_positive_int((action.params or {}).get("pband", 0))
        bands = list((action.params or {}).get("ca_pool") or [])
        if not bands:
            bands = []
            if pband > 0:
                bands.append(pband)
            bands.extend((action.params or {}).get("dl_bands", []) or [])
        elif pband > 0:
            bands.append(pband)
        pool_sig = "+".join(f"B{band}" for band in normalize_band_list(bands))
        return f"B{pband}@pband:{pool_sig}" if pband > 0 and pool_sig else pool_sig
    bands = action.params.get("resolved_target_bands", action.params.get("bands", []))
    return "+".join(f"B{band}" for band in normalize_band_list(bands))


def objective_id_for_bands(prefix: str, bands: List[int]) -> str:
    return f"{prefix}:" + "+".join(f"B{band}" for band in normalize_band_list(bands))


def current_active_bands(modem: CellularIntelligenceGeneration) -> List[int]:
    active: List[int] = []
    for carrier in getattr(modem.cellular, 'carriers', []) or []:
        band = _safe_positive_int(getattr(carrier, "band", 0))
        try:
            state = int(getattr(carrier, "state", 0) or 0)
        except Exception:
            state = 0
        is_active = bool(getattr(carrier, "is_pcell", False)) or state >= 1
        if is_active and band > 0:
            active.append(band)
    if not active:
        primary_band = _safe_positive_int(getattr(modem.cellular, 'primary_band', 0))
        if primary_band > 0:
            active.append(primary_band)
    return sorted(set(active))


def _safe_positive_int(value) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
