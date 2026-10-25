"""Bind a disconnected operator draft to newly observed SIM/APN settings."""
from collections.abc import Mapping
import re

from arch4.customer_contracts.network_intention import PhysicalSimNetworkIntention
from arch4.foundation.network_intention import validate_network_intention
from arch4.core_contracts.network_settings import CoreApnSettings, CoreNetworkSettingsTarget, validate_network_observation


NETWORK_INPUT_MESSAGES = {
    "network_intention_explicit_selection_required": "Network selection is missing. Choose AUTO, or choose Specified and enter a network name or 5–6 digit code.",
    "network_intention_sim_country_required": "Select the SIM country.",
    "network_intention_roaming_country_required": "Select the roaming country.",
    "network_intention_country_unverified": "Select a country from the list.",
    "network_intention_exact_plmn_required": "Choose a network from the list, enter its name with the correct country, or enter a 5–6 digit network code.",
    "network_intention_target_country_mismatch": "The network code does not match the selected roaming country.",
    "network_intention_target_country_not_verified": "The network code could not be matched to the selected country.",
    "network_subscription_changed_reopen_editor": "The SIM changed. Disconnect and select or enter an APN for the active SIM.",
    "network_settings_current_subscription_required": "The active SIM could not be identified. Disconnect and reconnect to retry.",
    "network_settings_change_required": "No settings change is selected.",
    "network_current_apn_not_editable": "This modem has not provided an editable current APN.",
    "network_settings_profile_id_required": "An editable current APN profile is required.",
    "network_settings_name_required": "Enter an APN profile name.",
    "network_settings_name_invalid": "Use an APN profile name of at most 128 characters without control characters.",
    "network_settings_apn_required": "Enter the APN value.",
    "network_settings_apn_invalid": "Use letters, numbers, dots or hyphens for the APN value.",
    "network_settings_username_invalid": "Use an APN username of at most 128 characters without control characters.",
    "network_settings_authentication_invalid": "Select the APN authentication type.",
    "network_settings_ip_protocol_invalid": "Select the APN IP protocol.",
    "network_settings_exact_plmn_required": "Select a network name and country, or enter its 5–6 digit code, before changing an APN profile.",
    "apn_profile_network_required": "Select or enter a 5–6 digit network code before saving this APN profile.",
    "apn_profile_name_invalid": "Enter a profile name other than NEW, up to 128 characters.",
    "apn_profile_password_not_saved": "This profile has no saved password. Enter its password before connecting.",
    "apn_explicit_password_required": "Enter the APN password before connecting this G5TC.",
    "apn_profile_scope_changed": "This APN profile belongs to a different SIM or network. Select it again.",
    "apn_profile_library_full": "The APN profile library is full (256 profiles). Reuse an existing name.",
}

def validate_connect_network_draft_shape(draft: Mapping) -> None:
    fields = {"schema", "intention", "subscription_key", "apn", "network_plmn", "saved_profile_id"}
    if not isinstance(draft, Mapping) or set(draft) != fields or draft["schema"] != "NetworkConnectDraft.v1":
        raise ValueError("network_connect_exact_draft_required")


def validate_connect_network_draft(draft: Mapping) -> None:
    validate_connect_network_draft_shape(draft)
    validate_network_draft({key: draft[key] for key in ("intention", "subscription_key", "apn")})
    if draft["subscription_key"] and not re.fullmatch(r"[a-f0-9]{64}", draft["subscription_key"]):
        raise ValueError("network_settings_subscription_binding_invalid")
    if not isinstance(draft["saved_profile_id"], str):
        raise ValueError("apn_profile_reference_invalid")
    if draft["apn"] is not None and (not isinstance(draft["network_plmn"], str)
            or not re.fullmatch(r"[0-9]{5,6}", draft["network_plmn"])):
        raise ValueError("network_settings_exact_plmn_required")
    if draft["saved_profile_id"] and not draft["subscription_key"]:
        raise ValueError("apn_profile_scope_changed")


def validate_connect_network_draft_for_requirements(
    draft: Mapping, *, apn_write_requires_explicit_password: bool
) -> None:
    """Validate declared APN-write requirements before starting a router session."""
    validate_connect_network_draft(draft)
    if not apn_write_requires_explicit_password or draft["apn"] is None or draft["saved_profile_id"]:
        return
    from arch4.protected_credentials import open_apn_credentials

    _username, password = open_apn_credentials(draft["apn"]["protected_credentials"])
    if password is None:
        raise ValueError("apn_explicit_password_required")


def connect_network_input_message(error: str, draft) -> str:
    """Explain the actual entered values using the sole country/network reference."""
    message = NETWORK_INPUT_MESSAGES.get(error)
    if message is None:
        raise ValueError(error)
    intention = draft.get("intention") or {}
    country = intention.get("roaming_country") if intention.get("roaming_enabled") else intention.get("sim_country")
    code = intention.get("target_plmn") or ""
    if error == "network_intention_target_country_mismatch":
        from arch4.foundation.network_country_reference import country_verdict
        actual = country_verdict("", code)[1]
        return f"You selected {country}, but network code {code} belongs to {actual}. Choose a network in {country}, or correct the country."
    if error == "network_intention_exact_plmn_required" and code:
        from arch4.foundation.apn_profiles import public_network_choices
        from arch4.foundation.network_intention import resolve_network_choice
        _code, _label, explanation = resolve_network_choice(code, public_network_choices(), country=country or "")
        if explanation:
            return explanation
    return message


