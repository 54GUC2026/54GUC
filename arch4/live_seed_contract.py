"""Neutral LIVE MODULE startup/runtime contract helpers.

This module holds contract-shaping helpers consumed by both LIVE MODULE and
GUI layers. Keeping them outside ``arch4.brain`` avoids GUI boundary leaks
while preserving one canonical seed/runtime-capability surface.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from arch4.coerce import safe_bool
from arch4.interfaces import normalize_band_list
from arch4.runtime_support_tier import publish_support_authority_fields


def coerce_contract_list(value: Any) -> list:
    """Keep list-shaped live-contract fields list-only."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _runtime_capabilities_payload(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        return None
    payload = dict(value)
    if "support_tier" in payload:
        publish_support_authority_fields(payload)
    return payload


def build_live_seed_payload(capabilities: dict | None) -> Optional[Dict[str, Any]]:
    """Build the canonical first-paint live payload from startup capability truth."""
    if not isinstance(capabilities, dict) or not capabilities:
        return None

    runtime_identity = _mapping(capabilities.get("runtime_identity"))
    live_capture = _mapping(capabilities.get("live_capture"))

    ts = float(capabilities.get("ts", time.time()) or time.time())
    serving_band = int(live_capture.get("serving_band", 0) or 0)
    primary_band = int(live_capture.get("primary_band", 0) or 0)
    radio_connected = safe_bool(live_capture.get("radio_connected", False), False)
    ca_active = safe_bool(live_capture.get("ca_active", False), False)
    ca_signature = str(live_capture.get("ca_signature", "") or "")
    configured_band_pool = normalize_band_list(coerce_contract_list(live_capture.get("configured_band_pool")))
    active_band_set = normalize_band_list(coerce_contract_list(live_capture.get("active_band_set")))
    radio_register_value = {
        "radio_connected": radio_connected,
        "provider_name": str(runtime_identity.get("provider_name", "") or ""),
        "provider_plmn": str(runtime_identity.get("provider_plmn", "") or ""),
        "model": str(runtime_identity.get("model", "") or ""),
        "router_model": str(runtime_identity.get("router_model", "") or ""),
        "serving_band": serving_band,
        "primary_band": primary_band,
        "serving_band_signature": str(live_capture.get("serving_band_signature", "") or ""),
        "configured_band_pool": list(configured_band_pool),
        "configured_band_pool_signature": str(live_capture.get("configured_band_pool_signature", "") or ""),
        "configured_band_pool_source": str(live_capture.get("configured_band_pool_source", "") or ""),
        "active_band_set": list(active_band_set),
        "pci": int(live_capture.get("pci", 0) or 0),
        "earfcn": int(live_capture.get("earfcn", 0) or 0),
        "earfcn_dl": int(live_capture.get("earfcn", 0) or 0),
        "cell_id": int(live_capture.get("cell_id", 0) or 0),
        "sinr_db": float(live_capture.get("sinr_db", 0.0) or 0.0),
        "rsrp_dbm": float(live_capture.get("rsrp_dbm", 0.0) or 0.0),
        "rsrq_db": float(live_capture.get("rsrq_db", 0.0) or 0.0),
        "mcs_dl": int(live_capture.get("mcs_dl", 0) or 0),
        "mcs_ul": int(live_capture.get("mcs_ul", 0) or 0),
        "cqi": int(live_capture.get("cqi", 0) or 0),
    }

    result = {
        "sequence": int(capabilities.get("sequence", 0) or 0),
        "timestamp": ts,
        "seed_state": "WORKING" if radio_connected else "OPTIMIZING",
        "source_kind": str(capabilities.get("source_kind", "startup_capabilities") or "startup_capabilities"),
        "runtime_identity": dict(runtime_identity),
        "live_capture": dict(live_capture),
        "radio_connected": radio_connected,
        "vendor": str(runtime_identity.get("vendor", "") or ""),
        "model": str(runtime_identity.get("model", "") or ""),
        "router_model": str(runtime_identity.get("router_model", "") or ""),
        "firmware": str(runtime_identity.get("firmware", "") or ""),
        "deployed_bands": str(runtime_identity.get("deployed_bands", "") or ""),
        "serial": str(runtime_identity.get("serial", "") or ""),
        "imei": str(runtime_identity.get("imei", "") or ""),
        "provider_plmn": str(runtime_identity.get("provider_plmn", "") or ""),
        "provider_name": str(runtime_identity.get("provider_name", "") or ""),
        "provider": str(runtime_identity.get("provider_name", "") or ""),
        "plmn": str(runtime_identity.get("provider_plmn", "") or ""),
        "ca_active": ca_active,
        "ca_signature": ca_signature,
        "ca_state_text": str(live_capture.get("ca_state_text", "") or ""),
        "radio_state_text": str(live_capture.get("radio_state_text", "") or ""),
        "serving_band": serving_band,
        "primary_band": primary_band,
        "serving_band_signature": str(live_capture.get("serving_band_signature", "") or ""),
        "pci": int(live_capture.get("pci", 0) or 0),
        "sinr_db": float(live_capture.get("sinr_db", 0.0) or 0.0),
        "rsrp_dbm": float(live_capture.get("rsrp_dbm", 0.0) or 0.0),
        "rsrq_db": float(live_capture.get("rsrq_db", 0.0) or 0.0),
        "mcs_dl": int(live_capture.get("mcs_dl", 0) or 0),
        "mcs_dl0": int(live_capture.get("mcs_dl0", 0) or 0),
        "mcs_dl1": int(live_capture.get("mcs_dl1", 0) or 0),
        "mcs_ul": int(live_capture.get("mcs_ul", 0) or 0),
        "cqi": int(live_capture.get("cqi", 0) or 0),
        "cqi0": int(live_capture.get("cqi0", 0) or 0),
        "cqi1": int(live_capture.get("cqi1", 0) or 0),
        "ri": int(live_capture.get("ri", 0) or 0),
        "qam_dl0": live_capture.get("qam_dl0", ""),
        "qam_dl1": live_capture.get("qam_dl1", ""),
        "qam_ul": live_capture.get("qam_ul", ""),
        "configured_band_pool": list(configured_band_pool),
        "configured_band_pool_signature": str(live_capture.get("configured_band_pool_signature", "") or ""),
        "configured_band_pool_source": str(live_capture.get("configured_band_pool_source", "") or ""),
        "active_band_set": list(active_band_set),
        "carrier_count": int(live_capture.get("carrier_count", 0) or 0),
        "earfcn": int(live_capture.get("earfcn", 0) or 0),
        "cell_id": int(live_capture.get("cell_id", 0) or 0),
        "tx_power": live_capture.get("tx_power", "") or "",
        "dl_bandwidth": live_capture.get("dl_bandwidth", "") or "",
        "ul_bandwidth": live_capture.get("ul_bandwidth", "") or "",
        "dl_bandwidth_mhz": float(live_capture.get("dl_bandwidth_mhz", 0.0) or 0.0),
        "ul_bandwidth_mhz": float(live_capture.get("ul_bandwidth_mhz", 0.0) or 0.0),
        "dl_freq_mhz": float(live_capture.get("dl_freq_mhz", 0.0) or 0.0),
        "ul_freq_mhz": float(live_capture.get("ul_freq_mhz", 0.0) or 0.0),
        "antenna": str(live_capture.get("antenna", "") or ""),
        "antenna_mode": str(live_capture.get("antenna_mode", "") or ""),
        "antenna_mode_source": str(live_capture.get("antenna_mode_source", "") or ""),
        "antenna_switch_api_supported": live_capture.get("antenna_switch_api_supported"),
        "external_antenna_ports_present": live_capture.get("external_antenna_ports_present"),
        "neighbors": coerce_contract_list(live_capture.get("neighbors")),
        "runtime_registers": {
            "radio": {
                "owner": "modem_live",
                "valid": bool(radio_connected),
                "stale": False,
                "dirty": False,
                "trust": "live",
                "source_kind": "live",
                "validity": "valid" if radio_connected else "invalid",
                "value": radio_register_value,
            }
        },
        "capability_contract": dict(capabilities.get("capability_contract") or {})
        if isinstance(capabilities.get("capability_contract"), dict)
        else {},
        "runtime_capabilities": (
            _runtime_capabilities_payload(capabilities.get("runtime_capabilities"))
            or _runtime_capabilities_payload(capabilities)
            or {}
        ),
        "modem_health": dict(
            capabilities.get("modem_health")
            or {
                "state": "ok" if radio_connected else "degraded",
                "outage_class": "none",
            }
        ),
        "runtime_stale": dict(capabilities.get("runtime_stale") or {"active": False}),
    }
    radio_optional = {
        "serving_band": ("serving_band",),
        "primary_band": ("primary_band",),
        "serving_band_signature": ("serving_band_signature",),
        "configured_band_pool": ("configured_band_pool",),
        "configured_band_pool_signature": ("configured_band_pool_signature",),
        "configured_band_pool_source": ("configured_band_pool_source",),
        "active_band_set": ("active_band_set",),
        "pci": ("pci",),
        "earfcn": ("earfcn", "earfcn_dl"),
        "cell_id": ("cell_id",),
        "sinr_db": ("sinr_db",),
        "rsrp_dbm": ("rsrp_dbm",),
        "rsrq_db": ("rsrq_db",),
        "mcs_dl": ("mcs_dl",),
        "mcs_ul": ("mcs_ul",),
        "cqi": ("cqi",),
    }
    for capture_name, output_names in radio_optional.items():
        if capture_name not in live_capture:
            for output_name in output_names:
                radio_register_value.pop(output_name, None)
    top_optional = {
        "ca_signature": "ca_signature",
        "serving_band": "serving_band",
        "primary_band": "primary_band",
        "serving_band_signature": "serving_band_signature",
        "pci": "pci",
        "sinr_db": "sinr_db",
        "rsrp_dbm": "rsrp_dbm",
        "rsrq_db": "rsrq_db",
        "mcs_dl": "mcs_dl",
        "mcs_dl0": "mcs_dl0",
        "mcs_dl1": "mcs_dl1",
        "mcs_ul": "mcs_ul",
        "cqi": "cqi",
        "cqi0": "cqi0",
        "cqi1": "cqi1",
        "ri": "ri",
        "qam_dl0": "qam_dl0",
        "qam_dl1": "qam_dl1",
        "qam_ul": "qam_ul",
        "configured_band_pool": "configured_band_pool",
        "configured_band_pool_signature": "configured_band_pool_signature",
        "configured_band_pool_source": "configured_band_pool_source",
        "active_band_set": "active_band_set",
        "carrier_count": "carrier_count",
        "earfcn": "earfcn",
        "cell_id": "cell_id",
        "tx_power": "tx_power",
        "dl_bandwidth": "dl_bandwidth",
        "ul_bandwidth": "ul_bandwidth",
        "dl_bandwidth_mhz": "dl_bandwidth_mhz",
        "ul_bandwidth_mhz": "ul_bandwidth_mhz",
        "dl_freq_mhz": "dl_freq_mhz",
        "ul_freq_mhz": "ul_freq_mhz",
        "antenna": "antenna",
        "antenna_mode": "antenna_mode",
        "antenna_mode_source": "antenna_mode_source",
        "antenna_switch_api_supported": "antenna_switch_api_supported",
        "external_antenna_ports_present": "external_antenna_ports_present",
        "neighbors": "neighbors",
    }
    for output_name, capture_name in top_optional.items():
        if capture_name not in live_capture:
            result.pop(output_name, None)
    return result


def extract_live_runtime_capabilities(payload: dict | None) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return None
    runtime_caps = payload.get("runtime_capabilities")
    if isinstance(runtime_caps, dict) and runtime_caps:
        return _runtime_capabilities_payload(runtime_caps)
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict) and (
        "band_select" in capabilities
        or "support_tier" in capabilities
        or "capability_contract" in capabilities
        or "static" in capabilities
    ):
        return _runtime_capabilities_payload(capabilities)
    if (
        "band_select" in payload
        or "support_tier" in payload
        or "capability_contract" in payload
        or "static" in payload
    ):
        return _runtime_capabilities_payload(payload)
    return None
