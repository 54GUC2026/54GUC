"""Canonical RAT-scoped runtime model.

This owner turns flat LTE/NR modem facts into explicit RAT groups. It does not
read modem transport, paint GUI, write telemetry files, or decide actions.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.modem_live.nsa_snapshot import (
    IDENTITY_ABSENT,
    NSA_PHYSICAL_SNAPSHOT_SCHEMA,
    RAT_MODE_NSA,
    RAT_MODE_SA_UNSUPPORTED,
    validate_nsa_physical_snapshot,
)


RAT_MODEL_OWNER = "runtime.rat_model"
RAT_SNAPSHOT_SCHEMA = "54guc.rat_snapshot.v1"

LTE_ONLY = "LTE_ONLY"
LTE_NSA_ANCHOR = "LTE_NSA_ANCHOR"
NR_NSA_ACTIVE = "NR_NSA_ACTIVE"
RAT_UNKNOWN = "RAT_UNKNOWN"
RAT_TRANSITION = "RAT_TRANSITION"
RAT_UNSUPPORTED = "RAT_UNSUPPORTED"

RAT_STATES: frozenset[str] = frozenset(
    {
        LTE_ONLY,
        LTE_NSA_ANCHOR,
        NR_NSA_ACTIVE,
        RAT_UNKNOWN,
        RAT_TRANSITION,
        RAT_UNSUPPORTED,
    }
)

RAT_GROUPS: tuple[str, ...] = (
    "lte_radio",
    "nr_radio",
    "rat_state",
    "end_to_end_performance",
    "fused_quality",
    "rat_transition",
)


class RatModelError(ValueError):
    """Raised when a RAT model input would create untrusted runtime truth."""


def build_rat_snapshot(
    source: Mapping[str, Any] | Any,
    *,
    previous: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """Return the canonical RAT snapshot for a modem/status source."""

    payload = _as_mapping(source)
    profile_payload = _as_mapping(profile)
    lte = _lte_radio(payload)
    nr = _nr_radio(payload, profile=profile_payload)
    end_to_end = _end_to_end_performance(payload)
    state = _rat_state(lte, nr, profile=profile_payload)
    transition = _rat_transition(
        state,
        lte,
        nr,
        payload=payload,
        previous=previous,
    )
    fused = _fused_quality(payload, lte=lte, nr=nr, end_to_end=end_to_end)

    if transition["active"]:
        state = {
            **state,
            "previous_state": transition["from"],
            "stable_state": state["state"],
            "state": RAT_TRANSITION,
            "transition": True,
        }

    return {
        "schema": RAT_SNAPSHOT_SCHEMA,
        "owner": RAT_MODEL_OWNER,
        "lte_radio": lte,
        "nr_radio": nr,
        "rat_state": state,
        "end_to_end_performance": end_to_end,
        "fused_quality": fused,
        "rat_transition": transition,
    }


def rat_learning_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the RAT fields learning/proof rows must carry."""

    snap = _mapping(snapshot)
    if snap.get("schema") != RAT_SNAPSHOT_SCHEMA or snap.get("owner") != RAT_MODEL_OWNER:
        raise RatModelError("learning context requires canonical RAT snapshot")
    rat_state = _mapping(snap.get("rat_state"))
    lte = _mapping(snap.get("lte_radio"))
    nr = _mapping(snap.get("nr_radio"))
    transition = _mapping(snap.get("rat_transition"))
    return {
        "rat_state": str(rat_state.get("state") or RAT_UNKNOWN),
        "rat_stable_state": str(rat_state.get("stable_state") or rat_state.get("state") or RAT_UNKNOWN),
        "rat_transition_dirty": bool(transition.get("dirty", False)),
        "lte_anchor_band": _int(lte.get("serving_band"), 0),
        "lte_pci": _int(lte.get("pci"), 0),
        "lte_earfcn": _int(lte.get("earfcn_dl"), 0),
        "nr_mode": str(nr.get("mode") or ""),
        "nr_band": _int(nr.get("band"), 0),
        "nr_active": bool(nr.get("active", False)),
        "nr_identity_completeness": str(
            nr.get("identity_completeness") or IDENTITY_ABSENT
        ),
    }


