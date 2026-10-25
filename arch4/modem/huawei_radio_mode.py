"""HiLink configured-mode codec used only by the existing Huawei I/O owner.

The selected profile supplies the exact XML dialect and permitted native modes.
Keep configured LTE/NR preferences separate from active radio state and
frequency-lock restrictions. Session, polling, retry and control authority
remain with their existing owners.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from arch4.core_contracts.command_targets import CoreRadioModeTarget
from arch4.core_contracts.network_settings import subscription_key
from arch4.modem_live.radio_mode_configuration import (
    mode_key_for_native, native_mode_for_key, profile_radio_modes, radio_mode_observation,
    require_radio_mode_configuration,
)


def contract(modem):
    definition = modem._definition
    document = definition.snapshot()
    profile = document["profile"]
    value = document.get("parameters", {}).get("radio_mode_contract")
    if (profile.get("device_radio_class") != "5g"
            or not profile.get("capabilities", {}).get("supports_radio_mode")
            or not isinstance(value, dict) or value.get("dialect") != "hilink_net_mode_v1"
            or definition.profile_id != modem._selected_profile_id):
        return None
    try:
        options = value["network_options"]
        if (not isinstance(options, list) or not options or len(set(options)) != len(options)
                or not set(options).issubset({"0", "1", "2"}) or value["nr_selector"] != "lock_freq"):
            return None
        modes = profile_radio_modes(definition)
        # The finite human profile codec cannot redefine this native protocol.
        # LTE carries its optional preference unchanged as preserved settings.
        if modes["lte_only"] != {"NetworkMode": "03"}:
            return None
        for key, native in modes.items():
            if key == "lte_only":
                continue
            if (not isinstance(native, dict) or native.get("NetworkMode") != "00"
                    or set(native) - {"NetworkMode", "networkOption"}
                    or "networkOption" in native and native["networkOption"] not in options):
                return None
    except (KeyError, TypeError, ValueError, RuntimeError):
        return None
    return value


def response(raw):
    if not isinstance(raw, str) or len(raw) > 1024 * 1024 or "<!" in raw:
        raise ValueError("huawei_radio_mode_native_response_invalid")
    root = ET.fromstring(raw)
    if root.tag != "response" or root.attrib:
        raise ValueError("huawei_radio_mode_native_response_invalid")
    return root


def scalar(root, tag, *, optional=False):
    rows = root.findall(tag)
    if optional and not rows:
        return None
    if len(rows) != 1 or list(rows[0]) or rows[0].attrib:
        raise ValueError("huawei_radio_mode_native_field_invalid")
    value = (rows[0].text or "").strip()
    if len(value) > 4096:
        raise ValueError("huawei_radio_mode_native_field_invalid")
    return value


def native_ok(raw):
    try:
        root = response(raw)
        return not list(root) and (root.text or "").strip() == "OK"
    except (ValueError, ET.ParseError):
        return False


def preserve_lte_write(modem, xml_body):
    """A selected 5G profile changes LTE bands without implicitly changing RAT.

    This small preservation read is not mode capability evidence. If it is
    unavailable, ordinary LTE emergency handling retains its existing body.
    """
    settings = contract(modem)
    if settings is None:
        return xml_body, None
    try:
        fresh = response(modem._api_get("/api/net/net-mode", lane="control"))
        mode = scalar(fresh, "NetworkMode")
        network_band = scalar(fresh, "NetworkBand")
        option = scalar(fresh, "networkOption", optional=True)
        if (mode not in {"00", "03"}
                or not re.fullmatch(r"[0-9a-fA-F]{1,32}", network_band or "")
                or option is not None and option not in settings["network_options"]):
            raise ValueError("huawei_lte_mode_preservation_unavailable")
        root = ET.fromstring(xml_body)
        root.find("NetworkMode").text = mode
        root.find("NetworkBand").text = network_band
        if option is not None:
            ET.SubElement(root, "networkOption").text = option
        return ET.tostring(root, encoding="unicode"), True
    except (ValueError, TypeError, KeyError, RuntimeError, ET.ParseError):
        return xml_body, False


def _nr_selection(root):
    info = root.findall("nr_info")
    if len(info) != 1:
        raise ValueError("huawei_nr_selection_unavailable")
    info = info[0]
    kind = scalar(info, "lock_mode")
    if kind not in {"0", "1", "2", "3"}:
        raise ValueError("huawei_nr_selection_unavailable")
    if kind == "0":
        return [], "auto", {"lock_mode": kind}
    rows = info.findall("./freq_infos/freq_info")
    if not rows or len(rows) > 128:
        raise ValueError("huawei_nr_selection_unavailable")
    bands, restrictions = [], []
    for row in rows:
        if (any(list(child) or child.attrib for child in row)
                or len({child.tag for child in row}) != len(row)):
            raise ValueError("huawei_nr_selection_unavailable")
        band = scalar(row, "band")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", band):
            raise ValueError("huawei_nr_selection_unavailable")
        bands.append(int(band))
        # Frequency/cell restrictions are also preserved; bands have their own guard.
        restrictions.append({child.tag: child.text or "" for child in row if child.tag != "band"})
    return sorted(set(bands)), "manual", {"lock_mode": kind, "restrictions": restrictions}


def _access_modes(root):
    # The native mode response nests repeated Access nodes under AccessList.
    # Require the native repeated Access nodes; never infer an omitted mode.
    lists = root.findall("AccessList")
    if len(lists) != 1 or lists[0].attrib or not 1 <= len(lists[0]) <= 32:
        raise ValueError("huawei_radio_mode_options_unavailable")
    modes = []
    for row in lists[0]:
        value = (row.text or "").strip()
        if row.tag != "Access" or row.attrib or list(row) or not re.fullmatch(r"[0-9]{2}", value):
            raise ValueError("huawei_radio_mode_options_unavailable")
        modes.append(value)
    if len(set(modes)) != len(modes):
        raise ValueError("huawei_radio_mode_options_unavailable")
    return set(modes)


def _decode(modem, settings, info, replies):
    profile = modem._definition.metadata
    model, firmware = str(info.get("model") or ""), str(info.get("firmware") or "")
    if (not model or not firmware or firmware.lower() == "unknown"
            or not any(token.upper() in model.upper() for token in profile.get("model_contains", ()))
            or any(token.upper() not in firmware.upper() for token in profile.get("firmware_contains", ()))
            or any(token.upper() in firmware.upper() for token in settings.get("blocked_firmware_tokens", ()))
            or not (info.get("serial") or info.get("imei"))
            or not re.fullmatch(r"[0-9]{14,16}", str(info.get("imsi") or ""))):
        raise ValueError("huawei_radio_mode_identity_unavailable")
    root = replies["/api/net/net-mode"]
    if any("nrband" in child.tag.lower() for child in root):
        raise ValueError("huawei_radio_mode_unmodeled_nr_selection")
    native_mode = scalar(root, "NetworkMode")
    network_band, lte_mask = scalar(root, "NetworkBand"), scalar(root, "LTEBand")
    option = scalar(root, "networkOption", optional=True)
    if not re.fullmatch(r"[0-9a-fA-F]{1,32}", network_band or ""):
        raise ValueError("huawei_radio_mode_network_band_unavailable")
    if not re.fullmatch(r"[0-9a-fA-F]{1,32}", lte_mask or ""):
        raise ValueError("huawei_radio_mode_lte_selection_unavailable")
    if option is not None and option not in settings["network_options"]:
        raise ValueError("huawei_radio_mode_network_option_unavailable")
    options = _access_modes(replies["/api/net/net-mode-list"])
    if not {"00", "03"}.issubset(options):
        raise ValueError("huawei_radio_mode_options_unavailable")
    choice = {"NetworkMode": native_mode}
    if native_mode != "03" and option is not None:
        choice["networkOption"] = option
    key = mode_key_for_native(modem._definition, choice)
    from arch4.modem.huawei_parsers import hex_to_bands, is_all_bands_mask
    lte_kind = "auto" if is_all_bands_mask(lte_mask) else "manual"
    lte_bands = [] if lte_kind == "auto" else hex_to_bands(lte_mask, auto_is_empty=True)
    if settings["nr_selector"] != "lock_freq":
        raise ValueError("huawei_radio_mode_nr_selection_unavailable")
    nr_bands, nr_kind, nr_constraints = _nr_selection(replies["/api/net/lock-freq"])
    register = replies["/api/net/register"]
    operator = {name: scalar(register, name) for name in ("Mode", "Plmn", "Rat")}
    connection = replies["/api/dialup/connection"]
    roaming = scalar(connection, "RoamAutoConnectEnable")
    profiles = replies["/api/dialup/profiles"]
    current = scalar(profiles, "CurrentProfile")
    if not current:
        raise ValueError("huawei_radio_mode_active_apn_unavailable")
    active = [row for row in profiles.findall("./Profiles/Profile") if scalar(row, "Index") == current]
    if len(active) != 1:
        raise ValueError("huawei_radio_mode_active_apn_unavailable")
    apn = active[0]
    if (any(list(child) or child.attrib for child in apn)
            or len({child.tag for child in apn}) != len(apn)):
        raise ValueError("huawei_radio_mode_active_apn_invalid")
    preserved = {"NetworkBand": network_band, "networkOption": option,
        "operator": operator, "roaming": roaming,
        "active_apn": {child.tag: child.text or "" for child in apn},
        "nr_constraints": nr_constraints,
        "device": {name: info.get(name) or "" for name in
            ("model", "firmware", "hardware_version", "serial", "imei")}}
    observation = radio_mode_observation(modem._definition, mode_key=key,
        lte_bands=lte_bands, nr_bands=nr_bands, lte_selection_mode=lte_kind,
        nr_selection_mode=nr_kind, subscription_key=subscription_key(info["imsi"]),
        preserved_configuration=preserved)
    fields = {"NetworkMode": native_mode, "NetworkBand": network_band, "LTEBand": lte_mask}
    if option is not None:
        fields["networkOption"] = option
    return observation, fields


def iter_status(modem, lane, *, timeout=None, required=False, include_native=False):
    settings = contract(modem)
    if settings is None:
        if required:
            raise RuntimeError("huawei_radio_mode_not_qualified")
        yield {"radio_mode": None}
        return
    try:
        info = modem.get_modem_info(timeout=timeout)
        if modem._target_guard_enabled():
            modem._apply_target_guard(info)
        yield None
        replies = {}
        paths = ["/api/net/net-mode", "/api/net/net-mode-list", "/api/net/register",
            "/api/dialup/connection", "/api/dialup/profiles"]
        if settings["nr_selector"] == "lock_freq":
            paths.append("/api/net/lock-freq")
        for path in paths:
            replies[path] = response(modem._api_get(path, timeout=timeout, lane=lane))
            yield None
        observation, fields = _decode(modem, settings, info, replies)
        yield (observation, fields) if include_native else {"radio_mode": observation}
    except (ValueError, TypeError, KeyError, RuntimeError, ET.ParseError) as exc:
        if required:
            raise RuntimeError("huawei_radio_mode_observation_unavailable") from exc
        yield {"radio_mode": None}


def status(modem, lane, *, timeout=None, required=False):
    result = {"radio_mode": None}
    for value in iter_status(modem, lane, timeout=timeout, required=required):
        if value is not None:
            result = value
    return result


def execute(modem, plan):
    target = plan.target
    if not isinstance(target, CoreRadioModeTarget) or plan.stage_kind != "radio_mode" or plan.ordinal != 1:
        raise ValueError("huawei_radio_mode_typed_stage_required")
    with modem._api_control_operation_lock:
        observed, fields = next(value for value in iter_status(modem, "control",
            required=True, include_native=True) if value is not None)
        require_radio_mode_configuration(target, observed)
        native = native_mode_for_key(modem._definition, target.mode_key)
        if target.mode != "lte_only" and native.get("networkOption") != fields.get("networkOption"):
            raise RuntimeError("radio_mode_original_configuration_changed")
        fields["NetworkMode"] = native["NetworkMode"]
        root = ET.Element("request")
        for key, value in fields.items():
            ET.SubElement(root, key).text = value
        return modem._execute_physical_post(plan, path="/api/net/net-mode",
            xml_body=ET.tostring(root, encoding="unicode"), timeout=(2., modem._netmode_submit_timeout_s),
            allow_response_unavailable=True, csrf_delay_s=0.1)
