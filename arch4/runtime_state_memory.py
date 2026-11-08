"""Runtime state-memory owner.

This module owns the live current/recent runtime frame surface.  JSON exports
such as current_state.json may persist this data, but they are not the live
authority for in-process consumers.
"""

from __future__ import annotations

import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

from arch4.coerce import safe_bool, safe_float, safe_int

STATE_MEMORY_SCHEMA_VERSION = "runtime_state_memory.v1"
STATE_MEMORY_OWNER = "state_memory"
STATE_MEMORY_DEFAULT_MAX_RECENT = 180

_RADIO_KEYS = (
    "backend",
    "provider_name",
    "provider_plmn",
    "radio_connected",
    "serving_band",
    "serving_band_signature",
    "active_band_set",
    "active_band_set_signature",
    "configured_band_pool",
    "configured_band_pool_signature",
    "cell_id",
    "enb_id",
    "pci",
    "earfcn_dl",
    "earfcn",
    "rsrp_dbm",
    "rsrq_db",
    "sinr_db",
    "rssi_dbm",
    "ri",
    "ri_valid",
)

_TRAFFIC_KEYS = (
    "has_traffic",
    "traffic_active",
    "dl_mbps",
    "ul_mbps",
    "rx_bytes_per_sec",
    "tx_bytes_per_sec",
    "traffic_valid",
    "packet_session_connected",
)

_LATENCY_KEYS = (
    "rtt_ms",
    "rtt_sliding_avg",
    "rtt_baseline",
    "rtt_effective_baseline",
    "rtt_delta",
    "rtt_delta_valid",
    "rtt_dl_jitter_pct",
    "rtt_dl_jitter_valid",
)

_QUALITY_KEYS = (
    "cq_cell",
    "cq_cell_valid",
    "cq_cell_display",
    "cq_cell_display_valid",
    "congestion_pct",
    "congestion_pct_valid",
    "congestion_display_pct",
    "congestion_display_valid",
    "congestion_internal_bucket",
    "congestion_bucket",
    "congestion_rsrq_contributes",
    "congestion_rsrq_reason",
    "congestion_rsrq_pct",
)

