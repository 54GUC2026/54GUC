"""Typed, non-authoritative diagnostic facts emitted by modem backends."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import math
from typing import Any, Mapping


_NATIVE_READ_CONTEXT: ContextVar[Any] = ContextVar("modem_native_read_diagnostic", default=None)


@contextmanager
def native_read_diagnostic_scope(sink, **context):
    """Correlate native failures, including pre-read identity checks, to this call.

    Context-local: a maintenance thread cannot overwrite a fast-read failure.
    The supplied existing event-log port owns privacy, durability and retention.
    """
    token = _NATIVE_READ_CONTEXT.set((sink, context) if callable(sink) else None)
    try:
        yield
    finally:
        _NATIVE_READ_CONTEXT.reset(token)


def record_native_read_failure(**evidence):
    context = _NATIVE_READ_CONTEXT.get()
    if context is None:
        return
    sink, metadata = context
    try:
        if sink("modem_native_read_failure", event_owner="54guc.modem_live.native_read",
                severity="WARNING", **metadata, **evidence) is not True:
            raise RuntimeError("native_read_diagnostic_delivery_unacknowledged")
    except Exception as exc:
        # A diagnostic sink must never replace the native result/exception.
        # The Foundation port also reports its own persistence health failure.
        logging.getLogger(__name__).error("Native read diagnostic delivery failed: %s", type(exc).__name__)


def native_exception_chain(error):
    """Keep transport types/OS codes, never exception messages, URLs or bodies."""
    pending, seen, chain = [error], set(), []
    while pending and len(chain) < 6:
        item = pending.pop(0)
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        row = {"type": type(item).__name__[:80]}
        for name in ("errno", "winerror"):
            value = getattr(item, name, None)
            if type(value) is int:
                row[name] = value
        chain.append(row)
        pending.extend(child for child in (
            getattr(item, "reason", None), item.__cause__, item.__context__, *item.args[:6]
        ) if isinstance(child, BaseException))
    return chain


MODEM_BACKEND_DIAGNOSTIC_SCHEMA = "ModemBackendDiagnostic.v1"
MODEM_BACKEND_DIAGNOSTIC_SEVERITIES = frozenset(
    {"INFO", "WARNING", "ERROR"}
)


@dataclass(frozen=True, slots=True)
class ModemBackendDiagnostic:
    owner: str
    sequence: int
    observed_at: float
    kind: str
    backend: str
    severity: str
    data: Mapping[str, Any]
    schema: str = MODEM_BACKEND_DIAGNOSTIC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEM_BACKEND_DIAGNOSTIC_SCHEMA:
            raise ValueError("modem_backend_diagnostic_schema_invalid")
        if not str(self.owner or "").strip():
            raise ValueError("modem_backend_diagnostic_owner_required")
        if int(self.sequence) <= 0:
            raise ValueError("modem_backend_diagnostic_positive_sequence_required")
        if not math.isfinite(float(self.observed_at)) or self.observed_at <= 0.0:
            raise ValueError("modem_backend_diagnostic_positive_timestamp_required")
        if not str(self.kind or "").strip():
            raise ValueError("modem_backend_diagnostic_kind_required")
        if not str(self.backend or "").strip():
            raise ValueError("modem_backend_diagnostic_backend_required")
        severity = str(self.severity or "").strip().upper()
        if severity not in MODEM_BACKEND_DIAGNOSTIC_SEVERITIES:
            raise ValueError("modem_backend_diagnostic_severity_invalid")
        if not isinstance(self.data, Mapping):
            raise TypeError("modem_backend_diagnostic_data_mapping_required")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "data", dict(self.data))

    @property
    def diagnostic_id(self) -> str:
        return f"{self.owner}:{int(self.sequence)}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": int(self.sequence),
            "observed_at": float(self.observed_at),
            "kind": self.kind,
            "backend": self.backend,
            "severity": self.severity,
            "diagnostic_id": self.diagnostic_id,
            "data": dict(self.data),
        }


def parse_modem_backend_diagnostic(
    payload: Mapping[str, Any],
) -> ModemBackendDiagnostic:
    if not isinstance(payload, Mapping):
        raise TypeError("modem_backend_diagnostic_mapping_required")
    diagnostic = ModemBackendDiagnostic(
        schema=str(payload.get("schema") or ""),
        owner=str(payload.get("owner") or ""),
        sequence=int(payload.get("sequence", 0) or 0),
        observed_at=float(payload.get("observed_at", 0.0) or 0.0),
        kind=str(payload.get("kind") or ""),
        backend=str(payload.get("backend") or ""),
        severity=str(payload.get("severity") or ""),
        data=(
            payload.get("data")
            if isinstance(payload.get("data"), Mapping)
            else {}
        ),
    )
    supplied_id = str(payload.get("diagnostic_id") or "").strip()
    if supplied_id and supplied_id != diagnostic.diagnostic_id:
        raise ValueError("modem_backend_diagnostic_id_mismatch")
    return diagnostic


__all__ = (
    "MODEM_BACKEND_DIAGNOSTIC_SCHEMA",
    "MODEM_BACKEND_DIAGNOSTIC_SEVERITIES",
    "ModemBackendDiagnostic",
    "parse_modem_backend_diagnostic",
)