def admit_connect_network_draft(draft, *, writable: bool, observe_only: bool,
                                apn_write_requires_explicit_password: bool):
    """One explicit Connect decision: apply the entire valid choice or send none.

    Normal device login is independent of optional settings. Known input errors
    explain the no-write decision; malformed contracts and internal faults escape.
    """
    if draft is None:
        return None, ""
    validate_connect_network_draft_shape(draft)
    if not writable:
        return None, "SIM, network and APN changes are not available for this device. Use the router's own settings."
    if observe_only:
        return None, "Observe mode cannot apply SIM, network or APN changes. Select Live mode if you want to change them."
    try:
        validate_connect_network_draft_for_requirements(
            draft, apn_write_requires_explicit_password=apn_write_requires_explicit_password)
    except ValueError as exc:
        message = connect_network_input_message(str(exc), draft)
        return None, (message + " Your requested changes were not applied. "
                      "Connecting with the router's existing settings. "
                      "To change them, click Disconnect, correct the fields, then click Connect.")
    return draft, ""


def bind_connect_network_draft(draft: Mapping, observation: Mapping) -> tuple[dict, dict | None]:
    """No cached native profile ID or another SIM's saved password crosses Connect."""
    validate_connect_network_draft(draft)
    observed = validate_network_observation(observation)
    if not observed["subscription_key"]:
        raise ValueError("network_settings_current_subscription_required")
    apn = draft["apn"]
    profile = None
    if apn is not None:
        if draft["subscription_key"] and draft["subscription_key"] != observed["subscription_key"]:
            raise ValueError("network_subscription_changed_reopen_editor")
        # This binds an operator choice for saving. @active is explicitly
        # unbound and can never pass the command binding check below.
        native = observed["apn"] or {}
        apn = {**apn, "profile_id": "@new" if apn["profile_id"] == "@new"
               else native.get("profile_id") or "@active"}
        profile = {"subscription_key": observed["subscription_key"], "network_plmn": draft["network_plmn"],
                   "saved_profile_id": draft["saved_profile_id"], "apn": apn}
    bound = {"intention": draft["intention"], "subscription_key": observed["subscription_key"], "apn": apn}
    network_choice_from_draft(bound, observed)
    return bound, profile


def validate_network_draft(draft: Mapping) -> PhysicalSimNetworkIntention:
    if not isinstance(draft, Mapping) or set(draft) != {"intention", "subscription_key", "apn"}:
        raise ValueError("network_settings_exact_draft_required")
    intention = PhysicalSimNetworkIntention.from_payload(draft["intention"])
    validate_network_intention(intention)
    if not isinstance(draft["subscription_key"], str):
        raise ValueError("network_settings_subscription_binding_invalid")
    if draft["apn"] is not None:
        CoreApnSettings.from_mapping(draft["apn"])
    return intention


def network_choice_from_draft(draft: Mapping, observation: Mapping) -> CoreNetworkSettingsTarget:
    """Validate a SIM-bound operator choice for storage, not permission to write."""
    intention = validate_network_draft(draft)
    observed = validate_network_observation(observation)
    bound = draft["subscription_key"]
    if not bound or bound != observed["subscription_key"]:
        raise ValueError("network_subscription_changed_reopen_editor")
    apn = CoreApnSettings.from_mapping(draft["apn"]) if draft["apn"] is not None else None
    return CoreNetworkSettingsTarget(observed["subscription_key"], intention.roaming_enabled,
                                     intention.selection, intention.target_plmn, apn)


def require_network_target_binding(target: CoreNetworkSettingsTarget, observation: Mapping, *, allow_profile_creation: bool = False) -> CoreNetworkSettingsTarget:
    """Native editability is required for applying, never for remembering a choice."""
    observed = validate_network_observation(observation)
    if target.subscription_key != observed["subscription_key"]:
        raise ValueError("network_subscription_changed_reopen_editor")
    apn = target.apn
    if apn and apn.creates_profile:
        if not allow_profile_creation:
            raise ValueError("network_current_apn_not_editable")
        return target
    if apn and (apn.profile_id == "@active" or not observed["apn"]
                or not observed["apn"]["editable"] or apn.profile_id != observed["apn"]["profile_id"]):
        raise ValueError("network_current_apn_not_editable")
    return target


def network_target_already_current(target: CoreNetworkSettingsTarget, observation: Mapping) -> bool:
    """Complete public-target comparison; explicit secrets are never presumed equal.

    Callers must establish fresh same-session settings and mobile data enabled.
    This is intent reconciliation, not physical-command readback or permission.
    """
    from arch4.protected_credentials import open_apn_credentials

    observed = validate_network_observation(observation)
    if target.subscription_key != observed['subscription_key']:
        raise ValueError('network_subscription_changed_reopen_editor')
    if target.roaming_enabled is not None and target.roaming_enabled != observed['roaming_enabled']:
        return False
    if target.selection != 'keep' and (target.selection != observed['selection']
            or target.selection == 'manual' and target.target_plmn != observed['target_plmn']):
        return False
    if target.apn is not None:
        username, password = open_apn_credentials(target.apn.protected_credentials)
        if username != target.apn.username:
            raise ValueError('apn_profile_credentials_mismatch')
        if (password is not None or target.apn.creates_profile
                or not target.apn.matches_observation(observed['apn'])):
            return False
    return True


def network_target_from_draft(draft: Mapping, observation: Mapping) -> CoreNetworkSettingsTarget:
    return require_network_target_binding(network_choice_from_draft(draft, observation), observation)
