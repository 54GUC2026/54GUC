"""Passive DL peak evidence from normal traffic.

This module never probes the network. It only turns already-owned runtime
register traffic into an identity-scoped lower-bound capacity fact.
"""

from __future__ import annotations

from typing import Any, Mapping


PASSIVE_SCHEMA = "passive_dl_peak_fact.v1"
OWNER = "arch4.smart_loop.capacity_probe"
MIN_PASSIVE_DL_MBPS = 0.4
FRESH_TRAFFIC_AFTER_S = 10.0


def build_passive_dl_peak_fact(
    *,
    identity: Mapping[str, Any],
    radio: Mapping[str, Any],
    traffic: Mapping[str, Any],
    traffic_register_metadata: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    now: float,
) -> tuple[dict[str, Any], bool]:
    """Return the current context passive peak fact and whether it advanced."""

    context = _context(identity=identity, radio=radio)
    if not context:
        return {}, False

    previous_fact = _mapping(previous)
    if previous_fact.get("schema") != PASSIVE_SCHEMA or previous_fact.get("owner") != OWNER:
        previous_fact = {}
    same_context = previous_fact and str(previous_fact.get("context_key") or "") == str(context["context_key"])

    dl_mbps = _float(traffic.get("dl_mbps", traffic.get("throughput_dl_mbps")), 0.0)
    sample_ts = _traffic_sample_ts(
        traffic=traffic,
        traffic_register_metadata=traffic_register_metadata,
        now=now,
    )
    fresh = sample_ts > 0.0 and (float(now) - sample_ts) <= FRESH_TRAFFIC_AFTER_S
    if (
        not _traffic_valid(
            traffic,
            traffic_register_metadata=traffic_register_metadata,
        )
        or not fresh
        or dl_mbps < MIN_PASSIVE_DL_MBPS
    ):
        return (dict(previous_fact), False) if same_context else ({}, False)

    previous_peak = _float(previous_fact.get("peak_dl_mbps_lower_bound"), 0.0) if same_context else 0.0
    if same_context and dl_mbps <= previous_peak:
        return dict(previous_fact), False

    fact = {
        "schema": PASSIVE_SCHEMA,
        "owner": OWNER,
        "observed": True,
        "source": "runtime_traffic_register",
        "identity_code": str(context["identity_code"]),
        "identity_status": str(context["identity_status"]),
        "model": str(context["model"]),
        "provider_plmn": str(context["provider_plmn"]),
        "cell_id": int(context["cell_id"]),
        "pci": int(context["pci"]),
        "earfcn": int(context["earfcn"]),
        "serving_band": int(context["serving_band"]),
        "target_bands": list(context["target_bands"]),
        "nr_band": str(context["nr_band"]),
        "nr_pci": int(context["nr_pci"]),
        "nr_arfcn": int(context["nr_arfcn"]),
        "nr_cell_id": str(context["nr_cell_id"]),
        "context_key": str(context["context_key"]),
        "peak_dl_mbps_lower_bound": round(float(dl_mbps), 3),
        "sample_ts": float(sample_ts),
        "traffic_rate_source": str(traffic.get("traffic_rate_source") or ""),
        "observation_count": int(_float(previous_fact.get("observation_count"), 0.0) if same_context else 0) + 1,
    }
    return fact, True


def _context(*, identity: Mapping[str, Any], radio: Mapping[str, Any]) -> dict[str, Any]:
    identity_code = str(identity.get("identity_code") or "").strip()
    identity_status = str(identity.get("identity_status") or "").strip().lower()
    if not identity_code or identity_code in {
        "0x0000000000000000",
        "0X0000000000000000",
    }:
        return {}
    if identity_status not in {"exact", "confirmed"}:
        return {}
    serving_band = _int(radio.get("serving_band") if "serving_band" in radio else radio.get("primary_band"), 0)
    if serving_band <= 0:
        return {}
    target_bands = _int_list(radio.get("configured_band_pool") or ())
    if serving_band not in target_bands:
        target_bands = [serving_band, *target_bands]
    nr_band = str(radio.get("nr_band") if "nr_band" in radio else radio.get("nr5g_band") or "")
    nr_pci = _int(radio.get("nr_pci") if "nr_pci" in radio else radio.get("nr5g_pci"), 0)
    nr_arfcn = _int(radio.get("nr_arfcn") if "nr_arfcn" in radio else radio.get("nr5g_arfcn"), 0)
    nr_cell_id = str(radio.get("nr_cell_id") if "nr_cell_id" in radio else radio.get("nr5g_cell_id") or "")
    context_parts = (
        identity_code,
        str(radio.get("provider_plmn") or ""),
        str(_int(radio.get("cell_id"), 0)),
        str(_int(radio.get("pci"), 0)),
        str(_int(radio.get("earfcn_dl") if "earfcn_dl" in radio else radio.get("earfcn"), 0)),
        str(serving_band),
        "+".join(str(item) for item in target_bands),
        nr_band,
        str(nr_pci),
        str(nr_arfcn),
        nr_cell_id,
    )
    return {
        "identity_code": identity_code,
        "identity_status": identity_status,
        "model": str(identity.get("model") or ""),
        "provider_plmn": str(radio.get("provider_plmn") or ""),
        "cell_id": _int(radio.get("cell_id"), 0),
        "pci": _int(radio.get("pci"), 0),
        "earfcn": _int(radio.get("earfcn_dl") if "earfcn_dl" in radio else radio.get("earfcn"), 0),
        "serving_band": serving_band,
        "target_bands": target_bands,
        "nr_band": nr_band,
        "nr_pci": nr_pci,
        "nr_arfcn": nr_arfcn,
        "nr_cell_id": nr_cell_id,
        "context_key": "|".join(context_parts),
    }


def _traffic_valid(
    traffic: Mapping[str, Any],
    *,
    traffic_register_metadata: Mapping[str, Any],
) -> bool:
    if "valid" in traffic_register_metadata and not _bool(
        traffic_register_metadata.get("valid"),
        False,
    ):
        return False
    if _bool(traffic_register_metadata.get("stale"), False):
        return False
    if "traffic_valid" in traffic and not _bool(traffic.get("traffic_valid"), False):
        return False
    if str(traffic.get("traffic_invalid_reason") or "").strip():
        return False
    return True


def _traffic_sample_ts(
    *,
    traffic: Mapping[str, Any],
    traffic_register_metadata: Mapping[str, Any],
    now: float,
) -> float:
    sample_ts = _float(traffic.get("sample_ts"), 0.0)
    if sample_ts > 0.0:
        return sample_ts
    register_updated_at = _float(traffic_register_metadata.get("updated_at"), 0.0)
    if register_updated_at > 0.0:
        return register_updated_at
    for key in ("updated_at", "timestamp", "ts"):
        parsed = _float(traffic.get(key), 0.0)
        if parsed > 0.0:
            return parsed
    return float(now)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for item in value:
        parsed = _int(item, 0)
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return out


__all__ = [
    "OWNER",
    "PASSIVE_SCHEMA",
    "build_passive_dl_peak_fact",
]
