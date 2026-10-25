"""State machine for the protected Modem Live read lane."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time


class AcquisitionLaneState(str, Enum):
    HEALTHY = "healthy"
    TIMED_OUT = "timed_out"
    WEDGED = "wedged"
    RECOVERING = "recovering"
    REPLACED = "replaced"


@dataclass(slots=True)
class AcquisitionLaneStateAuthority:
    state: AcquisitionLaneState = AcquisitionLaneState.HEALTHY
    changed_at: float = 0.0
    reason: str = ""
    timeout_count: int = 0
    recovery_count: int = 0

    def __post_init__(self) -> None:
        if self.changed_at <= 0.0:
            self.changed_at = time.time()

    def mark(
        self,
        state: AcquisitionLaneState,
        reason: str = "",
    ) -> dict[str, object]:
        if not isinstance(state, AcquisitionLaneState):
            raise TypeError("acquisition_lane_state_required")
        if state is AcquisitionLaneState.TIMED_OUT:
            self.timeout_count += 1
        if state is AcquisitionLaneState.RECOVERING:
            self.recovery_count += 1
        if self.state is not state or self.reason != str(reason or ""):
            self.state = state
            self.reason = str(reason or "")
            self.changed_at = time.time()
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": "ModemLiveAcquisitionLaneState.v1",
            "owner": "54guc.modem_live.acquisition_lane_state",
            "state": self.state.value,
            "changed_at": float(self.changed_at),
            "reason": self.reason,
            "timeout_count": int(self.timeout_count),
            "recovery_count": int(self.recovery_count),
        }


__all__ = ("AcquisitionLaneState", "AcquisitionLaneStateAuthority")
