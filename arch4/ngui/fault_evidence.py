"""Keep bounded, privacy-scrubbed NGUI fault evidence in the parent."""

from __future__ import annotations

import json
import math
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from arch4.foundation.atomic_bounded_journal import (
    AtomicBoundedJournal,
)
from arch4.privacy_scrub import scrub_record, scrub_text


FILENAME = "ngui_faults.jsonl"
SCHEMA = "NGUIFaultEvidence.v1"
OWNER = "54guc.ngui.fault_evidence"
MAX_RECORDS = 256
MAX_BYTES = 256 * 1024
REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "sequence",
        "kind",
        "ts",
        "event_owner",
        "fault_owner",
    }
)
EVENT_KINDS = frozenset({"controller_fault", "controller_recovery"})


def ngui_fault_evidence_record_errors(
    record: object,
    *,
    previous_sequence: int = 0,
    record_bytes: int = 0,
) -> tuple[str, ...]:
    """Return the result of recording NGUI fault evidence."""

    if not isinstance(record, dict):
        return ("top_level_object_required",)
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(record))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if record.get("schema") != SCHEMA:
        errors.append("schema_invalid")
    if record.get("owner") != OWNER:
        errors.append("owner_invalid")
    sequence = record.get("sequence")
    if type(sequence) is not int or sequence <= 0:
        errors.append("sequence_invalid")
    elif previous_sequence and sequence <= previous_sequence:
        errors.append("sequence_not_monotonic")
    if record.get("kind") not in EVENT_KINDS:
        errors.append("kind_invalid")
    timestamp = record.get("ts")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) <= 0.0
    ):
        errors.append("timestamp_invalid")
    for field in ("event_owner", "fault_owner"):
        if not isinstance(record.get(field), str):
            errors.append(f"{field}_string_required")
    if record_bytes > MAX_BYTES:
        errors.append("record_byte_budget_exceeded")
    return tuple(errors)


class NGUIFaultEvidenceJournal:
    """Write faults found in the NGUI parent to durable evidence."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        report_error: Callable[[str], None],
        max_records: int = MAX_RECORDS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        if not callable(report_error):
            raise TypeError("ngui_fault_evidence_report_error_required")
        self.path = Path(runtime_dir).resolve() / FILENAME
        self._report_error = report_error
        self._max_records = max(1, int(max_records))
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = RLock()
        self._journal = AtomicBoundedJournal(
            self.path,
            owner=OWNER,
            max_records=self._max_records,
            max_bytes=self._max_bytes,
            validator=self._validate_atomic_record,
            recovery_reporter=self._report_recovery_failure,
        )
        self._next_sequence = self._journal.status().last_committed_sequence + 1

    def _report_recovery_failure(
        self,
        operation: str,
        exc: BaseException,
    ) -> None:
        self._report_error(
            "NGUI fault evidence recovery failed: "
            f"{operation}: {type(exc).__name__}: {exc}"
        )

    def _validate_atomic_record(
        self,
        record: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> None:
        encoded = json.dumps(
            dict(record),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"
        errors = ngui_fault_evidence_record_errors(
            record,
            previous_sequence=(
                int(previous.get("sequence") or 0)
                if isinstance(previous, dict)
                else 0
            ),
            record_bytes=len(encoded.encode("utf-8")),
        )
        if errors:
            raise ValueError(
                "ngui_fault_evidence_record_invalid:" + ",".join(errors)
            )

    def log_event(self, kind: str, **payload: Any) -> None:
        event_kind = str(kind or "").strip()
        if event_kind not in {"controller_fault", "controller_recovery"}:
            raise ValueError("ngui_fault_evidence_event_kind_invalid")
        with self._lock:
            sequence = self._next_sequence
            event_payload = scrub_record(dict(payload))
            event_payload = {
                key: scrub_text(value) if isinstance(value, str) else value
                for key, value in event_payload.items()
            }
            event_owner = str(event_payload.pop("event_owner", "") or "")
            fault_owner = str(event_payload.pop("owner", "") or "")
            record = {
                "schema": SCHEMA,
                "owner": OWNER,
                "sequence": sequence,
                "kind": event_kind,
                "event_owner": event_owner,
                "fault_owner": fault_owner,
                **event_payload,
            }
            try:
                self._journal.commit(record)
            except Exception as exc:
                self._report_error(
                    "NGUI fault evidence write failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise
            self._next_sequence += 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            journal = self._journal.status()
            return {
                "schema": "NGUIFaultEvidenceStatus.v1",
                "owner": OWNER,
                "path": str(self.path),
                "next_sequence": self._next_sequence,
                "write_count": journal.commit_count,
                "failure_count": journal.failure_count,
                "last_error": journal.last_error,
                "max_records": self._max_records,
                "max_bytes": self._max_bytes,
                "mechanism": "foundation_atomic_bounded_journal_adapter",
            }


__all__ = (
    "FILENAME",
    "EVENT_KINDS",
    "MAX_BYTES",
    "MAX_RECORDS",
    "NGUIFaultEvidenceJournal",
    "OWNER",
    "REQUIRED_FIELDS",
    "SCHEMA",
    "ngui_fault_evidence_record_errors",
)
