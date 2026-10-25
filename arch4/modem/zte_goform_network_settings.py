"""Qualified GoForm settings reads; only MF296R B04 owns network writes."""
from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.core_contracts.network_settings import CoreNetworkSettingsTarget, subscription_key, validate_network_observation


def _read_contract(modem):
    profile = modem._resolve_firmware_profile()
    contract = profile.network_settings if profile is not None else ""
    if contract not in {"goform_mf296r_b04", "goform_mc801a_at_b14_read_assumption", "goform_g5b_b03_read_assumption"}:
        raise RuntimeError("network_inspection_profile_or_subscription_unavailable")
    return contract


def _write_qualified(modem):
    profile = modem._resolve_firmware_profile()
    return profile is not None and profile.network_settings == "goform_mf296r_b04"


def iter_network_settings(modem):
    contract = _read_contract(modem)
    connection = modem._read_goform(("ConnectionMode",), multi_data=False)
    yield None
    fields = ("imsi", "net_select_mode", "wan_apn", "ppp_username", "ppp_auth_mode", "pdp_type", "profile_name", "Current_index")
    if contract in {"goform_mc801a_at_b14_read_assumption", "goform_g5b_b03_read_assumption"}:
        fields += ("sim_imsi",)
    raw = modem._read_goform(fields)
    if not connection and not raw:
        return None
    native_sims = {modem._normalize_imsi(raw.get(key)) for key in fields if key in {"imsi", "sim_imsi"}}
    native_sims.discard("")
    if len(native_sims) > 1:
        raise RuntimeError("network_subscription_conflicting_native_aliases")
    imsi = next(iter(native_sims), "")
    apn = None
    if raw.get("wan_apn"):
        apn = {"profile_id": str(raw.get("Current_index") or ""), "name": str(raw.get("profile_name") or ""),
               "apn": str(raw["wan_apn"]), "username": str(raw.get("ppp_username") or ""),
               "authentication": {"none": "none", "pap": "pap", "chap": "chap"}.get(raw.get("ppp_auth_mode")),
               "ip_protocol": {"IP": "ipv4", "IPv6": "ipv6", "IPv4v6": "ipv4v6"}.get(raw.get("pdp_type")),
               "editable": False}
    return NetworkSettingsRead(validate_network_observation({"schema": "NetworkSettingsObservation.v1",
        "subscription_key": subscription_key(imsi),
        "selection": {"auto_select": "auto", "manual_select": "manual"}.get(raw.get("net_select_mode")),
        "target_plmn": None, "roaming_enabled": {"on": True, "off": False}.get(connection.get("autoConnectWhenRoaming")), "apn": apn}), imsi)


def network_payload(modem, plan):
    target = plan.target
    if not isinstance(target, CoreNetworkSettingsTarget) or plan.stage_kind not in target.stage_kinds or not _write_qualified(modem):
        raise RuntimeError("zte_goform_network_exact_b04_contract_required")
    raw = modem._read_goform(("imsi", "ppp_status"))
    if subscription_key(str(raw.get("imsi") or "")) != target.subscription_key:
        raise RuntimeError("network_subscription_changed_or_unavailable")
    if raw.get("ppp_status") != "ppp_disconnected":
        raise RuntimeError("mf296r_network_changes_require_data_disconnected_in_router_webui")
    if target.apn:
        raise RuntimeError("mf296r_apn_write_schema_unqualified")
    if plan.stage_kind == "network_roaming":
        current = modem._read_goform(("ConnectionMode",), multi_data=False)
        if current.get("connectionMode") not in {"auto_dial", "manual_dial"}:
            raise RuntimeError("mf296r_connection_mode_unavailable")
        return {"isTest": "false", "goformId": "SET_CONNECTION_MODE", "ConnectionMode": current["connectionMode"],
                "roam_setting_option": "on" if target.roaming_enabled else "off"}
    # Firmware AUTO operation also rewrites the RAT preference. No authorized
    # standalone AUTO implementation, guessed RAT, or disconnect/reconnect.
    raise RuntimeError("mf296r_operator_operation_unqualified")
