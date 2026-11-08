"""Exact G5TC NG net_select choices using the existing authenticated ubus.

B17's configured getter is captured. The setter is public G5TC protocol proof,
not a new assertion that all B17 operators grant writes or restore service.
"""
import re
import time

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


def status(modem, lane, *, required=False, use_session_sim=False):
    if not qualified(modem):
        raise RuntimeError("zte_zwrt_selected_profile_radio_mode_not_qualified")
    try:
        calls = _calls()
        if not use_session_sim:
            return _decode_status(modem, modem._ubus_call(calls, lane=lane), required=required)
        # Normal maintenance keeps the R26.2 single configuration batch.
        # Only immutable, admitted SIM context is omitted; writes/readback
        # continue to use all fresh replies through the default path above.
        session = modem._ubus_session_id
        cached = getattr(modem, '_zwrt_session_sim_info', None)
        if (not isinstance(cached, dict) or not subscription_key(active_sim(cached)['imsi'])
                or active_sim(cached)['imsi'] != modem._last_status.get('imsi')):
            return {'radio_mode': None}
        cached = dict(cached)
        replies = modem._ubus_call([call for index, call in enumerate(calls) if index != 1], lane=lane)
        if (not isinstance(replies, list) or len(replies) != len(calls) - 1
                or modem._ubus_session_id != session
                or getattr(modem, '_zwrt_session_sim_info', None) != cached):
            return {'radio_mode': None}
        return _decode_status(modem, [replies[0], None, *replies[1:]],
                              required=required, session_sim=cached)
    except ConnectionError as exc:
        if required:
            raise RuntimeError("zte_zwrt_radio_mode_observation_unavailable") from exc
        return {"radio_mode": None}


def _calls():
    return [
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


def observations(modem, lane, *, use_session_sim=False):
    """Bounded native RPC groups, with a fresh same-session cohort guard."""
    calls = _calls()
    session = modem._ubus_session_id
    cached_sim = getattr(modem, '_zwrt_session_sim_info', None) if use_session_sim else None
    if use_session_sim and (not isinstance(cached_sim, dict)
            or not subscription_key(active_sim(cached_sim)['imsi'])
            or active_sim(cached_sim)['imsi'] != modem._last_status.get('imsi')):
        yield ({'radio_mode':None}, time.time())
        return
    selected = [(index, call) for index,call in enumerate(calls) if not use_session_sim or index != 1]
    replies = [None] * len(calls)
    acquired_at = time.time()
    for offset in range(0, len(selected), 2):
        group = selected[offset:offset + 2]
        batch = modem._ubus_call([call for index,call in group], lane=lane)
        if not isinstance(batch, list):
            yield ({"radio_mode": None}, acquired_at)
            return
        if len(batch) != len(group):
            yield ({'radio_mode':None}, acquired_at)
            return
        for (index,call),reply in zip(group,batch):
            replies[index] = reply
        yield None
        if modem._ubus_session_id != session:
            yield ({"radio_mode": None}, acquired_at)
            return
    guard_count = 1 if use_session_sim else 2
    guard = modem._ubus_call(calls[:guard_count], lane=lane)
    if (modem._ubus_session_id != session or guard != replies[:guard_count]
            or use_session_sim and cached_sim != getattr(modem, '_zwrt_session_sim_info', None)):
        yield ({"radio_mode": None}, acquired_at)
        return
    yield (_decode_status(modem, replies, session_sim=cached_sim), acquired_at)


def _readable_network_status(modem, replies, session_sim):
    """Independent facts in the existing maintenance cohort, not write proof."""
    from arch4.core_contracts.network_settings import validate_network_observation
    from arch4.modem.zte_ng_network_settings import _selected_apn, _selection, _wwan_flags, _mobile_data_enabled, _qualified
    from arch4.modem_live.profile_definition.native_inputs import native_view
    if session_sim is None or not isinstance(replies, list) or len(replies) != len(_calls()):
        return {}
    def data(index):
        row = replies[index]
        return native_view(row['data']) if (isinstance(row, dict) and row.get('success') is True
            and isinstance(row.get('data'), dict)) else {}
    subscription = subscription_key(active_sim(session_sim)['imsi'])
    if not subscription:
        return {}
    net, wwan, apn_mode = data(0), data(2), data(6)
    selected = data(3) if str(apn_mode.get('apn_mode')) == '1' else data(8)
    try:
        apn = _selected_apn(apn_mode, selected)
    except (TypeError, ValueError):
        apn = None
    if apn is not None and not _qualified(modem):
        apn['editable'] = False  # A readable B22 APN does not qualify B17 writes.
    try:
        selection, plmn = _selection(modem, net)
    except (TypeError, ValueError):
        selection, plmn = None, None
    status = {}
    enabled = _mobile_data_enabled(wwan)
    if enabled is not None:
        status['mobile_data_enabled'] = enabled
    if apn is not None or selection is not None or _wwan_flags(wwan)['roam_enable'] is not None:
        observation = dict(
            schema='NetworkSettingsObservation.v1', subscription_key=subscription,
            selection=selection, target_plmn=plmn,
            roaming_enabled={0:False, 1:True}.get(_wwan_flags(wwan)['roam_enable']), apn=apn)
        try:
            status['network_settings'] = validate_network_observation(observation)
        except (TypeError, ValueError):
            observation['apn'] = None
            status['network_settings'] = validate_network_observation(observation)
    return status


def _decode_status(modem, replies, *, required=False, session_sim=None):
    readable = _readable_network_status(modem, replies, session_sim)
    try:
        _require_mode_semantics(modem._definition)
        # Only the selected APN list is required in the completed native cohort.
        if (not isinstance(replies, list) or len(replies) != len(_calls())
                or any(not isinstance(reply, dict) or reply.get("success") is not True
                       or not isinstance(reply.get("data"), dict)
                       for index, reply in enumerate(replies) if index not in ((1,3,8) if session_sim is not None else (3,8)))):
            raise ValueError("zwrt_native_configuration_unavailable")
        net, sim, wwan, _, device, common_reply, apn_mode, serial, _ = [
            reply.get("data") if isinstance(reply, dict) else None for reply in replies]
        if session_sim is not None:
            sim = session_sim  # Explicit session context, not a fresh SIM reply.
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
        return {**readable, "radio_mode": observed}
    except (TypeError, ValueError, KeyError, RuntimeError, ConnectionError) as exc:
        if required:
            raise RuntimeError("zte_zwrt_radio_mode_observation_unavailable") from exc
        return {**readable, "radio_mode": None}


def mode_call(modem, plan):
    if plan.stage_kind != "radio_mode" or plan.ordinal != 1:
        raise ValueError("zte_zwrt_radio_mode_stage_invalid")
    observed = status(modem, "event_write", required=True)["radio_mode"]
    require_radio_mode_configuration(plan.target, observed)
    return {"service":"zte_nwinfo_api", "method":"nwinfo_set_netselect",
        "params":{"net_select":native_mode_for_key(modem._definition, plan.target.mode_key)}}
