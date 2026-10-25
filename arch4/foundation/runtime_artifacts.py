"""Runtime artifacts for Core, NGUI, and Foundation. This module owns current_state.json
and the bounded cgf_evidence.sqlite3 journal. Both writers are fail-soft: storage errors
are reported but never stop modem observation, GUI updates, or commands.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.command_transactions import CommandTerminalReceipt
from arch4.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    cleanup_abandoned_atomic_temps,
)
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_OWNER,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
    CoreRuntimeSnapshot,
    CoreRuntimeSnapshotPort,
)
from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFacts,
)
from arch4.core_contracts.current_health import CoreCurrentHealthView
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.foundation.diagnostic_payload import decode_payload, encode_payload
from arch4.log_processor import (
    CRITICAL_LANE,
    MemoryFirstLogProcessor,
    MemoryLogRecord,
)
from arch4.privacy_scrub import scrub_record, scrub_text
from arch4.sqlite_recovery import quarantine_corrupt_sqlite
from arch4.runtime_register_contract_names import (
    REGISTER_CA,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
)


CURRENT_STATE_FILENAME = "current_state.json"
CGF_EVIDENCE_FILENAME = "cgf_evidence.sqlite3"
RUNTIME_HEALTH_FILENAME = "runtime_health.json"
SHUTDOWN_RECEIPT_FILENAME = "shutdown_receipt.json"

CURRENT_STATE_SCHEMA = "FoundationCoreCurrentState.v3"
CURRENT_STATE_OWNER = "arch4.foundation.core_current_state_publisher"
EVENT_SCHEMA = "FoundationDiagnosticEvent.v1"
EVENT_OWNER = "arch4.foundation.cgf_evidence_store"
RUNTIME_HEALTH_SCHEMA = "FoundationRuntimeHealth.v1"
RUNTIME_HEALTH_OWNER = "arch4.foundation.runtime_health_publisher"
SHUTDOWN_RECEIPT_SCHEMA = "NGUIShutdownReceipt.v1"
SHUTDOWN_RECEIPT_OWNER = "arch4.ngui.orchestrator"


def publish_shutdown_receipt_artifact(
    storage_root: str | Path,
    receipt: Mapping[str, Any],
) -> Path:
    """Atomically publish the final parent-owned product shutdown truth."""

    if not isinstance(receipt, Mapping):
        raise TypeError("ngui_shutdown_receipt_mapping_required")
    record = dict(receipt)
    if record.get("schema") != SHUTDOWN_RECEIPT_SCHEMA:
        raise ValueError("ngui_shutdown_receipt_schema_required")
    if record.get("owner") != SHUTDOWN_RECEIPT_OWNER:
        raise ValueError("ngui_shutdown_receipt_owner_required")
    if record.get("status") not in {"complete", "degraded"}:
        raise ValueError("ngui_shutdown_receipt_terminal_status_required")
    if float(record.get("finished_at") or 0.0) <= 0.0:
        raise ValueError("ngui_shutdown_receipt_finished_at_required")
    serializable = _json_value(record)
    if not isinstance(serializable, dict):
        raise TypeError("ngui_shutdown_receipt_json_object_required")
    target = Path(storage_root) / SHUTDOWN_RECEIPT_FILENAME
    atomic_write_json(
        target,
        serializable,
        sort_keys=True,
        trailing_newline=True,
    )
    return target

_RUNTIME_HEALTH_VOLATILE_KEYS = frozenset(
    {
        "active_execution_count",
        "age_s",
        "attempted",
        "cycle_count",
        "last_attempt_ts",
        "last_sequence",
        "last_success_at",
        "last_success_ts",
        "next_retry_at",
        "published_at",
        "registered_at",
        "started_at",
        "success_count",
        "successes",
        "updated_at",
    }
)


def _runtime_health_meaning(value: Any) -> Any:
    """Remove progress-only churn from the durable health signature."""

    if isinstance(value, Mapping):
        return {
            str(key): _runtime_health_meaning(item)
            for key, item in value.items()
            if str(key) not in _RUNTIME_HEALTH_VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_runtime_health_meaning(item) for item in value]
    return value

EVENT_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "sequence",
        "ts",
        "kind",
        "generation",
        "transaction_id",
        "event_owner",
        "severity",
        "operation",
        "detail",
        "degraded_behavior",
        "data",
    }
)
EVENT_TEXT_FIELD_LIMITS = MappingProxyType(
    {
        "kind": 128,
        "transaction_id": 512,
        "event_owner": 512,
        "severity": 16,
        "operation": 512,
        "detail": 16 * 1024,
        "degraded_behavior": 2048,
    }
)
EVENT_SEVERITIES = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_DEFAULT_MAX_EVENT_RECORDS = 512
MAX_EVENT_RECORD_BYTES = 512 * 1024
_DEFAULT_MAX_EVENT_BYTES = MAX_EVENT_RECORD_BYTES
_EVENT_DATABASE_VERSION = 2
_EVENT_REFERENCE_BYTES = 64
CURRENT_STATE_MAX_BYTES = 4 * 1024 * 1024
RUNTIME_HEALTH_MAX_BYTES = 1024 * 1024
_RUNTIME_SURFACE_MAX_AGE_S = 8.0
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "license_key",
    "private_key",
)

log = logging.getLogger("arch4.foundation.runtime_artifacts")


def foundation_diagnostic_event_record_errors(
    record: object,
    *,
    previous_sequence: int = 0,
    previous_timestamp: float = 0.0,
    record_bytes: int = 0,
) -> tuple[str, ...]:
    """Check one Foundation diagnostic record. The writer and file validator use this same
    contract; file traversal and line numbers belong to the validator.
    """

    if not isinstance(record, dict):
        return ("top_level_object_required",)
    errors: list[str] = []
    fields = set(record)
    missing = sorted(EVENT_REQUIRED_FIELDS - fields)
    extra = sorted(fields - EVENT_REQUIRED_FIELDS)
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if extra:
        errors.append(f"unexpected_fields:{','.join(extra)}")
    if record.get("schema") != EVENT_SCHEMA:
        errors.append("schema_invalid")
    if record.get("owner") != EVENT_OWNER:
        errors.append("owner_invalid")
    sequence = record.get("sequence")
    if type(sequence) is not int or sequence <= 0:
        errors.append("sequence_invalid")
    elif previous_sequence and sequence <= previous_sequence:
        errors.append("sequence_not_monotonic")
    timestamp = record.get("ts")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) <= 0.0
    ):
        errors.append("timestamp_invalid")
    generation = record.get("generation")
    if type(generation) is not int or generation < 0:
        errors.append("generation_invalid")
    for field, limit in EVENT_TEXT_FIELD_LIMITS.items():
        value = record.get(field)
        if not isinstance(value, str):
            errors.append(f"{field}_string_required")
        elif len(value) > limit:
            errors.append(f"{field}_length_exceeded")
    severity = record.get("severity")
    if isinstance(severity, str) and severity not in EVENT_SEVERITIES:
        errors.append("severity_invalid")
    kind = record.get("kind")
    if isinstance(kind, str) and not kind.strip():
        errors.append("kind_required")
    if not isinstance(record.get("data"), dict):
        errors.append("data_object_required")
    if record_bytes > MAX_EVENT_RECORD_BYTES:
        errors.append("record_byte_budget_exceeded")
    return tuple(errors)


def _redacted_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _json_value(value: Any) -> Any:
    """Freeze common runtime values into a strict, finite JSON value."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (Mapping, MappingProxyType)):
        return {
            str(key): "[REDACTED]" if _redacted_key(key) else _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, deque)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json_value(item) for item in sorted(value, key=str)]
    if isinstance(value, SimpleNamespace):
        return _json_value(vars(value))
    if is_dataclass(value):
        return _json_value(asdict(value))
    to_payload = getattr(value, "to_payload", None)
    if callable(to_payload):
        return _json_value(to_payload())
    raise TypeError(
        f"foundation_runtime_artifact_value_not_serializable:{type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class ArtifactWriterStatus:
    owner: str
    path: str
    attempted: int
    written: int
    failures: int
    last_sequence: int
    last_success_ts: float
    last_failure_ts: float
    last_error: str

    @property
    def healthy(self) -> bool:
        return self.failures == 0 or self.last_success_ts >= self.last_failure_ts

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "FoundationArtifactWriterStatus.v1",
            "owner": self.owner,
            "path": self.path,
            "healthy": self.healthy,
            "attempted": self.attempted,
            "written": self.written,
            "failures": self.failures,
            "last_sequence": self.last_sequence,
            "last_success_ts": self.last_success_ts,
            "last_failure_ts": self.last_failure_ts,
            "last_error": self.last_error,
        }


