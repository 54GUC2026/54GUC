"""Foundation's APN library operations; called on the external-action worker.

Saving records an operator choice, not modem truth. Only Command Authority can
apply it or confirm it. No password leaves this boundary except as DPAPI data.
"""
from collections.abc import Mapping
from dataclasses import replace

from arch4.core_contracts.network_settings import CoreApnSettings
from arch4.customer_contracts.apn_profiles import SavedApnProfile, apn_profile_id
from arch4.customer_contracts.settings import FoundationOperatorSettings
from arch4.protected_credentials import open_apn_credentials, seal_apn_credentials


def public_network_choices() -> list[dict[str, str]]:
    """Project the existing operator catalogue without reading saved settings."""
    from arch4.operator_profiles import available_operator_contexts
    networks = {}
    for context in available_operator_contexts().values():
        networks.update(context.provider_plmn_map)
    return [{"plmn": code, "name": name} for code, name in sorted(networks.items())]


def public_apn_library(settings: FoundationOperatorSettings) -> dict:
    return {"schema": "ApnProfileLibrary.v1", "state": "ready", "error": "",
            "networks": public_network_choices(),
            "profiles": [profile.public_mapping() for _, profile in sorted(settings.apn_profiles.items())]}


def save_apn_choice(store, payload: Mapping) -> dict:
    if not isinstance(payload, Mapping) or set(payload) != {"subscription_key", "network_plmn", "saved_profile_id", "apn"}:
        raise ValueError("apn_profile_choice_exact_fields_required")
    apn = CoreApnSettings.from_mapping(payload["apn"])
    key = apn_profile_id(payload["subscription_key"], payload["network_plmn"], apn.name)
    current = store.load()
    saved_id = payload["saved_profile_id"]
    if not isinstance(saved_id, str):
        raise ValueError("apn_profile_reference_invalid")
    if saved_id:
        saved = current.apn_profiles.get(saved_id)
        if (saved is None or saved.subscription_key != payload["subscription_key"]
                or saved.network_plmn != payload["network_plmn"]):
            raise ValueError("apn_profile_scope_changed")
        if not saved.password_known:
            raise ValueError("apn_profile_password_not_saved")
        _, password = open_apn_credentials(saved.protected_credentials)
        if password is None:
            raise ValueError("apn_profile_password_not_saved")
        apn = replace(apn, protected_credentials=seal_apn_credentials(apn.username, password))
    username, password = open_apn_credentials(apn.protected_credentials)
    if username != apn.username:
        raise ValueError("apn_profile_credentials_mismatch")
    profile = SavedApnProfile(key, payload["subscription_key"], payload["network_plmn"],
                              apn.name, apn.apn, apn.username, apn.authentication,
                              apn.ip_protocol, apn.protected_credentials, password is not None)
    profiles = dict(current.apn_profiles)
    profiles[key] = profile
    updated = FoundationOperatorSettings(current.operator_fields, current.traffic_plans, profiles,
                                        current.unassigned_traffic_plans)
    store.save(updated)
    return {"library": public_apn_library(updated), "apn": apn.to_mapping(), "profile_id": key}
