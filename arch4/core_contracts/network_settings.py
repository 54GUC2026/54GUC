"""Exact network command values; no protocol, policy, polling or credentials I/O."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import ClassVar, Mapping


def subscription_key(imsi: str) -> str:
    """Identify a subscription for settings and learning, never the physical router."""
    if not isinstance(imsi, str) or not re.fullmatch(r"[0-9]{14,16}", imsi):
        return ""
    return hashlib.sha256(("54GUC.subscription.v1:" + imsi).encode()).hexdigest()


def _text(value: object, field: str, *, required: bool = False, limit: int = 128) -> None:
    if type(value) is not str or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"network_settings_{field}_invalid")
    if value != value.strip() or required and not value:
        raise ValueError(f"network_settings_{field}_required")


@dataclass(frozen=True, slots=True)
class CoreApnSettings:
    profile_id: str
    name: str
    apn: str
    authentication: str
    ip_protocol: str
    username: str
    protected_credentials: str

    def __post_init__(self) -> None:
        for name in ("profile_id", "name", "apn"):
            _text(getattr(self, name), name, required=True)
        _text(self.username, "username")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,99}", self.apn):
            raise ValueError("network_settings_apn_invalid")
        if self.authentication not in {"none", "pap", "chap", "auto"}:
            raise ValueError("network_settings_authentication_invalid")
        if self.ip_protocol not in {"ipv4", "ipv6", "ipv4v6"}:
            raise ValueError("network_settings_ip_protocol_invalid")
        if not isinstance(self.protected_credentials, str) or not re.fullmatch(
            r"dpapi:v1:[A-Za-z0-9+/]{40,16000}={0,2}", self.protected_credentials
        ):
            raise ValueError("network_settings_protected_credentials_required")

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)

    @property
    def creates_profile(self) -> bool:
        """Explicit create intention; never a substitute native profile ID."""
        return self.profile_id == "@new"

    def matches_observation(self, observed: object) -> bool:
        if not isinstance(observed, Mapping):
            return False
        if self.creates_profile:
            if (not observed.get("editable") or not observed.get("profile_id")
                    or str(observed["profile_id"]).startswith("@")):
                return False
        elif observed.get("profile_id") != self.profile_id:
            return False
        return all(observed.get(key) == getattr(self, key) for key in
                   ("name", "apn", "username", "authentication", "ip_protocol"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CoreApnSettings:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("network_apn_exact_fields_required")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CoreNetworkSettingsTarget:
    subscription_key: str
    roaming_enabled: bool | None
    selection: str
    target_plmn: str
    apn: CoreApnSettings | None = None
    command_kind: ClassVar[str] = "NETWORK_SETTINGS"
    schema: ClassVar[str] = "CoreNetworkSettingsTarget.v1"

    def __post_init__(self) -> None:
        if type(self.subscription_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", self.subscription_key):
            raise ValueError("network_settings_current_subscription_required")
        if self.roaming_enabled is not None and type(self.roaming_enabled) is not bool:
            raise TypeError("network_settings_roaming_bool_required")
        if self.selection not in {"keep", "auto", "manual"}:
            raise ValueError("network_settings_explicit_selection_required")
        if type(self.target_plmn) is not str:
            raise TypeError("network_settings_plmn_string_required")
        if self.selection == "manual":
            if not re.fullmatch(r"[0-9]{5,6}", self.target_plmn):
                raise ValueError("network_settings_exact_plmn_required")
        elif self.target_plmn:
            raise ValueError("network_settings_inactive_plmn_forbidden")
        if self.apn is not None and not isinstance(self.apn, CoreApnSettings):
            raise TypeError("network_settings_typed_apn_required")
        if self.roaming_enabled is None and self.selection == "keep" and self.apn is None:
            raise ValueError("network_settings_change_required")

    def to_mapping(self) -> dict[str, object]:
        return {"lever": self.command_kind, "subscription_key": self.subscription_key,
                "roaming_enabled": self.roaming_enabled, "selection": self.selection,
                "target_plmn": self.target_plmn,
                "apn": self.apn.to_mapping() if self.apn else None}

    @property
    def stage_kinds(self) -> tuple[str, ...]:
        return network_stage_kinds(self, coordination_profile="direct")


def network_stage_kinds(target: CoreNetworkSettingsTarget, *, coordination_profile: str) -> tuple[str, ...]:
    """One stage-shape owner; transport adapters never invent another sequence."""
    if coordination_profile in {"zte_zwrt", "tplink_dev2"}:
        return ("network_data_hold",) + (
            ("network_apn_create",) if coordination_profile == "tplink_dev2"
            and target.apn and target.apn.creates_profile else ()
        ) + (
            ("network_apn",) if target.apn else ()
        ) + (("network_roaming",) if target.roaming_enabled is not None else ()) + (
            ("network_operator",) if target.selection != "keep" else ()
        ) + ("network_data_start",)
    if coordination_profile not in {"direct", "openwrt", "routeros"}:
        raise ValueError("network_settings_coordination_profile_invalid")
    return _direct_network_stages(target)


def _direct_network_stages(target: CoreNetworkSettingsTarget) -> tuple[str, ...]:
    # Restrict operator before enabling roaming; explicit disable comes first.
    stages = ["network_roaming"] if target.roaming_enabled is False else []
    if target.selection != "keep":
        stages.append("network_operator")
    if target.roaming_enabled is True:
        stages.append("network_roaming")
    if target.apn:
        stages.append("network_apn")
    return tuple(stages)


def network_target_from_mapping(value: Mapping[str, object]) -> CoreNetworkSettingsTarget:
    if not isinstance(value, Mapping) or set(value) != {
        "lever", "subscription_key", "roaming_enabled", "selection", "target_plmn", "apn"
    } or value["lever"] != "NETWORK_SETTINGS":
        raise ValueError("network_settings_exact_target_required")
    return CoreNetworkSettingsTarget(
        subscription_key=value["subscription_key"], roaming_enabled=value["roaming_enabled"],
        selection=value["selection"], target_plmn=value["target_plmn"],
        apn=CoreApnSettings.from_mapping(value["apn"]) if value["apn"] is not None else None,
    )


def validate_network_observation(value: object) -> dict[str, object]:
    """Validate the complete sanitized cell, including explicit unknowns."""
    fields = {"schema", "subscription_key", "selection", "target_plmn", "roaming_enabled", "apn"}
    if not isinstance(value, Mapping) or set(value) != fields or value["schema"] != "NetworkSettingsObservation.v1":
        raise ValueError("network_observation_exact_fields_required")
    row = dict(value)
    if row["selection"] not in {None, "auto", "manual"}:
        raise ValueError("network_observation_selection_invalid")
    if row["roaming_enabled"] is not None and type(row["roaming_enabled"]) is not bool:
        raise TypeError("network_observation_roaming_invalid")
    if type(row["subscription_key"]) is not str or row["subscription_key"] and not re.fullmatch(r"[a-f0-9]{64}", row["subscription_key"]):
        raise ValueError("network_observation_subscription_invalid")
    if row["target_plmn"] is not None and (type(row["target_plmn"]) is not str or row["target_plmn"] and not re.fullmatch(r"[0-9]{5,6}", row["target_plmn"])):
        raise ValueError("network_observation_target_invalid")
    apn = row["apn"]
    if apn is not None:
        if not isinstance(apn, Mapping) or set(apn) != {"profile_id", "name", "apn", "username", "authentication", "ip_protocol", "editable"}:
            raise ValueError("network_observation_apn_exact_fields_required")
        for key in ("profile_id", "name", "apn", "username"):
            _text(apn[key], key)
        if apn["authentication"] not in {None, "none", "pap", "chap", "auto"} or apn["ip_protocol"] not in {None, "ipv4", "ipv6", "ipv4v6"}:
            raise ValueError("network_observation_apn_enum_invalid")
        if type(apn["editable"]) is not bool:
            raise TypeError("network_observation_apn_editable_invalid")
        row["apn"] = dict(apn)
    return row
