"""Action-history storage helpers for ARCH4 memory."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from arch4.interfaces import ActionRecord


class ActionHistoryStore:
    """Bounded action-history buffer with owner-backed lifecycle projections."""

    def __init__(self, *, max_history: int = 4000) -> None:
        self._max_history = max(1, int(max_history or 1))
        self._records: List[ActionRecord] = []

    @property
    def max_history(self) -> int:
        return self._max_history

    def resize(self, max_history: int) -> None:
        self._max_history = max(1, int(max_history or 1))
        self._trim()

    @property
    def records(self) -> List[ActionRecord]:
        return self._records

    def set_records(self, records: Iterable[ActionRecord]) -> None:
        self._records = list(records or [])
        self._trim()

    def append(self, record: ActionRecord) -> None:
        self._records.append(record)
        self._trim()

    def upsert(self, record: ActionRecord) -> None:
        tid = str(getattr(record, "decision_trace_id", "") or "")
        for idx in range(len(self._records) - 1, -1, -1):
            existing = self._records[idx]
            existing_tid = str(getattr(existing, "decision_trace_id", "") or "")
            if tid and existing_tid == tid:
                self._records[idx] = record
                return
            if (
                not tid
                and abs(
                    float(getattr(existing, "timestamp", 0.0) or 0.0)
                    - float(getattr(record, "timestamp", 0.0) or 0.0)
                )
                < 0.001
                and getattr(existing, "lever", None) == getattr(record, "lever", None)
                and dict(getattr(existing, "action_params", {}) or {})
                == dict(getattr(record, "action_params", {}) or {})
            ):
                self._records[idx] = record
                return
        self.append(record)

    def latest(self) -> ActionRecord | None:
        return self._records[-1] if self._records else None

    def reversed(self) -> Iterable[ActionRecord]:
        return reversed(self._records)

    def recent_lifecycle(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        n = max(1, int(limit or 1))
        results: List[Dict[str, Any]] = []
        for rec in reversed(self._records):
            stage = str(getattr(rec, "objective_stage", "") or "")
            if not stage:
                continue
            results.append(
                {
                    "ts": float(getattr(rec, "timestamp", 0.0) or 0.0),
                    "stage": stage,
                    "status": str(getattr(rec, "objective_status", "") or ""),
                    "objective_id": str(getattr(rec, "proposal_objective_id", "") or ""),
                    "lever": str(rec.lever.name if hasattr(rec.lever, "name") else ""),
                    "intent": str(getattr(rec, "proposal_intent", "") or ""),
                    "reason": str(getattr(rec, "proposal_reason", "") or ""),
                    "decision_trace_id": str(getattr(rec, "decision_trace_id", "") or ""),
                    "until_ts": float(getattr(rec, "objective_until_ts", 0.0) or 0.0),
                    "provider_plmn": str(getattr(rec, "provider_plmn", "") or ""),
                    "provider_name": str(getattr(rec, "provider_name", "") or ""),
                    "metadata": dict(getattr(rec, "objective_metadata", {}) or {}),
                }
            )
            if len(results) >= n:
                break
        return list(reversed(results))

    def _trim(self) -> None:
        if len(self._records) > self._max_history:
            self._records = self._records[-self._max_history :]
