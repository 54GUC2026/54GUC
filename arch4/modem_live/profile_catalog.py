"""Public read interface for mutable Modem Live profile data.

The reviewed catalog remains private in ``arch4.modem.catalog``. Other code
receives a profile ID or read-only connection data through this module. Offline
review tools may request a copied record but must not import the catalog.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from arch4.modem_live.connection_profiles import (
    BackendProfileContract,
    backend_profile_contract,
    connection_config_fields_for_backend,
    connection_transport_for_backend,
)
from arch4.modem.catalog import (
    _profile_model_tokens_match, _profile_text,
    _ALL_KNOWN_ACTIONS,
    FAMILY_HUAWEI_HILINK,
    FAMILY_OPENWRT_MM,
    FAMILY_ROUTEROS_AT,
    FAMILY_ZTE_WEBUI,
    FAMILY_ZTE_ZWRT,
    FIRMWARE_PROFILE_OVERLAYS,
    FirmwareProfileOverlay,
    firmware_profile_by_id,
    get_backend_action_semantics,
    get_backend_capability_flags,
    get_backend_entry,
    get_backend_family,
    get_backend_support_level,
    match_firmware_profile,
    profile_launch_backend,
    SUPPORT_LEVEL_ADVANCED_CONTROL,
    SUPPORT_LEVEL_FULL_EXPERT,
    SUPPORT_LEVEL_MONITOR,
    SUPPORT_LEVEL_SAFE_CONTROL,
)


@dataclass(frozen=True, slots=True)
class ModemProfileSelection:
    """Read-only connection data for one Modem Live profile ID."""

    profile_id: str
    device_radio_class: str
    backend: str
    label: str
    manufacturer: str
    model_hint: str
    target_guard_model: str
    target_guard_firmware: str
    software_hint: str
    family: str
    overlay: str
    profile_path: str
    env_host_keys: tuple[str, ...]
    env_user_keys: tuple[str, ...]
    config_host_field: str
    config_user_field: str
    config_password_field: str
    config_ping_field: str
    transport: str
    default_username: str
    provenance: str
    apn_write_requires_explicit_password: bool
    network_settings_writable: bool
    apn_profile_creation: bool

    @property
    def launcher_row(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "device_radio_class": self.device_radio_class,
            "backend": self.backend,
            "label": self.label,
            "manufacturer": self.manufacturer,
            "model_hint": self.model_hint,
            "target_guard_model": self.target_guard_model,
            "software_hint": self.software_hint,
            "family": self.family,
            "overlay": self.overlay,
            "profile_path": self.profile_path,
            "transport": self.transport,
            "provenance": self.provenance,
        }


def _modem_profile(profile_id: object) -> FirmwareProfileOverlay | None:
    """Return one profile record for use inside Modem Live."""

    return firmware_profile_by_id(profile_id)


def modem_profile_record(profile_id: object) -> dict[str, Any]:
    """Return a plain copy for other Modem Live components."""

    profile = _modem_profile(profile_id)
    return asdict(profile) if profile is not None else {}


def modem_profile_definition(profile_id: str):
    """The immutable startup-selected rule owner; no file reread or fallback."""
    from arch4.modem_live.profile_definition.registry import startup_registry
    return startup_registry().select(profile_id)


def modem_profile_definition_issues() -> tuple[dict[str, str], ...]:
    """Safe, actionable file errors for the existing UI/diagnostic consumer."""
    from arch4.modem_live.profile_definition.registry import startup_registry
    return tuple({'file':item.file,'profile_id':item.profile_id,'message':item.message}
                 for item in startup_registry().errors)


def modem_profile_native_identity_qualified(profile_id: str, *, model: str,
                                            firmware: str, hardware: str) -> bool:
    """Apply the selected catalog contract to the complete native identity."""
    if any(not isinstance(value, str) for value in (profile_id, model, firmware, hardware)):
        raise TypeError("native_device_identity_text_required")
    profile = _modem_profile(profile_id)
    if profile is None:
        raise ValueError("native_identity_unknown_selected_profile")
    return (model == profile.target_guard_model
            and profile.native_firmware_qualified(firmware)
            and bool(hardware.strip())
            and (not profile.hardware_exact or hardware == profile.hardware_exact))


def modem_profile_hardware_qualified(profile_id: str, *, model: str) -> bool:
    """Compare observed/stored hardware using the existing catalog model selector.

    No firmware is substituted: hardware association and current firmware
    qualification are different checks in the same admission owner.
    """
    profile=_modem_profile(profile_id)
    if profile is None or not str(model).strip():
        return False
    if (profile.firmware_exact or profile.firmware_pattern) and profile.target_guard_model:
        return _profile_text(model)==_profile_text(profile.target_guard_model)
    return _profile_model_tokens_match(backend_name=profile.backend,
        model_text=_profile_text(model),firmware_text='',tokens=profile.model_contains)


def modem_profile_network_settings_readable(profile_id: str, *, firmware: str) -> bool:
    """Project the catalog's read qualification, independently of write permission."""
    if not isinstance(profile_id, str) or not isinstance(firmware, str):
        raise TypeError("network_inspection_profile_and_firmware_text_required")
    profile = _modem_profile(profile_id)
    if profile is None:
        raise ValueError("network_inspection_unknown_confirmed_profile")
    return profile.network_settings_qualified(firmware)


