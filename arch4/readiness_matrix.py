"""
5G class readiness contract — machine-readable export.

Source of truth: arch4/modem/catalog.py overlays, not manual docs.

Usage:
    from arch4.readiness_matrix import export_5g_class_readiness
    matrix = export_5g_class_readiness()   # returns dict

    # or run as script:
    python -m arch4.readiness_matrix
"""

from __future__ import annotations

import json
from typing import Any, Dict

from arch4.modem_live.profile_catalog import (
    match_modem_profile,
    modem_action_names,
    modem_backend_action_semantics,
    modem_backend_entry,
)


# ── Representative model strings for overlay matching ──────────────────────
# HiLink 5G backends share backend="b818" overlays, so we need model hints
# to hit the right model-specific overlay rather than the generic baseline.
_MODEL_HINTS: dict[str, tuple[str, str]] = {
    "h112":  ("h112",  "H112"),
    "h122":  ("b818",  "H122"),   # H122 overlays use backend="b818"
    "h155":  ("b818",  "H155"),   # H155 overlays use backend="b818"
    "mc888": ("mc888", "MC888"),
    "g5tc":  ("g5tc",  ""),   # resolves via FAMILY_ZTE_ZWRT to zte_zwrt_ubus_baseline
    "zte_zwrt": ("zte_zwrt", ""),
    "chateau_5g": ("chateau_5g", ""),
    "nr5103e": ("nr5103e", ""),
}

_REPRESENTATIVE_MODELS: dict[str, str] = {
    "h112": "H112-370",
    "h122": "H122-373",
    "h155": "H155-381",
    "mc888": "MC888",
    "g5tc": "G5TC",
    "zte_zwrt": "G5TC",
    "chateau_5g": "Chateau 5G ax",
    "nr5103e": "NR5103E",
}

_API_FAMILY_BY_FAMILY: dict[str, str] = {
    "huawei_hilink_like": "huawei_hilink_http_xml",
    "zte_webui_like": "zte_goform_http",
    "zte_zwrt_ubus_like": "zte_ubus_json_rpc",
    "routeros_at_like": "routeros_api_plus_at",
    "zyxel_webui_like": "zyxel_webui_json",
    "openwrt_modemmanager_like": "openwrt_modemmanager",
}

_NR_FIELD_ALIASES: dict[str, str] = {
    "nr5g_active": "nsa_active",
    "nr5g_band": "nr_band",
    "nr5g_pci": "nr_pci",
    "nr5g_arfcn": "nr_arfcn",
    "nr5g_cell_id": "nr_cell_id",
    "nr5g_rsrp_dbm": "nr_rsrp_dbm",
    "nr5g_rsrq_db": "nr_rsrq_db",
    "nr5g_sinr_db": "nr_sinr_db",
}

_MODEL_BOUNDARIES: dict[str, dict[str, Any]] = {
    "MC801": {
        "vendor": "ZTE",
        "family": "zte_webui_like",
        "api_family": "zte_goform_http",
        "readiness_class": "mc888",
        "support_status": "unsupported_explicit",
        "reuse_backend_hint": "",
        "fiveg_mode_contract": "nsa_only",
        "sa_support": "unsupported",
        "reason": (
            "Plain MC801 (Snapdragon X50) must not inherit MC801A/MC888 class truth. "
            "Current integrated code supports the MC801A/MC888/MC889 goform lane, "
            "not the older MC801 NSA-only variant."
        ),
        "source": "docs/HW_SKU_SPECS_ZTE_HUAWEI_5G_CPE_2026-04-12.md",
    },
}

def _overlay_for_class(backend: str):
    """Return the best generic (no carrier firmware) overlay for a backend class."""
    hint = _MODEL_HINTS.get(backend, (backend, ""))
    lookup_backend, model = hint
    return match_modem_profile(backend=lookup_backend, model=model, firmware="")


def _representative_model(backend: str) -> str:
    return _REPRESENTATIVE_MODELS.get(backend, backend.upper())


def _api_family(backend: str) -> str:
    entry = modem_backend_entry(backend)
    if entry is None:
        return "unknown"
    return _API_FAMILY_BY_FAMILY.get(entry.family, "unknown")

# ── The 5 active 5G classes covered by this matrix ─────────────────────────
# Selected as the classes with live or near-live backend implementations
# that expose a 5G NR surface (NSA or SA).
_5G_CLASSES = ("h112", "h122", "h155", "mc888", "g5tc")


def _lte_rf_readiness(backend: str, profile_provenance: str) -> str:
    """Assess LTE RF core field readiness from the best generic overlay.

    Returns: "confirmed" | "expected" | "partial" | "unknown"
    """
    # Match the generic (firmware_contains=()) overlay for the backend
    p = _overlay_for_class(backend)
    if p is None:
        return "unknown"
    # Fields every LTE backend must have
    core_fields = ("sinr_db", "rsrp_dbm", "rsrq_db", "serving_band",
                   "pci", "cell_id", "earfcn_dl")
    covered = sum(1 for f in core_fields if f in p.supported_fields)
    if covered == len(core_fields):
        return "confirmed" if p.provenance == "observed" else "expected"
    if covered >= len(core_fields) - 2:
        return "partial"
    return "unknown"


