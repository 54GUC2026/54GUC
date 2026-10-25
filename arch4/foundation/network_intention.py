"""Validate declared selection against the existing offline country reference.

This is prerequisite validation, not command admission or an enforcement verdict.
It owns no modem state, I/O, operator aliases or country dataset.
"""
import re

from arch4.customer_contracts.network_intention import PhysicalSimNetworkIntention
from arch4.foundation.network_country_reference import MCC_NAMES, country_verdict, name_token


def resolve_network_choice(text: str, networks, *, country: str = "") -> tuple[str, str, str]:
    """Resolve explicit user input, never infer serving state or modem permission.

    Return code, display label and actionable input error. An explicit numeric
    code remains usable outside our catalogue; names must resolve uniquely.
    """
    text = text.strip()
    if not text:
        return "", "", ""
    catalogue = {}
    for row in networks:
        code = str(row.get("plmn", ""))
        label = str(row.get("name", "")).strip()
        if re.fullmatch(r"[0-9]{5,6}", code):
            catalogue.setdefault(code, set()).add(label)
    numeric = re.fullmatch(r"[0-9]{5,6}", text)
    labelled = re.fullmatch(r"(.+?)\s*\(([0-9]{5,6})\)", text)
    if numeric or labelled:
        code = text if numeric else labelled.group(2)
        label = "" if numeric else labelled.group(1).strip()
        names = {name for name in catalogue.get(code, ()) if name}
        if label:
            matching = {c for c, labels in catalogue.items() if name_token(label) in {name_token(n) for n in labels if n}}
            if matching and code not in matching:
                actual = ", ".join(sorted(names)) or "a different provider"
                return "", "", f"You entered {label}, but network code {code} is assigned to {actual}. Choose the intended provider from the list."
        canonical = sorted(names)[0] if names else label
        if country and country_verdict(country, code)[0] == "mismatch":
            actual = country_verdict("", code)[1]
            return code, f"{canonical} ({code})" if canonical else code, f"You selected {country}, but network code {code} belongs to {actual}. Choose a network in {country}, or correct the country."
        return code, f"{canonical} ({code})" if canonical else code, ""
    matches = {code for code, labels in catalogue.items()
               if name_token(text) in {name_token(n) for n in labels if n}
               and (not country or country_verdict(country, code)[0] == "confirmed")}
    if len(matches) == 1:
        code = matches.pop()
        label = sorted(n for n in catalogue[code] if name_token(n) == name_token(text))[0]
        return code, f"{label} ({code})", ""
    if len(matches) > 1:
        return "", "", "More than one network matches. Select its country and choose the network from the list."
    return "", "", "Choose a network from the list, or enter its 5–6 digit network code."


def validate_network_intention(intention: PhysicalSimNetworkIntention) -> None:
    if not isinstance(intention, PhysicalSimNetworkIntention):
        raise TypeError("network_intention_typed_contract_required")
    known = {name_token(name) for names in MCC_NAMES.values() for name in names} | set(MCC_NAMES)
    for country in (intention.sim_country, intention.roaming_country):
        if country and name_token(country) not in known:
            raise ValueError("network_intention_country_unverified")
    country = intention.roaming_country if intention.roaming_enabled else intention.sim_country
    if intention.selection == "manual" and country:
        verdict, _ = country_verdict(country, intention.target_plmn)
        if verdict != "confirmed":
            raise ValueError(f"network_intention_target_country_{verdict}")
    # AUTO authorizes no particular PLMN here. It is not a location/geofence
    # guarantee; only a qualified modem operation could enforce such a limit.
