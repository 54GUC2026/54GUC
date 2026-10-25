"""Exact G5TC NG net_select choices using the existing authenticated ubus.

B17's configured getter is captured. The setter is public G5TC protocol proof,
not a new assertion that all B17 operators grant writes or restore service.
"""
import re

from arch4.core_contracts.network_settings import subscription_key
from arch4.modem.zte_parsers import decode_zwrt_lte_mask
from arch4.modem.zte_zwrt_sim import active_sim
from arch4.modem_live.radio_mode_configuration import (
    mode_key_for_native, native_mode_for_key, profile_radio_modes, radio_mode_observation,
    require_radio_mode_configuration,
)


def qualified(modem):
    definition = getattr(modem, "_definition", None)
    return bool(definition is not None
        and definition.metadata["capabilities"].get("supports_radio_mode") is True)


def _selected(source, names):
    return {key: {"present": key in source, "value": source.get(key)} for key in names}


def _require_mode_semantics(definition):
    modes = profile_radio_modes(definition)
    if modes["lte_only"] != "Only_LTE":
        raise ValueError("zte_zwrt_radio_mode_lte_only_invalid")
    if any(type(value) is not str or value not in {"LTE_AND_5G", "WL_AND_5G"}
            for key, value in modes.items() if key != "lte_only"):
        raise ValueError("zte_zwrt_radio_mode_enabled_invalid")