def _nr_detail_readiness(backend: str) -> str:
    """Assess NR detail field readiness.

    Returns: "confirmed" | "expected" | "partial" | "none"
    """
    p = _overlay_for_class(backend)
    if p is None:
        return "none"
    supported = {
        _NR_FIELD_ALIASES.get(field, field): field
        for field in (p.supported_fields or ())
        if field.startswith("nr") or field.startswith("nr5g")
    }
    nr_fields = ("nsa_active", "nr_rsrp_dbm", "nr_sinr_db")
    present = [f for f in nr_fields if f in supported]
    if len(present) == len(nr_fields):
        trust = p.get_field_trust("nsa_active")
        if trust == "untested":
            trust = p.get_field_trust("nr5g_active")
        return "confirmed" if trust == "confirmed" else "expected"
    if present:
        return "partial"
    return "none"


def _scheduler_source(backend: str) -> str:
    """Describe the scheduler authority mode for this class overlay."""
    p = _overlay_for_class(backend)
    if p is None:
        return "none"
    return str(p.get_scheduler_signal_mode() or "none")


def _action_trust_posture(backend: str) -> str:
    """Summarise the action surface posture for the generic (unlocked) lane.

    Returns: "full_control" | "band_only" | "rat_mode_only" | "read_only"
    """
    sem = modem_backend_action_semantics(backend)
    entry = modem_backend_entry(backend)
    if sem is None or entry is None:
        return "read_only"
    caps = entry.capabilities
    band = caps.get("supports_band_selection") or caps.get("supports_band_lock")
    cell = caps.get("supports_cell_lock")
    nr   = caps.get("supports_nr_band_lock")
    if band and cell and nr:
        return "full_control"
    if band and nr and sem.nr_control_mode in ("mixed", "nsa_band", "sa_band"):
        return "band_and_nr"
    if band:
        return "band_only"
    if sem.nr_control_mode in ("rat_mode",):
        return "rat_mode_only"
    return "read_only"


def _probe_suitability(backend: str) -> str:
    """Assess probe/test suitability for this backend class.

    Returns: "suitable" | "limited" | "not_suitable"
    """
    entry = modem_backend_entry(backend)
    if entry is None or not entry.implemented:
        return "not_suitable"
    caps = entry.capabilities
    # Must be able to read at minimum: RF + identity + connection
    has_rf = True   # all implemented backends expose RF
    has_action = (
        caps.get("supports_band_selection")
        or caps.get("supports_cell_lock")
        or caps.get("supports_nr_band_lock")
    )
    if has_rf and has_action:
        return "suitable"
    if has_rf:
        return "limited"
    return "not_suitable"


def _class_record(backend: str) -> Dict[str, Any]:
    """Build the full readiness record for one 5G class."""
    entry = modem_backend_entry(backend)
    sem = modem_backend_action_semantics(backend)
    p     = _overlay_for_class(backend)

    lte_readiness = _lte_rf_readiness(backend, p.provenance if p else "")
    nr_readiness  = _nr_detail_readiness(backend)
    sched_source  = _scheduler_source(backend)
    action_posture = _action_trust_posture(backend)
    probe_suit     = _probe_suitability(backend)

    # Collect supported NR fields from generic overlay
    nr_fields_available: list[str] = []
    if p is not None:
        nr_fields_available = sorted(
            {
                _NR_FIELD_ALIASES.get(f, f)
                for f in p.supported_fields
                if f.startswith("nr") or f.startswith("nr5g")
            }
        )

    # Action trust from generic overlay
    action_trust: Dict[str, str] = {}
    if p is not None:
        action_trust = {a: p.get_action_trust(a) for a in modem_action_names()}

    return {
        "backend": backend,
        "display_name": entry.display_name if entry else backend,
        "family": entry.family if entry else "unknown",
        "api_family": _api_family(backend),
        "representative_model": _representative_model(backend),
        "implemented": entry.implemented if entry else False,
        "lte_rf_core_readiness": lte_readiness,
        "nr_detail_readiness": nr_readiness,
        "scheduler_source": sched_source,
        "action_trust_posture": action_posture,
        "probe_test_suitability": probe_suit,
        "nr_control_mode": sem.nr_control_mode if sem else "none",
        "ca_visibility": sem.ca_visibility if sem else "none",
        "supports_nr_band_lock": entry.capabilities.get("supports_nr_band_lock", False) if entry else False,
        "supports_cell_lock": entry.capabilities.get("supports_cell_lock", False) if entry else False,
        "nr_fields_in_generic_overlay": nr_fields_available,
        "action_trust_generic_overlay": action_trust,
        "generic_overlay_provenance": p.provenance if p else "none",
        "generic_overlay_id": p.profile_id if p else "none",
    }


def export_5g_class_readiness(
    classes: tuple[str, ...] = _5G_CLASSES,
) -> Dict[str, Any]:
    """Export the machine-readable 5G class readiness matrix.

    Source: arch4/modem/catalog.py overlays only — no manual data.

    Returns a dict with schema, timestamp, and per-class records.
    Each record encodes:
      - LTE RF core readiness
      - NR detail readiness
      - Scheduler source (how synthetic lane gets CQI/QAM input)
      - Action trust posture (what the generic/unlocked lane can do)
      - Probe/test suitability
    """
    import time
    return {
        "schema": "5g_class_readiness_v1",
        "generated_ts": round(time.time()),
        "source": "arch4/modem/catalog.py",
        "model_boundaries": dict(_MODEL_BOUNDARIES),
        "classes": {cls: _class_record(cls) for cls in classes},
    }


if __name__ == "__main__":
    matrix = export_5g_class_readiness()
    print(json.dumps(matrix, indent=2))
