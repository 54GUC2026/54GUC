"""Persist the last non-secret SIM/network form for a confirmed router."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Mapping

from arch4.atomic_io import atomic_write_json


SCHEMA = "NGUINetworkPreset.v1"
OWNER = "arch4.ngui.network_preset_store"
FILENAME = "ngui_network_presets.json"
_FIELDS = {
    "profile_id", "router_serial", "firmware", "subscription_key", "sim_country",
    "roaming_enabled", "selection", "network_plmn", "network_country", "apn",
    "saved_profile_id", "password_known",
}
_APN_FIELDS = {"name", "apn", "username", "authentication", "ip_protocol"}


def _validated(row: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(row, Mapping) or set(row) != _FIELDS:
        raise ValueError("NGUI network preset fields mismatch")
    result = dict(row)
    for name in _FIELDS - {"roaming_enabled", "apn", "password_known"}:
        value = result[name]
        if (type(value) is not str or len(value) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError(f"NGUI network preset {name} is invalid")
    if not result["profile_id"] or not re.fullmatch(r"[a-f0-9]{64}", result["subscription_key"]):
        raise ValueError("NGUI network preset scope is invalid")
    if result["selection"] not in {"auto", "manual", ""}:
        raise ValueError("NGUI network selection is invalid")
    if result["network_plmn"] and not re.fullmatch(r"[0-9]{5,6}", result["network_plmn"]):
        raise ValueError("NGUI network code is invalid")
    if result["selection"] == "manual" and not result["network_plmn"]:
        raise ValueError("NGUI manual network requires a code")
    if result["roaming_enabled"] is not None and type(result["roaming_enabled"]) is not bool:
        raise ValueError("NGUI roaming value is invalid")
    if type(result["password_known"]) is not bool:
        raise ValueError("NGUI APN password state is invalid")
    apn = result["apn"]
    if not isinstance(apn, Mapping) or set(apn) != _APN_FIELDS:
        raise ValueError("NGUI APN preset fields mismatch")
    for name, value in apn.items():
        if (type(value) is not str or len(value) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError(f"NGUI APN preset {name} is invalid")
    result["apn"] = dict(apn)
    if result["saved_profile_id"] and not re.fullmatch(r"[a-f0-9]{64}", result["saved_profile_id"]):
        raise ValueError("NGUI saved APN reference is invalid")
    return result


class NGUINetworkPresetStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root).resolve() / FILENAME

    def load_all(self) -> dict[str, dict[str, object]]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"schema", "owner", "profiles"}
                or payload["schema"] != SCHEMA or payload["owner"] != OWNER
                or not isinstance(payload["profiles"], dict)):
            raise ValueError("NGUI network preset file is invalid")
        profiles = {str(key): _validated(row) for key, row in payload["profiles"].items()}
        if any(row["profile_id"] != key for key, row in profiles.items()):
            raise ValueError("NGUI network preset profile mismatch")
        return profiles

    def load(self, profile_id: str) -> dict[str, object] | None:
        return self.load_all().get(profile_id)

    def save(self, row: Mapping[str, object]) -> None:
        result = _validated(row)
        profiles: dict[str, object] = self.load_all()
        profiles[str(result["profile_id"])] = result
        atomic_write_json(self.path, {"schema": SCHEMA, "owner": OWNER, "profiles": profiles},
                          sort_keys=True, trailing_newline=True, mode=0o600)