FaultReporter = Callable[[str, str, BaseException, str], None]

FOUNDATION_CURRENT_STATE_CLIENT_ID = "foundation.current_state"


def core_current_line_values(
    current_facts: CoreAcceptedCurrentFacts,
    line: str,
) -> dict[str, Any]:
    """Project one line without creating another current-truth owner."""

    if not isinstance(current_facts, CoreAcceptedCurrentFacts):
        raise TypeError("foundation_core_current_facts_required")
    if type(line) is not str or not line or line != line.strip():
        raise TypeError("foundation_core_current_line_invalid")
    values: dict[str, Any] = {}
    for fact in current_facts.facts:
        if fact.line != line or fact.disposition != "available":
            continue
        envelope = fact.value.to_mapping()
        if envelope.get("present") is not True:
            raise RuntimeError("foundation_core_current_fact_envelope_invalid")
        values[fact.field] = envelope["value"]
    return values


def core_current_line_metadata(
    current_facts: CoreAcceptedCurrentFacts,
    line: str,
) -> dict[str, Any]:
    """Project source metadata directly from Core-owned accepted facts."""

    if not isinstance(current_facts, CoreAcceptedCurrentFacts):
        raise TypeError("foundation_core_current_facts_required")
    if type(line) is not str or not line or line != line.strip():
        raise TypeError("foundation_core_current_line_invalid")
    facts = tuple(fact for fact in current_facts.facts if fact.line == line)
    if not facts:
        raise KeyError("foundation_core_current_line_not_declared:" + line)
    return {
        "schema": "CoreAcceptedCurrentLineProjection.v1",
        "owner": current_facts.owner,
        "name": line,
        "matrix_identity": current_facts.matrix_identity.as_dict(),
        "facts": {
            fact.field: {
                "disposition": fact.disposition,
                "reason": fact.reason,
                "sampled_at": fact.sampled_at,
                "acquired_at": fact.acquired_at,
                "line_sequence": fact.line_sequence,
                "source_sequence": fact.source_sequence,
                "source": fact.source,
            }
            for fact in facts
        },
    }


def foundation_runtime_mode(
    *,
    probe_mode: bool = False,
    observe_only: bool = False,
    check_only: bool = False,
) -> str:
    return (
        "PROBE"
        if probe_mode
        else "OBSERVE"
        if observe_only
        else "CHECK"
        if check_only
        else "LIVE"
    )


def foundation_write_authority(
    *,
    probe_mode: bool = False,
    observe_only: bool = False,
    check_only: bool = False,
) -> tuple[str, str]:
    if check_only:
        return "none_check_only", "Check-only mode does not allow write actions."
    if observe_only:
        return "none_observe_only", "Observe-only mode does not allow write actions."
    if probe_mode:
        return (
            "manual_safe_lane",
            "Probe/Test mode keeps only the tester-safe manual lane enabled.",
        )
    return "full", ""


def _runtime_status_phase(phase: str, *, cycle_count: int) -> str:
    normalized = str(phase or "").strip().lower()
    if normalized in {"init", "startup_begin", "startup_ok"}:
        return "starting"
    if normalized == "startup_blocked":
        return "startup_blocked"
    if normalized == "reconnecting":
        return "reconnecting"
    if normalized == "connection_lost":
        return "connection_lost"
    if normalized in {
        "cycle_started",
        "cycle_ok",
        "cycle_recoverable_fault",
        "cycle_code_fault",
        "modem_read_missed",
    }:
        return "running" if cycle_count > 0 else "starting"
    if normalized == "check_complete":
        return "ready"
    if normalized in {
        "fatal_cycle",
        "fatal_runner",
        "fatal_startup",
        "runtime_suspended",
    }:
        return "error"
    if normalized in {
        "shutdown_begin",
        "shutdown_complete",
        "shutdown_degraded",
    }:
        return "shutdown"
    return normalized or "unknown"


@dataclass(frozen=True, slots=True)
class FoundationRuntimeHealthSnapshot:
    phase: str
    observed_at: float
    process_id: int
    instance_id: str
    modem_backend: str
    runtime_mode: str
    write_authority: str
    write_block_reason: str
    cycle_count: int
    last_progress_at: float
    consecutive_failures: int
    last_error: str
    workers: CoreFrozenPayload
    faults: CoreFrozenPayload
    artifact_writers: CoreFrozenPayload
    incidents: CoreFrozenPayload
    degradation_reasons: tuple[str, ...]
    detail: CoreFrozenPayload
    schema: str = RUNTIME_HEALTH_SCHEMA
    owner: str = RUNTIME_HEALTH_OWNER

    def __post_init__(self) -> None:
        if self.schema != RUNTIME_HEALTH_SCHEMA:
            raise ValueError("foundation_runtime_health_schema_invalid")
        if self.owner != RUNTIME_HEALTH_OWNER:
            raise ValueError("foundation_runtime_health_owner_invalid")
        phase = str(self.phase or "").strip()
        if not phase:
            raise ValueError("foundation_runtime_health_phase_required")
        if float(self.observed_at) <= 0.0:
            raise ValueError("foundation_runtime_health_timestamp_required")
        if int(self.process_id) <= 0:
            raise ValueError("foundation_runtime_health_process_id_required")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "observed_at", float(self.observed_at))
        object.__setattr__(self, "process_id", int(self.process_id))
        object.__setattr__(self, "cycle_count", max(0, int(self.cycle_count)))
        object.__setattr__(
            self,
            "last_progress_at",
            max(0.0, float(self.last_progress_at)),
        )
        object.__setattr__(
            self,
            "consecutive_failures",
            max(0, int(self.consecutive_failures)),
        )
        for field_name in (
            "workers",
            "faults",
            "artifact_writers",
            "incidents",
            "detail",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"foundation_runtime_health_{field_name}_payload_required"
                )

    def to_payload(self, *, sequence: int) -> dict[str, Any]:
        status_phase = _runtime_status_phase(
            self.phase,
            cycle_count=self.cycle_count,
        )
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": int(sequence),
            "ts": self.observed_at,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.observed_at)),
            "pid": self.process_id,
            "instance_id": str(self.instance_id or ""),
            "modem_backend": str(self.modem_backend or ""),
            "runtime_mode": str(self.runtime_mode or ""),
            "write_authority": str(self.write_authority or ""),
            "write_block_reason": str(self.write_block_reason or ""),
            "phase": self.phase,
            "status_phase": status_phase,
            "cycle_count": self.cycle_count,
            "last_progress_ts": self.last_progress_at,
            "consecutive_failures": self.consecutive_failures,
            "last_error": str(self.last_error or ""),
            "degraded": bool(self.degradation_reasons),
            "degradation_reasons": list(self.degradation_reasons),
            "workers": self.workers.to_payload(),
            "faults": self.faults.to_payload(),
            "artifact_writers": self.artifact_writers.to_payload(),
            "incidents": self.incidents.to_payload(),
            "detail": self.detail.to_payload(),
        }


_DEGRADED_STATE_WORDS = frozenset(
    {"degraded", "down", "error", "failed", "fatal", "stalled", "wedge", "wedged"}
)