def _selection_from_profile(
    profile: FirmwareProfileOverlay,
) -> ModemProfileSelection:
    backend = profile.driver or profile_launch_backend(profile.profile_id, profile.backend)
    if not backend:
        raise ValueError(
            f"exact modem profile has no launch backend: {profile.profile_id}"
        )
    connection: BackendProfileContract | None = backend_profile_contract(backend)
    if connection is None:
        raise ValueError(
            f"exact modem profile {profile.profile_id} resolves to unknown backend {backend!r}"
        )
    model_hint = next(
        (
            str(token).strip()
            for token in tuple(profile.model_contains or ())
            if str(token).strip()
        ),
        "",
    ) or connection.model_hint
    target_guard_model = str(
        getattr(profile, "target_guard_model", "") or model_hint
    ).strip()
    software_hint = " ".join(
        str(item).strip()
        for item in tuple(profile.firmware_contains or ())
        if str(item).strip()
    ) or connection.software_hint
    return ModemProfileSelection(
        profile_id=str(profile.profile_id),
        device_radio_class=profile.device_radio_class,
        backend=backend,
        label=str(profile.display_name or connection.label),
        manufacturer=str(profile.vendor or connection.manufacturer),
        model_hint=model_hint,
        target_guard_model=target_guard_model,
        target_guard_firmware=profile.firmware_exact,
        software_hint=software_hint,
        family=str(profile.family or connection.family),
        overlay=connection.overlay,
        profile_path=connection.profile_path,
        env_host_keys=connection.env_host_keys,
        env_user_keys=connection.env_user_keys,
        config_host_field=connection.config_host_field,
        config_user_field=connection.config_user_field,
        config_password_field=connection.config_password_field,
        config_ping_field=connection.config_ping_field,
        transport=connection.transport,
        default_username=profile.login_wire_username or connection.default_username,
        provenance=str(profile.provenance or ""),
        apn_write_requires_explicit_password=profile.apn_write_requires_explicit_password,
        network_settings_writable=bool(profile.capabilities["supports_network_settings"]),
        apn_profile_creation=profile.apn_profile_creation,
    )


def modem_profile_selections() -> tuple[ModemProfileSelection, ...]:
    return tuple(_selection_from_profile(profile) for profile in FIRMWARE_PROFILE_OVERLAYS)


def modem_profile_selection(profile_id: object) -> ModemProfileSelection | None:
    wanted = str(profile_id or "").strip().casefold()
    if not wanted:
        return None
    return next(
        (
            selection
            for selection in modem_profile_selections()
            if selection.profile_id.casefold() == wanted
        ),
        None,
    )


def modem_profile_ids() -> tuple[str, ...]:
    return tuple(selection.profile_id for selection in modem_profile_selections())


def modem_profile_launch_catalog() -> tuple[dict[str, str], ...]:
    return tuple(selection.launcher_row for selection in modem_profile_selections())


def modem_connection_config_fields(
    backend: object,
) -> tuple[str, str, str, str]:
    """Return declared ``Arch4Config`` fields for one backend connection."""

    return connection_config_fields_for_backend(backend)


def modem_connection_transport(backend: object) -> str:
    """Return the declared physical transport for one backend."""

    return connection_transport_for_backend(backend)


def unresolved_modem_profile_errors() -> list[str]:
    errors: list[str] = []
    for profile in FIRMWARE_PROFILE_OVERLAYS:
        try:
            _selection_from_profile(profile)
        except Exception as exc:
            errors.append(f"{profile.profile_id}: {exc}")
    return errors


def modem_backend_entry(backend: object):
    """Return backend metadata for review and reporting."""

    return get_backend_entry(str(backend or ""))


def modem_backend_family(backend: object) -> str:
    return str(get_backend_family(str(backend or "")) or "")


def modem_backend_capability_flags(backend: object) -> dict[str, bool]:
    return dict(get_backend_capability_flags(str(backend or "")))


def modem_backend_action_semantics(backend: object):
    return get_backend_action_semantics(str(backend or ""))


def modem_backend_support_level(backend: object) -> str:
    return str(get_backend_support_level(str(backend or "")) or "")


def match_modem_profile(*, backend: object, model: object, firmware: object):
    return match_firmware_profile(
        backend=str(backend or ""),
        model=str(model or ""),
        firmware=str(firmware or ""),
    )


def modem_action_names() -> tuple[str, ...]:
    return tuple(_ALL_KNOWN_ACTIONS)


__all__ = [
    "ModemProfileSelection",
    "FAMILY_HUAWEI_HILINK",
    "FAMILY_OPENWRT_MM",
    "FAMILY_ROUTEROS_AT",
    "FAMILY_ZTE_WEBUI",
    "FAMILY_ZTE_ZWRT",
    "SUPPORT_LEVEL_ADVANCED_CONTROL",
    "SUPPORT_LEVEL_FULL_EXPERT",
    "SUPPORT_LEVEL_MONITOR",
    "SUPPORT_LEVEL_SAFE_CONTROL",
    "modem_action_names",
    "modem_backend_action_semantics",
    "modem_backend_capability_flags",
    "modem_backend_entry",
    "modem_backend_family",
    "modem_backend_support_level",
    "modem_connection_config_fields",
    "modem_connection_transport",
    "modem_profile_ids",
    "modem_profile_launch_catalog",
    "modem_profile_record",
    "modem_profile_native_identity_qualified",
    "modem_profile_hardware_qualified",
    "modem_profile_network_settings_readable",
    "modem_profile_selection",
    "modem_profile_selections",
    "match_modem_profile",
    "unresolved_modem_profile_errors",
]
