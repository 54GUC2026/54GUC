"""Canonical Core clock wake contract for independent customer hooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CoreClockWake:
    sequence: int
    lane: str
    emitted_at: float
    due_at: float
    schema: str = "CoreClockWake.v1"
    owner: str = "54guc.core.runtime_clock"

    def __post_init__(self) -> None:
        if int(self.sequence) <= 0:
            raise ValueError("core_clock_wake_positive_sequence_required")
        lane = str(self.lane or "").strip()
        if lane not in {"50ms", "100ms"}:
            raise ValueError(f"core_clock_wake_lane_invalid:{lane or 'missing'}")
        emitted_at = float(self.emitted_at)
        due_at = float(self.due_at)
        if emitted_at <= 0.0 or due_at <= 0.0:
            raise ValueError("core_clock_wake_positive_time_required")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "emitted_at", emitted_at)
        object.__setattr__(self, "due_at", due_at)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": self.sequence,
            "lane": self.lane,
            "emitted_at": self.emitted_at,
            "due_at": self.due_at,
        }


@dataclass(frozen=True, slots=True)
class CoreClockWakeRecord:
    key: str
    wake: CoreClockWake
    fingerprint: str


class CoreClockWakeClientPort(Protocol):
    """Customer cursor over canonical Core wakes; exposes no Core owner."""

    def claim_next(
        self,
        client_id: str,
        *,
        lane: str,
    ) -> CoreClockWakeRecord | None: ...

    def acknowledge(self, client_id: str, *, lane: str, key: str) -> None: ...

    def release(
        self,
        client_id: str,
        *,
        lane: str,
        key: str,
        error: BaseException,
    ) -> None: ...

    def status(self) -> dict[str, Any]: ...


__all__ = (
    "CoreClockWake",
    "CoreClockWakeClientPort",
    "CoreClockWakeRecord",
)
