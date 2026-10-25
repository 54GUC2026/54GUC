"""HiLink network adapter, called only by the existing acquisition/stage owners."""
from __future__ import annotations

import base64
import copy
import re
from xml.etree import ElementTree as ET

from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.core_contracts.network_settings import (
    CoreNetworkSettingsTarget, subscription_key, validate_network_observation,
)
from arch4.modem.catalog import match_firmware_profile
from arch4.protected_credentials import open_apn_credentials


_HILINK_DIALECTS = frozenset({"hilink_b818", "hilink_b535_232", "hilink_b535_333", "hilink_h173_c983_read_assumption"})
_AUTH = {"0": "auto", "1": "pap", "2": "chap"}
_IP = {"0": "ipv4", "1": "ipv6", "2": "ipv4v6"}


def require_mobile_data_profile(modem):
    profile = profile_for(modem)
    if profile is None or not profile.capabilities.get("supports_mobile_data", False) or profile.network_settings not in {
        "hilink_b818", "hilink_b535_232", "hilink_b535_333"
    }:
        raise RuntimeError("huawei_mobile_data_profile_unqualified")


def read_mobile_data(modem, *, timeout=None):
    require_mobile_data_profile(modem)
    path = "/api/dialup/mobile-dataswitch"
    xml = modem._api_get(path, timeout=timeout)
    root = _response(xml)
    raw = root.findtext("dataswitch") if root is not None else None
    value = {"0": False, "1": True}.get(raw)
    if root is not None:
        values = modem._decode_profile_values({path: xml}, {"slow.mobile_data_enabled"})
        value = values.get("slow.mobile_data_enabled", value)
    return {"mobile_data_enabled": value}


def profile_for(modem):
    info = modem._device_info_cache
    profile = match_firmware_profile(
        backend=modem._backend_name, model=info.get("model", ""),
        firmware=info.get("firmware", ""),
    )
    return profile if profile and profile.network_settings in _HILINK_DIALECTS else None


def _read(modem, path: str, *, timeout=None) -> ET.Element | None:
    return _response(modem._api_get(path, timeout=timeout))


def _response(raw: str | None) -> ET.Element | None:
    if not raw or "<!" in raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    return root if root.tag == "response" else None


