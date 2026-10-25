"""Typed immutable Core event envelope for customer streams."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from arch4.core_contracts.payload import CoreFrozenPayload


class CoreEventKind(str, Enum):
    SESSION = "session"
    IDENTITY = "identity"
    CAPABILITY = "capability"
    LOWER_CONTROLLER_CHANGE = "lower_controller_change"
    COMMAND_LIFECYCLE = "command_lifecycle"
    SAFETY = "safety"
    CONFIGURATION = "configuration"
    WORKER_HEALTH = "worker_health"
    FAULT = "fault"


@dataclass(frozen=True, slots=True)
class CoreEvent:
    sequence: int
    event_id: str
    kind: CoreEventKind
    session_id: str
    occurred_at: float
    detail: CoreFrozenPayload
    schema: str = "CoreEvent.v1"
    owner: str = "54guc.core.event_stream"

    def __post_init__(self) -> None:
        if int(self.sequence) <= 0:
            raise ValueError("core_event_positive_sequence_required")
        object.__setattr__(self, "sequence", int(self.sequence))
        for field_name in ("event_id", "session_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"core_event_{field_name}_required")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.kind, CoreEventKind):
            raise TypeError("core_event_typed_kind_required")
        if float(self.occurred_at) <= 0.0:
            raise ValueError("core_event_positive_time_required")
        object.__setattr__(self, "occurred_at", float(self.occurred_at))
        if not isinstance(self.detail, CoreFrozenPayload):
            raise TypeError("core_event_detail_payload_required")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "detail": self.detail.to_payload(),
        }


__all__ = ("CoreEvent", "CoreEventKind")
