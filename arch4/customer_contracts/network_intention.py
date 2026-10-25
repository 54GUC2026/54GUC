"""Physical-SIM user intention only: never observed network or modem capability.

Both future Sign In and connected Apply must use this exact contract. Older
optional country/provider hints have no conversion or defaulting authority.
Country/PLMN compatibility and modem effect still require owner admission.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import ClassVar, Mapping


@dataclass(frozen=True, slots=True)
class PhysicalSimNetworkIntention:
    sim_country: str
    roaming_enabled: bool | None
    selection: str
    home_provider: str = ""
    roaming_country: str = ""
    target_plmn: str = ""

    SCHEMA: ClassVar[str] = "PhysicalSimNetworkIntention.v1"

    def __post_init__(self) -> None:
        for field in ("sim_country", "home_provider", "roaming_country"):
            value = getattr(self, field)
            if type(value) is not str or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"network_intention_{field}_invalid")
            object.__setattr__(self, field, value.strip())
        if self.roaming_enabled is not None and type(self.roaming_enabled) is not bool:
            raise TypeError("network_intention_roaming_bool_required")
        if type(self.selection) is not str or self.selection not in {"keep", "auto", "manual"}:
            raise ValueError("network_intention_explicit_selection_required")
        if type(self.target_plmn) is not str:
            raise TypeError("network_intention_plmn_string_required")
        if not self.roaming_enabled:
            if self.home_provider or self.roaming_country:
                raise ValueError("network_intention_inactive_roaming_fields")
        else:
            if self.home_provider or self.selection == "keep":
                raise ValueError("network_intention_inactive_home_fields")
        if self.selection in {"auto", "keep"} and self.target_plmn:
            raise ValueError("network_intention_auto_has_target")
        if self.selection == "manual" and not re.fullmatch(r"[0-9]{5,6}", self.target_plmn):
            raise ValueError("network_intention_exact_plmn_required")

    def to_payload(self) -> dict[str, object]:
        return {"schema": self.SCHEMA, **asdict(self)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> PhysicalSimNetworkIntention:
        fields = {"schema", "sim_country", "roaming_enabled", "selection", "home_provider",
                  "roaming_country", "target_plmn"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError("network_intention_exact_payload_required")
        if payload["schema"] != cls.SCHEMA:
            raise ValueError("network_intention_schema_invalid")
        return cls(**{key: value for key, value in payload.items() if key != "schema"})
