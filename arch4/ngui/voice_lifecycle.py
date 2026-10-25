"""Typed SINR voice lifecycle across the GUI toolkit boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, Mapping


SINR_VOICE_LIFECYCLE_SCHEMA = "NGUISINRVoiceLifecycle.v1"
SINR_VOICE_LIFECYCLE_OWNER = "arch4.ngui.voice_lifecycle"


class SINRVoiceState(str, Enum):
    INACTIVE = "inactive"
    PENDING = "pending"
    ACTIVE = "active"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NGUISINRVoiceRequest:
    sequence: int
    enabled: bool
    requested_at: float

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("SINR voice request sequence must be positive")
        if self.requested_at <= 0:
            raise ValueError("SINR voice request timestamp must be positive")


@dataclass(frozen=True, slots=True)
class NGUISINRVoiceSnapshot:
    sequence: int
    state: SINRVoiceState
    active: bool
    desired_active: bool
    updated_at: float
    message: str = ""

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "schema": SINR_VOICE_LIFECYCLE_SCHEMA,
            "owner": SINR_VOICE_LIFECYCLE_OWNER,
            "sequence": int(self.sequence),
            "state": self.state.value,
            "active": bool(self.active),
            "desired_active": bool(self.desired_active),
            "updated_at": float(self.updated_at),
            "message": str(self.message or ""),
        }


class NGUISINRVoiceLifecycle:
    """NGUI read model for acknowledged GUI speech operations."""

    REQUEST_TIMEOUT_S = 5.0

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = NGUISINRVoiceSnapshot(
            sequence=0,
            state=SINRVoiceState.INACTIVE,
            active=False,
            desired_active=False,
            updated_at=0.0,
        )

    def request(self, *, enabled: bool, now: float) -> NGUISINRVoiceRequest:
        if now <= 0:
            raise ValueError("SINR voice request timestamp must be positive")
        with self._lock:
            if self._snapshot.state is SINRVoiceState.PENDING:
                raise RuntimeError("SINR voice request is already pending")
            if bool(enabled) is self._snapshot.active:
                raise RuntimeError("SINR voice already has the requested state")
            sequence = self._snapshot.sequence + 1
            self._snapshot = NGUISINRVoiceSnapshot(
                sequence=sequence,
                state=SINRVoiceState.PENDING,
                active=self._snapshot.active,
                desired_active=bool(enabled),
                updated_at=float(now),
                message=(
                    "Starting SINR voice readout."
                    if enabled
                    else "Stopping SINR voice readout."
                ),
            )
            return NGUISINRVoiceRequest(sequence, bool(enabled), float(now))

    def acknowledge(
        self,
        *,
        sequence: int,
        active: bool,
        now: float,
        error: str = "",
    ) -> bool:
        if now <= 0:
            raise ValueError("SINR voice acknowledgement timestamp must be positive")
        with self._lock:
            current = self._snapshot
            if (
                current.state is not SINRVoiceState.PENDING
                or int(sequence) != current.sequence
            ):
                return False
            detail = str(error or "").strip()
            accepted = not detail and bool(active) is current.desired_active
            if accepted:
                state = (
                    SINRVoiceState.ACTIVE
                    if current.desired_active
                    else SINRVoiceState.INACTIVE
                )
                message = (
                    "SINR voice readout active."
                    if current.desired_active
                    else "SINR voice readout stopped."
                )
            else:
                state = SINRVoiceState.FAILED
                message = detail or "SINR voice acknowledgement did not match the request."
            self._snapshot = NGUISINRVoiceSnapshot(
                sequence=current.sequence,
                state=state,
                active=bool(active),
                desired_active=bool(active),
                updated_at=float(now),
                message=message,
            )
            return True

    def fail_request(self, *, sequence: int, now: float, error: str) -> bool:
        with self._lock:
            current = self._snapshot
        return self.acknowledge(
            sequence=sequence,
            active=current.active,
            now=now,
            error=str(error or "SINR voice request failed"),
        )

    def fail_active(self, *, sequence: int, now: float, error: str) -> bool:
        """Publish an Engine failure after speech started successfully."""

        if now <= 0:
            raise ValueError("SINR voice failure timestamp must be positive")
        detail = str(error or "").strip() or "SINR voice engine failed"
        with self._lock:
            current = self._snapshot
            if (
                current.state is not SINRVoiceState.ACTIVE
                or int(sequence) != current.sequence
            ):
                return False
            self._snapshot = NGUISINRVoiceSnapshot(
                sequence=current.sequence,
                state=SINRVoiceState.FAILED,
                active=False,
                desired_active=False,
                updated_at=float(now),
                message=detail,
            )
            return True

    def expire(self, *, now: float) -> bool:
        with self._lock:
            current = self._snapshot
            expired = (
                current.state is SINRVoiceState.PENDING
                and float(now) - current.updated_at >= self.REQUEST_TIMEOUT_S
            )
            if not expired:
                return False
            self._snapshot = NGUISINRVoiceSnapshot(
                sequence=current.sequence,
                state=SINRVoiceState.FAILED,
                active=current.active,
                desired_active=current.active,
                updated_at=float(now),
                message="SINR voice request timed out.",
            )
            return True

    def snapshot(self) -> NGUISINRVoiceSnapshot:
        with self._lock:
            return self._snapshot

    def reset_runtime_state(
        self,
    ) -> tuple[NGUISINRVoiceSnapshot, NGUISINRVoiceSnapshot]:
        with self._lock:
            prior = self._snapshot
            self._snapshot = NGUISINRVoiceSnapshot(
                sequence=0,
                state=SINRVoiceState.INACTIVE,
                active=False,
                desired_active=False,
                updated_at=0.0,
            )
            return prior, self._snapshot


__all__ = (
    "NGUISINRVoiceLifecycle",
    "NGUISINRVoiceRequest",
    "NGUISINRVoiceSnapshot",
    "SINRVoiceState",
    "SINR_VOICE_LIFECYCLE_OWNER",
    "SINR_VOICE_LIFECYCLE_SCHEMA",
)