def _active_profile(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    active = root.findtext("CurrentProfile")
    rows = [row for row in root.findall("./Profiles/Profile") if row.findtext("Index") == active]
    return rows[0] if len(rows) == 1 else None


def _selection(modem, profile, register, *, timeout=None):
    """Exact firmware adapter for the currently observed operator selection.

    B818 H220 reports Mode but leaves Plmn empty after manual registration.
    Its manual operator is proved by Mode plus a fresh current-PLMN receipt,
    never by the requested target or a retained provider. B535 keeps its own
    register-Plmn contract. This is not proof of persistence after an outage.
    """
    mode = register.findtext("Mode") if register is not None else None
    selection = {"0": "auto", "1": "manual"}.get(mode)
    plmn = register.findtext("Plmn", "") if register is not None else None
    if selection == "manual" and profile.network_settings == "hilink_b818":
        before = float(modem._plmn_cache_ts or 0.0)
        provider = modem._get_plmn_cached(max_age_s=0.0, timeout=timeout)
        current = str(provider.get("plmn") or "")
        if (float(modem._plmn_cache_ts or 0.0) <= before
                or not re.fullmatch(r"[0-9]{5,6}", current)
                or plmn and plmn != current):
            return selection, None
        plmn = current
    return selection, plmn


def iter_network_settings(modem):
    profile = profile_for(modem)
    if profile is None:
        raise RuntimeError("network_inspection_profile_or_subscription_unavailable")
    timeout = getattr(modem, "_maintenance_timeout", None)
    identity = _read(modem, "/api/device/information", timeout=timeout)
    imsi = (identity.findtext("Imsi") or identity.findtext("IMSI") or "") if identity is not None else ""
    yield None
    register = _read(modem, "/api/net/register", timeout=timeout)
    yield None
    selection, plmn = _selection(modem, profile, register, timeout=timeout)
    if selection == "manual" and profile.network_settings == "hilink_b818":
        yield None
    connection = _read(modem, "/api/dialup/connection", timeout=timeout)
    yield None
    apn = _active_profile(_read(modem, "/api/dialup/profiles", timeout=timeout))
    roaming = connection.findtext("RoamAutoConnectEnable") if connection is not None else None
    apn_value = None
    if apn is not None:
        apn_value = {
            "profile_id": apn.findtext("Index", ""), "name": apn.findtext("Name", ""),
            "apn": apn.findtext("ApnName", ""), "username": apn.findtext("Username", ""),
            "authentication": _AUTH.get(apn.findtext("AuthMode")),
            "ip_protocol": _IP.get(apn.findtext("iptype")),
            "editable": profile.network_settings_writable and apn.findtext("ReadOnly") == "0",
        }
    return NetworkSettingsRead(validate_network_observation({
        "schema": "NetworkSettingsObservation.v1",
        "subscription_key": subscription_key(imsi),
        "selection": selection,
        "target_plmn": plmn,
        "roaming_enabled": {"0": False, "1": True}.get(roaming), "apn": apn_value,
    }), imsi)


def _request(fields: dict[str, str]) -> str:
    root = ET.Element("request")
    for key, value in fields.items():
        ET.SubElement(root, key).text = value
    return ET.tostring(root, encoding="unicode")


def _encrypt_profile(modem, xml: str) -> str:
    """Firmware public.js: base64 XML, RSA chunks, concatenated fixed-width hex."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    public = _read(modem, "/api/webserver/publickey")
    login = _read(modem, "/api/user/state-login")
    if public is None or login is None:
        raise RuntimeError("huawei_apn_encryption_parameters_unavailable")
    pad_type = login.findtext("rsapadingtype")
    if pad_type not in {"0", "1"}:
        raise RuntimeError("huawei_apn_encryption_padding_unknown")
    key = rsa.RSAPublicNumbers(int(public.findtext("encpubkeye", ""), 16),
                              int(public.findtext("encpubkeyn", ""), 16)).public_key()
    if key.key_size != 2048:
        raise RuntimeError("huawei_apn_encryption_key_size_unqualified")
    pad = padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None) if pad_type == "1" else padding.PKCS1v15()
    size = 214 if pad_type == "1" else 245
    encoded = base64.b64encode(xml.encode("utf-8"))
    return "".join(key.encrypt(encoded[i:i + size], pad).hex() for i in range(0, len(encoded), size))


def execute_network_stage(modem, plan):
    target = plan.target
    if not isinstance(target, CoreNetworkSettingsTarget) or plan.stage_kind not in target.stage_kinds:
        raise TypeError("huawei_network_typed_stage_required")
    if modem._backend_name == "h173":
        raise NotImplementedError("h173_network_settings_are_read_only")
    # Fresh physical subscription proof before every mutation. Never compare
    # the user-edited SIM-country hint with hardware identity/history.
    info = modem.get_modem_info()
    if subscription_key(info.get("imsi", "")) != target.subscription_key:
        raise RuntimeError("network_subscription_changed_or_unavailable")
    profile = match_firmware_profile(backend=modem._backend_name, model=info.get("model", ""), firmware=info.get("firmware", ""))
    if profile is None or profile.network_settings not in _HILINK_DIALECTS:
        raise RuntimeError("huawei_network_exact_profile_unsupported")
    active = None
    credentials = None
    if target.apn:
        # Validate all APN prerequisites even at the first operator stage.
        active = _active_profile(_read(modem, "/api/dialup/profiles"))
        if active is None or active.findtext("Index") != target.apn.profile_id:
            raise RuntimeError("network_apn_active_profile_changed")
        if active.findtext("ReadOnly") != "0":
            raise RuntimeError("network_apn_firmware_profile_read_only")
        if target.apn.authentication not in _AUTH.values():
            raise RuntimeError("huawei_apn_authentication_unsupported")
        credentials = open_apn_credentials(target.apn.protected_credentials)
        if credentials[0] != target.apn.username:
            raise ValueError("network_apn_credential_username_mismatch")
    content_type = "application/xml"
    if plan.stage_kind == "network_operator":
        rat = ""
        if target.selection == "manual":
            if profile.network_settings == "hilink_b535_333":
                rat = "255"  # This exact profile's combined-PLMN UI operation.
            else:
                current = _read(modem, "/api/net/current-plmn")
                if current is not None and current.findtext("Numeric") == target.target_plmn:
                    rat = current.findtext("Rat", "")
                else:
                    scanned = _read(modem, "/api/net/plmn-list", timeout=(3.0, 30.0))
                    rats = {row.findtext("Rat", "") for row in scanned.findall(".//Network") if row.findtext("Numeric") == target.target_plmn and row.findtext("State") != "3"} if scanned is not None else set()
                    if len(rats) != 1:
                        raise RuntimeError("network_target_unavailable_or_rat_ambiguous")
                    rat = rats.pop()
                if rat not in {"0", "2", "7"}:
                    raise RuntimeError("huawei_network_rat_unqualified")
        path = "/api/net/register"
        body = _request({"Mode": "1" if target.selection == "manual" else "0", "Plmn": target.target_plmn, "Rat": rat})
    elif plan.stage_kind == "network_roaming":
        if target.roaming_enabled and target.selection == "manual":
            registered = _read(modem, "/api/net/register")
            if _selection(modem, profile, registered) != ("manual", target.target_plmn):
                raise RuntimeError("network_manual_configuration_required_before_enabling_roaming")
        connection = _read(modem, "/api/dialup/connection")
        if connection is None or connection.find("RoamAutoConnectEnable") is None:
            raise RuntimeError("huawei_roaming_configuration_unavailable")
        connection.tag = "request"
        connection.find("RoamAutoConnectEnable").text = "1" if target.roaming_enabled else "0"
        path, body = "/api/dialup/connection", ET.tostring(connection, encoding="unicode")
    elif plan.stage_kind == "network_apn" and target.apn and active is not None:
        row = copy.deepcopy(active)
        changes = {"Name": target.apn.name, "ApnIsStatic": "1", "ApnName": target.apn.apn,
                   "Username": credentials[0], "AuthMode": {v: k for k, v in _AUTH.items()}[target.apn.authentication],
                   "iptype": {v: k for k, v in _IP.items()}[target.apn.ip_protocol]}
        if credentials[1] is not None:
            changes["Password"] = credentials[1]
        else:
            for child in row.findall("Password"):
                row.remove(child)
        for key, value in changes.items():
            child = row.find(key)
            if child is None:
                child = ET.SubElement(row, key)
            child.text = value
        root = ET.fromstring(_request({"Delete": "0", "SetDefault": "0", "Modify": "2"}))
        root.append(row)
        path, body = "/api/dialup/profiles", _encrypt_profile(modem, ET.tostring(root, encoding="unicode"))
        content_type = "application/x-www-form-urlencoded; charset=UTF-8;enc"
    else:
        raise RuntimeError("huawei_network_stage_unsupported")
    return modem._execute_physical_post(plan, path=path, xml_body=body, timeout=(3.0, 15.0),
                                        allow_response_unavailable=False, csrf_delay_s=0.1,
                                        content_type=content_type)
