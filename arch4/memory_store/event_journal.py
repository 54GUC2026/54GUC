from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional

from arch4.atomic_io import append_text_durable, atomic_write_text

_JOURNAL_COUNTER_LOCK = Lock()
_JOURNAL_WRITE_SEQ = 0


def _next_write_seq() -> int:
    global _JOURNAL_WRITE_SEQ
    with _JOURNAL_COUNTER_LOCK:
        _JOURNAL_WRITE_SEQ += 1
        return int(_JOURNAL_WRITE_SEQ)


class JsonlEventJournal:
    """Bounded durable JSONL boundary for current-run receipts and cursors."""

    DEFAULT_MAX_RECORDS = 4096
    DEFAULT_MAX_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        root_dir: Path | str,
        filename: str,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.filename = str(filename)
        self.path = self.root_dir / self.filename
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(4096, int(max_bytes))
        self._append_count = 0
        self._rewrite_count = 0
        self._bytes_appended = 0
        self._bytes_rewritten = 0
        self._last_write_bytes = 0
        self._last_write_seq = 0
        self._retained_count = sum(
            1 for _ in self.iter_events_reverse(limit=self.max_records + 1)
        )
        if self._retained_count > self.max_records or self._current_size() > self.max_bytes:
            self._compact_to_budget()

    def append(
        self,
        event: Mapping[str, Any],
        *,
        durable: bool = True,
    ) -> None:
        line = json.dumps(dict(event), ensure_ascii=True) + "\n"
        if durable:
            append_text_durable(self.path, line)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        size = len(line.encode("utf-8"))
        self._last_write_seq = _next_write_seq()
        self._append_count += 1
        self._bytes_appended += size
        self._last_write_bytes = size
        self._retained_count += 1
        if self._retained_count > self.max_records or self._current_size() > self.max_bytes:
            self._compact_to_budget()

    def rewrite(self, events: Iterable[Mapping[str, Any]]) -> None:
        total_bytes = 0
        lines: list[str] = []
        for event in events:
            line = json.dumps(dict(event), ensure_ascii=True) + "\n"
            lines.append(line)
            total_bytes += len(line.encode("utf-8"))
        atomic_write_text(self.path, "".join(lines))
        self._last_write_seq = _next_write_seq()
        self._rewrite_count += 1
        self._bytes_rewritten += total_bytes
        self._last_write_bytes = total_bytes
        self._retained_count = len(lines)

    def _current_size(self) -> int:
        try:
            return int(self.path.stat().st_size) if self.path.exists() else 0
        except OSError:
            return 0

    def _compact_to_budget(self) -> None:
        """Retain the newest complete mapping records within both budgets."""

        newest: list[Dict[str, Any]] = []
        used = 0
        for row in self.iter_events_reverse(limit=self.max_records):
            encoded = json.dumps(dict(row), ensure_ascii=True) + "\n"
            size = len(encoded.encode("utf-8"))
            if newest and used + size > self.max_bytes:
                break
            if size > self.max_bytes:
                raise ValueError("jsonl_event_record_exceeds_journal_budget")
            newest.append(dict(row))
            used += size
        self.rewrite(reversed(newest))

    def count(self) -> int:
        """Return the valid-event sequence length, never a physical-line count.

        A persistence cursor is a count of decoded mapping events.  Malformed,
        truncated, blank, or non-object rows have no sequence number and can
        therefore never move a restart cursor past a later valid event.
        """

        return len(self.read_all())

    def malformed_count(self) -> int:
        """Return invalid nonblank physical rows for diagnostics only."""

        if not self.path.exists():
            return 0
        malformed = 0
        with open(self.path, "rb") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if not isinstance(raw, dict):
                    malformed += 1
        return malformed

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    if isinstance(raw, dict):
                        out.append(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Skip malformed lines — corrupted/truncated journal entry
                    continue
        return out

    def iter_events(self, *, limit: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        events = self.read_all()
        if limit is not None:
            events = events[-max(0, int(limit)) :]
        return iter(events)

    def iter_events_reverse(
        self,
        *,
        limit: Optional[int] = None,
        validator: Callable[[Mapping[str, Any]], None] | None = None,
        block_bytes: int = 64 * 1024,
    ) -> Iterator[Dict[str, Any]]:
        """Yield newest valid mapping rows without reading the whole journal.

        Complete-state journals restore from their newest checksum-valid row.
        Reading a multi-hundred-megabyte append history into a live process in
        order to find that row is not a legal status or restart dependency.
        A caller-supplied validator owns its schema/checksum contract; malformed
        JSON and rejected rows are skipped while the scan continues backwards.
        """

        maximum = None if limit is None else max(0, int(limit))
        if maximum == 0 or not self.path.exists():
            return
        chunk_size = max(1024, int(block_bytes or 0))
        yielded = 0
        with open(self.path, "rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            partial = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                partial = handle.read(read_size) + partial
                lines = partial.split(b"\n")
                partial = lines.pop(0)
                for raw_line in reversed(lines):
                    row = self._decode_mapping_line(raw_line)
                    if row is None:
                        continue
                    if validator is not None:
                        try:
                            validator(row)
                        except Exception:
                            continue
                    yield row
                    yielded += 1
                    if maximum is not None and yielded >= maximum:
                        return
            row = self._decode_mapping_line(partial)
            if row is not None:
                if validator is not None:
                    try:
                        validator(row)
                    except Exception:
                        return
                yield row

    def iter_from(self, start_index: int, *, limit: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        events = self.read_all()
        start = max(0, int(start_index or 0))
        if start:
            events = events[start:]
        if limit is not None:
            events = events[: max(0, int(limit))]
        return iter(events)

    @staticmethod
    def _decode_mapping_line(line: bytes) -> Dict[str, Any] | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            raw = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return dict(raw) if isinstance(raw, dict) else None

    def writer_status(self) -> dict[str, int | str]:
        """Return O(1) live-writer metrics without enumerating journal rows."""

        try:
            current_size = self._current_size()
        except Exception:
            current_size = 0
        return {
            "path": str(self.path),
            "append_count": int(self._append_count),
            "rewrite_count": int(self._rewrite_count),
            "write_count": int(self._append_count + self._rewrite_count),
            "bytes_appended": int(self._bytes_appended),
            "bytes_rewritten": int(self._bytes_rewritten),
            "bytes_written": int(self._bytes_appended + self._bytes_rewritten),
            "last_write_bytes": int(self._last_write_bytes),
            "last_write_seq": int(self._last_write_seq),
            "current_size_bytes": int(current_size),
            "retained_count": int(self._retained_count),
            "max_records": int(self.max_records),
            "max_bytes": int(self.max_bytes),
            "row_enumeration": "not_performed",
        }

    def stats(self) -> dict[str, int | str]:
        return {
            **self.writer_status(),
            "valid_event_count": int(self.count()),
            "malformed_row_count": int(self.malformed_count()),
            "row_enumeration": "complete",
        }
