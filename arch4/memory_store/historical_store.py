from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .snapshot_store import JsonSnapshotStore


class HistoricalStore:
    """
    Historical journal persistence boundary.

    Phase 4 moves historical state out of `memory.json` extras into its own
    snapshot while keeping legacy startup fallback available.
    """

    def __init__(self, root_dir: Path | str):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot = JsonSnapshotStore(self.root_dir, "historical.json")

    @property
    def path(self) -> Path:
        return self.snapshot.path

    def exists(self) -> bool:
        return self.snapshot.exists()

    def read_state(self) -> Optional[Dict[str, Any]]:
        raw = self.snapshot.read()
        return raw if isinstance(raw, dict) else None

    def write_state(self, payload: Dict[str, Any]) -> None:
        self.snapshot.write(dict(payload or {}), indent=2, sort_keys=True)

    def stats(self) -> Dict[str, Any]:
        return self.snapshot.stats()