def validate_rat_scoped_learning_row(row: Mapping[str, Any], *, nr_capable: bool) -> dict[str, Any]:
    """Validate learning/proof rows that may involve NR-capable profiles."""

    data = dict(_mapping(row))
    if nr_capable and str(data.get("rat_state") or "").strip() not in RAT_STATES:
        raise RatModelError("NR-capable learning/proof row requires canonical rat_state")
    if bool(data.get("rat_transition_dirty", False)) and str(data.get("proof_grade") or ""):
        raise RatModelError("RAT transition dirty window cannot carry a final proof grade")
    return data


def _as_mapping(value: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(vars(value))
    except TypeError:
        return {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on", "active", "connected"}:
            return True
        if token in {"0", "false", "no", "off", "inactive", "disconnected", ""}:
            return False
    return bool(value)


def _first_int(payload: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = _int(payload.get(key), 0)
        if value > 0:
            return value
    return 0


def _first_float(payload: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = _float(payload.get(key), 0.0)
        if value != 0.0:
            return value
    return 0.0


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def _lte_radio(payload: Mapping[str, Any]) -> dict[str, Any]:
    serving_band = _first_int(payload, "serving_band", "pband", "primary_band", "band")
    pci = _first_int(payload, "pci", "lte_pci")
    earfcn = _first_int(payload, "earfcn_dl", "earfcn", "lte_earfcn")
    cell_id = _first_int(payload, "cell_id", "lte_cell_id")
    rsrp = _first_float(payload, "rsrp_dbm", "lte_rsrp_dbm")
    rsrq = _first_float(payload, "rsrq_db", "lte_rsrq_db")
    sinr = _first_float(payload, "sinr_db", "lte_sinr_db")
    present = any((serving_band > 0, pci > 0, earfcn > 0, cell_id > 0, rsrp < 0.0, rsrq < 0.0, sinr != 0.0))
    return {
        "present": bool(present),
        "valid": bool(present),
        "serving_band": serving_band,
        "band_signature": _first_text(payload, "serving_band_signature"),
        "configured_band_pool": list(payload.get("configured_band_pool") or []),
        "configured_band_pool_signature": _first_text(payload, "configured_band_pool_signature"),
        "active_band_set": list(payload.get("active_band_set") or []),
        "active_band_set_signature": _first_text(payload, "active_band_set_signature"),
        "pci": pci,
        "earfcn_dl": earfcn,
        "cell_id": cell_id,
        "enb_id": _first_int(payload, "enb_id"),
        "rsrp_dbm": rsrp,
        "rsrq_db": rsrq,
        "sinr_db": sinr,
        "rssi_dbm": _first_float(payload, "rssi_dbm"),
        "bandwidth_dl_mhz": _first_float(payload, "dl_bandwidth_mhz", "dl_bw_mhz"),
        "bandwidth_ul_mhz": _first_float(payload, "ul_bandwidth_mhz", "ul_bw_mhz"),
        "ca_active": _bool(payload.get("ca_active")),
        "carrier_count": _first_int(payload, "carrier_count"),
    }


def _nr_supported(payload: Mapping[str, Any], profile: Mapping[str, Any]) -> bool | None:
    unsupported = payload.get("unsupported_fields")
    unsupported_set = {str(item).strip() for item in unsupported} if isinstance(unsupported, (list, tuple, set, frozenset)) else set()
    if {"nr_band", "nr5g_active", "nr_sinr_db"} & unsupported_set:
        return False
    for mapping in (profile, _mapping(profile.get("capabilities")), _mapping(payload.get("capabilities"))):
        for key in ("supports_nr", "nr_supported", "supports_5g", "supports_nr_band_lock"):
            if key in mapping:
                return _bool(mapping.get(key))
    return None


def _nr_radio(payload: Mapping[str, Any], *, profile: Mapping[str, Any]) -> dict[str, Any]:
    supported = _nr_supported(payload, profile=profile)
    snapshot_value = payload.get("nsa_snapshot")
    if not isinstance(snapshot_value, Mapping):
        return {
            "supported": supported,
            "support_state": "supported" if supported is True else "unsupported" if supported is False else "unknown",
            "active": False,
            "valid": False,
            "mode": "",
            "nsa_active": False,
            "sa_active": False,
            "band": 0,
            "pci": 0,
            "arfcn": 0,
            "cell_id": 0,
            "rsrp_dbm": 0.0,
            "rsrq_db": 0.0,
            "sinr_db": 0.0,
            "bandwidth_mhz": 0.0,
            "scs_khz": 0.0,
            "cells": [],
            "identity_completeness": IDENTITY_ABSENT,
            "missing_evidence": ["nsa_snapshot"],
        }
    snapshot = validate_nsa_physical_snapshot(snapshot_value)
    fields = _mapping(snapshot.get("fields"))
    mode = str(snapshot.get("rat_mode") or "")
    sa_active = mode == RAT_MODE_SA_UNSUPPORTED
    active = bool(payload.get("nr_secondary_active", False))
    nsa_active = bool(active and mode == RAT_MODE_NSA)

    def field(name: str, default: Any) -> Any:
        row = _mapping(fields.get(name))
        return row.get("value") if row.get("available") is True else default

    missing = []
    if bool(snapshot.get("secondary_active", False)) and not active:
        missing.append("stable_nsa_transition")
    return {
        "supported": supported,
        "support_state": "supported" if supported is True else "unsupported" if supported is False else "unknown",
        "active": active,
        "valid": active and supported is not False and not sa_active,
        "mode": mode,
        "nsa_active": bool(nsa_active),
        "sa_active": bool(sa_active),
        "band": _int(field("nr.band", 0), 0),
        "pci": _int(field("nr.pci", 0), 0),
        "arfcn": _int(field("nr.arfcn", 0), 0),
        "cell_id": _int(field("nr.nci", 0), 0),
        "rsrp_dbm": _float(field("nr.ss_rsrp_dbm", 0.0), 0.0),
        "rsrq_db": _float(field("nr.ss_rsrq_db", 0.0), 0.0),
        "sinr_db": _float(field("nr.ss_sinr_db", 0.0), 0.0),
        "bandwidth_mhz": _float(field("nr.bandwidth_mhz", 0.0), 0.0),
        "scs_khz": _float(field("nr.scs_khz", 0.0), 0.0),
        "cells": list(field("nr.cells", [])) if isinstance(field("nr.cells", []), list) else [],
        "identity_completeness": str(
            snapshot.get("identity_completeness") or IDENTITY_ABSENT
        ),
        "missing_evidence": missing,
    }


def _end_to_end_performance(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dl_mbps": _first_float(payload, "dl_mbps", "throughput_dl_mbps"),
        "ul_mbps": _first_float(payload, "ul_mbps", "throughput_ul_mbps"),
        "rtt_ms": _first_float(payload, "rtt_ms"),
        "jitter_pct": _first_float(payload, "rtt_jitter_pct", "jitter_pct"),
        "congestion_pct": _first_float(payload, "congestion_pct"),
        "traffic_valid": _bool(payload.get("traffic_valid", True)),
    }


def _rat_state(lte: Mapping[str, Any], nr: Mapping[str, Any], *, profile: Mapping[str, Any]) -> dict[str, Any]:
    lte_present = bool(lte.get("present"))
    nr_active = bool(nr.get("active"))
    nr_supported = nr.get("supported")
    mode = str(nr.get("mode") or "")
    if bool(nr.get("sa_active")):
        state = RAT_UNSUPPORTED
    elif bool(nr.get("nsa_active")):
        state = NR_NSA_ACTIVE if nr_active else LTE_NSA_ANCHOR
    elif lte_present and nr_supported is True:
        state = LTE_NSA_ANCHOR
    elif lte_present:
        state = LTE_ONLY
    elif nr_supported is False:
        state = RAT_UNSUPPORTED
    else:
        state = RAT_UNKNOWN
    evidence = {
        "lte_present": lte_present,
        "nr_active": nr_active,
        "nr_supported": nr_supported,
        "nr_mode": mode,
        "profile_id": str(profile.get("profile_id") or ""),
    }
    confidence = "observed" if lte_present or nr_active else "unsupported" if nr_supported is False else "unknown"
    return {
        "state": state,
        "stable_state": state,
        "confidence": confidence,
        "transition": False,
        "evidence": evidence,
    }


def _rat_transition(
    state: Mapping[str, Any],
    lte: Mapping[str, Any],
    nr: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    del lte, nr, previous
    new_state = str(state.get("state") or RAT_UNKNOWN)
    transition_state = str(payload.get("nr_transition_state") or "")
    payload_dirty = bool(payload.get("nr_transition_dirty", False))
    active = payload_dirty
    return {
        "active": active,
        "dirty": payload_dirty,
        "from": "",
        "to": new_state,
        "changed_fields": [],
        "reason": transition_state if active else "",
    }


def _fused_quality(
    payload: Mapping[str, Any],
    *,
    lte: Mapping[str, Any],
    nr: Mapping[str, Any],
    end_to_end: Mapping[str, Any],
) -> dict[str, Any]:
    explicit = payload.get("fused_quality")
    if isinstance(explicit, Mapping):
        ingredients = explicit.get("ingredients")
        confidence = str(explicit.get("confidence") or "").strip()
        if explicit.get("value_pct") is not None and (not isinstance(ingredients, Mapping) or not confidence):
            raise RatModelError("fused_quality value requires ingredients and confidence")
        return {
            "available": explicit.get("value_pct") is not None,
            "value_pct": _float(explicit.get("value_pct"), 0.0),
            "confidence": confidence or "unknown",
            "ingredients": dict(ingredients or {}),
            "reason": str(explicit.get("reason") or ""),
            "owner": RAT_MODEL_OWNER,
        }

    value = _first_float(payload, "cq_norm_pct", "conn_quality_pct", "conn_quality")
    ingredients = {
        "lte": {
            "present": bool(lte.get("present")),
            "sinr_db": lte.get("sinr_db"),
            "rsrp_dbm": lte.get("rsrp_dbm"),
            "rsrq_db": lte.get("rsrq_db"),
            "ca_active": bool(lte.get("ca_active")),
        },
        "nr": {
            "active": bool(nr.get("active")),
            "mode": nr.get("mode"),
            "band": nr.get("band"),
            "sinr_db": nr.get("sinr_db"),
        },
        "end_to_end": dict(end_to_end),
    }
    useful = value > 0.0 and (bool(lte.get("present")) or bool(nr.get("active")) or bool(end_to_end.get("traffic_valid")))
    return {
        "available": useful,
        "value_pct": value if useful else 0.0,
        "confidence": "medium" if useful else "unknown",
        "ingredients": ingredients if useful else {},
        "reason": "ingredients_present" if useful else "quality_missing_or_untrusted",
        "owner": RAT_MODEL_OWNER,
    }


__all__ = [
    "LTE_NSA_ANCHOR",
    "LTE_ONLY",
    "NR_NSA_ACTIVE",
    "RAT_GROUPS",
    "RAT_MODEL_OWNER",
    "RAT_SNAPSHOT_SCHEMA",
    "RAT_STATES",
    "RAT_TRANSITION",
    "RAT_UNKNOWN",
    "RAT_UNSUPPORTED",
    "RatModelError",
    "build_rat_snapshot",
    "rat_learning_context",
    "validate_rat_scoped_learning_row",
]