def _foundation_degradation_reasons(
    *,
    workers: Mapping[str, Any],
    faults: Mapping[str, Any],
    artifact_writers: Mapping[str, Any],
    incidents: Mapping[str, Any],
    consecutive_failures: int,
    last_error: str,
    terminal_clean: bool = False,
) -> tuple[str, ...]:
    """Derive current degradation from all live subordinate authorities."""

    reasons: set[str] = set()
    if consecutive_failures:
        reasons.add("foundation.consecutive_failures")
    if str(last_error or ""):
        reasons.add("foundation.last_error")

    def inspect(value: Any, path: str) -> None:
        if not isinstance(value, Mapping):
            return
        state = str(value.get("state") or "").strip().lower()
        clean_requested_stop = bool(
            terminal_clean
            and state == "stopped"
            and not str(value.get("last_error") or "")
            and not str(value.get("terminal_reason") or "")
        )
        clean_worker_summary = bool(
            terminal_clean
            and path == "workers"
            and not tuple(value.get("required_failed") or ())
            and set(value.get("required_unhealthy") or ())
            == set(value.get("required_stopped") or ())
        )
        if (
            value.get("healthy") is False
            and not clean_requested_stop
            and not clean_worker_summary
        ):
            reasons.add(f"{path}.healthy_false")
        if value.get("degraded") is True and not clean_worker_summary:
            reasons.add(f"{path}.degraded")
        for key in ("state", "status", "phase"):
            state_word = str(value.get(key) or "").strip().lower()
            if state_word in _DEGRADED_STATE_WORDS:
                reasons.add(f"{path}.{key}_{state_word}")
        writer_error = str(value.get("last_error") or "")
        if writer_error:
            try:
                failure_ts = float(value.get("last_failure_ts") or 0.0)
                success_ts = float(value.get("last_success_ts") or 0.0)
            except (TypeError, ValueError):
                failure_ts, success_ts = 1.0, 0.0
            if failure_ts >= success_ts:
                reasons.add(f"{path}.last_error")
        for key, nested in value.items():
            if isinstance(nested, Mapping):
                inspect(nested, f"{path}.{key}")

    inspect(workers, "workers")
    inspect(faults, "faults")
    inspect(artifact_writers, "artifact_writers")
    try:
        active_count = int(incidents.get("active_count") or 0)
    except (TypeError, ValueError):
        active_count = 0
    if active_count:
        reasons.add("incidents.active")
    return tuple(sorted(reasons))


class _WriterHealth:
    def __init__(self, *, owner: str, path: Path) -> None:
        self.owner = owner
        self.path = path
        self.attempted = 0
        self.written = 0
        self.failures = 0
        self.last_sequence = 0
        self.last_success_ts = 0.0
        self.last_failure_ts = 0.0
        self.last_error = ""

    def success(self, *, sequence: int, now: float) -> None:
        self.written += 1
        self.last_sequence = max(self.last_sequence, int(sequence))
        self.last_success_ts = float(now)
        self.last_error = ""

    def failure(self, exc: BaseException, *, now: float) -> None:
        self.failures += 1
        self.last_failure_ts = float(now)
        self.last_error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> ArtifactWriterStatus:
        return ArtifactWriterStatus(
            owner=self.owner,
            path=str(self.path),
            attempted=self.attempted,
            written=self.written,
            failures=self.failures,
            last_sequence=self.last_sequence,
            last_success_ts=self.last_success_ts,
            last_failure_ts=self.last_failure_ts,
            last_error=self.last_error,
        )


