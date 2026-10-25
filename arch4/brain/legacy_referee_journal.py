"""Read-only forensic access to quarantined pre-layered Referee rows."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OWNER = "arch4.brain.legacy_referee_journal"
SCHEMA = "LegacyRefereeJournalForensicSummary.v1"
FILENAME = "referee_journal.jsonl"


@dataclass(frozen=True, slots=True)
class LegacyRefereeJournalEntry:
    ts: float
    decision_trace_id: str
    outcome_code: str
    outcome_level: str
    lever: str
    band: int
    hour: int
    raw: dict[str, Any]


class LegacyRefereeJournalReader:
    """Read legacy rows without pruning, rewriting or learning from them."""

    def __init__(
        self,
        memory_dir: Path | str,
        *,
        retention_days: int = 7,
    ) -> None:
        self.path = Path(memory_dir).expanduser().resolve() / FILENAME
        self.retention_days = max(1, int(retention_days or 7))

    def entries(self, *, now: float | None = None) -> list[LegacyRefereeJournalEntry]:
        if not self.path.exists():
            return []
        cutoff = float(time.time() if now is None else now) - (
            self.retention_days * 86400.0
        )
        entries: list[LegacyRefereeJournalEntry] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                ts = _float(raw.get("ts"))
                if ts < cutoff:
                    continue
                entries.append(
                    LegacyRefereeJournalEntry(
                        ts=ts,
                        decision_trace_id=str(
                            raw.get("decision_trace_id") or ""
                        ),
                        outcome_code=str(raw.get("outcome_code") or ""),
                        outcome_level=str(raw.get("outcome_level") or ""),
                        lever=str(raw.get("lever") or ""),
                        band=_int(raw.get("band")),
                        hour=_int(raw.get("hour")) % 24,
                        raw=dict(raw),
                    )
                )
        return entries

    def summary(self, *, now: float | None = None) -> dict[str, Any]:
        rows = self.entries(now=now)
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "source": str(self.path),
            "row_count": len(rows),
            "active_learning": False,
            "quarantine_reason": (
                "pre_layered_binary_referee_rows_have_no_exact_reward_scope"
            ),
            "latest_ts": max((row.ts for row in rows), default=0.0),
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "LegacyRefereeJournalEntry",
    "LegacyRefereeJournalReader",
    "OWNER",
    "SCHEMA",
]
