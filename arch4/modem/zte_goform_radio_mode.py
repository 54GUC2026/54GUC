"""Stock GoForm configured-mode contract; no nested writes or retries.

The immutable selected profile declares the finite dialect. The router's own
AUTO_MODES must independently contain both the saved choice and Only_LTE.
Active LTE/ENDC and the hardware band catalogue never select a preference.
"""
from __future__ import annotations

import re
import time
import uuid
from contextlib import nullcontext

import requests

from arch4.core_contracts.network_settings import subscription_key
from arch4.modem.backend_acquisition import (
    BackendAcquisitionRequest, capture_backend_acquisition,
)
from arch4.modem.zte_parsers import CONNECTED_PPP_STATES, ZTE_BAND_MASKS
from arch4.modem_live.radio_mode_configuration import (
    mode_key_for_native, native_mode_for_key, profile_radio_modes,
    radio_mode_observation, require_radio_mode_configuration,
)


CONTRACT = "stock_goform_configured_preference_v1"
MODE_CHOICES = {"lte_only": "Only_LTE", "network_auto": "NETWORK_auto",
    "lte_and_5g": "LTE_AND_5G", "wl_and_5g": "WL_AND_5G", "four_g_and_5g": "4G_AND_5G"}
MODE_READ_FIELDS = (
    "loginfo", "net_select", "net_select_mode", "m_netselect_save",
    "wan_apn", "lte_band_lock", "nr5g_nsa_band_lock", "nr5g_sa_band_lock",
    "imsi", "sim_imsi", "ppp_status", "model_name", "product_name", "wa_inner_version",
)


def contract_enabled(modem):
    definition = getattr(modem, "_definition", None)
    if definition is None:
        return False
    document = definition.snapshot()
    metadata = document["profile"]
    modes = document.get("parameters", {}).get("radio_modes")
    return (metadata["family"] == "zte_webui_like" and metadata["device_radio_class"] == "5g"
        and metadata["capabilities"].get("supports_radio_mode") is True
        and document.get("parameters", {}).get("goform_radio_mode_contract") == CONTRACT
        and isinstance(modes, dict) and len(modes) >= 2
        and modes.get("lte_only") == "Only_LTE"
        and all(MODE_CHOICES.get(key) == value for key, value in modes.items()))


def native_mode_options(source):
    """Read only a bounded literal option list; never evaluate firmware JS."""
    if type(source) is not str or len(source) > 2 * 1024 * 1024:
        return frozenset()
    arrays = re.findall(r"\bAUTO_MODES['\"]?\s*[:=]\s*\[([^\[\]]*)\]", source)
    if len(arrays) != 1:
        return frozenset()
    values = re.findall(r"\bvalue['\"]?\s*:\s*['\"]([A-Za-z0-9][A-Za-z0-9_/-]{0,63})['\"]", arrays[0])
    if not 2 <= len(values) <= 32 or len(values) != len(set(values)):
        return frozenset()
    return frozenset(values)


def read_native_mode_options(modem):
    if not contract_enabled(modem):
        return frozenset()
    try:
        root = modem._zte_http_get(modem._base_url + "/js/config/config.js",
            operation="radio_mode_config_root", lane="boot_static",
            headers=modem._goform_headers(), timeout=modem._maintenance_read_timeout)
        if root.status_code != 200:
            return frozenset()
        options = native_mode_options(root.text)
        if not options:
            path = modem._mf296r_device_config_path(root.text)
            if not path:
                return frozenset()
            response = modem._zte_http_get(modem._base_url + path,
                operation="radio_mode_device_config", lane="boot_static",
                headers=modem._goform_headers(), timeout=modem._maintenance_read_timeout)
            if response.status_code != 200:
                return frozenset()
            options = native_mode_options(response.text)
        admitted = frozenset(profile_radio_modes(modem._definition).values())
        return options & admitted if "Only_LTE" in options else frozenset()
    except (requests.RequestException, TypeError, ValueError, RuntimeError):
        return frozenset()


def _text(raw, name, *, empty=False):
    value = raw.get(name)
    if type(value) is not str or value != value.strip() or len(value) > 4096 or (not empty and not value):
        raise RuntimeError("zte_goform_radio_mode_configuration_unavailable")
    return value