class CoreCurrentStatePublisher:
    """The only atomic ``current_state.json`` publisher for the light product."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        fault_reporter: FaultReporter | None = None,
        max_payload_bytes: int = CURRENT_STATE_MAX_BYTES,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.path = self.runtime_dir / CURRENT_STATE_FILENAME
        self._lock = threading.RLock()
        self._fault_reporter = fault_reporter
        self._max_payload_bytes = max(1024, int(max_payload_bytes))
        self._health = _WriterHealth(owner=CURRENT_STATE_OWNER, path=self.path)
        self._last_core_snapshot_sequence = 0
        try:
            cleanup_abandoned_atomic_temps(self.path)
        except Exception as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("cleanup_abandoned_current_state_temp", exc)
        self._recover_existing_sequence()

    def _recover_existing_sequence(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("current_state_existing_payload_not_object")
            if payload.get("schema") != CURRENT_STATE_SCHEMA:
                raise ValueError("current_state_existing_schema_invalid")
            if payload.get("owner") != CURRENT_STATE_OWNER:
                raise ValueError("current_state_existing_owner_invalid")
            sequence = int(payload.get("sequence") or 0)
            if sequence <= 0:
                raise ValueError("current_state_existing_sequence_invalid")
            self._health.last_sequence = sequence
        except Exception as exc:
            now = time.time()
            self._health.failure(exc, now=now)
            self._report_failure("recover_current_state", exc)

    def _report_failure(self, operation: str, exc: BaseException) -> None:
        log.error("%s failed: %s: %s", operation, type(exc).__name__, exc)
        if self._fault_reporter is None:
            return
        try:
            self._fault_reporter(
                CURRENT_STATE_OWNER,
                operation,
                exc,
                "retain_last_atomic_core_snapshot_and_continue_runtime",
            )
        except Exception as report_error:
            log.error(
                "current-state fault reporting failed: %s: %s",
                type(report_error).__name__,
                report_error,
            )

    def publish(
        self,
        snapshot: CoreRuntimeSnapshot,
        *,
        current_health: CoreCurrentHealthView,
    ) -> bool:
        if type(snapshot) is not CoreRuntimeSnapshot:
            raise TypeError("core_current_state_typed_snapshot_required")
        if not isinstance(current_health, CoreCurrentHealthView):
            raise TypeError("core_current_state_current_health_required")
        if current_health.namespace != snapshot.matrix_identity.namespace:
            raise RuntimeError("core_current_state_health_namespace_mismatch")
        core_sequence = int(snapshot.sequence)
        now = time.time()
        with self._lock:
            self._health.attempted += 1
            if core_sequence <= self._last_core_snapshot_sequence:
                raise RuntimeError(
                    "core_current_state_core_sequence_non_monotonic:"
                    f"incoming={core_sequence};"
                    f"published={self._last_core_snapshot_sequence}"
                )
            artifact_sequence = self._health.last_sequence + 1
            payload = {
                "schema": CURRENT_STATE_SCHEMA,
                "owner": CURRENT_STATE_OWNER,
                "sequence": artifact_sequence,
                "published_at": float(snapshot.published_at),
                "core_snapshot": snapshot.to_payload(),
                "core_current_health": current_health.to_payload(),
            }
            try:
                serialized_bytes = len(
                    json.dumps(
                        payload,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                ) + 1
                if serialized_bytes > self._max_payload_bytes:
                    raise ValueError(
                        "foundation_current_state_payload_exceeds_budget:"
                        f"bytes={serialized_bytes};limit={self._max_payload_bytes}"
                    )
                atomic_write_json(
                    self.path,
                    payload,
                    sort_keys=True,
                    trailing_newline=True,
                )
            except Exception as exc:
                self._health.failure(exc, now=now)
                self._report_failure("publish_current_state", exc)
                return False
            self._last_core_snapshot_sequence = core_sequence
            self._health.success(sequence=artifact_sequence, now=now)
            return True

    def status(self) -> ArtifactWriterStatus:
        with self._lock:
            return self._health.snapshot()


class FoundationRuntimeHealthPublisher:
    """The only atomic process-liveness writer for the light product."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        fault_reporter: FaultReporter | None = None,
        max_payload_bytes: int = RUNTIME_HEALTH_MAX_BYTES,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.path = self.runtime_dir / RUNTIME_HEALTH_FILENAME
        self._lock = threading.RLock()
        self._fault_reporter = fault_reporter
        self._max_payload_bytes = max(1024, int(max_payload_bytes))
        self._health = _WriterHealth(owner=RUNTIME_HEALTH_OWNER, path=self.path)
        try:
            cleanup_abandoned_atomic_temps(self.path)
        except Exception as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("cleanup_abandoned_runtime_health_temp", exc)
        self._recover_existing_sequence()

    def _report_failure(self, operation: str, exc: BaseException) -> None:
        log.error("%s failed: %s: %s", operation, type(exc).__name__, exc)
        if self._fault_reporter is None:
            return
        try:
            self._fault_reporter(
                RUNTIME_HEALTH_OWNER,
                operation,
                exc,
                "retain_in_memory_liveness_and_continue_runtime",
            )
        except Exception as report_error:
            log.error(
                "runtime-health fault reporting failed: %s: %s",
                type(report_error).__name__,
                report_error,
            )

    def _recover_existing_sequence(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("runtime_health_existing_payload_not_object")
            if payload.get("schema") != RUNTIME_HEALTH_SCHEMA:
                raise ValueError("runtime_health_existing_schema_invalid")
            if payload.get("owner") != RUNTIME_HEALTH_OWNER:
                raise ValueError("runtime_health_existing_owner_invalid")
            sequence = int(payload.get("sequence") or 0)
            if sequence <= 0:
                raise ValueError("runtime_health_existing_sequence_invalid")
            self._health.last_sequence = sequence
        except Exception as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("recover_runtime_health", exc)

    def publish(self, snapshot: FoundationRuntimeHealthSnapshot) -> bool:
        if type(snapshot) is not FoundationRuntimeHealthSnapshot:
            raise TypeError("foundation_runtime_health_typed_snapshot_required")
        now = time.time()
        with self._lock:
            self._health.attempted += 1
            sequence = self._health.last_sequence + 1
            try:
                payload = _json_value(snapshot.to_payload(sequence=sequence))
                serialized_bytes = len(
                    json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                ) + 1
                if serialized_bytes > self._max_payload_bytes:
                    raise ValueError(
                        "foundation_runtime_health_payload_exceeds_budget:"
                        f"bytes={serialized_bytes};limit={self._max_payload_bytes}"
                    )
                atomic_write_json(
                    self.path,
                    payload,
                    sort_keys=True,
                    trailing_newline=True,
                )
            except Exception as exc:
                self._health.failure(exc, now=now)
                self._report_failure("publish_runtime_health", exc)
                return False
            self._health.success(sequence=sequence, now=now)
            return True

    def status(self) -> ArtifactWriterStatus:
        with self._lock:
            return self._health.snapshot()


class FoundationRuntimeHealthService:
    """Own process health state and publish its liveness record."""

    _PROGRESS_PHASES = frozenset(
        {
            "startup_begin",
            "startup_ok",
            "cycle_started",
            "cycle_ok",
            "cycle_recoverable_fault",
            "modem_read_missed",
            "connection_lost",
            "check_complete",
            "running",
        }
    )
    _FAILURE_PHASES = frozenset(
        {
            "startup_blocked",
            "reconnecting",
            "cycle_code_fault",
            "runtime_suspended",
            "fatal_cycle",
            "fatal_runner",
            "fatal_startup",
        }
    )

    def __init__(
        self,
        *,
        publisher: FoundationRuntimeHealthPublisher,
        modem_backend: str,
        probe_mode: bool = False,
        observe_only: bool = False,
        check_only: bool = False,
        instance_id: str = "",
        artifact_status_provider: Callable[[], Mapping[str, Any]] | None = None,
        incident_status_provider: Callable[[], Mapping[str, Any]] | None = None,
        minimum_interval_s: float = 5.0,
    ) -> None:
        if not isinstance(publisher, FoundationRuntimeHealthPublisher):
            raise TypeError("foundation_runtime_health_publisher_required")
        self._publisher = publisher
        self._modem_backend = str(modem_backend or "")
        self._runtime_mode = foundation_runtime_mode(
            probe_mode=probe_mode,
            observe_only=observe_only,
            check_only=check_only,
        )
        self._write_authority, self._write_block_reason = (
            foundation_write_authority(
                probe_mode=probe_mode,
                observe_only=observe_only,
                check_only=check_only,
            )
        )
        self._instance_id = str(instance_id or "")
        self._artifact_status_provider = artifact_status_provider
        self._incident_status_provider = incident_status_provider
        self._minimum_interval_s = max(0.1, float(minimum_interval_s))
        self._lock = threading.RLock()
        self._last_progress_at = 0.0
        self._consecutive_failures = 0
        self._last_error = ""
        self._last_snapshot: FoundationRuntimeHealthSnapshot | None = None
        self._last_publish_signature = ""
        self._last_publish_at = 0.0
        self._finalizing = False
        self._finalized = False

    @property
    def runtime_dir(self) -> Path:
        return self._publisher.runtime_dir

    def mark(
        self,
        phase: str,
        *,
        cycle_count: int = 0,
        error: BaseException | str | None = None,
        workers: Mapping[str, Any] | None = None,
        faults: Mapping[str, Any] | None = None,
        detail: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> bool:
        return self._publish_phase(
            phase,
            cycle_count=cycle_count,
            error=error,
            workers=workers,
            faults=faults,
            detail=detail,
            observed_at=observed_at,
            terminal=False,
        )

    def _publish_phase(
        self,
        phase: str,
        *,
        cycle_count: int,
        error: BaseException | str | None,
        workers: Mapping[str, Any] | None,
        faults: Mapping[str, Any] | None,
        detail: Mapping[str, Any] | None,
        observed_at: float | None,
        terminal: bool,
    ) -> bool:
        normalized_phase = str(phase or "").strip()
        if not normalized_phase:
            raise ValueError("foundation_runtime_health_phase_required")
        now = float(time.time() if observed_at is None else observed_at)
        if now <= 0.0:
            raise ValueError("foundation_runtime_health_timestamp_required")
        with self._lock:
            if self._finalized:
                raise RuntimeError("foundation_runtime_health_finalized")
            if self._finalizing and not terminal:
                raise RuntimeError("foundation_runtime_health_finalizing")
            if terminal:
                if not self._finalizing:
                    raise RuntimeError(
                        "foundation_runtime_health_terminal_without_fence"
                    )
            elif normalized_phase in {
                "shutdown_complete",
                "shutdown_degraded",
            }:
                raise RuntimeError(
                    "foundation_runtime_health_terminal_phase_reserved"
                )
            error_text = (
                f"{type(error).__name__}: {error}"
                if isinstance(error, BaseException)
                else str(error or "")
            )
            if normalized_phase in self._PROGRESS_PHASES:
                self._last_progress_at = now
            if normalized_phase in self._FAILURE_PHASES:
                self._consecutive_failures += 1
                if error is not None:
                    self._last_error = (
                        f"{type(error).__name__}: {error}"
                        if isinstance(error, BaseException)
                        else str(error)
                    )
            elif normalized_phase in {"startup_ok", "cycle_ok", "running"}:
                self._consecutive_failures = 0
                self._last_error = ""
            try:
                artifacts = (
                    dict(self._artifact_status_provider() or {})
                    if self._artifact_status_provider is not None
                    else {}
                )
            except Exception as exc:
                artifacts = {
                    "status_provider": {
                        "healthy": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                }
            try:
                incidents = (
                    dict(self._incident_status_provider() or {})
                    if self._incident_status_provider is not None
                    else {}
                )
            except Exception as exc:
                incidents = {
                    "active_count": 1,
                    "status_provider": {
                        "healthy": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                }
            worker_payload = _json_value(workers or {})
            fault_payload = _json_value(faults or {})
            detail_payload = _json_value(detail or {})
            degradation_reasons = _foundation_degradation_reasons(
                workers=worker_payload,
                faults=fault_payload,
                artifact_writers=artifacts,
                incidents=incidents,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                terminal_clean=bool(
                    terminal and normalized_phase == "shutdown_complete"
                ),
            )
            publish_signature = json.dumps(
                _runtime_health_meaning({
                    "phase": normalized_phase,
                    "error": error_text,
                    "workers": worker_payload,
                    "faults": fault_payload,
                    "artifact_writers": artifacts,
                    "incidents": incidents,
                    "degradation_reasons": degradation_reasons,
                    "detail": detail_payload,
                }),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if (
                publish_signature == self._last_publish_signature
                and self._last_publish_at > 0.0
                and now - self._last_publish_at < self._minimum_interval_s
            ):
                return True
            snapshot = FoundationRuntimeHealthSnapshot(
                phase=normalized_phase,
                observed_at=now,
                process_id=os.getpid(),
                instance_id=self._instance_id,
                modem_backend=self._modem_backend,
                runtime_mode=self._runtime_mode,
                write_authority=self._write_authority,
                write_block_reason=self._write_block_reason,
                cycle_count=max(0, int(cycle_count)),
                last_progress_at=self._last_progress_at,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                workers=CoreFrozenPayload.from_mapping(worker_payload),
                faults=CoreFrozenPayload.from_mapping(fault_payload),
                artifact_writers=CoreFrozenPayload.from_mapping(
                    _json_value(artifacts)
                ),
                incidents=CoreFrozenPayload.from_mapping(_json_value(incidents)),
                degradation_reasons=degradation_reasons,
                detail=CoreFrozenPayload.from_mapping(detail_payload),
            )
            written = self._publisher.publish(snapshot)
            if written:
                self._last_snapshot = snapshot
                self._last_publish_signature = publish_signature
                self._last_publish_at = now
            return written

    def finalize(
        self,
        *,
        cycle_count: int,
        degraded: bool,
        workers: Mapping[str, Any] | None = None,
        faults: Mapping[str, Any] | None = None,
        detail: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Publish the only terminal health state and permanently fence writes."""

        with self._lock:
            if self._finalized:
                return {
                    "owner": "arch4.foundation.runtime_health_service",
                    "status": "already_finalized",
                    "phase": (
                        self._last_snapshot.phase
                        if self._last_snapshot is not None
                        else ""
                    ),
                    "writer": self._publisher.status().to_payload(),
                }
            if self._finalizing:
                raise RuntimeError(
                    "foundation_runtime_health_finalize_reentrant"
                )
            self._finalizing = True
        phase = "shutdown_degraded" if degraded else "shutdown_complete"
        try:
            written = self._publish_phase(
                phase,
                cycle_count=cycle_count,
                error=None,
                workers=workers,
                faults=faults,
                detail=detail,
                observed_at=observed_at,
                terminal=True,
            )
            if not written:
                raise RuntimeError(
                    "foundation_runtime_health_terminal_publish_failed"
                )
        finally:
            with self._lock:
                self._finalizing = False
                self._finalized = True
        return {
            "owner": "arch4.foundation.runtime_health_service",
            "status": "finalized",
            "phase": phase,
            "writer": self._publisher.status().to_payload(),
        }

    def latest(self) -> FoundationRuntimeHealthSnapshot | None:
        with self._lock:
            return self._last_snapshot

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "FoundationRuntimeHealthServiceStatus.v1",
                "owner": "arch4.foundation.runtime_health_service",
                "runtime_mode": self._runtime_mode,
                "minimum_interval_s": self._minimum_interval_s,
                "last_phase": (
                    self._last_snapshot.phase if self._last_snapshot is not None else ""
                ),
                "last_progress_at": self._last_progress_at,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "finalizing": self._finalizing,
                "finalized": self._finalized,
                "writer": self._publisher.status().to_payload(),
            }

    def close(self) -> dict[str, Any]:
        """Synchronous atomic writer has no retained handle; receipt is explicit."""

        with self._lock:
            if not self._finalized:
                raise RuntimeError(
                    "foundation_runtime_health_close_before_finalize"
                )
        return {
            "owner": "arch4.foundation.runtime_health_service",
            "status": "closed",
            "writer": self._publisher.status().to_payload(),
        }


class CgfEvidenceStore:
    """The only bounded current-run operational-evidence owner."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        max_records: int = _DEFAULT_MAX_EVENT_RECORDS,
        max_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
        fault_reporter: FaultReporter | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.path = self.runtime_dir / CGF_EVIDENCE_FILENAME
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1024, int(max_bytes))
        self._fault_reporter = fault_reporter
        self._lock = threading.RLock()
        self._health = _WriterHealth(owner=EVENT_OWNER, path=self.path)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = self._open_database()
        except sqlite3.DatabaseError as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("open_cgf_evidence", exc)
            quarantine_corrupt_sqlite(self.path, exc)
            self._connection = self._open_database()
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events"
        ).fetchone()
        last_sequence = int(row[0] if row else 0)
        self._health.last_sequence = last_sequence
        self._next_sequence = last_sequence + 1
        self._log_processor = MemoryFirstLogProcessor(
            owner=EVENT_OWNER,
            critical_sink=self._write_batch,
            routine_sink=self._write_batch,
        )

    def _open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=1.0,
            check_same_thread=False,
        )
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if (tables and version != _EVENT_DATABASE_VERSION) or (
                not tables and version != 0
            ):
                raise ValueError("cgf_evidence_writer_format_unsupported")
            if tables:
                _require_v2_evidence_schema(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS event_payloads ("
                "digest BLOB PRIMARY KEY, compressed BLOB NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    payload_digest BLOB NOT NULL REFERENCES event_payloads(digest)
                )
                """
            )
            connection.execute(f"PRAGMA user_version={_EVENT_DATABASE_VERSION}")
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    def _report_failure(self, operation: str, exc: BaseException) -> None:
        log.error("%s failed: %s: %s", operation, type(exc).__name__, exc)
        if self._fault_reporter is None:
            return
        try:
            self._fault_reporter(
                EVENT_OWNER,
                operation,
                exc,
                "retain_event_in_controller_memory_and_continue_runtime",
            )
        except Exception as report_error:
            log.error(
                "diagnostic fault reporting failed: %s: %s",
                type(report_error).__name__,
                report_error,
            )

    def _write_batch(self, batch: tuple[MemoryLogRecord, ...]) -> None:
        """Write one admitted memory batch in one SQLite transaction."""

        if not batch:
            return
        # The same memory-first persistence owner retains the startup trace
        # separately from the short rolling operational journal.
        wire_rows = tuple(item for item in batch if "wire_trace" in item.payload)
        if wire_rows:
            self._write_wire_batch(wire_rows)
        batch = tuple(item for item in batch if "wire_trace" not in item.payload)
        if not batch:
            return
        now = time.time()
        try:
            with self._lock, self._connection:
                for item in batch:
                    payload = item.payload
                    record = dict(payload["record"])
                    record_bytes = int(payload["record_bytes"])
                    errors = foundation_diagnostic_event_record_errors(
                        record,
                        previous_sequence=int(record["sequence"]) - 1,
                        record_bytes=record_bytes,
                    )
                    if errors:
                        raise ValueError(
                            "foundation_diagnostic_event_contract_invalid:"
                            + ",".join(errors)
                        )
                    self._connection.execute(
                        "INSERT OR IGNORE INTO event_payloads(digest, compressed) VALUES (?, ?)",
                        (payload["digest"], payload["compressed"]),
                    )
                    self._connection.execute(
                        "INSERT INTO events(sequence, ts, payload_digest) VALUES (?, ?, ?)",
                        (
                            int(record["sequence"]),
                            float(record["ts"]),
                            payload["digest"],
                        ),
                    )
                self._prune_locked()
            with self._lock:
                for item in batch:
                    self._health.success(
                        sequence=int(item.payload["record"]["sequence"]),
                        now=now,
                    )
        except Exception as exc:
            with self._lock:
                self._health.failure(exc, now=now)
            self._report_failure("append_diagnostic_event_batch", exc)
            raise

    def log_modem_wire(self, record: dict[str, Any]) -> bool:
        from arch4.modem_live.startup_trace import TRACE_SCHEMA, TRACE_OWNER
        try:
            if record.get("schema") != TRACE_SCHEMA or record.get("owner") != TRACE_OWNER:
                raise ValueError("modem_startup_trace_contract_invalid")
            clean = scrub_record(record)
            return self._log_processor.submit(
                key=f"wire:{clean['trace_id']}:{clean['sequence']}",
                lane=CRITICAL_LANE, observed_at=float(clean["ts"]),
                payload={"wire_trace": clean},
            )
        except Exception as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("admit_modem_startup_trace", exc)
            return False

    def _write_wire_batch(self, batch: tuple[MemoryLogRecord, ...]) -> None:
        from arch4.modem_live.startup_trace import TRACE_FILENAME, TRACE_MAX_BYTES
        path = self.runtime_dir / TRACE_FILENAME
        try:
            with self._lock:
                incoming = [dict(item.payload['wire_trace']) for item in batch]
                starts = {row['trace_id'] for row in incoming if row.get('kind') == 'trace_started'}
                prior = []
                replace_previous_launch = False
                if starts and path.exists():
                    # A new launch explicitly replaces the preceding trace,
                    # even an old full/malformed file. Same-launch retry must
                    # preserve already committed rows and validate collisions.
                    with path.open(encoding='utf-8') as stream:
                        first = stream.readline(TRACE_MAX_BYTES + 1)
                    try:
                        replace_previous_launch = json.loads(first).get('trace_id') not in starts
                    except (ValueError, AttributeError):
                        replace_previous_launch = True
                if path.exists() and not replace_previous_launch and path.stat().st_size > TRACE_MAX_BYTES:
                    raise ValueError("modem_startup_trace_file_budget_exhausted")
                if path.exists() and not replace_previous_launch:
                    prior = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                identity = lambda row: (row['trace_id'], row['sequence'])
                retained = {identity(row): row for row in prior}
                if any(row.get('kind') == 'trace_started' and identity(row) not in retained for row in incoming):
                    retained = {}
                changed = False
                for row in incoming:
                    key = identity(row)
                    if key in retained:
                        if retained[key] != row:
                            raise ValueError('modem_startup_trace_identity_collision')
                        continue
                    retained[key] = row
                    changed = True
                if not changed:
                    return
                data = ''.join(json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n' for row in retained.values())
                if len(data.encode('utf-8')) > TRACE_MAX_BYTES:
                    raise ValueError("modem_startup_trace_file_budget_exhausted")
                # Atomic replacement also covers a partial OS write. Retrying a
                # mixed trace/SQLite batch cannot append the trace a second time.
                atomic_write_text(path, data)
        except Exception as exc:
            self._health.failure(exc, now=time.time())
            self._report_failure("write_modem_startup_trace", exc)
            raise

    def log_event(self, kind: str, **payload: Any) -> bool:
        now = time.time()
        with self._lock:
            self._health.attempted += 1
            sequence = self._next_sequence
            try:
                event_kind = str(kind or "").strip()
                clean_payload = scrub_record(dict(payload))
                record = {
                    "schema": EVENT_SCHEMA,
                    "owner": EVENT_OWNER,
                    "sequence": sequence,
                    "ts": float(clean_payload.pop("ts", now) or now),
                    "kind": scrub_text(event_kind),
                    "generation": int(clean_payload.pop("generation", 0) or 0),
                    "transaction_id": scrub_text(
                        str(clean_payload.pop("transaction_id", "") or "")
                    ),
                    "event_owner": scrub_text(
                        str(
                            clean_payload.pop(
                                "event_owner",
                                clean_payload.pop("owner", ""),
                            )
                            or ""
                        )
                    ),
                    "severity": str(
                        clean_payload.pop("severity", "INFO") or "INFO"
                    ).upper(),
                    "operation": scrub_text(
                        str(clean_payload.pop("operation", "") or "")
                    ),
                    "detail": scrub_text(
                        str(clean_payload.pop("detail", "") or "")
                    ),
                    "degraded_behavior": scrub_text(
                        str(clean_payload.pop("degraded_behavior", "") or "")
                    ),
                    "data": _json_value(clean_payload),
                }
                line = json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ) + "\n"
                contract_errors = foundation_diagnostic_event_record_errors(
                    record,
                    previous_sequence=sequence - 1,
                    record_bytes=len(line.encode("utf-8")),
                )
                if contract_errors:
                    raise ValueError(
                        "foundation_diagnostic_event_contract_invalid:"
                        + ",".join(contract_errors)
                    )
                digest, compressed = encode_payload(record)
                if len(compressed) + len(digest) + _EVENT_REFERENCE_BYTES > self.max_bytes:
                    raise ValueError("foundation_diagnostic_event_exceeds_journal_budget")
                record_bytes = len(line.encode("utf-8"))
                accepted = self._log_processor.submit(
                    key=f"cgf-event:{sequence}",
                    lane=CRITICAL_LANE,
                    observed_at=float(record["ts"]),
                    payload={
                        "record": record,
                        "digest": digest,
                        "compressed": compressed,
                        "record_bytes": record_bytes,
                    },
                )
            except Exception as exc:
                self._health.failure(exc, now=now)
                self._report_failure("admit_diagnostic_event", exc)
                return False
            if not accepted:
                return False
            self._next_sequence += 1
            return True

    def events_snapshot(self) -> tuple[dict[str, Any], ...]:
        # Evidence reads are outside the hot path and return one bounded,
        # durable snapshot.
        self.flush()
        with self._lock:
            return _read_evidence_rows(self._connection, max_events=self.max_records)

    def _prune_locked(self) -> None:
        rows = self._connection.execute(
            "SELECT sequence, payload_digest FROM events ORDER BY sequence"
        ).fetchall()
        sizes = dict(self._connection.execute(
            "SELECT digest, LENGTH(compressed) + LENGTH(digest) FROM event_payloads"
        ).fetchall())
        references = Counter(row[1] for row in rows)
        size = sum(sizes.values()) + len(rows) * _EVENT_REFERENCE_BYTES
        removed = []
        for sequence, digest in rows:
            if len(rows) - len(removed) <= self.max_records and size <= self.max_bytes:
                break
            removed.append((sequence,))
            references[digest] -= 1
            size -= _EVENT_REFERENCE_BYTES
            if not references[digest]:
                size -= sizes[digest]
        self._connection.executemany("DELETE FROM events WHERE sequence = ?", removed)
        self._connection.execute(
            "DELETE FROM event_payloads WHERE digest NOT IN "
            "(SELECT payload_digest FROM events)"
        )

    def log_fault(
        self,
        *,
        owner: str,
        operation: str,
        error: BaseException,
        severity: str = "ERROR",
        degraded_behavior: str,
        generation: int = 0,
        transaction_id: str = "",
    ) -> bool:
        return self.log_event(
            "runtime_fault",
            generation=generation,
            transaction_id=transaction_id,
            event_owner=owner,
            operation=operation,
            severity=severity,
            detail=f"{type(error).__name__}: {error}",
            error_class=type(error).__name__,
            degraded_behavior=degraded_behavior,
        )

    def log_state_transition(
        self,
        old_state: Any,
        new_state: Any,
        *,
        reason: str = "",
    ) -> bool:
        old_name = str(getattr(old_state, "name", old_state) or "")
        new_name = str(getattr(new_state, "name", new_state) or "")
        detail = str(reason or "").strip() or f"state transition {old_name}->{new_name}"
        return self.log_event(
            "state_transition",
            old_state=old_name,
            new_state=new_name,
            reason=detail,
        )

    def log_action(
        self,
        receipt: CommandTerminalReceipt,
    ) -> bool:
        if type(receipt) is not CommandTerminalReceipt:
            raise TypeError(
                "foundation_action_diagnostic_terminal_receipt_required"
            )
        payload = receipt.as_dict()
        return self.log_event(
            "action_terminal",
            ts=receipt.terminal_ts,
            generation=max(
                int(receipt.target_generation),
                int(receipt.settle_generation),
            ),
            transaction_id=receipt.root_id,
            event_owner=str(payload.get("owner") or ""),
            operation=str(payload.get("command") or ""),
            severity=(
                "INFO"
                if bool(payload.get("accepted"))
                else "WARNING"
            ),
            detail=str(payload.get("reason") or ""),
            command_terminal_receipt=payload,
        )

    def log_startup(self, constraints: Mapping[str, Any]) -> bool:
        return self.log_event("startup", constraints=dict(constraints or {}))

    def status(self) -> ArtifactWriterStatus:
        with self._lock:
            return self._health.snapshot()

    def log_processor_status(self) -> dict[str, Any]:
        return self._log_processor.status()

    def flush(self) -> None:
        """Request one explicit durable checkpoint from the evidence owner."""

        self._log_processor.flush()

    def close(self) -> None:
        self._log_processor.close()
        with self._lock:
            self._connection.close()


def _require_v2_evidence_schema(connection: sqlite3.Connection) -> None:
    """A version label alone cannot authorize repairing or using another layout."""

    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    events = tuple(row[1] for row in connection.execute("PRAGMA table_info(events)"))
    payloads = tuple(row[1] for row in connection.execute("PRAGMA table_info(event_payloads)"))
    if (tables != {"events", "event_payloads"}
            or events != ("sequence", "ts", "payload_digest")
            or payloads != ("digest", "compressed")):
        raise ValueError("cgf_evidence_v2_schema_invalid")


def _read_v1_diagnostic_rows(
    connection: sqlite3.Connection, *, max_events: int,
) -> tuple[dict[str, Any], ...]:
    """Approved permanent read-only adapter for pre-compression support bundles."""

    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(events)"))
    if columns != ("sequence", "ts", "kind", "record_json", "record_bytes"):
        raise ValueError("cgf_evidence_v1_schema_invalid")
    rows = connection.execute(
        "SELECT record_json FROM events ORDER BY sequence DESC LIMIT ?",
        (max_events,),
    ).fetchall()
    return tuple(json.loads(str(row[0])) for row in reversed(rows))


def _read_evidence_rows(
    connection: sqlite3.Connection, *, max_events: int,
) -> tuple[dict[str, Any], ...]:
    """One decoder for store snapshots, support exports and diagnostic tools."""

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    limit = max(1, int(max_events))
    if version == 0:
        records = _read_v1_diagnostic_rows(connection, max_events=limit)
    elif version == _EVENT_DATABASE_VERSION:
        _require_v2_evidence_schema(connection)
        rows = connection.execute(
            "SELECT e.sequence, e.ts, e.payload_digest, p.compressed FROM events e "
            "LEFT JOIN event_payloads p ON p.digest = e.payload_digest "
            "ORDER BY e.sequence DESC LIMIT ?", (limit,),
        ).fetchall()
        records = tuple(decode_payload(digest, compressed, sequence=seq, ts=ts,
                                      max_bytes=MAX_EVENT_RECORD_BYTES)
                        for seq, ts, digest, compressed in reversed(rows))
    else:
        raise ValueError("cgf_evidence_reader_format_unsupported")
    previous_sequence = 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=True, separators=(",", ":"),
                              sort_keys=True).encode("utf-8")) + 1
        errors = foundation_diagnostic_event_record_errors(
            record, previous_sequence=previous_sequence, record_bytes=size,
        )
        if errors:
            raise ValueError("cgf_evidence_event_invalid:" + ",".join(errors))
        previous_sequence = int(record["sequence"])
    return records


def read_cgf_evidence_events(
    path: str | Path,
    *,
    max_events: int,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Read bounded event evidence through its owner module."""

    evidence_path = Path(path).resolve()
    empty = {
        "path": evidence_path.name,
        "disposition": "missing",
        "reason": "",
        "sequence": 0,
        "record_count": 0,
        "sha256": "",
    }
    if not evidence_path.is_file():
        return (), empty
    try:
        connection = sqlite3.connect(
            f"file:{evidence_path.as_posix()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        try:
            records = _read_evidence_rows(connection, max_events=max_events)
        finally:
            connection.close()
        previous_sequence = int(records[-1]["sequence"]) if records else 0
        canonical = json.dumps(
            records,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return records, {
            "path": evidence_path.name,
            "disposition": "captured",
            "reason": "",
            "sequence": previous_sequence,
            "record_count": len(records),
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }
    except Exception as exc:
        return (), {
            **empty,
            "disposition": "invalid_contract",
            "reason": f"{type(exc).__name__}: {exc}",
        }


class FoundationCurrentStateClient:
    """Read Core snapshots and drive the current-state file writer. This boundary cannot
    access mutable Core registers. Product Supervisor owns scheduling; publish_once
    handles one bounded update.
    """

    def __init__(
        self,
        *,
        snapshot_port: CoreRuntimeSnapshotPort,
        current_health_provider: Callable[[], CoreCurrentHealthView],
        publisher: CoreCurrentStatePublisher,
        client_id: str = FOUNDATION_CURRENT_STATE_CLIENT_ID,
        fault_reporter: FaultReporter | None = None,
        minimum_interval_s: float = 30.0,
    ) -> None:
        if not callable(getattr(snapshot_port, "snapshot_for", None)):
            raise TypeError("foundation_current_state_core_snapshot_port_required")
        if not callable(current_health_provider):
            raise TypeError(
                "foundation_current_state_health_provider_required"
            )
        if not isinstance(publisher, CoreCurrentStatePublisher):
            raise TypeError("foundation_current_state_file_publisher_required")
        self.client_id = str(client_id or "").strip()
        if not self.client_id:
            raise ValueError("foundation_current_state_client_id_required")
        self._snapshot_port = snapshot_port
        self._current_health_provider = current_health_provider
        self._publisher = publisher
        self._fault_reporter = fault_reporter
        self._minimum_interval_s = max(0.1, float(minimum_interval_s))
        self._schedule_lock = threading.RLock()
        self._last_attempt_monotonic = 0.0
        self._service_failures = 0
        self._last_service_error = ""

    @property
    def runtime_dir(self) -> Path:
        return self._publisher.runtime_dir

    def publish_once(
        self,
        *,
        health: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        published_at: float | None = None,
    ) -> bool:
        snapshot = self._snapshot_port.snapshot_for(
            self.client_id,
            health=health,
            metadata=metadata,
            published_at=published_at,
        )
        current_health = self._current_health_provider()
        if not isinstance(current_health, CoreCurrentHealthView):
            raise TypeError(
                "foundation_current_state_typed_health_required"
            )
        return self._publisher.publish(
            snapshot,
            current_health=current_health,
        )

    def publish_if_due(
        self,
        *,
        health: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        published_at: float | None = None,
        force: bool = False,
    ) -> bool | None:
        """Publish only when the interval is due. None means no write was needed. False
        means a write was tried and failed. Diagnostic file failure does not stop Core
        or NGUI.
        """

        monotonic_now = time.monotonic()
        with self._schedule_lock:
            if (
                not force
                and self._last_attempt_monotonic > 0.0
                and monotonic_now - self._last_attempt_monotonic
                < self._minimum_interval_s
            ):
                return None
            self._last_attempt_monotonic = monotonic_now
        try:
            result = self.publish_once(
                health=health,
                metadata=metadata,
                published_at=published_at,
            )
            if result:
                self._last_service_error = ""
            return result
        except Exception as exc:
            self._service_failures += 1
            self._last_service_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "foundation current-state snapshot failed: %s",
                self._last_service_error,
            )
            if self._fault_reporter is not None:
                try:
                    self._fault_reporter(
                        CURRENT_STATE_OWNER,
                        "capture_core_snapshot",
                        exc,
                        "retain_last_atomic_core_snapshot_and_continue_runtime",
                    )
                except Exception as report_error:
                    log.error(
                        "current-state snapshot fault reporting failed: %s: %s",
                        type(report_error).__name__,
                        report_error,
                    )
            return False

    def status(self) -> ArtifactWriterStatus:
        return self._publisher.status()

    def close(self) -> dict[str, Any]:
        """Synchronous atomic writer has no retained handle; receipt is explicit."""

        return {
            "owner": "arch4.foundation.current_state_client",
            "status": "closed",
            "writer": self._publisher.status().to_payload(),
        }

    def service_status(self) -> dict[str, Any]:
        return {
            "schema": "FoundationCurrentStateClientStatus.v1",
            "owner": "arch4.foundation.current_state_client",
            "client_id": self.client_id,
            "minimum_interval_s": self._minimum_interval_s,
            "service_failures": self._service_failures,
            "last_service_error": self._last_service_error,
            "writer": self._publisher.status().to_payload(),
        }


def read_foundation_runtime_surface_bundle(
    runtime_dir: str | Path,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Strict read-only adapter for support and runtime-watch consumers."""

    root = Path(runtime_dir).resolve()
    observed_at = float(time.time() if now is None else now)
    if observed_at <= 0.0:
        raise ValueError("foundation_runtime_surface_positive_timestamp_required")

    from arch4.foundation.evidence_snapshot import (
        capture_foundation_evidence_snapshot,
    )

    current_path = root / CURRENT_STATE_FILENAME
    evidence = capture_foundation_evidence_snapshot(
        root,
        now=observed_at,
        require_complete=False,
    )
    envelope_value = evidence.get("current_state")
    envelope = dict(envelope_value) if isinstance(envelope_value, dict) else {}
    current_status = dict(
        (evidence.get("source_status") or {}).get("current_state") or {}
    )
    current_source = str(current_status.get("disposition") or "missing")
    envelope_valid = bool(
        envelope.get("schema") == CURRENT_STATE_SCHEMA
        and envelope.get("owner") == CURRENT_STATE_OWNER
    )
    core_snapshot = (
        envelope.get("core_snapshot")
        if envelope_valid and isinstance(envelope.get("core_snapshot"), dict)
        else {}
    )
    core_valid = bool(
        core_snapshot.get("schema") == CORE_RUNTIME_SNAPSHOT_SCHEMA
        and core_snapshot.get("owner") == CORE_RUNTIME_SNAPSHOT_OWNER
    )
    if not core_valid:
        core_snapshot = {}
    current_health_value = (
        envelope.get("core_current_health")
        if envelope_valid
        else None
    )
    current_facts: CoreAcceptedCurrentFacts | None = None
    core_current_health: CoreCurrentHealthView | None = None
    if core_valid:
        try:
            current_facts = CoreAcceptedCurrentFacts.from_payload(
                core_snapshot["accepted_current_facts"]
            )
            core_current_health = CoreCurrentHealthView.from_payload(
                current_health_value
            )
            snapshot_identity = MatrixGenerationIdentity.from_mapping(
                core_snapshot["matrix_identity"]
            )
            if (
                current_facts.matrix_identity != snapshot_identity
                or core_current_health.namespace
                != snapshot_identity.namespace
            ):
                raise RuntimeError(
                    "foundation_runtime_surface_projection_identity_mismatch"
                )
        except Exception:
            current_facts = None
            core_current_health = None
            core_valid = False
            core_snapshot = {}
    try:
        published_at = (
            float(envelope.get("published_at") or 0.0) if core_valid else 0.0
        )
    except Exception:
        published_at = 0.0
    current_age_s = (
        max(0.0, observed_at - published_at) if published_at > 0.0 else None
    )
    current_fresh = bool(
        core_valid
        and current_age_s is not None
        and current_age_s <= _RUNTIME_SURFACE_MAX_AGE_S
    )
    registers = (
        core_snapshot.get("registers")
        if isinstance(core_snapshot.get("registers"), dict)
        else {}
    )

    def register_value(name: str) -> dict[str, Any]:
        row = registers.get(name) if isinstance(registers, dict) else None
        value = row.get("value") if isinstance(row, dict) else None
        return dict(value) if isinstance(value, dict) else {}

    declared_lines = (
        tuple(
            sorted(
                {
                    fact.line
                    for fact in current_facts.facts
                }
            )
        )
        if current_facts is not None
        else ()
    )
    accepted_current_lines = (
        {
            line: core_current_line_values(current_facts, line)
            for line in declared_lines
        }
        if current_facts is not None
        else {}
    )
    accepted_current_line_metadata = (
        {
            line: core_current_line_metadata(current_facts, line)
            for line in declared_lines
        }
        if current_facts is not None
        else {}
    )
    register_values = {
        name: register_value(name)
        for name in (REGISTER_CA, REGISTER_LATENCY, REGISTER_QUALITY)
    }

    health_path = root / RUNTIME_HEALTH_FILENAME
    health_value = evidence.get("runtime_health")
    runtime_health = dict(health_value) if isinstance(health_value, dict) else {}
    health_status = dict(
        (evidence.get("source_status") or {}).get("runtime_health") or {}
    )
    runtime_source = str(health_status.get("disposition") or "missing")
    runtime_valid = bool(
        runtime_health.get("schema") == RUNTIME_HEALTH_SCHEMA
        and runtime_health.get("owner") == RUNTIME_HEALTH_OWNER
    )
    if not runtime_valid:
        runtime_health = {}
    try:
        runtime_ts = float(runtime_health.get("ts") or 0.0)
    except Exception:
        runtime_ts = 0.0
    runtime_age_s = (
        max(0.0, observed_at - runtime_ts) if runtime_ts > 0.0 else None
    )

    degraded_state = ""
    if current_source == "invalid_contract":
        degraded_state = "INVALID_SCHEMA"
    elif not envelope:
        degraded_state = "MISSING"
    elif not core_valid:
        degraded_state = "INVALID_SCHEMA"
    elif not current_fresh:
        degraded_state = "STALE"

    return {
        "schema": "FoundationRuntimeSurfaceBundle.v1",
        "owner": "arch4.foundation.runtime_surface_reader",
        "current_state": dict(envelope) if core_valid else {},
        "core_snapshot": dict(core_snapshot),
        "current_state_source": current_source,
        "current_state_path": str(current_path),
        "current_state_age_s": current_age_s,
        "current_state_fresh": current_fresh,
        "current_state_degraded_state": degraded_state,
        "runtime": dict(runtime_health),
        "runtime_health": dict(runtime_health),
        "runtime_health_source": runtime_source,
        "runtime_health_path": str(health_path),
        "runtime_health_age_s": runtime_age_s,
        "runtime_registers": dict(registers),
        "accepted_current_facts": (
            current_facts.to_payload()
            if current_facts is not None
            else {}
        ),
        "modem_live": {
            "lines": accepted_current_lines,
            "line_metadata": accepted_current_line_metadata,
        },
        "register_values": register_values,
        "core_health": {
            "current": (
                core_current_health.to_payload()
                if core_current_health is not None
                else {}
            ),
        },
    }


def summarize_foundation_runtime_health_status(payload: Any) -> dict[str, Any]:
    """Summarize one Foundation runtime-health payload without old sidecar code."""

    data = payload if isinstance(payload, dict) else {}
    raw_phase = str(data.get("phase", "") or "").strip()
    try:
        cycle_count = int(data.get("cycle_count") or 0)
    except Exception:
        cycle_count = 0
    try:
        last_cycle_completed_ts = float(data.get("last_cycle_completed_ts") or 0.0)
    except Exception:
        last_cycle_completed_ts = 0.0
    try:
        last_cycle_ts = float(data.get("last_cycle_ts") or 0.0)
    except Exception:
        last_cycle_ts = 0.0
    has_seen_cycle = bool(cycle_count > 0 or last_cycle_completed_ts > 0.0 or last_cycle_ts > 0.0)
    return {
        "raw_phase": raw_phase or "unknown",
        "status_phase": _runtime_status_phase(raw_phase, cycle_count=1 if has_seen_cycle else 0),
        "cycle_in_progress": raw_phase == "cycle_started",
        "has_completed_cycle": has_seen_cycle,
    }


def require_foundation_current_state_client(owner: object) -> FoundationCurrentStateClient:
    client = getattr(owner, "current_state", None)
    if not isinstance(client, FoundationCurrentStateClient):
        raise RuntimeError("foundation_current_state_client_unavailable")
    return client


def require_foundation_runtime_health_service(
    owner: object,
) -> FoundationRuntimeHealthService:
    service = getattr(owner, "runtime_health", None)
    if not isinstance(service, FoundationRuntimeHealthService):
        raise RuntimeError("foundation_runtime_health_service_unavailable")
    return service


def require_cgf_evidence_store(owner: object) -> CgfEvidenceStore:
    store = getattr(owner, "diagnostics", None)
    if not isinstance(store, CgfEvidenceStore):
        raise RuntimeError("foundation_cgf_evidence_store_unavailable")
    return store


__all__ = (
    "ArtifactWriterStatus",
    "CoreCurrentStatePublisher",
    "CURRENT_STATE_FILENAME",
    "CURRENT_STATE_OWNER",
    "CURRENT_STATE_MAX_BYTES",
    "CURRENT_STATE_SCHEMA",
    "CGF_EVIDENCE_FILENAME",
    "EVENT_OWNER",
    "EVENT_REQUIRED_FIELDS",
    "EVENT_SCHEMA",
    "EVENT_SEVERITIES",
    "EVENT_TEXT_FIELD_LIMITS",
    "MAX_EVENT_RECORD_BYTES",
    "FOUNDATION_CURRENT_STATE_CLIENT_ID",
    "CgfEvidenceStore",
    "FoundationCurrentStateClient",
    "FoundationRuntimeHealthPublisher",
    "FoundationRuntimeHealthService",
    "FoundationRuntimeHealthSnapshot",
    "RUNTIME_HEALTH_FILENAME",
    "RUNTIME_HEALTH_OWNER",
    "RUNTIME_HEALTH_MAX_BYTES",
    "RUNTIME_HEALTH_SCHEMA",
    "SHUTDOWN_RECEIPT_FILENAME",
    "SHUTDOWN_RECEIPT_OWNER",
    "SHUTDOWN_RECEIPT_SCHEMA",
    "foundation_runtime_mode",
    "foundation_diagnostic_event_record_errors",
    "foundation_write_authority",
    "publish_shutdown_receipt_artifact",
    "core_current_line_metadata",
    "core_current_line_values",
    "read_foundation_runtime_surface_bundle",
    "read_cgf_evidence_events",
    "summarize_foundation_runtime_health_status",
    "require_foundation_current_state_client",
    "require_cgf_evidence_store",
    "require_foundation_runtime_health_service",
)
