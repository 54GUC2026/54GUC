"""Single Foundation incident identity shared by all evidence projections."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from threading import RLock
import time
from typing import Any

from arch4.privacy_scrub import scrub_text


FOUNDATION_INCIDENT_SCHEMA = "FoundationIncidentReceipt.v1"
FOUNDATION_INCIDENT_OWNER = "54guc.foundation.incident_receipt"


@dataclass(frozen=True, slots=True)
class FoundationIncidentReceipt:
    incident_id: str
    sequence: int
    state: str
    fault_owner: str
    operation: str
    generation: int
    occurred_at: float
    severity: str
    retryable: bool
    error_type: str
    detail: str
    degraded_behavior: str
    recovered_at: float = 0.0
    schema: str = FOUNDATION_INCIDENT_SCHEMA
    owner: str = FOUNDATION_INCIDENT_OWNER

    def __post_init__(self) -> None:
        if self.schema != FOUNDATION_INCIDENT_SCHEMA:
            raise ValueError("foundation_incident_schema_invalid")
        if self.owner != FOUNDATION_INCIDENT_OWNER:
            raise ValueError("foundation_incident_owner_invalid")
        if not self.incident_id or self.sequence <= 0:
            raise ValueError("foundation_incident_identity_invalid")
        if self.state not in {"active", "recovered"}:
            raise ValueError("foundation_incident_state_invalid")
        if not self.fault_owner or not self.operation:
            raise ValueError("foundation_incident_scope_invalid")
        if not math.isfinite(self.occurred_at) or self.occurred_at <= 0.0:
            raise ValueError("foundation_incident_timestamp_invalid")
        if self.state == "recovered" and self.recovered_at < self.occurred_at:
            raise ValueError("foundation_incident_recovery_timestamp_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "incident_id": self.incident_id,
            "sequence": self.sequence,
            "state": self.state,
            "fault_owner": self.fault_owner,
            "operation": self.operation,
            "generation": self.generation,
            "occurred_at": self.occurred_at,
            "severity": self.severity,
            "retryable": self.retryable,
            "error_type": self.error_type,
            "detail": self.detail,
            "degraded_behavior": self.degraded_behavior,
            "recovered_at": self.recovered_at,
        }


class FoundationIncidentAuthority:
    """Create and resolve one incident identity per active owner operation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = 0
        self._active: dict[tuple[str, str], FoundationIncidentReceipt] = {}
        self._last_receipt: FoundationIncidentReceipt | None = None

    @staticmethod
    def _identity(
        *,
        sequence: int,
        fault_owner: str,
        operation: str,
        generation: int,
        error_type: str,
    ) -> str:
        seed = (
            f"{sequence}|{fault_owner}|{operation}|{generation}|{error_type}"
        ).encode("utf-8")
        return "foundation-incident:" + hashlib.sha256(seed).hexdigest()[:24]

    def record_failure(
        self,
        *,
        fault_owner: str,
        operation: str,
        generation: int,
        severity: str,
        retryable: bool,
        error: BaseException,
        degraded_behavior: str,
        occurred_at: float | None = None,
    ) -> FoundationIncidentReceipt:
        normalized_owner = str(fault_owner or "").strip()
        normalized_operation = str(operation or "").strip()
        if not normalized_owner or not normalized_operation:
            raise ValueError("foundation_incident_scope_required")
        now = float(time.time() if occurred_at is None else occurred_at)
        with self._lock:
            existing = self._active.get(
                (normalized_owner, normalized_operation)
            )
            if existing is not None:
                self._last_receipt = existing
                return existing
            self._sequence += 1
            receipt = FoundationIncidentReceipt(
                incident_id=self._identity(
                    sequence=self._sequence,
                    fault_owner=normalized_owner,
                    operation=normalized_operation,
                    generation=int(generation),
                    error_type=type(error).__name__,
                ),
                sequence=self._sequence,
                state="active",
                fault_owner=normalized_owner,
                operation=normalized_operation,
                generation=int(generation),
                occurred_at=now,
                severity=str(severity or "error").strip().lower() or "error",
                retryable=bool(retryable),
                error_type=type(error).__name__,
                detail=scrub_text(str(error)),
                degraded_behavior=scrub_text(str(degraded_behavior or "")),
            )
            self._active[(normalized_owner, normalized_operation)] = receipt
            self._last_receipt = receipt
            return receipt

    def record_recovery(
        self,
        *,
        fault_owner: str,
        operation: str,
        generation: int,
        detail: str = "recovered",
        recovered_at: float | None = None,
    ) -> FoundationIncidentReceipt | None:
        key = (str(fault_owner or "").strip(), str(operation or "").strip())
        with self._lock:
            active = self._active.pop(key, None)
            if active is None:
                return None
            self._sequence += 1
            receipt = FoundationIncidentReceipt(
                incident_id=active.incident_id,
                sequence=self._sequence,
                state="recovered",
                fault_owner=active.fault_owner,
                operation=active.operation,
                generation=int(generation),
                occurred_at=active.occurred_at,
                severity=active.severity,
                retryable=active.retryable,
                error_type=active.error_type,
                detail=scrub_text(str(detail or "recovered")),
                degraded_behavior=active.degraded_behavior,
                recovered_at=float(
                    time.time() if recovered_at is None else recovered_at
                ),
            )
            self._last_receipt = receipt
            return receipt

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = tuple(
                receipt.to_payload()
                for _key, receipt in sorted(self._active.items())
            )
            return {
                "schema": "FoundationIncidentAuthoritySnapshot.v1",
                "owner": FOUNDATION_INCIDENT_OWNER,
                "sequence": self._sequence,
                "active_count": len(active),
                "active": list(active),
                "last_receipt": (
                    self._last_receipt.to_payload()
                    if self._last_receipt is not None
                    else None
                ),
            }


__all__ = (
    "FOUNDATION_INCIDENT_OWNER",
    "FOUNDATION_INCIDENT_SCHEMA",
    "FoundationIncidentAuthority",
    "FoundationIncidentReceipt",
)
