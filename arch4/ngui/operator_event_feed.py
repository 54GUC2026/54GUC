"""Dashboard event feed for state transitions, independent of the GUI toolkit."""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import RLock
import time


@dataclass(frozen=True, slots=True)
class OperatorEvent:
    timestamp: float
    text: str
    tone: str = "info"

    def line(self) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(self.timestamp))} {self.text}"


class NGUIOperatorEventFeed:
    """Keep bounded, deduplicated operator messages for the Dashboard."""

    def __init__(
        self,
        *,
        max_lines: int = 100,
        prune_lines: int | None = None,
        event_dedup_window_s: float = 10.0,
        suppressed_dedup_window_s: float = 30.0,
    ) -> None:
        if max_lines < 1:
            raise ValueError("operator event feed requires at least one line")
        normalized_prune_lines = (
            min(80, int(max_lines))
            if prune_lines is None
            else int(prune_lines)
        )
        if normalized_prune_lines < 1 or normalized_prune_lines > max_lines:
            raise ValueError("operator event prune size must fit inside retention")
        self._events: list[OperatorEvent] = []
        self._max_lines = int(max_lines)
        self._prune_lines = normalized_prune_lines
        self._event_dedup_window_s = float(event_dedup_window_s)
        self._suppressed_dedup_window_s = float(suppressed_dedup_window_s)
        self._last_event_text = ""
        self._last_event_ts = 0.0
        self._last_suppressed_key = ""
        self._last_suppressed_ts = 0.0
        self._last_seen_by_text: dict[str, float] = {}
        self._internet_transitions: dict[tuple[str, float], float] = {}
        self._lock = RLock()

    def record(
        self,
        text: str,
        *,
        timestamp: float | None = None,
        tone: str = "info",
    ) -> bool:
        message = " ".join(str(text or "").split())
        if message == "Internet connection lost." or message.startswith("Internet connection restored after "):
            raise ValueError("operator_internet_transition_identity_required")
        if not message:
            return False
        now = float(time.time() if timestamp is None else timestamp)
        with self._lock:
            if (
                message in self._last_seen_by_text
                and now - self._last_seen_by_text[message]
                < self._event_dedup_window_s
            ):
                self._last_event_ts = now
                self._last_seen_by_text[message] = now
                return False
            self._append(now, message, tone)
            self._last_event_text = message
            self._last_event_ts = now
            self._last_seen_by_text[message] = now
            return True

    def record_internet_transition(
        self, *, started_at: float, restored_at: float | None = None,
    ) -> bool:
        """Render Core's interval once, including a loss missed between UI frames."""
        if type(started_at) not in {int, float} or not math.isfinite(started_at) or started_at <= 0:
            raise ValueError("operator_internet_onset_required")
        if restored_at is not None and (
            type(restored_at) not in {int, float} or not math.isfinite(restored_at)
            or restored_at < started_at
        ):
            raise ValueError("operator_internet_restore_invalid")
        with self._lock:
            changed = False
            transitions = [("lost", started_at, "Internet connection lost.", "warning")]
            if restored_at is not None:
                transitions.append(("restored", restored_at,
                    f"Internet connection restored after {restored_at - started_at:.1f} seconds.", "info"))
            for phase, timestamp, message, tone in transitions:
                key = (phase, started_at)
                if key in self._internet_transitions:
                    if self._internet_transitions[key] != timestamp:
                        raise ValueError("operator_internet_transition_conflict")
                    continue
                self._internet_transitions[key] = timestamp
                self._append(timestamp, message, tone)
                changed = True
            while len(self._internet_transitions) > 2 * self._max_lines:
                del self._internet_transitions[next(iter(self._internet_transitions))]
            return changed

    def record_operator_action(
        self,
        action: str,
        reason: str,
        *,
        suppressed: bool,
        failed: bool = False,
        timestamp: float | None = None,
    ) -> bool:
        now = float(time.time() if timestamp is None else timestamp)
        action_text = " ".join(str(action or "action").split())
        if action_text == "NR_BAND_LOCK":
            action_text = "NR band selection"
        reason_text = " ".join(str(reason or "").split())
        with self._lock:
            if failed:
                self._append(
                    now,
                    f"[FAILED] {action_text}: {reason_text}".rstrip(": "),
                    "warning",
                )
                return True
            if suppressed:
                key = f"{action_text}:{reason_text}"
                if (
                    key == self._last_suppressed_key
                    and now - self._last_suppressed_ts
                    < self._suppressed_dedup_window_s
                ):
                    return False
                self._append(
                    now,
                    f"[SUPPRESSED] {action_text}: {reason_text}".rstrip(": "),
                    "muted",
                )
                self._last_suppressed_key = key
                self._last_suppressed_ts = now
                return True
            self._append(
                now,
                f"ACTION {action_text}: {reason_text}".rstrip(": "),
                "action",
            )
            return True

    def text(self, *, limit: int | None = None) -> str:
        if limit is not None and limit < 1:
            raise ValueError("operator event text limit must be positive")
        with self._lock:
            events = tuple(self._events)
            if limit is not None:
                events = events[-int(limit) :]
        return "\n".join(event.line() for event in events)

    def snapshot(self) -> tuple[OperatorEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def reset_runtime_state(self) -> tuple[dict[str, object], dict[str, object]]:
        with self._lock:
            prior = self._reset_observation_unlocked()
            self._events.clear()
            self._last_event_text = ""
            self._last_event_ts = 0.0
            self._last_suppressed_key = ""
            self._last_suppressed_ts = 0.0
            self._last_seen_by_text.clear()
            self._internet_transitions.clear()
            return prior, self._reset_observation_unlocked()

    def _reset_observation_unlocked(self) -> dict[str, object]:
        return {
            "events": tuple(self._events),
            "last_event_text": self._last_event_text,
            "last_event_ts": self._last_event_ts,
            "last_suppressed_key": self._last_suppressed_key,
            "last_suppressed_ts": self._last_suppressed_ts,
        }

    def _append(self, timestamp: float, text: str, tone: str) -> None:
        event = OperatorEvent(
            timestamp=float(timestamp),
            text=str(text),
            tone=str(tone or "info"),
        )
        index = len(self._events)
        while index > 0 and self._events[index - 1].timestamp > event.timestamp:
            index -= 1
        self._events.insert(index, event)
        if len(self._events) > self._max_lines:
            del self._events[: min(self._prune_lines, len(self._events))]

__all__ = ("NGUIOperatorEventFeed", "OperatorEvent")
