"""Conditional stock MiWiFi configured preference; no rooted API or AT path."""
from arch4.core_contracts.network_settings import subscription_key
from arch4.modem_live.radio_mode_configuration import (
    profile_radio_modes, mode_key_for_native, native_mode_for_key,
    radio_mode_observation, require_radio_mode_configuration,
)

NATIVE_MODES = {"lte_only": "4g", "mixed_auto": "auto", "mixed_nsa": "nsa", "sa_only": "sa"}


def qualified(modem):
    document = modem._definition.snapshot()
    return (document.get("parameters", {}).get("radio_mode_contract") == "stock_miwifi_mode_get_v1"
        and document["profile"]["capabilities"].get("supports_radio_mode") is True
        and profile_radio_modes(modem._definition) == NATIVE_MODES)


def iter_configuration(modem, lane):
    if not qualified(modem):
        raise RuntimeError("xiaomi_selected_profile_radio_mode_unavailable")
    from arch4.modem.xiaomi_miwifi import sim_identity
    replies = {}
    for endpoint in ("xqmobile/get_mobile_net_cfg", "xqdtcustom/get_sim_info",
                     "xqmobile/get_apn_info", "xqmobile/get_search_net_mode"):
        replies[endpoint] = modem._read(endpoint, lane)
        yield None
    bands = modem._bands(lane)
    yield None
    cfg = replies["xqmobile/get_mobile_net_cfg"]
    if any(type(cfg.get(name)) is not int or cfg[name] not in (0, 1)
           for name in ("networkdata", "networkroam", "networkauto")):
        raise RuntimeError("xiaomi_radio_mode_preserved_settings_unavailable")
    apn = replies["xqmobile/get_apn_info"]
    rows = apn.get("apnlist")
    if not isinstance(rows, list):
        raise RuntimeError("xiaomi_radio_mode_apn_unavailable")
    selected = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(apn.get("curid"))]
    if len(selected) != 1:
        raise RuntimeError("xiaomi_radio_mode_apn_unavailable")
    operator = replies["xqmobile/get_search_net_mode"]
    if type(operator.get("mode")) is not int or operator["mode"] not in (0, 1):
        raise RuntimeError("xiaomi_radio_mode_operator_unavailable")
    observed = radio_mode_observation(modem._definition,
        mode_key=mode_key_for_native(modem._definition, cfg.get("networktype")),
        lte_bands=bands["selected_band_set"], nr_bands=bands["nr5g_band_set"],
        lte_selection_mode="manual", nr_selection_mode="manual",
        subscription_key=subscription_key(sim_identity(replies["xqdtcustom/get_sim_info"])),
        preserved_configuration={"data": cfg["networkdata"], "roaming": cfg["networkroam"],
            "networkauto": cfg["networkauto"], "apn": selected[0], "apn_id": apn["curid"],
            "operator": operator, "device": modem._identity})
    yield observed, cfg


def iter_status(modem, lane):
    for item in iter_configuration(modem, lane):
        yield None if item is None else {"radio_mode": item[0]}


def status(modem, lane, *, required=False):
    try:
        return next(item for item in iter_status(modem, lane) if item is not None)
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        if required:
            raise RuntimeError("xiaomi_radio_mode_observation_unavailable") from exc
        return {"radio_mode": None}


def mode_fields(modem, plan):
    if plan.stage_kind != "radio_mode" or plan.ordinal != 1:
        raise ValueError("xiaomi_radio_mode_stage_invalid")
    observation, cfg = next(item for item in iter_configuration(modem, "event_write") if item is not None)
    require_radio_mode_configuration(plan.target, observation)
    fields = {"networktype": native_mode_for_key(modem._definition, plan.target.mode_key),
              "networkdata": str(cfg["networkdata"]), "networkroam": str(cfg["networkroam"])}
    endpoint, encoded = modem._definition.prepare_radio_mode_write(fields)
    if endpoint != "xqmobile/set_mobile_net_cfg" or encoded != fields:
        raise RuntimeError("xiaomi_radio_mode_native_template_changed_target")
    return encoded
