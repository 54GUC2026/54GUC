"""Keep operational requests until NGUI shows their result."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Mapping, MutableMapping

from arch4.atomic_io import atomic_write_json
from arch4.canonical_json import canonical_json_bytes
from arch4.operational_service_contracts import (
    OperationalKind,
    OperationalServiceReceipt,
    operational_request_from_payload,
    operational_request_kind,
    operational_request_matrix_identity,
    operational_request_to_payload,
)

from .runtime_ipc import (
    RuntimeIPCRequest,
    RuntimeNamespaceGenerationIdentity,
    RuntimeRequestKind,
)


NGUI_OPERATIONAL_OUTBOX_OWNER = "54guc.ngui.operational_outbox"
NGUI_OPERATIONAL_OUTBOX_SCHEMA = "NGUIOperationalOutbox.v2"
NGUI_OPERATIONAL_OUTBOX_FILENAME = "ngui_operational_outbox.json"
_OPERATIONAL_KIND_BY_RUNTIME_KIND = {
    RuntimeRequestKind.CORE_READ_DIAGNOSTICS: OperationalKind.READ_DIAGNOSTICS,
    RuntimeRequestKind.CORE_NETWORK_INSPECTION: OperationalKind.INSPECT_NETWORK_SETTINGS,
    RuntimeRequestKind.CORE_WIFI_CONTROL: OperationalKind.WIFI_SET_RADIO,
    RuntimeRequestKind.CORE_DOWNSTREAM_DIAGNOSTIC: (
        OperationalKind.SET_DOWNSTREAM_HEALTH
    ),
    RuntimeRequestKind.FOUNDATION_SMS_CONTROL: (
        OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL
    ),
    RuntimeRequestKind.FOUNDATION_ANTENNA_GUIDE: (
        OperationalKind.ANTENNA_GUIDE
    ),
    RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_PLAN: (
        OperationalKind.PLAN_BAND_SCHEDULE
    ),
    RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_PLAN: (
        OperationalKind.PLAN_BAND_SEQUENCE
    ),
    RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_CANCEL: (
        OperationalKind.CANCEL_BAND_SCHEDULE
    ),
    RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_CANCEL: (
        OperationalKind.CANCEL_BAND_SEQUENCE
    ),
    RuntimeRequestKind.PRODUCT_INTELLIGENCE_AUTOPILOT: (
        OperationalKind.SET_INTELLIGENCE_AUTOPILOT
    ),
    RuntimeRequestKind.PRODUCT_INTELLIGENCE_EXPLORE: (
        OperationalKind.SET_INTELLIGENCE_EXPLORE
    ),
}


class NGUIOperationalOutboxPhase(str, Enum):
    PENDING = "pending"
    VISIBLE_COMMITTED = "visible_committed"
    PRODUCT_ACKNOWLEDGED = "product_acknowledged"


@dataclass(frozen=True, slots=True)
class NGUIOperationalOutboxEntry:
    request_kind: RuntimeRequestKind
    sequence: int
    created_at: float
    request_payload: Mapping[str, object]
    request_fingerprint: str
    phase: NGUIOperationalOutboxPhase = NGUIOperationalOutboxPhase.PENDING
    visible_receipt: OperationalServiceReceipt | None = None

    def __post_init__(self) -> None:
        if self.request_kind is RuntimeRequestKind.MANUAL_NMI:
            raise ValueError("ngui_operational_outbox_manual_request_forbidden")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("ngui_operational_outbox_sequence_invalid")
        if type(self.created_at) is not float or self.created_at <= 0.0:
            raise ValueError("ngui_operational_outbox_created_at_invalid")
        if not isinstance(self.request_payload, Mapping):
            raise TypeError(
                "ngui_operational_outbox_request_payload_required"
            )
        request = operational_request_from_payload(self.request_payload)
        if (
            _OPERATIONAL_KIND_BY_RUNTIME_KIND.get(self.request_kind)
            is not operational_request_kind(request)
        ):
            raise ValueError(
                "ngui_operational_outbox_runtime_kind_mismatch"
            )
        expected = hashlib.sha256(
            canonical_json_bytes(self.request_payload)
        ).hexdigest()
        if self.request_fingerprint != expected:
            raise ValueError(
                "ngui_operational_outbox_request_fingerprint_mismatch"
            )
        if request.context.request_id != self.request_id:
            raise ValueError(
                "ngui_operational_outbox_request_identity_mismatch"
            )
        if not isinstance(self.phase, NGUIOperationalOutboxPhase):
            raise TypeError("ngui_operational_outbox_phase_required")
        visible = self.phase in {
            NGUIOperationalOutboxPhase.VISIBLE_COMMITTED,
            NGUIOperationalOutboxPhase.PRODUCT_ACKNOWLEDGED,
        }
        if visible != isinstance(
            self.visible_receipt,
            OperationalServiceReceipt,
        ):
            raise ValueError(
                "ngui_operational_outbox_visible_receipt_phase_mismatch"
            )
        if self.visible_receipt is not None:
            self._require_receipt_identity(self.visible_receipt)

    @property
    def request(self) -> object:
        return operational_request_from_payload(self.request_payload)

    @property
    def request_id(self) -> str:
        return self.request.context.request_id

    def to_payload(self) -> dict[str, object]:
        return {
            "request_kind": self.request_kind.value,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "request_payload": dict(self.request_payload),
            "request_fingerprint": self.request_fingerprint,
            "phase": self.phase.value,
            "visible_receipt": (
                self.visible_receipt.to_payload()
                if self.visible_receipt is not None
                else None
            ),
        }

    @classmethod
    def from_payload(
        cls,
        raw: Mapping[str, object],
    ) -> "NGUIOperationalOutboxEntry":
        if not isinstance(raw, Mapping):
            raise TypeError("ngui_operational_outbox_entry_mapping_required")
        row = dict(raw)
        if set(row) != {
            "request_kind",
            "sequence",
            "created_at",
            "request_payload",
            "request_fingerprint",
            "phase",
            "visible_receipt",
        }:
            raise ValueError("ngui_operational_outbox_entry_fields_invalid")
        payload = row["request_payload"]
        if not isinstance(payload, Mapping):
            raise TypeError(
                "ngui_operational_outbox_request_payload_mapping_required"
            )
        receipt_payload = row["visible_receipt"]
        if receipt_payload is not None and not isinstance(
            receipt_payload,
            Mapping,
        ):
            raise TypeError(
                "ngui_operational_outbox_visible_receipt_mapping_required"
            )
        return cls(
            request_kind=RuntimeRequestKind(row["request_kind"]),
            sequence=row["sequence"],
            created_at=row["created_at"],
            request_payload=dict(payload),
            request_fingerprint=row["request_fingerprint"],
            phase=NGUIOperationalOutboxPhase(row["phase"]),
            visible_receipt=(
                OperationalServiceReceipt.from_payload(receipt_payload)
                if receipt_payload is not None
                else None
            ),
        )

    def _require_receipt_identity(
        self,
        receipt: OperationalServiceReceipt,
    ) -> None:
        request = self.request
        if (
            receipt.request_id != self.request_id
            or operational_request_kind(request) is not receipt.kind
            or operational_request_matrix_identity(request)
            != receipt.matrix_identity
        ):
            raise ValueError(
                "ngui_operational_outbox_receipt_identity_mismatch"
            )


class NGUIOperationalOutbox:
    """Keep submitted requests until their result is acknowledged as visible."""

    def __init__(self, persistence_dir: str | Path) -> None:
        root = Path(persistence_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / NGUI_OPERATIONAL_OUTBOX_FILENAME
        self._lock = RLock()
        self._entries: dict[str, NGUIOperationalOutboxEntry] = {}
        self._restore()

    def admit(self, request: RuntimeIPCRequest) -> NGUIOperationalOutboxEntry:
        if not isinstance(request, RuntimeIPCRequest):
            raise TypeError("ngui_operational_outbox_ipc_request_required")
        if request.kind is RuntimeRequestKind.MANUAL_NMI:
            raise ValueError("ngui_operational_outbox_manual_request_forbidden")
        request_payload = operational_request_to_payload(request.payload)
        entry = NGUIOperationalOutboxEntry(
            request_kind=request.kind,
            sequence=request.sequence,
            created_at=request.created_at,
            request_payload=request_payload,
            request_fingerprint=hashlib.sha256(
                canonical_json_bytes(request_payload)
            ).hexdigest(),
        )
        with self._lock:
            prior = self._entries.get(entry.request_id)
            if prior is not None:
                if (
                    prior.request_kind is not entry.request_kind
                    or prior.request_fingerprint
                    != entry.request_fingerprint
                ):
                    raise RuntimeError(
                        "ngui_operational_outbox_request_id_conflict"
                    )
                return prior
            candidate = dict(self._entries)
            candidate[entry.request_id] = entry
            self._commit(candidate)
            self._entries = candidate
            return entry

    def acknowledge(self, receipt: OperationalServiceReceipt) -> None:
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("ngui_operational_outbox_receipt_required")
        with self._lock:
            entry = self._entries.get(receipt.request_id)
            if entry is None:
                raise RuntimeError(
                    "ngui_operational_outbox_receipt_without_submission"
                )
            entry._require_receipt_identity(receipt)
            if entry.visible_receipt is not None:
                if entry.visible_receipt != receipt:
                    raise RuntimeError(
                        "ngui_operational_outbox_visible_receipt_conflict"
                    )
                return
            visible = NGUIOperationalOutboxEntry(
                request_kind=entry.request_kind,
                sequence=entry.sequence,
                created_at=entry.created_at,
                request_payload=entry.request_payload,
                request_fingerprint=entry.request_fingerprint,
                phase=NGUIOperationalOutboxPhase.VISIBLE_COMMITTED,
                visible_receipt=receipt,
            )
            candidate = dict(self._entries)
            candidate[receipt.request_id] = visible
            self._commit(candidate)
            self._entries = candidate

    def require_receipt(
        self,
        receipt: OperationalServiceReceipt,
    ) -> NGUIOperationalOutboxEntry:
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("ngui_operational_outbox_receipt_required")
        with self._lock:
            entry = self._entries.get(receipt.request_id)
            if entry is None:
                raise RuntimeError(
                    "ngui_operational_outbox_receipt_without_submission"
                )
            entry._require_receipt_identity(receipt)
            if (
                entry.visible_receipt is not None
                and entry.visible_receipt != receipt
            ):
                raise RuntimeError(
                    "ngui_operational_outbox_visible_receipt_conflict"
                )
            return entry

    def was_visible(
        self,
        receipt: OperationalServiceReceipt,
    ) -> bool:
        return (
            self.require_receipt(receipt).phase
            is not NGUIOperationalOutboxPhase.PENDING
        )

    def confirm_product_acknowledgement(
        self,
        receipt: OperationalServiceReceipt,
    ) -> None:
        with self._lock:
            entry = self.require_receipt(receipt)
            if entry.phase is NGUIOperationalOutboxPhase.PENDING:
                raise RuntimeError(
                    "ngui_operational_outbox_product_ack_before_visible"
                )
            if (
                entry.phase
                is NGUIOperationalOutboxPhase.PRODUCT_ACKNOWLEDGED
            ):
                return
            candidate = dict(self._entries)
            del candidate[receipt.request_id]
            self._commit(candidate)
            self._entries = candidate

    def pending_for(
        self,
        generation_identity: RuntimeNamespaceGenerationIdentity,
    ) -> tuple[RuntimeIPCRequest, ...]:
        if not isinstance(
            generation_identity,
            RuntimeNamespaceGenerationIdentity,
        ):
            raise TypeError(
                "ngui_operational_outbox_generation_identity_required"
            )
        with self._lock:
            entries = sorted(
                self._entries.values(),
                key=lambda item: (item.sequence, item.request_id),
            )
            pending: list[RuntimeIPCRequest] = []
            for entry in entries:
                if (
                    entry.phase
                    is NGUIOperationalOutboxPhase.PRODUCT_ACKNOWLEDGED
                ):
                    continue
                request = entry.request
                if (
                    operational_request_matrix_identity(request).namespace
                    != generation_identity.product.runtime_namespace
                ):
                    continue
                request_identity = RuntimeNamespaceGenerationIdentity(
                    product=generation_identity.product,
                    matrix_identity=operational_request_matrix_identity(
                        request
                    ),
                )
                pending.append(
                    RuntimeIPCRequest(
                        kind=entry.request_kind,
                        generation_identity=request_identity,
                        sequence=entry.sequence,
                        created_at=entry.created_at,
                        payload=request,
                    )
                )
            return tuple(pending)

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                entry.phase
                is not NGUIOperationalOutboxPhase.PRODUCT_ACKNOWLEDGED
                for entry in self._entries.values()
            )

    def foreign_request_ids(
        self,
        generation_identity: RuntimeNamespaceGenerationIdentity,
    ) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    entry.request_id
                    for entry in self._entries.values()
                    if entry.phase
                    is not NGUIOperationalOutboxPhase.PRODUCT_ACKNOWLEDGED
                    and operational_request_matrix_identity(
                        entry.request
                    ).namespace
                    != generation_identity.product.runtime_namespace
                )
            )

    def _commit(
        self,
        entries: MutableMapping[str, NGUIOperationalOutboxEntry],
    ) -> None:
        # Product owns idempotency after acknowledgement. This outbox keeps
        # only requests that still wait for that handoff.
        retained = list(entries.values())
        content: dict[str, object] = {
            "schema": NGUI_OPERATIONAL_OUTBOX_SCHEMA,
            "owner": NGUI_OPERATIONAL_OUTBOX_OWNER,
            "entries": [
                entry.to_payload()
                for entry in sorted(
                    retained,
                    key=lambda item: (item.sequence, item.request_id),
                )
            ],
        }
        content["content_sha256"] = hashlib.sha256(
            canonical_json_bytes(content)
        ).hexdigest()
        atomic_write_json(
            self._path,
            content,
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )
        entries.clear()
        entries.update(
            {
                entry.request_id: entry
                for entry in retained
            }
        )

    def _restore(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("ngui_operational_outbox_corrupt") from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError("ngui_operational_outbox_mapping_required")
        row = dict(raw)
        if set(row) != {
            "schema",
            "owner",
            "entries",
            "content_sha256",
        }:
            raise RuntimeError("ngui_operational_outbox_fields_invalid")
        checksum = row.pop("content_sha256")
        if (
            row["schema"] != NGUI_OPERATIONAL_OUTBOX_SCHEMA
            or row["owner"] != NGUI_OPERATIONAL_OUTBOX_OWNER
            or type(checksum) is not str
            or checksum
            != hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        ):
            raise RuntimeError("ngui_operational_outbox_contract_invalid")
        entries = row["entries"]
        if not isinstance(entries, list):
            raise RuntimeError("ngui_operational_outbox_entries_list_required")
        restored: dict[str, NGUIOperationalOutboxEntry] = {}
        for item in entries:
            entry = NGUIOperationalOutboxEntry.from_payload(item)
            if entry.request_id in restored:
                raise RuntimeError(
                    "ngui_operational_outbox_duplicate_request"
                )
            restored[entry.request_id] = entry
        self._entries = restored


__all__ = (
    "NGUI_OPERATIONAL_OUTBOX_FILENAME",
    "NGUI_OPERATIONAL_OUTBOX_OWNER",
    "NGUI_OPERATIONAL_OUTBOX_SCHEMA",
    "NGUIOperationalOutboxPhase",
    "NGUIOperationalOutbox",
    "NGUIOperationalOutboxEntry",
)
