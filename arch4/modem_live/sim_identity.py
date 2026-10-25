"""Normalize SIM identity once at Modem Live; no modem reads or GUI inference."""
import re

from arch4.core_contracts.network_settings import subscription_key
from arch4.operator_profiles import resolve_provider_name

# Explicit national allocations, not a universal five-digit IMSI assumption.
# The configured MCC uses two-digit MNCs (00..99) under ITU OB 983 (2011).
# Montenegro: ITU OB 1002 (2012), MCC 297 / MNC 01, 02, 03.
_TWO_DIGIT_MCC = frozenset({"220"})
_TWO_DIGIT_PREFIX = frozenset({"29701", "29702", "29703"})


def sim_network_identity(imsi: str, *, mnc_length: int | None = None) -> dict:
    key = subscription_key(imsi)
    mcc = imsi[:3] if key else ""
    plmn = ""
    if key:
        if type(mnc_length) is int and mnc_length in {2, 3}:
            plmn = imsi[:3 + mnc_length]
        elif mcc in _TWO_DIGIT_MCC or imsi[:5] in _TWO_DIGIT_PREFIX:
            plmn = imsi[:5]
    name = resolve_provider_name(plmn) if plmn else ""
    return {"schema": "SimNetworkIdentity.v1", "subscription_key": key,
            "mcc": mcc, "home_plmn": plmn,
            "home_provider": "" if name == "Unknown" else name}


def validate_sim_network_identity(value):
    fields = {"schema", "subscription_key", "mcc", "home_plmn", "home_provider"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "SimNetworkIdentity.v1":
        raise ValueError("sim_network_identity_contract")
    if not all(isinstance(value[key], str) for key in fields):
        raise ValueError("sim_network_identity_text")
    if value["subscription_key"] and not re.fullmatch(r"[a-f0-9]{64}", value["subscription_key"]):
        raise ValueError("sim_network_identity_subscription")
    if value["mcc"] and not re.fullmatch(r"[0-9]{3}", value["mcc"]):
        raise ValueError("sim_network_identity_mcc")
    if value["home_plmn"] and not re.fullmatch(r"[0-9]{5,6}", value["home_plmn"]):
        raise ValueError("sim_network_identity_plmn")
    if (not value["subscription_key"] and any(value[key] for key in ("mcc", "home_plmn", "home_provider"))
            or value["home_plmn"] and (not value["mcc"] or not value["home_plmn"].startswith(value["mcc"]))
            or value["home_provider"] and not value["home_plmn"]):
        raise ValueError("sim_network_identity_inconsistent")
