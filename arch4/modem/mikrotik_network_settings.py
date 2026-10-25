"""RouterOS SIM/network boundary; reuse the existing selected-interface API."""
from __future__ import annotations

import time

from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.core_contracts.network_settings import subscription_key, validate_network_observation
from arch4.modem.catalog import match_firmware_profile


_AUTH = {"": "none", "none": "none", "pap": "pap", "chap": "chap", "pap,chap": "auto"}
_IP = {"ipv4": "ipv4", "ipv6": "ipv6", "ipv4-ipv6": "ipv4v6"}


def _qualified(modem) -> bool:
    info = modem._device_info_cache
    profile = match_firmware_profile(backend="mikrotik", model=info.get("model", ""),
                                    firmware=info.get("firmware", ""), os_name="RouterOS",
                                    os_version=info.get("os_version", info.get("software_version", "")))
    return bool(profile and profile.network_settings == "routeros_eg120k_v7")


def _selected_interface(modem, rows):
    selected = [row for row in rows if str(row.get("id") or row.get(".id") or "") == str(modem._lte_id)]
    if len(selected) != 1:
        raise RuntimeError("network_selected_lte_interface_unavailable")
    return selected[0]


def _selected_apn(row, rows, profiles):
    name = str(row.get("apn-profiles") or "")
    selected_apn = [item for item in profiles if str(item.get("name") or "") == name]
    apn = selected_apn[0] if len(selected_apn) == 1 else None
    shared = sum(name in str(item.get("apn-profiles") or "").split(",") for item in rows) > 1
    return apn, shared


def _live_subscription(modem) -> str:
    """Bind to the existing live monitor; never probe or use static identity."""
    observed_at = float(getattr(modem, "_last_lte_monitor_ts", 0.0))
    age_s = time.time() - observed_at
    max_age_s = modem.get_live_acquisition_contract().live_register_stale_s
    failed_at = float(getattr(modem, "_last_lte_monitor_failure_ts", 0.0))
    if observed_at <= 0 or age_s < 0 or age_s > max_age_s or failed_at >= observed_at:
        return ""
    monitor = getattr(modem, "_last_lte_monitor", None) or {}
    imsi = str(monitor.get("imsi") or "").strip()
    return subscription_key(imsi) if imsi else ""


def iter_network_settings(modem):
    """One cheap RouterOS read per admission; live polling owns SIM reads."""
    if not _qualified(modem):
        raise RuntimeError("network_inspection_profile_or_subscription_unavailable")
    subscription = _live_subscription(modem)
    if not subscription:
        return None
    transport, interface_id = modem._routeros, modem._lte_id
    rows = transport.get_resource("/interface/lte").get()
    row = _selected_interface(modem, rows)
    yield None  # Modem Live releases its I/O lock and admits pending live work.
    if (modem._routeros is not transport or modem._lte_id != interface_id
            or _live_subscription(modem) != subscription):
        return None
    profiles = transport.get_resource("/interface/lte/apn").get()
    if _live_subscription(modem) != subscription:
        return None
    apn, shared = _selected_apn(row, rows, profiles)
    operator = str(row.get("operator") or "")
    # RouterOS operator is requested configuration, never serving registration.
    selected = "manual" if operator.isdecimal() and len(operator) in {5, 6} else "auto" if not operator else None
    apn_value = None
    if apn is not None:
        apn_value = {"profile_id": str(apn.get("id") or apn.get(".id") or ""),
                     "name": str(apn.get("name") or ""), "apn": str(apn.get("apn") or ""),
                     "username": str(apn.get("user") or ""),
                     "authentication": _AUTH.get(str(apn.get("authentication") or "")),
                     "ip_protocol": _IP.get(str(apn.get("ip-type") or "")),
                     "editable": False}
    return NetworkSettingsRead(validate_network_observation({"schema": "NetworkSettingsObservation.v1",
        "subscription_key": subscription,
        "selection": selected, "target_plmn": operator if selected == "manual" else "" if selected == "auto" else None,
        "roaming_enabled": {"yes": True, "no": False, "true": True, "false": False}.get(str(row.get("allow-roaming")).lower()),
        "apn": apn_value}), "")
