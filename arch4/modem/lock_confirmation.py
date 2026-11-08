"""Shared modem lock confirmation helpers.

Lock-capable backends should report the same proof shape after a write:
requested target, write acceptance, backend readback, and observed effect.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional

from arch4.coerce import safe_bool


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def normalized_bands(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        band = _safe_int(value)
        if band is None or band <= 0 or band in seen:
            continue
        seen.add(band)
        result.append(band)
    return result


def parse_qnwlock_readback(raw: Any) -> Dict[str, Any]:
    """Parse a Quectel QNWLOCK response without over-trusting its format."""
    text = str(raw or "").strip()
    parsed: Dict[str, Any] = {
        "raw": text,
        "available": bool(text),
        "enabled": None,
        "earfcn": None,
        "pci": None,
    }
    if not text:
        return parsed

    # Match numeric tokens, but do not treat the "4" in common/4g as a field.
    ints = [int(m.group(0)) for m in re.finditer(r"(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])", text)]
    parsed["parsed_ints"] = ints

    upper = text.upper()
    if "ERROR" in upper:
        parsed["error"] = True
        return parsed
    if ints:
        if ints[0] in (0, 1):
            parsed["enabled"] = bool(ints[0])
            if ints[0] == 1 and len(ints) >= 3:
                parsed["earfcn"] = ints[1]
                parsed["pci"] = ints[2]
            return parsed
        if len(ints) >= 2:
            parsed["enabled"] = True
            parsed["earfcn"] = ints[-2]
            parsed["pci"] = ints[-1]
    return parsed


def effective_cell_match(effective: Mapping[str, Any], requested: Mapping[str, Any]) -> Optional[bool]:
    if safe_bool(requested.get("unlock"), False):
        return None
    req_pci = _safe_int(requested.get("pci"))
    req_earfcn = _safe_int(requested.get("earfcn"))
    eff_pci = _safe_int(effective.get("pci"))
    eff_earfcn = _safe_int(effective.get("earfcn") or effective.get("earfcn_dl"))
    if req_pci is None or req_earfcn is None or eff_pci is None or eff_earfcn is None:
        return None
    return req_pci == eff_pci and req_earfcn == eff_earfcn


def configured_band_match(configured_bands: Iterable[Any], requested_bands: Iterable[Any]) -> Optional[bool]:
    requested = set(normalized_bands(requested_bands))
    configured = set(normalized_bands(configured_bands))
    if not requested or not configured:
        return None
    return configured == requested


def effective_band_match(observed_bands: Iterable[Any], requested_bands: Iterable[Any]) -> Optional[bool]:
    requested = set(normalized_bands(requested_bands))
    observed = set(normalized_bands(observed_bands))
    if not requested or not observed:
        return None
    return observed.issubset(requested)


def build_lock_confirmation(
    *,
    lock_type: str,
    requested: Mapping[str, Any],
    accepted: bool,
    readback: Optional[Mapping[str, Any]] = None,
    effective: Optional[Mapping[str, Any]] = None,
    effective_cell_match_value: Optional[bool] = None,
    effective_band_match_value: Optional[bool] = None,
    effective_pband_match: Optional[bool] = None,
    configured_band_match_value: Optional[bool] = None,
    source: str = "",
) -> Dict[str, Any]:
    proof: Dict[str, Any] = {
        "schema": "lock_confirmation.v1",
        "type": str(lock_type or "").strip() or "lock",
        "requested": dict(requested or {}),
        "accepted": bool(accepted),
        "readback": dict(readback or {"available": False}),
        "effective": dict(effective or {}),
        "effective_cell_match": effective_cell_match_value,
        "effective_band_match": effective_band_match_value,
    }
    if configured_band_match_value is not None:
        proof["configured_band_match"] = bool(configured_band_match_value)
    if effective_pband_match is not None:
        proof["effective_pband_match"] = bool(effective_pband_match)
    if source:
        proof["source"] = str(source)
    return proof
