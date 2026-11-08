"""Core-owned active configuration proposal and result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.payload import CoreFrozenPayload


class CoreConfigDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    HELD = "held"


@dataclass(frozen=True, slots=True)
class CoreConfigProposal:
    proposal_id: str
    client_id: str
    client_role: CoreClientRole
    parent_generation_id: str
    changes: CoreFrozenPayload
    evidence: CoreFrozenPayload
    created_at: float
    schema: str = "CoreConfigProposal.v1"
    owner: str = "54guc.core.config_proposal"

    def __post_init__(self) -> None:
        for field_name in ("proposal_id", "client_id", "parent_generation_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"core_config_{field_name}_required")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.client_role, CoreClientRole):
            raise TypeError("core_config_typed_client_role_required")
        if self.client_role not in {
            CoreClientRole.OPERATOR,
            CoreClientRole.OPERATOR_AUTOMATION,
            CoreClientRole.AUTONOMOUS_INTELLIGENCE,
        }:
            raise PermissionError("core_config_client_role_forbidden")
        if not isinstance(self.changes, CoreFrozenPayload):
            raise TypeError("core_config_changes_payload_required")
        if not isinstance(self.evidence, CoreFrozenPayload):
            raise TypeError("core_config_evidence_payload_required")
        if float(self.created_at) <= 0.0:
            raise ValueError("core_config_positive_created_at_required")
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "proposal_id": self.proposal_id,
            "client_id": self.client_id,
            "client_role": self.client_role.value,
            "parent_generation_id": self.parent_generation_id,
            "changes": self.changes.to_payload(),
            "evidence": self.evidence.to_payload(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CoreConfigReceipt:
    proposal_id: str
    disposition: CoreConfigDisposition
    active_generation_id: str
    reason: str
    occurred_at: float
    schema: str = "CoreConfigReceipt.v1"
    owner: str = "54guc.core.active_config_generation"

    def __post_init__(self) -> None:
        for field_name in ("proposal_id", "active_generation_id", "reason"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"core_config_receipt_{field_name}_required")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.disposition, CoreConfigDisposition):
            raise TypeError("core_config_receipt_typed_disposition_required")
        if float(self.occurred_at) <= 0.0:
            raise ValueError("core_config_receipt_positive_time_required")
        object.__setattr__(self, "occurred_at", float(self.occurred_at))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "proposal_id": self.proposal_id,
            "disposition": self.disposition.value,
            "active_generation_id": self.active_generation_id,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True, slots=True)
class CoreConfigSnapshot:
    """Immutable customer view of the sole active Core configuration."""

    generation_id: str
    config_fingerprint: str
    values: CoreFrozenPayload
    metadata: CoreFrozenPayload
    occurred_at: float
    schema: str = "CoreConfigSnapshot.v1"
    owner: str = "54guc.core.active_config_generation"

    def __post_init__(self) -> None:
        for field_name in ("generation_id", "config_fingerprint"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"core_config_snapshot_{field_name}_required")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.values, CoreFrozenPayload):
            raise TypeError("core_config_snapshot_values_required")
        if not isinstance(self.metadata, CoreFrozenPayload):
            raise TypeError("core_config_snapshot_metadata_required")
        if float(self.occurred_at) <= 0.0:
            raise ValueError("core_config_snapshot_positive_time_required")
        object.__setattr__(self, "occurred_at", float(self.occurred_at))

    @property
    def fingerprint(self) -> str:
        return self.config_fingerprint

    def __getattr__(self, name: str) -> Any:
        values = self.values.to_mapping()
        if name in values:
            return values[name]
        raise AttributeError(name)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "generation_id": self.generation_id,
            "config_fingerprint": self.config_fingerprint,
            "values": self.values.to_payload(),
            "metadata": self.metadata.to_payload(),
            "occurred_at": self.occurred_at,
        }


@runtime_checkable
class CoreConfigurationClientPort(Protocol):
    """Only customer-facing configuration mutation/read boundary."""

    def submit_proposal(self, proposal: CoreConfigProposal) -> CoreConfigReceipt: ...

    def active_snapshot(self) -> CoreConfigSnapshot: ...


__all__ = (
    "CoreConfigDisposition",
    "CoreConfigurationClientPort",
    "CoreConfigProposal",
    "CoreConfigReceipt",
    "CoreConfigSnapshot",
)
