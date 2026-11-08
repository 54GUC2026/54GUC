"""Modem Live capability presentation owner.

GUI code must not decide modem capability truth. This module consumes the
runtime/Modem Live capability owner payload and publishes final display values
and action permissions for user-facing surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from arch4.interfaces import LeverType
from arch4.runtime_capability import (
    capability_contract_action_enabled,
    capability_contract_action_meta,
    capability_contract_read_meta,
    capability_contract_unsupported_fields,
)


_PROBE_TEST_SAFE_LANE = (
    "Probe/Test mode keeps only direct DL band selection, single-band lock, "
    "and band unlock/auto release enabled."
)
_PROBE_TEST_CAPABILITY_REASONS = {
    "cell_lock": f"Cell lock is disabled in Probe/Test mode. {_PROBE_TEST_SAFE_LANE}",
    "restart": f"Restart is disabled in Probe/Test mode. {_PROBE_TEST_SAFE_LANE}",
    "pband": f"PBAND is disabled in Probe/Test mode. {_PROBE_TEST_SAFE_LANE}",
    "nr_band_lock": f"NR band preference is disabled in Probe/Test mode. {_PROBE_TEST_SAFE_LANE}",
    "band_lock": f"Band lock is disabled in Probe/Test mode. {_PROBE_TEST_SAFE_LANE}",
}

GUI_CAPABILITY_KEYS: tuple[str, ...] = (
    "band_select",
    "band_lock",
    "cell_lock",
    "restart",
    "pband",
    "nr_band_lock",
)
LIVE_CONTROL_KEYS: tuple[tuple[str, str], ...] = (
    ("pband", "PBAND"),
    ("band_select", "CA Pool"),
    ("band_lock", "Band Lock"),
    ("cell_lock", "Cell Lock"),
    ("restart", "Restart"),
)
ROUTER_CHECK_KEYS: tuple[tuple[str, str], ...] = (
    ("mss_ipv4", "Max TCP Payload"),
    ("ttl_ipv4", "TTL IP"),
    ("keepalive", "Router keepalive"),
    ("dl_256qam", "DL 256QAM"),
    ("pci_lock_available", "PCI Lock"),
    ("ul_64qam", "UL 64QAM"),
    ("traffic_presence", "Traffic monitoring"),
    ("ue_category", "UE Category"),
    ("ue_capability", "UE info"),
    ("traffic_volume", "Traffic volume"),
    ("ca_details", "CA Details"),
    ("neighbors", "Neighbors"),
    ("plmn", "PLMN Read"),
    ("band_priority", "Band priority"),
)
STATUS_OK = "OK"
STATUS_MISS = "MISS"
STATUS_NA = "N/A"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_PROBING = "PROBING"
STATUS_UNSUPPORTED = "UNSUPPORTED"
_STATIC_ROUTER_CHECK_KEYS = {
    "mss_ipv4",
    "ttl_ipv4",
    "keepalive",
    "dl_256qam",
    "pci_lock_available",
    "ul_64qam",
    "ue_category",
}
_UE_CAPABILITY_KEYS = {"dl_256qam", "ul_64qam", "pci_lock_available", "ue_category"}
_TRUE_TEXT = {"1", "true", "yes", "ok", "supported", "enabled", "available", "cat12_implied"}
_FALSE_TEXT = {"0", "false", "no", "miss", "disabled", "not_enabled"}
_UNSUPPORTED_TEXT = {"unsupported", "not_supported", "not supported", "non_useful"}
_PROBING_TEXT = {"probing", "probe_pending", "collecting", "pending_probe", "loading"}
_UNKNOWN_TEXT = {"", "-", "--", "---", "n/a", "na", "unknown", "none", "null", "pending", "not_ready"}
_LEVER_CAPABILITY_KEYS = {
    LeverType.BAND_SELECTION: "band_select",
    LeverType.BAND_LOCK: "band_lock",
    LeverType.CELL_LOCK: "cell_lock",
    LeverType.RESTART: "restart",
    LeverType.PBAND: "pband",
    LeverType.NR_BAND_LOCK: "nr_band_lock",
}


@dataclass(frozen=True)
class CapabilityPresentationSurface:
    """Final Modem Live capability surface consumed by GUI renderers."""

    payload: dict[str, Any] | None = None
    access_text: str = "N/A"
    live_controls: dict[str, str] = field(default_factory=dict)
    router_checks: dict[str, str] = field(default_factory=dict)
    action_enabled: dict[str, bool] = field(default_factory=dict)
    action_reasons: dict[str, str] = field(default_factory=dict)
    source: str = "modem_live_capability_surface"

    def is_enabled(self, key: str) -> bool:
        return bool(self.action_enabled.get(str(key or "").strip().lower(), False))

    def reason(self, key: str) -> str:
        return str(self.action_reasons.get(str(key or "").strip().lower(), "") or "")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _owner_payload_from_runtime_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical capability-owner payload from a runtime envelope."""
    owner = payload.get("runtime_capabilities")
    if not isinstance(owner, Mapping) or not owner:
        return dict(payload)
    out = dict(owner)
    for key in (
        "runtime_identity_display",
        "runtime_identity",
        "resolved_identity_card",
        "capability_contract",
        "unsupported_fields",
    ):
        value = payload.get(key)
        if isinstance(value, Mapping) and value and key not in out:
            out[key] = dict(value)
        elif key == "unsupported_fields" and value and key not in out:
            out[key] = value
    return out


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in {"", "-", "--", "---", "n/a", "na", "unknown", "none", "null"}:
            return None
        if norm in {"1", "true", "yes", "ok", "supported", "enabled"}:
            return True
        if norm in {"0", "false", "no", "miss", "unsupported", "disabled"}:
            return False
    return bool(value)


