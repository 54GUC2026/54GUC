"""Customer attachment health as observed by the Core boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CoreClientHealthState(str, Enum):
    UNATTACHED = "unattached"
    ATTACHED = "attached"
    HEALTHY = "healthy"
    LAGGING = "lagging"
    FAULTED = "faulted"
    DETACHED = "detached"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class CoreClientHealth:
    client_id: str
    state: CoreClientHealthState
    cursor: int
    lag: int
    dropped_generation_count: int
    last_ack_at: float
    queue_high_water: int
    last_error: str
    observed_at: float
    schema: str = "CoreClientHealth.v1"
    owner: str = "54guc.core.client_health"

    def __post_init__(self) -> None:
        client_id = str(self.client_id or "").strip()
        if not client_id:
            raise ValueError("core_client_health_id_required")
        object.__setattr__(self, "client_id", client_id)
        if not isinstance(self.state, CoreClientHealthState):
            raise TypeError("core_client_health_typed_state_required")
        for field_name in (
            "cursor",
            "lag",
            "dropped_generation_count",
            "queue_high_water",
        ):
            value = int(getattr(self, field_name))
            if value < 0:
                raise ValueError(f"core_client_health_{field_name}_invalid")
            object.__setattr__(self, field_name, value)
        last_ack_at = float(self.last_ack_at)
        observed_at = float(self.observed_at)
        if last_ack_at < 0.0 or observed_at <= 0.0:
            raise ValueError("core_client_health_time_invalid")
        object.__setattr__(self, "last_ack_at", last_ack_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "last_error", str(self.last_error or ""))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "client_id": self.client_id,
            "state": self.state.value,
            "cursor": self.cursor,
            "lag": self.lag,
            "dropped_generation_count": self.dropped_generation_count,
            "last_ack_at": self.last_ack_at,
            "queue_high_water": self.queue_high_water,
            "last_error": self.last_error,
            "observed_at": self.observed_at,
        }


__all__ = ("CoreClientHealth", "CoreClientHealthState")
