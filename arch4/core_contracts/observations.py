"""Immutable accepted observation delivered from Core to every customer."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.rtt_observation import MatrixBoundRttObservation


CORE_OBSERVATION_SCHEMA = "CoreObservationFrame.v2"
CORE_OBSERVATION_OWNER = "54guc.core.accepted_observation"


@dataclass(frozen=True, slots=True)
class CoreObservationFrame:
    accepted_commit_id: str
    identity_code: str
    identity_status: str
    access_type: str
    matrix_identity: MatrixGenerationIdentity
    sampled_at: float
    accepted_at: float
    published_at: float
    matrix_rtt: MatrixBoundRttObservation
    radio_connected: bool | None
    derived_facts: CoreFrozenPayload
    capability_facts: CoreFrozenPayload
    lower_controller_change: CoreFrozenPayload
    schema: str = CORE_OBSERVATION_SCHEMA
    owner: str = CORE_OBSERVATION_OWNER

    def __post_init__(self) -> None:
        if self.schema != CORE_OBSERVATION_SCHEMA or self.owner != CORE_OBSERVATION_OWNER:
            raise ValueError("core_observation_owner_schema_invalid")
        for field_name in (
            "accepted_commit_id",
            "identity_code",
            "identity_status",
            "access_type",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(f"core_observation_{field_name}_required")
        if self.identity_status not in {"provisional", "confirmed"}:
            raise ValueError("core_observation_identity_status_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_observation_matrix_identity_required")
        for field_name in ("sampled_at", "accepted_at", "published_at"):
            if type(getattr(self, field_name)) not in {int, float}:
                raise TypeError(
                    f"core_observation_{field_name}_invalid"
                )
        sampled_at = float(self.sampled_at)
        accepted_at = float(self.accepted_at)
        published_at = float(self.published_at)
        if (
            not all(
                math.isfinite(value)
                for value in (sampled_at, accepted_at, published_at)
            )
            or sampled_at <= 0.0
            or accepted_at < sampled_at
            or published_at < accepted_at
        ):
            raise ValueError("core_observation_timestamp_order_invalid")
        object.__setattr__(self, "sampled_at", sampled_at)
        object.__setattr__(self, "accepted_at", accepted_at)
        object.__setattr__(self, "published_at", published_at)
        if not isinstance(self.matrix_rtt, MatrixBoundRttObservation):
            raise TypeError("core_observation_matrix_rtt_required")
        if self.matrix_rtt.matrix_identity != self.matrix_identity:
            raise ValueError("core_observation_matrix_rtt_identity_mismatch")
        if self.radio_connected is not None and type(self.radio_connected) is not bool:
            raise TypeError("core_observation_radio_connected_invalid")
        for field_name in (
            "derived_facts",
            "capability_facts",
            "lower_controller_change",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(f"core_observation_{field_name}_payload_required")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "accepted_commit_id": self.accepted_commit_id,
            "identity_code": self.identity_code,
            "identity_status": self.identity_status,
            "access_type": self.access_type,
            "matrix_identity": self.matrix_identity.as_dict(),
            "sampled_at": self.sampled_at,
            "accepted_at": self.accepted_at,
            "published_at": self.published_at,
            "matrix_rtt": self.matrix_rtt.as_dict(),
            "radio_connected": self.radio_connected,
            "derived_facts": self.derived_facts.to_payload(),
            "capability_facts": self.capability_facts.to_payload(),
            "lower_controller_change": self.lower_controller_change.to_payload(),
        }

    @property
    def session_id(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation

    @property
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def matrix_revision(self) -> str:
        return self.matrix_identity.matrix_revision


__all__ = (
    "CORE_OBSERVATION_OWNER",
    "CORE_OBSERVATION_SCHEMA",
    "CoreObservationFrame",
)
