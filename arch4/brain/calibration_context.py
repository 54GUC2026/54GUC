"""
Pure calibration radio-context helpers.

CalibrationAuthority owns candidate lifecycle. This module owns the small,
testable rules for deriving and storing band/cell context keys so context
changes cannot drift across authority, runtime, and tests.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arch4.brain.calibration_scoring import safe_float, summary_scalar
from arch4.runtime_identity import runtime_session_scope_key


def _positive_int_from_attr(value: Any) -> int:
    out = safe_float(value)
    if out is None:
        return 0
    parsed = int(out)
    return parsed if parsed > 0 else 0


def context_key_from_summary(summary: Optional[Dict[str, Any]]) -> str:
    if not isinstance(summary, dict):
        return ""
    band = int(summary_scalar(summary, "dominant_context_band") or 0)
    cell = int(summary_scalar(summary, "dominant_context_cell") or 0)
    if band <= 0 or cell <= 0:
        return ""
    return f"{band}|{cell}"


def context_key_from_calibration_status(calibration_status: Optional[Dict[str, Any]]) -> str:
    if not isinstance(calibration_status, dict):
        return ""
    context_key = str(calibration_status.get("current_context_key", "") or "").strip()
    if context_key:
        return context_key
    band = int(
        calibration_status.get(
            "current_context_band",
            calibration_status.get("serving_band", calibration_status.get("band", 0)),
        )
        or 0
    )
    cell = int(
        calibration_status.get(
            "current_context_cell",
            calibration_status.get("pci", calibration_status.get("cell", 0)),
        )
        or 0
    )
    if band <= 0 or cell <= 0:
        return ""
    return f"{band}|{cell}"


def positive_int_from_mapping(data: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.upper().startswith("B"):
                raw = raw[1:]
        out = safe_float(raw)
        if out is None:
            continue
        value = int(out)
        if value > 0:
            return value
    return 0


def context_key_from_sample(sample: Optional[Dict[str, Any]]) -> str:
    if not isinstance(sample, dict):
        return ""
    band = positive_int_from_mapping(
        sample,
        "current_context_band",
        "serving_band",
        "primary_band",
        "band",
    )
    cell = positive_int_from_mapping(
        sample,
        "current_context_cell",
        "pci",
        "serving_pci",
        "cell_id",
        "cell",
    )
    if band <= 0 or cell <= 0:
        return ""
    return f"{band}|{cell}"


def attach_current_radio_context(
    calibration_status: Optional[Dict[str, Any]],
    *,
    modem: Any = None,
) -> Dict[str, Any]:
    """Attach the current serving radio context to a calibration status row.

    Calibration status owns the explicit context fields. When that status has
    not yet emitted them, this helper is the single boundary allowed to derive
    them from the live modem sample for calibration/memory consumers.
    """

    out = dict(calibration_status or {})
    band = positive_int_from_mapping(
        out,
        "current_context_band",
        "serving_band",
        "band",
    )
    cell = positive_int_from_mapping(
        out,
        "current_context_cell",
        "pci",
        "cell",
    )
    if modem is not None:
        if band <= 0:
            band = _positive_int_from_attr(getattr(modem.cellular, 'primary_band', 0))
        if cell <= 0:
            cell = _positive_int_from_attr(getattr(modem.cellular, 'pci', 0))

    if band > 0:
        out.setdefault("serving_band", band)
        out.setdefault("current_context_band", band)
    if cell > 0:
        out.setdefault("pci", cell)
        out.setdefault("current_context_cell", cell)

    if band > 0 and cell > 0:
        out.setdefault("current_context_key", f"{band}|{cell}")
        out.setdefault("current_context_label", f"B{band} / PCI {cell}")
    return out


def split_context_key(context_key: str) -> tuple[int, int]:
    try:
        band_text, cell_text = str(context_key or "").split("|", 1)
        return int(band_text), int(cell_text)
    except Exception:
        return 0, 0


def context_baseline_storage_key(
    context_key: str,
    *,
    runtime_context: Optional[Dict[str, Any]] = None,
) -> str:
    normalized_context_key = str(context_key or "").strip()
    if not normalized_context_key:
        return ""
    scoped_context = runtime_context if isinstance(runtime_context, dict) else {}
    scope_key = runtime_session_scope_key(scoped_context)
    if not scope_key:
        return normalized_context_key
    return f"{normalized_context_key}::{scope_key}"