def _lte_restriction(value):
    if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[1-9][0-9]*)", value):
        raise RuntimeError("zte_goform_radio_mode_lte_restriction_unavailable")
    mask = int(value, 16 if value.lower().startswith("0x") else 10)
    known = sum(ZTE_BAND_MASKS.values())
    if not mask or mask & ~known:
        raise RuntimeError("zte_goform_radio_mode_lte_restriction_unavailable")
    return [band for band, bit in sorted(ZTE_BAND_MASKS.items()) if mask & bit]


def _nr_restriction(value):
    if value == "":
        return []
    if not re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", value):
        raise RuntimeError("zte_goform_radio_mode_nr_restriction_unavailable")
    bands = sorted(set(map(int, value.split(","))))
    if any(band > 1024 for band in bands):
        raise RuntimeError("zte_goform_radio_mode_nr_restriction_unavailable")
    return bands


def read_configuration(modem, *, lane=None):
    if not contract_enabled(modem) or not modem._connected or not modem._authenticated:
        raise RuntimeError("zte_goform_selected_profile_radio_mode_unavailable")
    options = getattr(modem, "_goform_radio_mode_options", frozenset())
    if "Only_LTE" not in options or len(options) < 2:
        raise RuntimeError("zte_goform_firmware_radio_mode_options_unavailable")
    # One physical reply contains mode, both masks, SIM and preserved settings.
    # A partial response never inherits any of these from _last_raw/_last_status.
    with (modem.physical_request_lane_scope(lane) if lane is not None else nullcontext()):
        raw = modem._read_goform(MODE_READ_FIELDS, timeout_s=modem._maintenance_read_timeout)
    if _text(raw, "loginfo") != "ok":
        raise RuntimeError("zte_goform_radio_mode_authentication_unavailable")
    model = raw.get("model_name") or raw.get("product_name")
    metadata = modem._definition.metadata
    firmware = _text(raw, "wa_inner_version")
    if (type(model) is not str or not any(token.casefold() in model.casefold() for token in metadata["model_contains"])
            or metadata.get("firmware_exact") and firmware != metadata["firmware_exact"]
            or not all(token.casefold() in firmware.casefold() for token in metadata["firmware_contains"])):
        raise RuntimeError("zte_goform_radio_mode_profile_identity_changed")
    native = _text(raw, "net_select")
    if native not in options:
        raise RuntimeError("zte_goform_radio_mode_option_not_admitted")
    key = mode_key_for_native(modem._definition, native)
    selection = _text(raw, "net_select_mode")
    saved_selection = _text(raw, "m_netselect_save", empty=True)
    # This native operation can reset a manual operator choice. Do not silently
    # register to a different provider; that configuration remains user-managed.
    if selection != "auto_select" or saved_selection not in {"", "auto_select"}:
        raise RuntimeError("zte_goform_radio_mode_manual_operator_preservation_unqualified")
    sims = set()
    for name in ("imsi", "sim_imsi"):
        value = raw.get(name)
        if value in (None, ""):
            continue
        if type(value) is not str or not subscription_key(value):
            raise RuntimeError("zte_goform_radio_mode_subscription_unavailable")
        sims.add(value)
    if len(sims) != 1:
        raise RuntimeError("zte_goform_radio_mode_subscription_unavailable")
    sim = subscription_key(next(iter(sims)))
    if not sim:
        raise RuntimeError("zte_goform_radio_mode_subscription_unavailable")
    observed = radio_mode_observation(modem._definition, mode_key=key,
        lte_bands=_lte_restriction(_text(raw, "lte_band_lock")),
        nr_bands=_nr_restriction(_text(raw, "nr5g_nsa_band_lock", empty=True)),
        lte_selection_mode="manual", nr_selection_mode="manual", subscription_key=sim,
        preserved_configuration={"net_select_mode": selection, "m_netselect_save": saved_selection,
            "wan_apn": _text(raw, "wan_apn", empty=True),
            "nr5g_sa_band_lock": _text(raw, "nr5g_sa_band_lock", empty=True)})
    return observed, _text(raw, "ppp_status")


def mode_status(modem, *, required=False):
    try:
        observation, _ = read_configuration(modem)
    except (TypeError, ValueError, RuntimeError, requests.RequestException):
        if required:
            raise
        observation = None
    return {"radio_mode": observation}