def _status_bool(value: Any, *, missing: str = "N/A", true_text: str = "OK", false_text: str = "MISS") -> str:
    flag = _bool_or_none(value)
    if flag is None:
        return missing
    return true_text if flag else false_text


def _normalized_text(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def _explicit_unsupported_fields(data: Mapping[str, Any]) -> frozenset[str]:
    unsupported = set(capability_contract_unsupported_fields(data))
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    fields = contract.get("fields") if isinstance(contract.get("fields"), Mapping) else {}
    for field, meta in fields.items():
        if isinstance(meta, Mapping) and str(meta.get("state") or "").strip().lower() in {"unsupported", "non_useful"}:
            unsupported.add(str(field))
    return frozenset(field for field in unsupported if field)


def _static_evidence_keys(data: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for source_name in ("static_evidence_keys", "static_owner_evidence", "capability_static_evidence_keys"):
        source = data.get(source_name)
        if isinstance(source, Mapping):
            keys.update(str(key) for key in source if str(key or "").strip())
        elif isinstance(source, (list, tuple, set, frozenset)):
            keys.update(str(key) for key in source if str(key or "").strip())

    profile = data.get("modem_profile") if isinstance(data.get("modem_profile"), Mapping) else {}
    profile_static = profile.get("static_config") if isinstance(profile.get("static_config"), Mapping) else {}
    profile_ue = profile.get("ue_capability") if isinstance(profile.get("ue_capability"), Mapping) else {}
    keys.update(str(key) for key in profile_static if str(key or "").strip())
    keys.update(str(key) for key in profile_ue if str(key or "").strip())

    live_evidence = data.get("live_capability_evidence")
    if isinstance(live_evidence, (list, tuple, set, frozenset)):
        live_keys = {str(key or "").strip() for key in live_evidence if str(key or "").strip()}
        keys.update(key for key in live_keys if key in _STATIC_ROUTER_CHECK_KEYS)
        if "ue_capability" in live_keys:
            keys.update(_UE_CAPABILITY_KEYS)
    return keys


def _static_value_has_owner(data: Mapping[str, Any], key: str, value: Any) -> bool:
    if key in _static_evidence_keys(data):
        return True
    if not isinstance(data.get("modem_profile"), Mapping) and "capability_contract" not in data:
        return True
    norm = _normalized_text(value)
    if norm in _UNSUPPORTED_TEXT or norm in _PROBING_TEXT or norm in _UNKNOWN_TEXT:
        return True
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str) and norm in _TRUE_TEXT:
        return True
    # Compatibility for older owner payloads that explicitly published static
    # facts without an evidence list. New Modem Live payloads include a profile
    # or evidence keys, so false defaults in those payloads do not pass here.
    return not isinstance(data.get("modem_profile"), Mapping)


def _router_check_status(value: Any, *, missing: str = STATUS_UNKNOWN) -> str:
    if value is None:
        return missing
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in _UNKNOWN_TEXT:
            return missing
        if norm in _PROBING_TEXT:
            return STATUS_PROBING
        if norm in _UNSUPPORTED_TEXT:
            return STATUS_UNSUPPORTED
        if norm in _TRUE_TEXT:
            return STATUS_OK
        if norm in _FALSE_TEXT:
            return STATUS_MISS
        return STATUS_OK if norm else missing
    if isinstance(value, bool):
        return STATUS_OK if value else STATUS_MISS
    if isinstance(value, (int, float)):
        return STATUS_OK if value else STATUS_MISS
    return STATUS_OK if bool(value) else STATUS_MISS


def _ue_category_status(value: Any, *, missing: str = STATUS_UNKNOWN) -> str:
    if value is None:
        return missing
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in _UNKNOWN_TEXT:
            return missing
        if norm in _PROBING_TEXT:
            return STATUS_PROBING
        if norm in _UNSUPPORTED_TEXT:
            return STATUS_UNSUPPORTED
    text = _clean_text(value)
    if text == STATUS_NA:
        return missing
    try:
        if int(str(text).strip()) <= 0:
            return missing
    except Exception:
        pass
    return text


def _router_check_value(data: Mapping[str, Any], static: Mapping[str, Any], key: str) -> str:
    if key == "pci_lock_available":
        action = capability_contract_action_meta(data, "cell_lock")
        if not isinstance(action, Mapping):
            return STATUS_UNKNOWN
        state = _text(action.get("state")).lower()
        if state in {"unsupported", "non_useful"}:
            return STATUS_UNSUPPORTED
        if state in {"supported", "estimated"}:
            return STATUS_OK if bool(action.get("write_allowed", False)) else STATUS_MISS
        return STATUS_UNKNOWN
    read_meta = capability_contract_read_meta(data, key)
    if isinstance(read_meta, Mapping) and str(
        read_meta.get("state") or ""
    ).strip().lower() in {"unsupported", "non_useful"}:
        return STATUS_UNSUPPORTED
    unsupported = _explicit_unsupported_fields(data)
    if key in unsupported:
        return STATUS_UNSUPPORTED
    if key in static:
        value = static.get(key)
        if key in _STATIC_ROUTER_CHECK_KEYS and not _static_value_has_owner(data, key, value):
            return STATUS_UNKNOWN
    elif key in data:
        value = data.get(key)
    else:
        return STATUS_UNKNOWN

    if key == "ue_category":
        return _ue_category_status(value)
    return _router_check_status(value)


def _live_bool(value: Any) -> str:
    return _status_bool(value, true_text="YES", false_text="NO")


def _clean_text(value: Any) -> str:
    lines = [line.strip() for line in str(value or "").replace("\r", "\n").splitlines() if line.strip()]
    lines = [line for line in lines if line.upper() != "OK"]
    lines = [line for line in lines if line.strip().lower() not in _UNKNOWN_TEXT]
    return "N/A" if not lines else lines[0] if len(lines) == 1 else f"{lines[0]}\n{' '.join(lines[1:])}"


def _nested_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _access_text_from_payload(data: Mapping[str, Any]) -> str:
    """Copy user-facing access from the Modem Live identity publication."""

    access_contract = _nested_mapping(data, "access_type_contract")
    text = _clean_text(access_contract.get("display_name"))
    if text != STATUS_NA:
        return text

    for owner_key in ("identity", "runtime_identity_display", "runtime_identity", "resolved_identity_card"):
        owner = _nested_mapping(data, owner_key)
        nested_contract = _nested_mapping(owner, "access_type_contract")
        text = _clean_text(nested_contract.get("display_name") or owner.get("access_type"))
        if text != STATUS_NA:
            return text

    return STATUS_NA


def _capability_reason_from_payload(key: str, payload: Any) -> str:
    normalized = str(key or "").strip().lower()
    meta = capability_contract_action_meta(payload, normalized)
    if isinstance(meta, Mapping):
        reason = _text(meta.get("evidence_reason"))
        if reason:
            return reason
        if bool(meta.get("write_allowed")):
            return ""
        state = _text(meta.get("state")).lower()
        if state == "estimated":
            return "This action is only estimated for this modem profile. Confirmed action evidence is required."
        if state == "unsupported":
            return "This action is not supported by the current modem profile."
    return ""


def probe_test_capability_gate(*, key: str, enabled: bool, probe_mode: bool) -> tuple[bool, str]:
    if not bool(enabled) or not bool(probe_mode):
        return bool(enabled), ""
    reason = _PROBE_TEST_CAPABILITY_REASONS.get(str(key or "").strip().lower(), "")
    if reason:
        return False, reason
    return True, ""


def _capability_enabled_from_payload(key: str, payload: Any) -> bool:
    contract_enabled = capability_contract_action_enabled(payload, key)
    if contract_enabled is not None:
        return bool(contract_enabled)
    data = _mapping(payload)
    if key in data:
        return bool(data.get(key))
    return False


def _live_control_text_from_payload(key: str, payload: Any) -> str:
    meta = capability_contract_action_meta(payload, key)
    if isinstance(meta, Mapping):
        state = _text(meta.get("state")).lower()
        if state == "estimated":
            return "BEST EFFORT"
        if state in {"unsupported", "non_useful"}:
            return STATUS_UNSUPPORTED
        return _live_bool(meta.get("write_allowed"))
    data = _mapping(payload)
    return _live_bool(data.get(key))


def _build_action_surface(payload: Any, *, probe_mode: bool) -> tuple[dict[str, bool], dict[str, str]]:
    enabled: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    for key in GUI_CAPABILITY_KEYS:
        raw_enabled = _capability_enabled_from_payload(key, payload)
        gated_enabled, probe_reason = probe_test_capability_gate(
            key=key,
            enabled=raw_enabled,
            probe_mode=bool(probe_mode),
        )
        reason = probe_reason or _capability_reason_from_payload(key, payload)
        enabled[key] = bool(gated_enabled)
        reasons[key] = reason if reason else ""
    return enabled, reasons


def build_capability_surface_from_payload(
    payload: Any,
    *,
    probe_mode: bool = False,
) -> CapabilityPresentationSurface:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    data = _owner_payload_from_runtime_envelope(data) if data else {}
    if not data:
        return CapabilityPresentationSurface(
            payload=None,
            live_controls={key: STATUS_PROBING for key, _title in LIVE_CONTROL_KEYS},
            router_checks={key: STATUS_PROBING for key, _title in ROUTER_CHECK_KEYS},
        )
    static = _mapping(data.get("static"))
    live_controls = {key: _live_control_text_from_payload(key, data) for key, _title in LIVE_CONTROL_KEYS}
    router_checks = {
        key: _router_check_value(data, static, key)
        for key, _title in ROUTER_CHECK_KEYS
    }

    action_enabled, action_reasons = _build_action_surface(data, probe_mode=bool(probe_mode))
    return CapabilityPresentationSurface(
        payload=data,
        access_text=_access_text_from_payload(data),
        live_controls=live_controls,
        router_checks=router_checks,
        action_enabled=action_enabled,
        action_reasons=action_reasons,
    )


def manual_action_allowed_by_surface(
    action: Any,
    surface: CapabilityPresentationSurface,
) -> tuple[bool, str]:
    lever = getattr(action, "lever", None)
    params = getattr(action, "params", {})
    params = params if isinstance(params, Mapping) else {}
    key = _LEVER_CAPABILITY_KEYS.get(lever)
    if key is None:
        return True, ""
    if lever == LeverType.BAND_SELECTION and params.get("nr_band") and not surface.is_enabled("nr_band_lock"):
        return False, surface.reason("nr_band_lock") or "NR band preference is not supported by this modem. No action taken."
    if surface.is_enabled(key):
        return True, ""
    unsupported_messages = {
        "band_select": "Band selection is not supported by this modem. No action taken.",
        "band_lock": "Band lock is not supported by this modem. No action taken.",
        "cell_lock": "Cell lock is not supported by this modem. No action taken.",
        "restart": "Restart is not supported by this modem. No action taken.",
        "pband": "PBAND requires ordinary band selection support. No action taken.",
        "nr_band_lock": "NR band preference is not supported by this modem. No action taken.",
    }
    reason = surface.reason(key)
    if reason:
        return False, reason
    if key in surface.action_enabled:
        return False, unsupported_messages.get(key, "Manual action is not supported by this modem. No action taken.")
    return False, unsupported_messages.get(key, "Manual action is not supported by this modem. No action taken.")
