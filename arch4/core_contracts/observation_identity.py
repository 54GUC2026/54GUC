"""Validated identity context carried into one Core observation frame."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arch4.core_contracts.payload import CoreFrozenPayload


CORE_OBSERVATION_IDENTITY_SCHEMA = "CoreObservationIdentityContext.v1"
CORE_OBSERVATION_IDENTITY_OWNER = "54guc.core.accepted_observation"


@dataclass(frozen=True, slots=True)
class CoreObservationIdentityContext:
    """Projection of Modem Live's validated identity, never an identity writer."""

    session_id: str
    identity_code: str
    identity_status: str
    access_type: str
    source_identity_status: str
    source_schema: str
    source_owner: str
    capability_facts: CoreFrozenPayload
    schema: str = CORE_OBSERVATION_IDENTITY_SCHEMA
    owner: str = CORE_OBSERVATION_IDENTITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != CORE_OBSERVATION_IDENTITY_SCHEMA
            or self.owner != CORE_OBSERVATION_IDENTITY_OWNER
        ):
            raise ValueError("core_observation_identity_owner_schema_invalid")
        for field_name in (
            "session_id",
            "identity_code",
            "access_type",
            "source_identity_status",
            "source_schema",
            "source_owner",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"core_observation_identity_{field_name}_required")
            object.__setattr__(self, field_name, value)
        identity_status = str(self.identity_status or "").strip().lower()
        if identity_status not in {"provisional", "confirmed"}:
            raise ValueError("core_observation_identity_status_invalid")
        object.__setattr__(self, "identity_status", identity_status)
        if not isinstance(self.capability_facts, CoreFrozenPayload):
            raise TypeError("core_observation_identity_capability_payload_required")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "session_id": self.session_id,
            "identity_code": self.identity_code,
            "identity_status": self.identity_status,
            "access_type": self.access_type,
            "source_identity_status": self.source_identity_status,
            "source_schema": self.source_schema,
            "source_owner": self.source_owner,
            "capability_facts": self.capability_facts.to_payload(),
        }


__all__ = (
    "CORE_OBSERVATION_IDENTITY_OWNER",
    "CORE_OBSERVATION_IDENTITY_SCHEMA",
    "CoreObservationIdentityContext",
)
