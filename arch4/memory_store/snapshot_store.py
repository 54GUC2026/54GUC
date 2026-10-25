from __future__ import annotations

import json
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Optional

from arch4.atomic_io import atomic_write_text

_COUNTER_LOCK = Lock()
_PATH_COUNTERS: dict[Path, dict[str, int]] = {}
_PATH_WRITE_LOCKS: dict[Path, RLock] = {}
_GLOBAL_WRITE_SEQ = 0


def _store_path_key(path: Path | str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target


def _counter_for(path: Path) -> dict[str, int]:
    key = _store_path_key(path)
    with _COUNTER_LOCK:
        return _PATH_COUNTERS.setdefault(
            key,
            {
                "write_count": 0,
                "bytes_written": 0,
                "last_write_bytes": 0,
                "last_write_seq": 0,
            },
        )


def _write_lock_for(path: Path) -> RLock:
    key = _store_path_key(path)
    with _COUNTER_LOCK:
        lock = _PATH_WRITE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _PATH_WRITE_LOCKS[key] = lock
        return lock


class JsonSnapshotStore:
    """
    Atomic JSON snapshot read/write helper.

    Phase 1 uses this behind existing snapshot file names so runtime behavior
    and external tooling remain unchanged.
    """

    def __init__(self, root_dir: Path | str, filename: str):
        self.root_dir = _store_path_key(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.filename = str(filename)
        self.path = self.root_dir / self.filename
        self._write_lock = _write_lock_for(self.path)
        _counter_for(self.path)

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Optional[Any]:
        if not self.path.exists():
            return None
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def write(self, payload: Any, *, indent: Optional[int] = 2, sort_keys: bool = False) -> None:
        encoded = json.dumps(payload, indent=indent, sort_keys=sort_keys)
        with self._write_lock:
            atomic_write_text(self.path, encoded)
        size = len(encoded.encode("utf-8"))
        counter = _counter_for(self.path)
        with _COUNTER_LOCK:
            global _GLOBAL_WRITE_SEQ
            _GLOBAL_WRITE_SEQ += 1
            counter["write_count"] += 1
            counter["bytes_written"] += size
            counter["last_write_bytes"] = size
            counter["last_write_seq"] = int(_GLOBAL_WRITE_SEQ)

    def stats(self) -> dict[str, int | str]:
        try:
            current_size = int(self.path.stat().st_size) if self.path.exists() else 0
        except Exception:
            current_size = 0
        counter = _counter_for(self.path)
        with _COUNTER_LOCK:
            write_count = int(counter.get("write_count", 0) or 0)
            bytes_written = int(counter.get("bytes_written", 0) or 0)
            last_write_bytes = int(counter.get("last_write_bytes", 0) or 0)
            last_write_seq = int(counter.get("last_write_seq", 0) or 0)
        return {
            "path": str(self.path),
            "write_count": write_count,
            "bytes_written": bytes_written,
            "last_write_bytes": last_write_bytes,
            "last_write_seq": last_write_seq,
            "current_size_bytes": int(current_size),
        }