def mode_payload(modem, plan):
    if plan.stage_kind not in {"radio_disconnect", "radio_mode", "radio_reconnect"}:
        raise RuntimeError("zte_goform_radio_mode_stage_invalid")
    if plan.ordinal not in {"radio_disconnect": {1}, "radio_mode": {1, 2}, "radio_reconnect": {3}}[plan.stage_kind]:
        raise RuntimeError("zte_goform_radio_mode_stage_ordinal_invalid")
    observation, ppp = read_configuration(modem)
    if plan.stage_kind == "radio_reconnect":
        if (getattr(plan, "preceding_disconnect", None) is None
                or getattr(plan, "preceding_radio_mode", None) is None
                or observation != plan.target.configuration()):
            raise RuntimeError("zte_goform_radio_mode_reconnect_proof_required")
    else:
        require_radio_mode_configuration(plan.target, observation)
    if plan.stage_kind == "radio_mode" and plan.ordinal != 1 and getattr(plan, "preceding_disconnect", None) is None:
        raise RuntimeError("zte_goform_radio_mode_disconnect_proof_required")
    native = native_mode_for_key(modem._definition, plan.target.mode_key)
    if native not in modem._goform_radio_mode_options:
        raise RuntimeError("zte_goform_radio_mode_option_not_admitted")
    if plan.stage_kind == "radio_disconnect":
        if ppp not in CONNECTED_PPP_STATES:
            raise RuntimeError("zte_goform_radio_mode_original_data_state_changed")
        return {"isTest": "false", "goformId": "DISCONNECT_NETWORK", "notCallback": "true"}
    if ppp != "ppp_disconnected":
        raise RuntimeError("zte_goform_radio_mode_requires_confirmed_data_disconnect")
    if plan.stage_kind == "radio_reconnect":
        return {"isTest": "false", "goformId": "CONNECT_NETWORK", "notCallback": "true"}
    return {"isTest": "false", "goformId": "SET_BEARER_PREFERENCE", "BearerPreference": native}


def prepare_mode_stages(modem, target):
    """Return only a fresh PPP precondition; the existing planner owns stages."""
    observation, ppp = read_configuration(modem)
    require_radio_mode_configuration(target, observation)
    if native_mode_for_key(modem._definition, target.mode_key) not in modem._goform_radio_mode_options:
        raise RuntimeError("zte_goform_radio_mode_option_not_admitted")
    if ppp == "ppp_disconnected":
        return False
    if ppp in CONNECTED_PPP_STATES:
        return True
    raise RuntimeError("zte_goform_radio_mode_original_data_state_unavailable")


def stage_ready(modem, plan):
    """One fresh readiness observation; the transaction owner owns waiting."""
    if (plan.stage_kind, plan.ordinal) not in {("radio_mode", 2), ("radio_reconnect", 3)}:
        raise RuntimeError("zte_goform_radio_mode_readiness_stage_invalid")
    request = BackendAcquisitionRequest(
        attempt_id="goform-data-session:" + uuid.uuid4().hex,
        lane="post_write", started_at=float(time.time()))
    observation, ppp = read_configuration(modem, lane="readback")
    if native_mode_for_key(modem._definition, plan.target.mode_key) not in modem._goform_radio_mode_options:
        raise RuntimeError("zte_goform_radio_mode_option_not_admitted")
    expected = plan.target.configuration(expected=True)
    if plan.stage_kind == "radio_mode":
        require_radio_mode_configuration(plan.target, observation)
    elif observation not in (expected, plan.target.configuration()):
        raise RuntimeError("radio_mode_original_configuration_changed")
    if (plan.stage_kind == "radio_reconnect" and observation == plan.target.configuration()
            and ppp in CONNECTED_PPP_STATES):
        if (plan.preceding_disconnect is None or plan.preceding_radio_mode is None):
            raise RuntimeError("zte_goform_radio_mode_reconnect_proof_required")
        # The firmware restored the original connected data session itself.
        # Carry this one fresh cohort; it is not a CONNECT write or its ACK.
        return capture_backend_acquisition(request=request,
            backend=modem._backend_name, profile_id=modem._definition.profile_id,
            modem=None, status={"radio_mode": observation, "data_session_connected": True},
            acquired_cells=frozenset({"slow.radio_mode"}))
    if ppp != "ppp_disconnected":
        if ppp in CONNECTED_PPP_STATES or ppp in {"ppp_disconnecting", "ppp_connecting"}:
            return False
        raise RuntimeError("zte_goform_radio_mode_original_data_state_unavailable")
    return plan.stage_kind == "radio_mode" or observation == plan.target.configuration()
