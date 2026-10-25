"""G5TC B17 experimental network boundary; one declared U60 ubus dialect.

Reference: open-u60-pro fdd048a36e1ad379a66d4042c8dbf5b4e18c34f6,
modem_ext.rs/router.rs and iOS MobileNetwork/APN models. Not B17 hardware proof.
Wire spelling is normalized at the ZWRT boundary; no guessed RAT or automatic
fallback registration.
"""
import re
import time
import requests

from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.modem.zte_zwrt_sim import active_sim
from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.core_contracts.network_diagnostics import NETWORK_DIAGNOSTIC_SCHEMA, validate_network_diagnostic
from arch4.core_contracts.network_settings import CoreNetworkSettingsTarget, network_stage_kinds, subscription_key, validate_network_observation
from arch4.protected_credentials import open_apn_credentials

NETWORK_SCAN_TIMEOUT_S = 30.0


def _qualified(modem):
    info = modem._canonical_identity_from_goform(modem._last_raw)
    profile = modem._resolve_firmware_profile()
    return bool(profile and profile.network_settings == "ng_g5tc_b17_experimental" and profile.network_settings_qualified(info.get("firmware", "")))


def _read(modem, service, method, params=None):
    try:
        call={"service": service, "method": method, "params": params or {}}
        if (service,method)==('zwrt_data','get_wwaniface'):
            call=modem._portable_ubus_read('zwrt_data.get_wwaniface',call)
        reply = modem._call_ubus_current_session(call, invalidate_session_on_denial=False)
    except (requests.RequestException, ConnectionError, TimeoutError):
        return None
    return reply["data"] if reply.get("success") and isinstance(reply.get("data"), dict) else None


def network_write_disposition(reply):
    """Only an explicit RPC refusal proves rejection, never an absent reply."""
    if reply.get("success") is True:
        return "accepted", "accepted"
    reason = str(reply.get("reason") or "")
    explicit = bool(reply.get("accessDenied"))
    if reason.startswith(("zte_zwrt_ubus_error:", "zte_zwrt_rpc_error:")):
        code = reason.partition(":")[2]
        explicit = explicit or bool(re.fullmatch(r"-?[0-9]+", code) and code != "-1")
    return (("rejected", "network_command_rejected") if explicit else
            ("response_unavailable", "network_command_reply_unavailable"))


def _binary_flag(value):
    """Active-APN flags may be JSON booleans or exact 0/1 numbers/text."""
    if type(value) is bool:
        return int(value)
    return int(value) if type(value) in (int, str) and str(value) in {"0", "1"} else None


def _wwan_flags(wwan):
    """WWAN configuration retains its declared numeric 0/1 wire contract."""
    return {key: int(value) if type(value) in (int, str) and str(value) in {"0", "1"} else None
            for key in ("enable", "connect_mode", "roam_enable")
            for value in [(wwan or {}).get(key)]}


def _mobile_data_enabled(wwan):
    """Resolve effective state without confusing the control flag with live IPv4.

    B17 reports enable=0 while IPv4 is connected. Conversely native ON without
    attachment is not deliberate OFF and must not suppress outage recovery.
    Missing/transition states cannot prove OFF. IPv6-only with enable=0 is
    ambiguous: this API family can retain that status after a disconnect.
    """
    status = (wwan or {}).get("connect_status")
    status = status.strip().lower() if type(status) is str else ""
    enabled = _wwan_flags(wwan)["enable"]
    if status in {"connected", "ipv4_connected", "ipv4_ipv6_connected", "ipv6_ipv4_connected"}:
        return True
    if status == "ipv6_connected":
        return True if enabled == 1 else None
    if status in {"disconnected", "ipv4_disconnected", "ipv6_disconnected",
                  "ipv4_ipv6_disconnected", "ipv6_ipv4_disconnected"}:
        return bool(enabled) if enabled is not None else None
    return None


def mobile_data_observation(modem):
    if not _qualified(modem):
        raise RuntimeError("network_profile_contract_required")
    wwan = _read(modem, "zwrt_data", "get_wwaniface", {"cid": 1})
    observed = {"mobile_data_enabled": _mobile_data_enabled(wwan)}
    replies = {"zwrt_data.get_wwaniface":wwan}
    addresses = modem._definition.available_addresses(replies,
        addresses={"slow.mobile_data_enabled"})
    corrected = modem._decode_profile_values(replies, addresses)
    if "slow.mobile_data_enabled" in corrected:
        observed["mobile_data_enabled"] = corrected["slow.mobile_data_enabled"]
    address = modem._extract_zwrt_wan_ip(wwan)
    if address:
        observed["wan_ip"] = address
    return observed