def _bands(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", value) is None:
        raise ValueError("zwrt_selected_bands_unavailable")
    result = [int(item) for item in value.split(",")]
    if len(result) != len(set(result)):
        raise ValueError("zwrt_selected_bands_duplicate")
    return sorted(result)


def status(modem, lane, *, required=False):
    if not qualified(modem):
        raise RuntimeError("zte_zwrt_selected_profile_radio_mode_not_qualified")
    calls = [
        {"service":"zte_nwinfo_api", "method":"nwinfo_get_netinfo", "params":{}},
        {"service":"zwrt_zte_mdm.api", "method":"get_sim_info", "params":{}},
        {"service":"zwrt_data", "method":"get_wwaniface", "params":{"cid":1}},
        {"service":"zwrt_apn_object", "method":"get_manu_apn_list", "params":{}},
        {"service":"zwrt_mc.device.manager", "method":"get_device_info", "params":{}},
        {"service":"uci", "method":"get", "params":{"config":"zwrt_common_info", "section":"common_config"}},
        {"service":"zwrt_apn_object", "method":"get_apn_mode", "params":{}},
        {"service":"zwrt_zte_mdm.api", "method":"get_modem_msn", "params":{}},
        {"service":"zwrt_apn_object", "method":"get_auto_apn_list", "params":{}},
    ]
    try:
        _require_mode_semantics(modem._definition)
        replies = modem._ubus_call(calls, lane=lane)
        # Both APN lists share this HTTP batch; only the selected one is required.
        if (not isinstance(replies, list) or len(replies) != len(calls)
                or any(not isinstance(reply, dict) or reply.get("success") is not True
                       or not isinstance(reply.get("data"), dict)
                       for index, reply in enumerate(replies) if index not in (3, 8))):
            raise ValueError("zwrt_native_configuration_unavailable")
        net, sim, wwan, _, device, common_reply, apn_mode, serial, _ = [
            reply.get("data") if isinstance(reply, dict) else None for reply in replies]
        if str(apn_mode.get("apn_mode")) not in {"0", "1"}:
            raise ValueError("zwrt_selected_apn_unavailable")
        selected_reply = replies[3 if str(apn_mode["apn_mode"]) == "1" else 8]
        if (not isinstance(selected_reply, dict) or selected_reply.get("success") is not True
                or not isinstance(selected_reply.get("data"), dict)):
            raise ValueError("zwrt_selected_apn_unavailable")
        apn = selected_reply["data"]
        from arch4.modem.zte_ng_network_settings import _selected_apn, _selection
        selected_apn = _selected_apn(apn_mode, apn)
        if selected_apn is None:
            raise ValueError("zwrt_selected_apn_unavailable")
        selection, manual_plmn = _selection(modem, net)
        if selection is None or selection == "manual" and manual_plmn is None:
            raise ValueError("zwrt_selected_operator_unavailable")
        common = common_reply.get("values")
        if not isinstance(common, dict) or any(not str(common.get(key) or "").strip()
                for key in ("model_name", "wa_inner_version", "hardware_version")):
            raise ValueError("zwrt_fresh_model_or_firmware_unavailable")
        if device.get("wa_inner_version") and device["wa_inner_version"] != common["wa_inner_version"]:
            raise ValueError("zwrt_fresh_firmware_conflict")
        from arch4.modem.catalog import firmware_profile_by_id
        profile = firmware_profile_by_id(modem._definition.profile_id)
        if (profile is None or profile.network_settings != "ng_g5tc_b17_experimental"
                or not profile.network_settings_qualified(str(common["wa_inner_version"]))):
            raise ValueError("zwrt_apn_shape_not_qualified_for_firmware")
        native_identity = {"backend":modem._backend_name, "vendor":"ZTE",
            "model":str(common["model_name"]), "firmware":str(common["wa_inner_version"]),
            "hardware_version":str(common["hardware_version"]),
            "serial":str(serial.get("modem_msn") or ""),
            "imei":str(device.get("modem_imei") or "")}
        from arch4.modem_live.device_admission import require_selected_device_admission
        require_selected_device_admission(backend=modem._backend_name,
            selected_profile_id=modem._definition.profile_id, observed=native_identity,
            associations=(), expected_serial=modem._expected_serial,
            expected_imei=modem._expected_imei)
        modem._apply_target_guard(native_identity)
        key = mode_key_for_native(modem._definition, net.get("net_select"))
        lte = _bands(net.get("lte_band"))
        mask = net.get("lte_band_lock")
        if (type(mask) is not str or re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", mask) is None
                or decode_zwrt_lte_mask(mask) != lte):
            raise ValueError("zwrt_lte_restrictions_conflict")
        nr = _bands(net.get("nr5g_nsa_band_lock"))
        identity = active_sim(sim)
        subscription = subscription_key(identity["imsi"])
        if not subscription or not isinstance(apn.get("apnListArray"), list):
            raise ValueError("zwrt_subscription_or_apn_unavailable")
        preserved = {
            "network": _selected(net, ("net_select_mode", "lock_lte_cell", "lock_nr_cell",
                "gw_band_lock", "nr5g_sa_band_lock", "nr5g_nrdc_band_lock")),
            "operator": {"selection": selection, "manual_plmn": manual_plmn,
                "manual_native": _selected(net, ("mccmnc", "plmn", "rmcc", "rmnc"))
                    if selection == "manual" else None},
            "wwan": _selected(wwan, ("cid", "enable", "connect_mode", "roam_enable")),
            "apn": apn,
            "selected_apn": selected_apn,
            "apn_selection": _selected(apn_mode, ("apn_mode",)),
            "serial": _selected(serial, ("modem_msn",)),
            "sim": _selected(sim, ("current_sim_slot", "sim_iccid", "iccid")),
            "device": _selected(device, ("modem_imei", "wa_version", "wa_inner_version")),
            "identity": native_identity,
        }
        # Empty ZWRT band locks have no established stock AUTO meaning. Do not
        # replace missing restrictions with hardware capabilities or active NR.
        observed = radio_mode_observation(modem._definition, mode_key=key,
            lte_bands=lte, nr_bands=nr, lte_selection_mode="manual",
            nr_selection_mode="manual", subscription_key=subscription,
            preserved_configuration=preserved)
        return {"radio_mode": observed}
    except (TypeError, ValueError, KeyError, RuntimeError, ConnectionError) as exc:
        if required:
            raise RuntimeError("zte_zwrt_radio_mode_observation_unavailable") from exc
        return {"radio_mode": None}


def mode_call(modem, plan):
    if plan.stage_kind != "radio_mode" or plan.ordinal != 1:
        raise ValueError("zte_zwrt_radio_mode_stage_invalid")
    observed = status(modem, "event_write", required=True)["radio_mode"]
    require_radio_mode_configuration(plan.target, observed)
    return {"service":"zte_nwinfo_api", "method":"nwinfo_set_netselect",
        "params":{"net_select":native_mode_for_key(modem._definition, plan.target.mode_key)}}
