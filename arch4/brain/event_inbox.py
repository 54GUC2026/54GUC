"""Brain-owned internal event inbox.

This module is deliberately free of modem I/O. Producers publish facts that
already exist in runtime state; the runtime clock drains and classifies them on
Brain time instead of waiting for the next modem/data tick.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class BrainEvent:
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    due_ts: float = 0.0
    priority: int = 50
    dedupe_key: str = ""
    created_ts: float = 0.0
    event_id: str = ""
    sequence: int = 0
    attempts: int = 0
    last_error: str = ""

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "due_ts": round(float(self.due_ts or 0.0), 3),
            "priority": int(self.priority),
            "dedupe_key": self.dedupe_key,
            "created_ts": round(float(self.created_ts or 0.0), 3),
            "sequence": int(self.sequence),
            "attempts": int(self.attempts),
            "last_error": str(self.last_error or ""),
        }


class BrainEventInbox:
    """Bounded, deduped event queue for internal Brain work."""

    def __init__(
        self,
        *,
        max_pending: int = 512,
        max_retries: int = 5,
        retry_base_s: float = 0.25,
        max_dead_letters: int = 128,
    ):
        self.max_pending = max(1, int(max_pending or 1))
        self.max_retries = max(0, int(max_retries or 0))
        self.retry_base_s = max(0.001, float(retry_base_s or 0.25))
        self._lock = threading.RLock()
        self._pending: list[BrainEvent] = []
        self._dead_letters: deque[BrainEvent] = deque(
            maxlen=max(1, int(max_dead_letters or 1))
        )
        self._handlers: dict[str, Callable[[BrainEvent], Any]] = {}
        self._sequence = 0
        self._last_drain: dict[str, Any] = {}
        self._last_error = ""
        self._published_count = 0
        self._processed_count = 0
        self._failed_count = 0
        self._dropped_count = 0
        self._superseded_count = 0
        self._retried_count = 0
        self._dead_letter_count = 0

    def register_handler(self, kind: str, handler: Callable[[BrainEvent], Any]) -> None:
        kind_key = str(kind or "").strip()
        if not kind_key:
            raise ValueError("brain event handler kind must be non-empty")
        if not callable(handler):
            raise TypeError("brain event handler must be callable")
        with self._lock:
            self._handlers[kind_key] = handler

    def publish(
        self,
        kind: str,
        *,
        payload: Mapping[str, Any] | None = None,
        source: str = "",
        due_ts: float | None = None,
        priority: int = 50,
        dedupe_key: str = "",
        event_id: str = "",
        created_ts: float | None = None,
    ) -> BrainEvent:
        kind_key = str(kind or "").strip()
        if not kind_key:
            raise ValueError("brain event kind must be non-empty")
        now = time.time() if created_ts is None else float(created_ts)
        due = now if due_ts is None else float(due_ts)
        with self._lock:
            self._sequence += 1
            dedupe = str(dedupe_key or "").strip()
            if dedupe:
                before = len(self._pending)
                self._pending = [event for event in self._pending if event.dedupe_key != dedupe]
                superseded = before - len(self._pending)
                self._superseded_count += superseded
            event = BrainEvent(
                kind=kind_key,
                payload=dict(payload or {}),
                source=str(source or ""),
                due_ts=due,
                priority=int(priority),
                dedupe_key=dedupe,
                created_ts=now,
                event_id=str(event_id or uuid4().hex),
                sequence=self._sequence,
            )
            self._pending.append(event)
            self._published_count += 1
            self._trim_pending_locked()
            return event

    def drain_due(
        self,
        *,
        now: float | None = None,
        max_events: int = 32,
    ) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        limit = max(1, int(max_events or 1))
        with self._lock:
            due, future = self._partition_due_locked(now_ts)
            due.sort(key=lambda event: (int(event.priority), float(event.due_ts), int(event.sequence)))
            selected = due[:limit]
            self._pending = due[limit:] + future
        processed = 0
        failed = 0
        retried = 0
        dead_lettered = 0
        errors: list[str] = []
        drained_status: list[dict[str, Any]] = []
        for event in selected:
            drained_status.append(event.to_status_dict())
            handler = None
            with self._lock:
                handler = self._handlers.get(event.kind)
            try:
                if handler is None:
                    raise RuntimeError(f"brain_event_handler_missing:{event.kind}")
                handler(event)
                processed += 1
            except Exception as exc:
                failed += 1
                error = str(exc or type(exc).__name__ or "brain_event_handler_failed")
                errors.append(error)
                retry = replace(
                    event,
                    attempts=int(event.attempts or 0) + 1,
                    last_error=error[:500],
                    due_ts=now_ts
                    + self.retry_base_s
                    * float(2 ** min(8, int(event.attempts or 0))),
                )
                with self._lock:
                    if retry.attempts <= self.max_retries:
                        self._pending.append(retry)
                        self._retried_count += 1
                        retried += 1
                        self._trim_pending_locked()
                    else:
                        self._dead_letters.append(retry)
                        self._dead_letter_count += 1
                        dead_lettered += 1
        with self._lock:
            self._processed_count += processed
            self._failed_count += failed
            self._last_error = "; ".join(error for error in errors if error)[:500]
            self._last_drain = {
                "ts": round(now_ts, 3),
                "due": len(due),
                "drained": len(selected),
                "processed": processed,
                "failed": failed,
                "retried": retried,
                "dead_lettered": dead_lettered,
                "pending": len(self._pending),
                "last_error": self._last_error,
                "events": drained_status,
            }
            return dict(self._last_drain)

    def pending_count(self, *, now: float | None = None) -> int:
        del now
        with self._lock:
            return len(self._pending)

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        with self._lock:
            due_count = sum(1 for event in self._pending if float(event.due_ts) <= now_ts)
            next_due_ts = min((float(event.due_ts) for event in self._pending), default=0.0)
            return {
                "schema_version": "brain_event_inbox.v1",
                "pending": len(self._pending),
                "due": due_count,
                "next_due_ts": round(next_due_ts, 3) if next_due_ts > 0.0 else 0.0,
                "published_count": self._published_count,
                "processed_count": self._processed_count,
                "failed_count": self._failed_count,
                "dropped_count": self._dropped_count,
                "superseded_count": self._superseded_count,
                "retried_count": self._retried_count,
                "dead_letter_count": self._dead_letter_count,
                "dead_letters": [
                    event.to_status_dict()
                    for event in list(self._dead_letters)[-8:]
                ],
                "last_error": self._last_error,
                "last_drain": dict(self._last_drain),
                "registered_handlers": sorted(self._handlers),
            }

    def _partition_due_locked(self, now_ts: float) -> tuple[list[BrainEvent], list[BrainEvent]]:
        due: list[BrainEvent] = []
        future: list[BrainEvent] = []
        for event in self._pending:
            if float(event.due_ts) <= now_ts:
                due.append(event)
            else:
                future.append(event)
        return due, future

    def _trim_pending_locked(self) -> None:
        overflow = len(self._pending) - self.max_pending
        if overflow <= 0:
            return
        self._pending.sort(key=lambda event: (int(event.priority), float(event.due_ts), int(event.sequence)))
        dropped = self._pending[-overflow:]
        del self._pending[-overflow:]
        self._dropped_count += overflow
        for event in dropped:
            self._dead_letters.append(
                replace(
                    event,
                    last_error="brain_event_inbox_capacity_drop",
                )
            )
            self._dead_letter_count += 1