_CA_KEYS = (
    "ca_active",
    "ca_signature",
    "ca_band_signature",
    "ca_state_text",
    "ca_source",
    "ca_component_band_set",
    "ca_component_band_signature",
    "ca_component_count",
    "cart",
    "cart_ld",
    "cart_ld_valid",
    "cast",
    "cast_ld",
    "cast_ld_valid",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _compact(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = _jsonable(value)
    return out


def _base_status(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    canonical = _mapping(payload.get("_canonical_status"))
    if canonical:
        return canonical
    base = _mapping(payload.get("_base_status"))
    return base if base else payload


def build_state_memory_frame(
    payload: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    sequence: int = 0,
    source: str = "",
) -> dict[str, Any]:
    """Build one bounded current/recent frame from the live status payload."""

    data = payload if isinstance(payload, Mapping) else {}
    base = _base_status(data)
    now_ts = float(time.time() if now is None else now)
    sample_ts = safe_float(data.get("sample_ts", base.get("sample_ts", base.get("ts", 0.0))), 0.0)
    sample_sequence = safe_int(data.get("sample_sequence", base.get("sample_sequence", 0)), 0)
    engine_tick_ts = safe_float(data.get("engine_tick_ts", data.get("ts", now_ts)), now_ts)
    sample_fresh = safe_bool(data.get("sample_fresh", base.get("sample_fresh", False)), False)
    radio = _compact(base, _RADIO_KEYS)
    traffic = _compact(base, _TRAFFIC_KEYS)
    latency = _compact(base, _LATENCY_KEYS)
    quality = _compact(base, _QUALITY_KEYS)
    ca = _compact(base, _CA_KEYS)
    mutation = _mapping(data.get("radio_mutation_window"))
    action = _mapping(data.get("mutating_action_transaction"))
    frame = {
        "schema_version": STATE_MEMORY_SCHEMA_VERSION,
        "owner": STATE_MEMORY_OWNER,
        "sequence": int(sequence),
        "ts": now_ts,
        "engine_tick_ts": engine_tick_ts,
        "sample_ts": sample_ts,
        "sample_sequence": sample_sequence,
        "sample_fresh": sample_fresh,
        "state": str(data.get("state") or base.get("state") or ""),
        "backend": str(data.get("backend") or base.get("backend") or ""),
        "session_scope_key": str(data.get("session_scope_key") or base.get("session_scope_key") or ""),
        "source": str(source or ""),
        "radio": radio,
        "traffic": traffic,
        "latency": latency,
        "quality": quality,
        "ca": ca,
        "mutation": _jsonable(mutation),
        "action": _jsonable(action),
    }
    return {key: value for key, value in frame.items() if value not in (None, "", [], {})}


@dataclass
class RuntimeStateMemory:
    max_recent: int = STATE_MEMORY_DEFAULT_MAX_RECENT
    _frames: deque[dict[str, Any]] = field(init=False)
    _sequence: int = 0

    def __post_init__(self) -> None:
        self.max_recent = max(1, int(self.max_recent or STATE_MEMORY_DEFAULT_MAX_RECENT))
        self._frames = deque(maxlen=self.max_recent)

    def publish(
        self,
        payload: Mapping[str, Any] | None,
        *,
        now: float | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        next_sequence = int(self._sequence + 1)
        frame = build_state_memory_frame(
            payload,
            now=now,
            sequence=next_sequence,
            source=source,
        )
        if self._frames and _same_sample_frame(self._frames[-1], frame):
            frame["sequence"] = int(self._frames[-1].get("sequence") or self._sequence)
            self._frames[-1] = frame
        else:
            self._sequence = next_sequence
            self._frames.append(frame)
        return self.snapshot()

    def current(self) -> dict[str, Any]:
        if not self._frames:
            return {}
        return deepcopy(self._frames[-1])

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        count = max(0, int(limit or 0))
        if count <= 0:
            return []
        return deepcopy(list(self._frames)[-count:])

    def snapshot(self, *, recent_limit: int = 8) -> dict[str, Any]:
        current = self.current()
        updated_at = safe_float(current.get("ts"), 0.0) if current else 0.0
        return {
            "schema_version": STATE_MEMORY_SCHEMA_VERSION,
            "owner": STATE_MEMORY_OWNER,
            "updated_at": updated_at,
            "sequence": int(self._sequence),
            "current": current,
            "recent": self.recent(recent_limit),
            "recent_count": len(self._frames),
            "max_recent": int(self.max_recent),
        }


def get_engine_state_memory(engine: Any) -> RuntimeStateMemory:
    memory = getattr(engine, "_runtime_state_memory", None)
    if isinstance(memory, RuntimeStateMemory):
        return memory
    memory = RuntimeStateMemory()
    setattr(engine, "_runtime_state_memory", memory)
    return memory


def publish_engine_state_memory(
    engine: Any,
    payload: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    source: str = "",
) -> dict[str, Any]:
    return get_engine_state_memory(engine).publish(payload, now=now, source=source)


def build_state_memory_snapshot_from_status(
    payload: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    source: str = "",
) -> dict[str, Any]:
    frame = build_state_memory_frame(payload, now=now, sequence=1, source=source)
    return {
        "schema_version": STATE_MEMORY_SCHEMA_VERSION,
        "owner": STATE_MEMORY_OWNER,
        "updated_at": safe_float(frame.get("ts"), 0.0),
        "sequence": 1 if frame else 0,
        "current": frame,
        "recent": [deepcopy(frame)] if frame else [],
        "recent_count": 1 if frame else 0,
        "max_recent": 1,
    }


def _same_sample_frame(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    previous_seq = safe_int(previous.get("sample_sequence"), 0)
    current_seq = safe_int(current.get("sample_sequence"), 0)
    if previous_seq > 0 and current_seq > 0:
        return previous_seq == current_seq
    previous_ts = safe_float(previous.get("sample_ts"), 0.0)
    current_ts = safe_float(current.get("sample_ts"), 0.0)
    return previous_ts > 0.0 and current_ts > 0.0 and abs(previous_ts - current_ts) < 0.001