def _data_call(flags, *, enabled, automatic=False):
    if type(enabled) is not bool or any(value is None for value in flags.values()):
        raise RuntimeError("network_wwan_configuration_unavailable")
    params = {"cid": 1, **flags, "enable": int(enabled)}
    if not enabled:params["connect_status"] = "disconnected"
    if automatic:params["connect_mode"] = 1
    return {"service": "zwrt_data", "method": "set_wwaniface", "params": params}


def mobile_data_call(modem, target):
    from arch4.core_contracts.command_targets import CoreMobileDataTarget
    if not isinstance(target, CoreMobileDataTarget) or not _qualified(modem):
        raise RuntimeError("network_profile_contract_required")
    flags = _wwan_flags(_read(modem, "zwrt_data", "get_wwaniface", {"cid": 1}))
    return _data_call(flags, enabled=target.enabled)


def _diagnostic(modem, *, connected, imsi, issues):
    modem._network_diagnostic_sequence += 1
    return ModemBackendDiagnostic(
        owner="modem_live.network_connection", sequence=modem._network_diagnostic_sequence,
        observed_at=time.time(), kind="network_connection_diagnostic",
        backend=modem._backend_name, severity="INFO" if connected is True else "WARNING",
        data=validate_network_diagnostic({"schema": NETWORK_DIAGNOSTIC_SCHEMA,
            "subscription_key": subscription_key(imsi), "mobile_connected": connected,
            "issues": issues}),
    )


def _iter_diagnostic(modem, *, wwan, net, sim, apn):
    """Read-only, demand-driven checks in the existing serialized inspection."""
    identity = active_sim(sim)
    imsi = identity["imsi"]
    radio = dict(net or {})
    if (wwan or {}).get("connect_status"):
        radio["ppp_status"] = wwan["connect_status"]
    connected = modem._raw_radio_connected(radio)
    # No additional diagnostic RPCs on the known-good connected path.
    if connected is True:
        return _diagnostic(modem, connected=True, imsi=imsi, issues=[])
    wan = _read(modem, "network.interface.zte_wan", "status")
    yield None
    if type((wan or {}).get("up")) is bool:
        radio["wan_connected"] = wan["up"]
        connected = modem._raw_radio_connected(radio)
    if connected is True:
        return _diagnostic(modem, connected=True, imsi=imsi, issues=[])
    issues = []
    mode_reply = _read(modem, "uci", "get", {"config": "zte_nwinfo", "section": "sys_info"})
    yield None
    values = (mode_reply or {}).get("values")
    radio_mode = values.get("operate_mode") if isinstance(values, dict) else None
    if radio_mode == "LPM":
        issues.append("radio_disabled")
    elif radio_mode != "ONLINE":
        issues.append("radio_mode_unavailable")
    sim_state = identity["status"]
    if sim_state in {"pin", "puk", "sim_pin", "sim_puk", "pin_required", "puk_required"}:
        issues.append("sim_locked")
    elif not subscription_key(imsi):
        issues.append("sim_unavailable")
    flags = _wwan_flags(wwan)
    data = _mobile_data_enabled(wwan)
    mode, roam = (flags[key] for key in ("connect_mode", "roam_enable"))
    if data is None:
        issues.append("data_state_unavailable")
    elif data is False:
        issues.append("data_disabled")
    if mode is None:
        issues.append("connection_mode_unavailable")
    elif mode == 0:
        issues.append("manual_connection")
    if roam is None:
        issues.append("roaming_state_unavailable")
    elif roam == 0:
        issues.append("roaming_disabled")
    if not apn:
        issues.append("apn_unavailable")
    elif not apn["editable"]:
        issues.append("apn_read_only")
    selection, _ = _selection(modem, net)
    if selection is None:
        issues.append("selection_unavailable")
    elif selection == "manual":
        reply = _read(modem, "zte_nwinfo_api", "nwinfo_m_netselect_result")
        yield None
        result = str((reply or {}).get("m_netselect_result", "")).lower()
        issues.append({"success": "registered_without_data", "1": "registered_without_data",
                       "fail": "registration_failed", "0": "registration_failed",
                       "pending": "registration_pending", "registering": "registration_pending",
                       "2": "registration_pending"}.get(result, "registration_result_unavailable"))
    issues.append("no_mobile_service" if connected is False else "mobile_state_unavailable")
    return _diagnostic(modem, connected=connected, imsi=imsi, issues=issues)


