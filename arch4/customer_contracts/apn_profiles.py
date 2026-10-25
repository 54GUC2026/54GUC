"""Operator-owned APN library values, scoped to one subscription and network."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import re
from typing import Mapping


MAX_APN_PROFILES = 256


def apn_profile_id(subscription_key: str, network_plmn: str, name: str) -> str:
    if not isinstance(subscription_key, str) or not re.fullmatch(r"[a-f0-9]{64}", subscription_key):
        raise ValueError("apn_profile_subscription_required")
    if not isinstance(network_plmn, str) or not re.fullmatch(r"[0-9]{5,6}", network_plmn):
        raise ValueError("apn_profile_network_required")
    if (not isinstance(name, str) or not name.strip() or name != name.strip()
            or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name.casefold() == "new"):
        raise ValueError("apn_profile_name_invalid")
    return sha256(("54GUC.apn.v1:" + subscription_key + ":" + network_plmn + ":" + name.casefold()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SavedApnProfile:
    profile_id: str
    subscription_key: str
    network_plmn: str
    name: str
    apn: str
    username: str
    authentication: str
    ip_protocol: str
    protected_credentials: str
    password_known: bool

    def __post_init__(self) -> None:
        if self.profile_id != apn_profile_id(self.subscription_key, self.network_plmn, self.name):
            raise ValueError("apn_profile_identity_mismatch")
        if not isinstance(self.apn, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,99}", self.apn):
            raise ValueError("network_settings_apn_invalid")
        if (not isinstance(self.username, str) or len(self.username) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in self.username)):
            raise ValueError("network_settings_username_invalid")
        if self.authentication not in {"none", "pap", "chap", "auto"}:
            raise ValueError("network_settings_authentication_invalid")
        if self.ip_protocol not in {"ipv4", "ipv6", "ipv4v6"}:
            raise ValueError("network_settings_ip_protocol_invalid")
        if (not isinstance(self.protected_credentials, str)
                or not re.fullmatch(r"dpapi:v1:[A-Za-z0-9+/]{40,16000}={0,2}", self.protected_credentials)):
            raise ValueError("network_settings_protected_credentials_required")
        if type(self.password_known) is not bool:
            raise ValueError("apn_profile_password_state_invalid")

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)

    def public_mapping(self) -> dict[str, object]:
        """The renderer receives metadata, never replayable credentials."""
        value = self.to_mapping()
        del value["protected_credentials"]
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SavedApnProfile:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("apn_profile_exact_fields_required")
        return cls(**dict(value))
