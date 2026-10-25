"""Bounded immutable 50/100 ms wake stream for Core customers."""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Any

from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core_contracts.clock import CoreClockWake, CoreClockWakeRecord
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.payload import CoreFrozenPayload


CORE_CLOCK_WAKE_STREAM_OWNER = "54guc.core.clock_wake_stream"
_HOOK_BY_LANE = {
    "50ms": "clock_wake_50ms",
    "100ms": "clock_wake_100ms",
}


class CoreClockWakeStream:
    """Own wake sequences and client cursors; never execute customer code."""

    def __init__(
        self,
        hook_registry: CoreExtensionHookRegistry,
        *,
        max_records_per_lane: int = 128,
    ) -> None:
        if not isinstance(hook_registry, CoreExtensionHookRegistry):
            raise TypeError("core_clock_wake_stream_hook_registry_required")
        retained = int(max_records_per_lane)
        if retained < 8:
            raise ValueError("core_clock_wake_stream_minimum_retention_required")
        self._hooks = hook_registry
        self._max_records_per_lane = retained
        self._lock = RLock()
        self._records: dict[str, OrderedDict[str, CoreClockWakeRecord]] = {
            lane: OrderedDict() for lane in _HOOK_BY_LANE
        }
        self._sequences = {lane: 0 for lane in _HOOK_BY_LANE}
        self._inflight: dict[tuple[str, str], str] = {}
        self._acked: dict[tuple[str, str], str] = {}
        self._acked_sequences: dict[tuple[str, str], int] = {}
        self._evicted_total = {lane: 0 for lane in _HOOK_BY_LANE}
        self._client_lag_loss: dict[tuple[str, str], int] = {}

    @staticmethod
    def _lane(value: str) -> str:
        lane = str(value or "").strip()
        if lane not in _HOOK_BY_LANE:
            raise ValueError(f"core_clock_wake_stream_lane_invalid:{lane or 'missing'}")
        return lane

    @staticmethod
    def _fingerprint(wake: CoreClockWake) -> str:
        return CoreFrozenPayload.from_mapping(wake.to_payload()).sha256

    def emit(
        self,
        *,
        lane: str,
        emitted_at: float,
        due_at: float,
    ) -> CoreClockWakeRecord:
        lane_name = self._lane(lane)
        with self._lock:
            sequence = self._sequences[lane_name] + 1
            wake = CoreClockWake(
                sequence=sequence,
                lane=lane_name,
                emitted_at=float(emitted_at),
                due_at=float(due_at),
            )
            key = f"{lane_name}:{sequence}"
            record = CoreClockWakeRecord(
                key=key,
                wake=wake,
                fingerprint=self._fingerprint(wake),
            )
            self._records[lane_name][key] = record
            self._sequences[lane_name] = sequence
            self._trim_locked(lane_name)
            return record

    def _trim_locked(self, lane: str) -> None:
        records = self._records[lane]
        while len(records) > self._max_records_per_lane:
            key, record = records.popitem(last=False)
            self._evicted_total[lane] += 1
            for cursor_key, inflight_key in tuple(self._inflight.items()):
                if cursor_key[1] == lane and inflight_key == key:
                    self._inflight.pop(cursor_key, None)
                    self._acked_sequences[cursor_key] = max(
                        self._acked_sequences.get(cursor_key, 0),
                        int(record.wake.sequence),
                    )
                    self._client_lag_loss[cursor_key] = (
                        self._client_lag_loss.get(cursor_key, 0) + 1
                    )

    def claim_next(
        self,
        client_id: str,
        *,
        lane: str,
    ) -> CoreClockWakeRecord | None:
        identity = str(client_id or "").strip()
        lane_name = self._lane(lane)
        self._hooks.require_attachment(
            identity,
            hook_id=_HOOK_BY_LANE[lane_name],
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        cursor_key = (identity, lane_name)
        with self._lock:
            if cursor_key in self._inflight:
                return None
            records = self._records[lane_name]
            if not records:
                return None
            acknowledged_sequence = self._acked_sequences.get(cursor_key, 0)
            first_sequence = int(next(iter(records.values())).wake.sequence)
            if first_sequence > acknowledged_sequence + 1:
                lost = first_sequence - acknowledged_sequence - 1
                self._client_lag_loss[cursor_key] = (
                    self._client_lag_loss.get(cursor_key, 0) + lost
                )
                acknowledged_sequence = first_sequence - 1
                self._acked_sequences[cursor_key] = acknowledged_sequence
            for key, record in records.items():
                if int(record.wake.sequence) <= acknowledged_sequence:
                    continue
                self._inflight[cursor_key] = key
                return record
            return None

    def acknowledge(self, client_id: str, *, lane: str, key: str) -> None:
        identity = str(client_id or "").strip()
        lane_name = self._lane(lane)
        self._hooks.require_attachment(
            identity,
            hook_id=_HOOK_BY_LANE[lane_name],
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        cursor_key = (identity, lane_name)
        expected = str(key or "").strip()
        with self._lock:
            if self._inflight.get(cursor_key) != expected:
                raise RuntimeError("core_clock_wake_stream_ack_mismatch")
            if expected not in self._records[lane_name]:
                raise RuntimeError("core_clock_wake_stream_ack_record_missing")
            sequence = int(self._records[lane_name][expected].wake.sequence)
            self._inflight.pop(cursor_key, None)
            self._acked[cursor_key] = expected
            self._acked_sequences[cursor_key] = sequence

    def release(
        self,
        client_id: str,
        *,
        lane: str,
        key: str,
        error: BaseException,
    ) -> None:
        identity = str(client_id or "").strip()
        lane_name = self._lane(lane)
        expected = str(key or "").strip()
        if not isinstance(error, BaseException):
            raise TypeError("core_clock_wake_stream_release_exception_required")
        cursor_key = (identity, lane_name)
        with self._lock:
            if self._inflight.get(cursor_key) != expected:
                raise RuntimeError("core_clock_wake_stream_release_mismatch")
            self._inflight.pop(cursor_key, None)
        self._hooks.mark_fault(
            identity,
            hook_id=_HOOK_BY_LANE[lane_name],
            error=error,
        )

    def latest(self, lane: str) -> CoreClockWakeRecord | None:
        lane_name = self._lane(lane)
        with self._lock:
            return next(reversed(self._records[lane_name].values()), None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "CoreClockWakeStreamStatus.v1",
                "owner": CORE_CLOCK_WAKE_STREAM_OWNER,
                "max_records_per_lane": self._max_records_per_lane,
                "lanes": {
                    lane: {
                        "sequence": self._sequences[lane],
                        "records": len(self._records[lane]),
                        "evicted_total": self._evicted_total[lane],
                        "latest_key": (
                            next(reversed(self._records[lane]), "")
                        ),
                    }
                    for lane in _HOOK_BY_LANE
                },
                "inflight": {
                    f"{client_id}:{lane}": key
                    for (client_id, lane), key in self._inflight.items()
                },
                "acked": {
                    f"{client_id}:{lane}": {
                        "key": key,
                        "sequence": self._acked_sequences.get(
                            (client_id, lane),
                            0,
                        ),
                    }
                    for (client_id, lane), key in self._acked.items()
                },
                "client_lag_loss": {
                    f"{client_id}:{lane}": count
                    for (client_id, lane), count in self._client_lag_loss.items()
                },
            }


def require_core_clock_wake_stream(port: Any) -> CoreClockWakeStream:
    stream = getattr(port, "clock_wake_stream", None)
    if not isinstance(stream, CoreClockWakeStream):
        raise RuntimeError("core_clock_wake_stream_unavailable")
    return stream


__all__ = (
    "CORE_CLOCK_WAKE_STREAM_OWNER",
    "CoreClockWakeRecord",
    "CoreClockWakeStream",
    "require_core_clock_wake_stream",
)
