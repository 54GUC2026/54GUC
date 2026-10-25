"""Shared runtime fault classification helpers."""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.runtime_boundary_contracts import FaultEvent, FaultPayload


_KNOWN_SUPPRESSED_FAULT_CATEGORIES = {
    "authority",
    "brain",
    "checkpoint",
    "decision",
    "gui",
    "io",
    "learning",
    "modem_live",
    "persistence",
    "recovery",
    "reporting",
    "runtime_event",
    "runtime_control",
    "runtime_status",
    "sidecar",
    "unknown",
}
_MAX_SUPPRESSED_FAULT_EVENTS = 32


def normalize_suppressed_fault_category(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    text = text.replace("-", "_").replace(" ", "_")
    return text if text in _KNOWN_SUPPRESSED_FAULT_CATEGORIES else "unknown"


def record_suppressed_fault(
    payload: Mapping[str, Any] | None,
    *,
    category: Any,
    source: Any = "",
    error_type: Any = "",
    severity: Any = "warning",
    detail: Any = "",
    recoverable: bool = True,
    metadata: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return payload with one categorized suppressed-fault observation added."""

    out = dict(payload or {})
    cat = normalize_suppressed_fault_category(category)
    ts = float(time.time() if now is None else now)
    event_metadata = dict(metadata or {})
    if error_type:
        event_metadata.setdefault("error_type", str(error_type))
    fault_event = FaultEvent(
        category=cat,
        source=str(source or ""),
        severity=str(severity or "warning"),
        detail=str(detail or error_type or ""),
        ts=ts,
        recoverable=bool(recoverable),
        metadata=event_metadata,
    ).as_dict()
    out["suppressed_faults"] = (
        FaultPayload.from_mapping(out.get("suppressed_faults"))
        .append_event(fault_event, max_events=_MAX_SUPPRESSED_FAULT_EVENTS)
        .as_dict()
    )
    return out


def ensure_suppressed_fault_surface(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(payload or {})
    out["suppressed_faults"] = FaultPayload.from_mapping(out.get("suppressed_faults")).as_dict()
    return out
