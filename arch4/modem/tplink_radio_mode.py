"""NX520v configured preference preservation on its admitted DEV2 dialect."""
import re

from arch4.modem_live.radio_mode_configuration import (
    mode_key_for_native, radio_mode_observation,
)
from arch4.core_contracts.network_settings import subscription_key


def manual_restrictions(selection):
    """Keep every configured RAT restriction; never silently discard a token."""
    native = selection.get("selectedBand")
    supported = selection.get("supportBandList")
    if not isinstance(native, str) or not isinstance(supported, str):
        raise ValueError("tplink_radio_mode_band_restrictions_unavailable")
    terms = native.split(";")
    if (len(native) > 8192 or len(terms) > 1024 or len(terms) != len(set(terms))
            or any(re.fullmatch(r"[WBN][1-9][0-9]{0,3}", term) is None
                   or int(term[1:]) > 1024 for term in terms)
            or not set(terms) <= set(supported.split(";"))):
        raise ValueError("tplink_radio_mode_band_restrictions_unavailable")
    return terms


def configured_status(modem, wan, selection, sim, lane):
    from arch4.modem.tplink_dev2 import bands
    modes = modem._definition.snapshot()["parameters"]["radio_modes"]
    if (modes.get('lte_only') != '4G Only' or modes.get('nsa_preferred') != '5G Preferred'
            or any(value not in {'5G Preferred', '5G NSA/4G', '5G SA Only'}
                   for key, value in modes.items() if key != 'lte_only')):
        raise ValueError('tplink_radio_mode_native_codec_invalid')
    options = str(wan.get("networkPreferredModeOptionList", "")).split(",")
    options = [value.strip() for value in options]
    native = wan.get("networkPreferredModeSelected")
    if (len(options) > 16 or len(set(options)) != len(options)
            or modes["lte_only"] not in options or native not in options):
        raise ValueError("tplink_radio_mode_native_options_unavailable")
    key = mode_key_for_native(modem._definition, native)
    kind = {"1": "auto", "2": "manual"}.get(str(selection.get("selectedMode")))
    if kind is None:
        raise ValueError("tplink_radio_mode_band_selection_unavailable")
    # Explicit native AUTO has no manual mask. Hardware supported bands remain
    # device specification, never a substitute for selected restrictions.
    terms = manual_restrictions(selection) if kind == "manual" else []
    restrictions = ";".join(terms)
    network = modem._read("DEV2_LTE_NETWORK_SEARCH", lane=lane)
    required = {"apn", "activeProfile", "profileID", "pdpType", "roamingEnabled", "dataSwitchStatusBackup"}
    if not required <= set(wan) or not {"selectedMode", "selectedOperator"} <= set(network):
        raise ValueError("tplink_radio_mode_preserved_settings_unavailable")
    preserved = {"wan": {name: wan[name] for name in (
        "apn", "apnType", "profileID", "activeProfile", "pdpType", "profileName",
        "username", "password", "authType", "roamingEnabled", "dataSwitchStatusBackup",
        "NATEnabled", "xlatMode", "antennaType", "imsSwitch", "nsaPrefer") if name in wan},
        "operator": {name: network[name] for name in ("selectedMode", "selectedOperator")},
        "umts_bands": sorted(int(term[1:]) for term in terms if term.startswith("W"))}
    return radio_mode_observation(modem._definition, mode_key=key,
        lte_bands=bands(restrictions, "B"), nr_bands=bands(restrictions, "N"),
        lte_selection_mode=kind, nr_selection_mode=kind,
        subscription_key=subscription_key(str(sim.get("IMSI", ""))),
        preserved_configuration=preserved)
