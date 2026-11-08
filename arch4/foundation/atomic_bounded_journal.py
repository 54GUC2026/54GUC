"""Bounded JSONL evidence journal. Each commit replaces the retained file atomically.
Memory state advances only after the file commit succeeds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock
from typing import Any

from arch4.atomic_io import atomic_write_text, cleanup_abandoned_atomic_temps


ATOMIC_BOUNDED_JOURNAL_OWNER = "54guc.foundation.atomic_bounded_journal"
RecordValidator = Callable[[Mapping[str, Any], Mapping[str, Any] | None], None]
AtomicTextWriter = Callable[..., None]
RecoveryReporter = Callable[[str, BaseException], None]


@dataclass(frozen=True, slots=True)
class AtomicBoundedJournalStatus:
    owner: str
    path: str
    retained_count: int
    retained_bytes: int
    max_records: int
    max_bytes: int
    commit_count: int
    failure_count: int
    last_error: str
    last_committed_sequence: int
    recovery_disposition: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "path": self.path,
            "retained_count": self.retained_count,
            "retained_bytes": self.retained_bytes,
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
            "commit_count": self.commit_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "last_committed_sequence": self.last_committed_sequence,
            "recovery_disposition": self.recovery_disposition,
            "mechanism": "atomic_complete_file_replace",
        }


class AtomicBoundedJournal:
    """Own one bounded JSONL artifact as an atomic complete-file snapshot."""

    def __init__(
        self,
        path: str | Path,
        *,
        owner: str,
        max_records: int,
        max_bytes: int,
        validator: RecordValidator,
        writer: AtomicTextWriter = atomic_write_text,
        recovery_reporter: RecoveryReporter | None = None,
    ) -> None:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("atomic_bounded_journal_owner_required")
        if not callable(validator):
            raise TypeError("atomic_bounded_journal_validator_required")
        if not callable(writer):
            raise TypeError("atomic_bounded_journal_writer_required")
        self.path = Path(path).resolve()
        self.owner = normalized_owner
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1024, int(max_bytes))
        self._validator = validator
        self._writer = writer
        self._recovery_reporter = recovery_reporter
        self._lock = RLock()
        self._records: tuple[dict[str, Any], ...] = ()
        self._encoded_lines: tuple[str, ...] = ()
        self._commit_count = 0
        self._failure_count = 0
        self._last_error = ""
        self._recovery_disposition = "new"
        try:
            cleanup_abandoned_atomic_temps(self.path)
            self._recover_existing()
        except Exception as exc:
            self._failure_count += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._report_recovery("recover_atomic_bounded_journal", exc)
            self._reset_corrupt()

    @staticmethod
    def _encode(record: Mapping[str, Any]) -> str:
        return (
            json.dumps(
                dict(record),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    def _report_recovery(self, operation: str, exc: BaseException) -> None:
        if self._recovery_reporter is None:
            return
        self._recovery_reporter(operation, exc)

    def _reset_corrupt(self) -> None:
        self._writer(self.path, "")
        self._records = ()
        self._encoded_lines = ()
        self._recovery_disposition = "corrupt_state_reported_and_reset"

    def _recover_existing(self) -> None:
        if not self.path.is_file():
            self._recovery_disposition = "new"
            return
        decoded: list[dict[str, Any]] = []
        lines: list[str] = []
        previous: Mapping[str, Any] | None = None
        for line_number, raw_line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except Exception as exc:
                raise ValueError(
                    f"atomic_bounded_journal_invalid_json:line={line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"atomic_bounded_journal_object_required:line={line_number}"
                )
            self._validator(record, previous)
            encoded = self._encode(record)
            decoded.append(dict(record))
            lines.append(encoded)
            previous = record
        retained_records, retained_lines = self._bounded(decoded, lines)
        if tuple(lines) != retained_lines:
            self._writer(self.path, "".join(retained_lines))
            self._recovery_disposition = "valid_state_bounded_atomically"
        else:
            self._recovery_disposition = "valid_state_restored"
        self._records = retained_records
        self._encoded_lines = retained_lines

    def _bounded(
        self,
        records: list[dict[str, Any]],
        lines: list[str],
    ) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
        if len(records) != len(lines):
            raise RuntimeError("atomic_bounded_journal_internal_shape_mismatch")
        retained_records: list[dict[str, Any]] = []
        retained_lines: list[str] = []
        retained_bytes = 0
        for record, line in reversed(tuple(zip(records, lines, strict=True))):
            size = len(line.encode("utf-8"))
            if size > self.max_bytes:
                if not retained_lines:
                    raise ValueError(
                        "atomic_bounded_journal_record_exceeds_byte_budget"
                    )
                continue
            if retained_lines and retained_bytes + size > self.max_bytes:
                break
            if len(retained_lines) >= self.max_records:
                break
            retained_records.append(dict(record))
            retained_lines.append(line)
            retained_bytes += size
        retained_records.reverse()
        retained_lines.reverse()
        return tuple(retained_records), tuple(retained_lines)

    def commit(self, record: Mapping[str, Any]) -> None:
        """Atomically commit one record and its retention result."""

        if not isinstance(record, Mapping):
            raise TypeError("atomic_bounded_journal_record_mapping_required")
        candidate = dict(record)
        with self._lock:
            previous = self._records[-1] if self._records else None
            self._validator(candidate, previous)
            encoded = self._encode(candidate)
            records, lines = self._bounded(
                [*self._records, candidate],
                [*self._encoded_lines, encoded],
            )
            try:
                self._writer(self.path, "".join(lines))
            except Exception as exc:
                self._failure_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            self._records = records
            self._encoded_lines = lines
            self._commit_count += 1
            self._last_error = ""

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(record) for record in self._records)

    def status(self) -> AtomicBoundedJournalStatus:
        with self._lock:
            last_sequence = 0
            if self._records:
                value = self._records[-1].get("sequence")
                if type(value) is int:
                    last_sequence = value
            return AtomicBoundedJournalStatus(
                owner=self.owner,
                path=str(self.path),
                retained_count=len(self._records),
                retained_bytes=sum(
                    len(line.encode("utf-8")) for line in self._encoded_lines
                ),
                max_records=self.max_records,
                max_bytes=self.max_bytes,
                commit_count=self._commit_count,
                failure_count=self._failure_count,
                last_error=self._last_error,
                last_committed_sequence=last_sequence,
                recovery_disposition=self._recovery_disposition,
            )


__all__ = (
    "ATOMIC_BOUNDED_JOURNAL_OWNER",
    "AtomicBoundedJournal",
    "AtomicBoundedJournalStatus",
)