def _selection(modem, net):
    """Current manual operator from one fresh reply, not a saved-lock claim.

    B17 compatibility with the public U60 dialect is user-authorized as an
    assumption. Never use the request, last raw status or a provider cache.
    Both inspection and the pre-roaming guard use this interpretation.
    """
    raw = dict(net or {})
    selection = {"auto_select": "auto", "manual_select": "manual"}.get(raw.get("net_select_mode"))
    if selection != "manual":
        return selection, None
    direct = str(raw.get("mccmnc") or raw.get("plmn") or "").strip()
    if direct:
        if not re.fullmatch(r"[0-9]{5,6}", direct):
            return selection, None
        raw["mccmnc"] = direct
    elif not (re.fullmatch(r"[0-9]{3}", str(raw.get("rmcc") or "").strip())
              and re.fullmatch(r"[0-9]{1,3}", str(raw.get("rmnc") or "").strip())):
        return selection, None
    plmn = modem._resolve_plmn(raw)
    return selection, plmn if re.fullmatch(r"[0-9]{5,6}", plmn) else None


def _selected_apn(mode, data):
    """Decode the existing selected manual/automatic native APN reply."""
    if mode is None or str(mode.get("apn_mode")) not in {"0", "1"}:
        return None
    manual = str(mode["apn_mode"]) == "1"
    rows = data.get("apnListArray") if data else None
    active = [r for r in rows if isinstance(r, dict) and _binary_flag(r.get("isEnable")) == 1] if isinstance(rows, list) else []
    if len(active) != 1:
        return None
    row = active[0]
    return {"profile_id": str(row.get("profileId") or ""), "name": str(row.get("profilename") or ""),
            "apn": str(row.get("wanapn") or ""), "username": str(row.get("username") or ""),
            "authentication": {"0": "none", "1": "pap", "2": "chap"}.get(str(row.get("pppAuthMode"))),
            "ip_protocol": {"1": "ipv4", "2": "ipv6", "3": "ipv4v6"}.get(str(row.get("pdpType"))),
            "editable": manual}


def _iter_apn(modem):
    mode = _read(modem, "zwrt_apn_object", "get_apn_mode")
    yield None
    if mode is None or str(mode.get("apn_mode")) not in {"0", "1"}:
        return None
    manual = str(mode["apn_mode"]) == "1"
    data = _read(modem, "zwrt_apn_object", "get_manu_apn_list" if manual else "get_auto_apn_list")
    return _selected_apn(mode, data)


def _apn(modem):
    steps = _iter_apn(modem)
    try:
        while True:
            next(steps)
    except StopIteration as done:
        return done.value
    finally:
        steps.close()


def iter_network_settings(modem):
    if not _qualified(modem):
        raise RuntimeError("network_inspection_profile_or_subscription_unavailable")
    wwan = _read(modem, "zwrt_data", "get_wwaniface", {"cid": 1})
    yield None
    net = _read(modem, "zte_nwinfo_api", "nwinfo_get_netinfo")
    yield None
    sim = _read(modem, "zwrt_zte_mdm.api", "get_sim_info")
    imsi = active_sim(sim)["imsi"]
    selection, plmn = _selection(modem, net)
    yield None
    apn = yield from _iter_apn(modem)
    diagnostic = yield from _iter_diagnostic(modem, wwan=wwan, net=net, sim=sim, apn=apn)
    return NetworkSettingsRead(validate_network_observation({"schema": "NetworkSettingsObservation.v1",
        "subscription_key": subscription_key(imsi),
        "selection": selection,
        "target_plmn": plmn,
        "roaming_enabled": {0: False, 1: True}.get(_wwan_flags(wwan)["roam_enable"]),
        "apn": apn}), imsi, diagnostic)


