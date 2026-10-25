"""Build scalar modem-capability cells for the Modem Live Matrix.

The nested capability contract is internal evidence for Core and reports. GUI
code reads the scalar cells made here and must not reinterpret that contract.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.modem_live.session_band_capability import normalize_capability_bands

from arch4.modem.live_capability_surface import (
    GUI_CAPABILITY_KEYS,
    LIVE_CONTROL_KEYS,
    ROUTER_CHECK_KEYS,
    STATUS_PROBING,
    build_capability_surface_from_payload,
)


CAPABILITY_UI_FIELD_KEYS: tuple[tuple[str, str], ...] = (
    ("pband_control", "pband"),
    ("ca_pool_control", "band_select"),
    ("band_lock", "band_lock"),
    ("cell_lock", "cell_lock"),
    ("restart", "restart"),
    ("max_tcp_payload", "mss_ipv4"),
    ("ttl_ip", "ttl_ipv4"),
    ("keepalive_enabled", "keepalive"),
    ("dl_256qam", "dl_256qam"),
    ("pci_lock", "pci_lock_available"),
    ("ul_64qam", "ul_64qam"),
    ("keepalive", "traffic_presence"),
    ("ue_category", "ue_category"),
    ("ue_info", "ue_capability"),
    ("traffic_volume", "traffic_volume"),
    ("ca_details", "ca_details"),
    ("neighbors_support", "neighbors"),
    ("plmn_read", "plmn"),
    ("band_priority", "band_priority"),
)

CAPABILITY_ACTION_KEYS: tuple[str, ...] = tuple(GUI_CAPABILITY_KEYS) + (
    "wifi_control",
    "network_settings",
    "mobile_data",
    "radio_mode",
)
CAPABILITY_READ_KEYS: tuple[str, ...] = (
    "traffic_presence",
    "traffic_volume",
    "ue_capability",
    "neighbors",
    "ca_details",
    "plmn",
    "band_priority",
)
CAPABILITY_PROOF_STATES = frozenset(
    {"supported", "estimated", "unsupported", "non_useful", "unknown"}
)

_LIVE_CONTROL_KEY_SET = frozenset(key for key, _label in LIVE_CONTROL_KEYS)
_ROUTER_CHECK_KEY_SET = frozenset(key for key, _label in ROUTER_CHECK_KEYS)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _proof_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    return state if state in CAPABILITY_PROOF_STATES else "unknown"


def _session_status(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"proven", "seeded", "unavailable", "unsupported"}:
        return state
    return "unknown"


def build_explicit_capability_cells(
    *,
    runtime_capabilities: Mapping[str, Any] | None,
    session_band_capability: Mapping[str, Any] | None,
    profile_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Build all scalar capability cells for one Matrix generation."""

    runtime = dict(runtime_capabilities or {})
    session = dict(session_band_capability or {})
    surface = build_capability_surface_from_payload(runtime)

    cells: dict[str, Any] = {
        "device_radio_class": profile_record["device_radio_class"] if profile_record else "unclassified",
        "session_lte_status": _session_status(session.get("lte_status")),
        "session_lte_bands": list(normalize_capability_bands(session.get("supported_lte_bands"))),
        "session_nr_status": _session_status(session.get("nr_status")),
        "session_nr_bands": list(normalize_capability_bands(session.get("supported_nr_bands"))),
        "session_capability_comparison": (
            str(session.get("comparison_status") or "").strip().lower()
            or "unknown"
        ),
        "session_capability_id": (
            str(session.get("capability_id") or "").strip() or "unknown"
        ),
    }

    for field_name, capability_key in CAPABILITY_UI_FIELD_KEYS:
        if capability_key in _LIVE_CONTROL_KEY_SET:
            value = surface.live_controls.get(capability_key, STATUS_PROBING)
        elif capability_key in _ROUTER_CHECK_KEY_SET:
            value = surface.router_checks.get(capability_key, STATUS_PROBING)
        else:
            raise RuntimeError(
                f"modem_live_capability_ui_key_undeclared:{capability_key}"
            )
        cells[field_name] = str(value or STATUS_PROBING)

    contract = _mapping(runtime.get("capability_contract"))
    actions = _mapping(contract.get("actions"))
    reads = _mapping(contract.get("reads"))
    for key in CAPABILITY_ACTION_KEYS:
        meta = _mapping(actions.get(key))
        cells[f"action_{key}_state"] = _proof_state(meta.get("state"))
        cells[f"action_{key}_allowed"] = bool(meta.get("write_allowed", False))
    for key in CAPABILITY_READ_KEYS:
        meta = _mapping(reads.get(key))
        cells[f"read_{key}_state"] = _proof_state(meta.get("state"))

    if "sms" not in runtime:
        sms_state = "unknown"
    else:
        sms_state = "supported" if bool(runtime.get("sms")) else "unsupported"
    cells["sms_state"] = sms_state
    cells["sms_supported"] = sms_state == "supported"
    return cells


__all__ = (
    "CAPABILITY_ACTION_KEYS",
    "CAPABILITY_PROOF_STATES",
    "CAPABILITY_READ_KEYS",
    "CAPABILITY_UI_FIELD_KEYS",
    "build_explicit_capability_cells",
)
