"""Typed append-only controller evidence journal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections import deque
import threading
import time
from typing import Any, Mapping, Protocol


class ControllerEventKind(str, Enum):
    GENERATION_ACCEPTED = "controller_generation_accepted"
    DECISION = "controller_decision"
    ACTION = "controller_action"
    PROOF = "controller_proof"
    LEARNING = "controller_learning"
    FAULT = "controller_fault"
    RECOVERY = "controller_recovery"
    CONFIGURATION = "controller_configuration"


def _freeze(values: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(((str(key), value) for key, value in (values or {}).items()), key=lambda row: row[0]))


class EventLogPort(Protocol):
    def log_event(self, kind: str, **payload: Any) -> None: ...


class EvidenceDeliveryFaultSink(Protocol):
    def record_evidence_delivery_fault(
        self,
        fault: "EvidenceDeliveryFault",
    ) -> None: ...


@dataclass(frozen=True)
class EvidenceDeliveryFault:
    event_sequence: int
    event_kind: str
    generation: int
    transaction_id: str
    event_owner: str
    error_type: str
    detail: str
    retryable: bool
    degraded_behavior: str
    ts: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_sequence": self.event_sequence,
            "event_kind": self.event_kind,
            "generation": self.generation,
            "transaction_id": self.transaction_id,
            "event_owner": self.event_owner,
            "error_type": self.error_type,
            "detail": self.detail,
            "retryable": self.retryable,
            "degraded_behavior": self.degraded_behavior,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class ControllerEvent:
    sequence: int
    kind: ControllerEventKind
    ts: float
    generation: int
    transaction_id: str
    owner: str
    payload: tuple[tuple[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_sequence": self.sequence,
            "ts": self.ts,
            "event_owner": self.owner,
            "generation": self.generation,
            "transaction_id": self.transaction_id,
            **dict(self.payload),
        }


class ForensicEventJournal:
    """Single append owner; compact forensic output remains a derived codec."""

    def __init__(
        self,
        logger: EventLogPort,
        *,
        retained_events: int = 1024,
        retained_delivery_faults: int = 128,
    ):
        if not callable(getattr(logger, "log_event", None)):
            raise TypeError("event_journal_requires_log_event_port")
        self._logger = logger
        self._lock = threading.Lock()
        self._sequence = 0
        self._events: deque[ControllerEvent] = deque(maxlen=max(1, int(retained_events)))
        self._delivery_faults: deque[EvidenceDeliveryFault] = deque(
            maxlen=max(1, int(retained_delivery_faults))
        )
        self._delivery_fault_sink: EvidenceDeliveryFaultSink | None = None
        self._delivery_failures = 0
        self._last_delivery_error = ""
        self._delivery_fault_sink_failures = 0
        self._last_delivery_fault_sink_error = ""

    def bind_delivery_fault_sink(self, sink: EvidenceDeliveryFaultSink) -> None:
        recorder = getattr(sink, "record_evidence_delivery_fault", None)
        if not callable(recorder):
            raise TypeError("event_journal_delivery_fault_sink_invalid")
        with self._lock:
            if self._delivery_fault_sink is not None and self._delivery_fault_sink is not sink:
                raise RuntimeError("event_journal_delivery_fault_sink_already_bound")
            self._delivery_fault_sink = sink

    def publish(
        self,
        kind: ControllerEventKind,
        *,
        generation: int,
        transaction_id: str = "",
        owner: str,
        payload: Mapping[str, Any] | None = None,
        ts: float | None = None,
    ) -> ControllerEvent:
        with self._lock:
            self._sequence += 1
            event = ControllerEvent(
                sequence=self._sequence,
                kind=kind,
                ts=float(time.time() if ts is None else ts),
                generation=int(generation or 0),
                transaction_id=str(transaction_id or ""),
                owner=str(owner or "unknown"),
                payload=_freeze(payload),
            )
            self._events.append(event)
        try:
            self._logger.log_event(event.kind.value, **event.to_payload())
        except Exception as exc:
            fault = EvidenceDeliveryFault(
                event_sequence=event.sequence,
                event_kind=event.kind.value,
                generation=event.generation,
                transaction_id=event.transaction_id,
                event_owner=event.owner,
                error_type=type(exc).__name__,
                detail=str(exc),
                retryable=True,
                degraded_behavior="retain_in_memory_and_continue_control",
                ts=time.time(),
            )
            with self._lock:
                self._delivery_failures += 1
                self._last_delivery_error = f"{type(exc).__name__}: {exc}"
                self._delivery_faults.append(fault)
                sink = self._delivery_fault_sink
            if sink is not None:
                try:
                    sink.record_evidence_delivery_fault(fault)
                except Exception as sink_error:
                    with self._lock:
                        self._delivery_fault_sink_failures += 1
                        self._last_delivery_fault_sink_error = (
                            f"{type(sink_error).__name__}: {sink_error}"
                        )
        return event

    def recent(self, *, after_sequence: int = 0) -> tuple[ControllerEvent, ...]:
        with self._lock:
            return tuple(
                event for event in self._events if event.sequence > int(after_sequence or 0)
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            oldest = self._events[0].sequence if self._events else 0
            newest = self._events[-1].sequence if self._events else 0
            return {
                "owner": "forensic_event_journal",
                "sequence": self._sequence,
                "retained_count": len(self._events),
                "retained_capacity": int(self._events.maxlen or 0),
                "oldest_sequence": oldest,
                "newest_sequence": newest,
                "delivery_failures": self._delivery_failures,
                "last_delivery_error": self._last_delivery_error,
                "delivery_fault_count": len(self._delivery_faults),
                "last_delivery_fault": (
                    self._delivery_faults[-1].to_payload()
                    if self._delivery_faults
                    else None
                ),
                "delivery_fault_sink_bound": self._delivery_fault_sink is not None,
                "delivery_fault_sink_failures": self._delivery_fault_sink_failures,
                "last_delivery_fault_sink_error": self._last_delivery_fault_sink_error,
            }

    def delivery_faults(self) -> tuple[EvidenceDeliveryFault, ...]:
        with self._lock:
            return tuple(self._delivery_faults)


def require_event_journal(owner: object) -> ForensicEventJournal:
    controller = getattr(owner, "controller", None)
    reporting = getattr(controller, "reporting", None)
    journal = getattr(reporting, "event_journal", None)
    if not isinstance(journal, ForensicEventJournal):
        raise RuntimeError("forensic_event_journal_unavailable")
    return journal


__all__ = [
    "ControllerEvent",
    "ControllerEventKind",
    "ForensicEventJournal",
    "require_event_journal",
]