def network_call(modem, plan):
    target = plan.target
    if not isinstance(target, CoreNetworkSettingsTarget) or not _qualified(modem):
        raise RuntimeError("network_profile_contract_required")
    stages = network_stage_kinds(target, coordination_profile="zte_zwrt")
    if plan.ordinal > len(stages) or stages[plan.ordinal - 1] != plan.stage_kind:
        raise RuntimeError("network_stage_order_invalid")
    sim = _read(modem, "zwrt_zte_mdm.api", "get_sim_info")
    if subscription_key(active_sim(sim)["imsi"]) != target.subscription_key:
        raise RuntimeError("network_subscription_changed_or_unavailable")
    # Validate entire requested APN before the first network mutation.
    credentials = None
    if target.apn and plan.stage_kind in {"network_data_hold", "network_apn"}:
        active = _apn(modem)
        if not active or not active["editable"] or active["profile_id"] != target.apn.profile_id:
            raise RuntimeError("network_active_manual_apn_required")
        if target.apn.authentication not in {"none", "pap", "chap"}:
            raise RuntimeError("network_apn_authentication_unsupported")
        credentials = open_apn_credentials(target.apn.protected_credentials)
        if credentials[0] != target.apn.username:
            raise ValueError("network_apn_credential_username_mismatch")
        if credentials[1] is None:
            raise RuntimeError("apn_explicit_password_required")
    wwan = _read(modem, "zwrt_data", "get_wwaniface", {"cid": 1})
    flags = _wwan_flags(wwan)
    if any(value is None for value in flags.values()):
        raise RuntimeError("network_wwan_configuration_unavailable")
    if plan.stage_kind != "network_data_hold" and _mobile_data_enabled(wwan) is not False:
        raise RuntimeError("network_data_hold_not_confirmed")
    if plan.stage_kind == "network_data_hold":
        return _data_call(flags, enabled=False)
    if plan.stage_kind == "network_data_start":
        if target.roaming_enabled is not None and flags["roam_enable"] != int(target.roaming_enabled):
            raise RuntimeError("network_roaming_not_confirmed")
        if target.apn:
            active = _apn(modem)
            if not active or any(active[key] != getattr(target.apn, key) for key in (
                "profile_id", "name", "apn", "username", "authentication", "ip_protocol")):
                raise RuntimeError("network_apn_not_confirmed")
        if target.selection != "keep":
            net = _read(modem, "zte_nwinfo_api", "nwinfo_get_netinfo")
            if _selection(modem, net)[0] != target.selection:
                raise RuntimeError("network_selection_not_confirmed")
        # The admitted operator stage must acknowledge the exact request before
        # Modem Live advances here. Attachment is checked by Core AFTER data is
        # enabled, never used as a prerequisite for the settings that enable it.
        return _data_call(flags, enabled=True, automatic=True)
    if plan.stage_kind == "network_operator":
        if target.selection == "auto":
            return {"service": "zte_nwinfo_api", "method": "nwinfo_set_netselect", "params": {"net_select_mode": "auto_select"}}
        # This bounded scan belongs to the admitted operator stage. B17 has no
        # WebUI selector to populate it. A refusal never triggers another API.
        started = modem._ubus_call({"service": "zte_nwinfo_api", "method": "nwinfo_manual_scan", "params": {}}, lane="event_write")
        if not started.get("success"):
            if network_write_disposition(started)[0] == "rejected":
                raise RuntimeError("network_operator_scan_rejected")
            raise RuntimeError("network_operator_scan_reply_unavailable")
        deadline = time.monotonic() + NETWORK_SCAN_TIMEOUT_S
        while time.monotonic() < deadline:
            status = _read(modem, "zte_nwinfo_api", "nwinfo_m_netselect_status")
            if (status or {}).get("m_netselect_status") in {"done", "complete", "2"}:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("network_operator_scan_timed_out")
        scan = _read(modem, "zte_nwinfo_api", "nwinfo_m_netselect_contents")
        rows = (scan or {}).get("m_netselect_contents")
        rats = {str(row["rat"]) for row in rows if isinstance(row, dict) and row.get("mcc_mnc") == target.target_plmn and row.get("status") in {"available", "current"} and row.get("rat")} if isinstance(rows, list) else set()
        if len(rats) != 1:
            raise RuntimeError("network_target_scan_rat_unavailable_or_ambiguous")
        return {"service": "zte_nwinfo_api", "method": "nwinfo_manual_register", "params": {"m_mcc_mnc": target.target_plmn, "m_rat": rats.pop()}}
    if plan.stage_kind == "network_roaming":
        return _data_call({**flags, "roam_enable": int(target.roaming_enabled)}, enabled=False)
    if plan.stage_kind == "network_apn" and target.apn:
        return {"service": "zwrt_apn_object", "method": "modify_manu_apn", "params": {
            "profileId": target.apn.profile_id, "profilename": target.apn.name, "wanapn": target.apn.apn,
            "pdpType": {"ipv4": 1, "ipv6": 2, "ipv4v6": 3}[target.apn.ip_protocol],
            "pppAuthMode": {"none": 0, "pap": 1, "chap": 2}[target.apn.authentication],
            "username": credentials[0], "password": credentials[1]}}
    raise RuntimeError("network_stage_unsupported")
