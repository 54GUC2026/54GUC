"""
Canonical compact forensic telemetry codec.

This module owns the compact forensic stream format.  The stream is a durable
derived surface for support/replay work; live GUI/runtime consumers must keep
using registers/current-state surfaces and must not read this file as a fresh
data bus.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Mapping

COMPACT_FORENSIC_OWNER = "arch4.telemetry.forensic_codec"
COMPACT_FORENSIC_SCHEMA_VERSION = "arch_forensic_compact_v1"
COMPACT_FORENSIC_BASENAME = "forensic_compact"
COMPACT_FORENSIC_FILE_PATTERN = f"{COMPACT_FORENSIC_BASENAME}_*.jsonl"

COMPACT_FORENSIC_RETENTION_DAYS = 14
COMPACT_FORENSIC_MAX_FILE_BYTES = 4 * 1024 * 1024
COMPACT_FORENSIC_MAX_RECORD_BYTES = 16 * 1024
COMPACT_FORENSIC_WRITE_PRESSURE_WARN_BYTES_PER_MIN = 256 * 1024

_ENVELOPE_KEYS = frozenset({"record_type", "ts", "iso", "tick"})
_EVENT_ENVELOPE_KEYS = frozenset({"ts", "iso", "tick", "seq", "kind", "event", "schema_version"})

TICK_FIELD_ORDER: tuple[str, ...] = (
    "engine_tick_ts",
    "sample_ts",
    "sample_fresh",
    "sample_sequence",
    "sample_age_s",
    "modem_sample_interval_s",
    "modem_sample_expected_interval_s",
    "modem_sample_slo_exceeded",
    "register_intake_interval_s",
    "register_intake_expected_interval_s",
    "register_intake_slo_exceeded",
    "register_intake_sequence_gap",
    "engine_intake_mode",
    "engine_intake_missing_samples",
    "engine_intake_missing_total",
    "provider_name",
    "provider_plmn",
    "model",
    "firmware",
    "radio_connected",
    "radio_state_text",
    "state",
    "sinr_db",
    "rsrp_dbm",
    "rsrq_db",
    "rssi_dbm",
    "rtt_ms",
    "rtt_delta",
    "rtt_delta_valid",
    "rtt_delta_display_ms",
    "rtt_delta_display_valid",
    "rtt_dl_jitter_pct",
    "rtt_dl_jitter_display_valid",
    "cq_cell_display",
    "cq_cell_display_valid",
    "congestion_pct",
    "congestion_pct_valid",
    "congestion_display_pct",
    "congestion_display_valid",
    "congestion_bucket",
    "congestion_rsrq_contributes",
    "congestion_rsrq_reason",
    "congestion_rsrq_gap_threshold_db",
    "ca_active",
    "cart",
    "cart_ld",
    "cart_ld_valid",
    "cast",
    "cast_ld",
    "cast_ld_valid",
    "serving_band",
    "primary_band",
    "serving_band_signature",
    "configured_band_pool_signature",
    "active_band_set_signature",
    "selected_band_set_signature",
    "ca_state_text",
    "pci",
    "earfcn",
    "cell_id",
    "tac",
    "rrc",
    "trans_mode",
    "network_type",
    "dl_mbps",
    "ul_mbps",
    "has_traffic",
    "traffic_accounting_source",
    "delivered_capacity_valid",
    "delivered_capacity_ratio",
    "dl_bottleneck_state",
    "dl_bottleneck_reason",
    "dl_bottleneck_proof_level",
    "action_lever",
    "action_reason",
    "action_text",
)

EVENT_EVIDENCE_KEYS: tuple[str, ...] = (
    "level",
    "code",
    "message",
    "owner",
    "source",
    "stage",
    "status",
    "lever",
    "params",
    "reason",
    "intent",
    "objective_id",
    "action_transaction_key",
    "action_root_id",
    "transaction_root_id",
    "transaction_id",
    "action_transaction",
    "decision_trace_id",
    "command_id",
    "proof_id",
    "action_executed",
    "success",
    "error",
    "details",
    "receipt",
    "old_state",
    "new_state",
    "change_kind",
    "band",
    "pci",
    "earfcn",
    "ca_sig",
    "bands",
    "pband",
    "ca_pool",
    "dl_bands",
    "target_band",
    "target_bands",
    "action_origin",
    "operator_initiated",
    "interaction_family",
    "initiator_attribution",
    "human_presence_proof",
    "request_source",
    "gui_action_id",
    "gui_action_source",
    "dispatch_latency_s",
    "execution_duration_s",
    "total_latency_s",
    "proof_opened_ts",
    "objective_hold_until_ts",
    "action_suppressed",
    "capture_id",
    "capture_reasons",
    "health_stage",
    "incident_class",
    "recommended_next_step",
)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _truncate_payload(value: Any, *, max_bytes: int) -> Any:
    text = _json_dumps({"v": _jsonable(value)})
    if len(text.encode("utf-8")) <= max_bytes:
        return _jsonable(value)
    preview = text[: max(0, min(len(text), max_bytes - 128))]
    return {
        "_truncated": True,
        "owner": COMPACT_FORENSIC_OWNER,
        "original_bytes": len(text.encode("utf-8", errors="replace")),
        "preview": preview,
    }


def _field_index() -> dict[str, int]:
    return {name: idx for idx, name in enumerate(TICK_FIELD_ORDER)}


class CompactForensicCodec:
    """Stateful encoder/decoder for compact forensic records."""

    def __init__(self) -> None:
        self._tick_state: dict[str, Any] = {}
        self._decode_tick_state: dict[str, Any] = {}
        self._fields: tuple[str, ...] = TICK_FIELD_ORDER

    @staticmethod
    def header_record(*, created_ts: float | None = None) -> dict[str, Any]:
        now = float(created_ts if created_ts is not None else time.time())
        return {
            "v": COMPACT_FORENSIC_SCHEMA_VERSION,
            "t": "schema",
            "owner": COMPACT_FORENSIC_OWNER,
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "fields": list(TICK_FIELD_ORDER),
            "rotation": {
                "max_file_bytes": COMPACT_FORENSIC_MAX_FILE_BYTES,
                "retention_days": COMPACT_FORENSIC_RETENTION_DAYS,
            },
            "write_pressure": {
                "warn_bytes_per_min": COMPACT_FORENSIC_WRITE_PRESSURE_WARN_BYTES_PER_MIN,
                "max_record_bytes": COMPACT_FORENSIC_MAX_RECORD_BYTES,
            },
        }

    @staticmethod
    def session_record(
        session: Mapping[str, Any],
        *,
        session_seq: int,
        ts: float | None = None,
    ) -> dict[str, Any]:
        now = float(ts if ts is not None else time.time())
        return {
            "v": COMPACT_FORENSIC_SCHEMA_VERSION,
            "t": "session",
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "seq": int(session_seq),
            "d": _jsonable(dict(session or {})),
        }

    def encode_tick(self, row: Mapping[str, Any]) -> dict[str, Any]:
        indices = _field_index()
        changed: dict[str, Any] = {}
        for field in self._fields:
            if field not in row:
                continue
            value = _jsonable(row.get(field))
            if self._tick_state.get(field) != value:
                self._tick_state[field] = value
                changed[str(indices[field])] = value
        return {
            "v": COMPACT_FORENSIC_SCHEMA_VERSION,
            "t": "tick",
            "ts": float(row.get("ts") or time.time()),
            "iso": str(row.get("iso") or ""),
            "n": int(row.get("tick") or 0),
            "d": changed,
        }

    def encode_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(event.get("kind") or event.get("event") or "")
        payload: dict[str, Any] = {}
        for key in EVENT_EVIDENCE_KEYS:
            if key in event:
                payload[key] = _jsonable(event.get(key))
        retained = _truncate_payload(
            payload,
            max_bytes=COMPACT_FORENSIC_MAX_RECORD_BYTES // 2,
        )
        if isinstance(retained, dict) and retained.get("_truncated") is True:
            for key in (
                "action_root_id",
                "transaction_root_id",
                "transaction_id",
                "command_id",
                "proof_id",
                "decision_trace_id",
                "action_executed",
            ):
                if key in payload:
                    retained[key] = payload[key]
        return {
            "v": COMPACT_FORENSIC_SCHEMA_VERSION,
            "t": "event",
            "ts": float(event.get("ts") or time.time()),
            "iso": str(event.get("iso") or ""),
            "tick": int(event.get("tick") or 0),
            "seq": int(event.get("seq") or 0),
            "k": kind,
            "d": retained,
        }

    def decode_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        record_type = str(record.get("t") or "")
        if record_type == "schema":
            self._decode_tick_state = {}
            fields = tuple(str(item) for item in record.get("fields", ()) if str(item))
            if fields:
                self._fields = fields
            return {
                "record_type": "compact_schema",
                "schema_version": str(record.get("v") or ""),
                "owner": str(record.get("owner") or ""),
                "fields": list(self._fields),
                "rotation": dict(record.get("rotation") or {}),
                "write_pressure": dict(record.get("write_pressure") or {}),
                "ts": record.get("ts"),
                "iso": record.get("iso"),
            }
        if record_type == "session":
            data = dict(record.get("d") or {})
            data.update(
                {
                    "record_type": "session_context",
                    "schema_version": str(record.get("v") or ""),
                    "ts": record.get("ts"),
                    "iso": record.get("iso"),
                    "session_seq": int(record.get("seq") or data.get("session_seq") or 0),
                }
            )
            return data
        if record_type == "event":
            kind = str(record.get("k") or "")
            payload = dict(record.get("d") or {})
            return {
                "record_type": "event",
                "ts": record.get("ts"),
                "iso": record.get("iso"),
                "tick": int(record.get("tick") or 0),
                "seq": int(record.get("seq") or 0),
                "kind": kind,
                "event": kind,
                "schema_version": "arch_event_v2",
                **payload,
            }
        if record_type == "tick":
            delta = record.get("d") if isinstance(record.get("d"), Mapping) else {}
            for raw_idx, value in delta.items():
                try:
                    field = self._fields[int(raw_idx)]
                except Exception:
                    continue
                self._decode_tick_state[field] = value
            out = dict(self._decode_tick_state)
            out.update(
                {
                    "record_type": "tick",
                    "ts": record.get("ts"),
                    "iso": record.get("iso"),
                    "tick": int(record.get("n") or 0),
                }
            )
            return out
        return {
            "record_type": "unknown_compact_record",
            "schema_version": str(record.get("v") or ""),
            "raw_type": record_type,
        }


class CompactForensicStreamWriter:
    """Append-only compact forensic writer with explicit rotation limits."""

    def __init__(
        self,
        output_dir: Path,
        *,
        tag: str,
        max_file_bytes: int = COMPACT_FORENSIC_MAX_FILE_BYTES,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.tag = str(tag or dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.max_file_bytes = max(64 * 1024, int(max_file_bytes or COMPACT_FORENSIC_MAX_FILE_BYTES))
        self.codec = CompactForensicCodec()
        self.path: Path | None = None
        self._file: Any = None
        self._part = 0
        self._bytes_written = 0
        self._records_written = 0
        self._rotations = 0
        self._last_session_signature = ""
        self._last_session: dict[str, Any] = {}
        self._open_new_file()

    def _file_path(self) -> Path:
        return self.output_dir / f"{COMPACT_FORENSIC_BASENAME}_{self.tag}_{self._part:03d}.jsonl"

    def _open_new_file(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self._file_path()
        self._file = self.path.open("a", buffering=1, encoding="utf-8")
        self._bytes_written = self.path.stat().st_size if self.path.exists() else 0
        if self._bytes_written == 0:
            self._write_record(self.codec.header_record(), allow_rotate=False)

    def _rotate(self, *, replay_session: bool = True) -> None:
        if self._file is not None:
            self._file.close()
        self._part += 1
        self._rotations += 1
        # Delta state belongs to one part only.  Replacing the codec makes the
        # next tick a complete keyframe instead of depending on the prior file.
        self.codec = CompactForensicCodec()
        self._open_new_file()
        if replay_session and self._last_session:
            self._write_record(
                self.codec.session_record(
                    self._last_session,
                    session_seq=int(self._last_session.get("session_seq") or 0),
                    ts=float(self._last_session.get("ts") or time.time()),
                ),
                allow_rotate=False,
            )

    def _write_record(
        self,
        record: Mapping[str, Any],
        *,
        allow_rotate: bool = True,
        replay_session_on_rotate: bool = True,
        rebuild_after_rotate: Any = None,
    ) -> None:
        payload = _json_dumps(dict(record))
        encoded = (payload + "\n").encode("utf-8", errors="replace")
        if len(encoded) > COMPACT_FORENSIC_MAX_RECORD_BYTES:
            record = {
                "v": COMPACT_FORENSIC_SCHEMA_VERSION,
                "t": "truncated",
                "owner": COMPACT_FORENSIC_OWNER,
                "reason": "record_exceeded_compact_budget",
                "original_bytes": len(encoded),
                "ts": time.time(),
            }
            payload = _json_dumps(record)
            encoded = (payload + "\n").encode("utf-8", errors="replace")
        if allow_rotate and self._bytes_written and self._bytes_written + len(encoded) > self.max_file_bytes:
            self._rotate(replay_session=replay_session_on_rotate)
            if rebuild_after_rotate is not None:
                record = rebuild_after_rotate()
                payload = _json_dumps(dict(record))
                encoded = (payload + "\n").encode("utf-8", errors="replace")
        if self._file is None:
            self._open_new_file()
        self._file.write(payload + "\n")
        self._bytes_written += len(encoded)
        self._records_written += 1

    def write_session(self, session: Mapping[str, Any]) -> None:
        signature = _json_dumps(dict(session or {}))
        if signature == self._last_session_signature:
            return
        self._last_session_signature = signature
        self._last_session = dict(session or {})
        self._write_record(
            self.codec.session_record(
                session,
                session_seq=int(session.get("session_seq") or 0),
                ts=float(session.get("ts") or time.time()),
            ),
            replay_session_on_rotate=False,
        )

    def write_tick(self, row: Mapping[str, Any]) -> None:
        self._write_record(
            self.codec.encode_tick(row),
            rebuild_after_rotate=lambda: self.codec.encode_tick(row),
        )

    def write_event(self, event: Mapping[str, Any]) -> None:
        self._write_record(self.codec.encode_event(event))

    def health_status(self) -> dict[str, Any]:
        return {
            "schema_version": COMPACT_FORENSIC_SCHEMA_VERSION,
            "owner": COMPACT_FORENSIC_OWNER,
            "path": str(self.path or ""),
            "records_written": self._records_written,
            "bytes_written_current_file": self._bytes_written,
            "rotations": self._rotations,
            "max_file_bytes": self.max_file_bytes,
            "retention_days": COMPACT_FORENSIC_RETENTION_DAYS,
            "warn_bytes_per_min": COMPACT_FORENSIC_WRITE_PRESSURE_WARN_BYTES_PER_MIN,
        }

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
