"""Durable Product owner for typed operational request execution and outcome."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from threading import RLock
from time import time
from typing import Callable, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.canonical_json import canonical_json_bytes
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.operational_service_contracts import (
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    _issue_product_operational_effect_permit,
    operational_request_from_payload,
    operational_request_fingerprint,
    operational_request_kind,
    operational_request_matrix_identity,
    operational_request_to_payload,
)


PRODUCT_OPERATIONAL_TRANSACTION_OWNER = (
    "54guc.product.operational_transaction"
)
PRODUCT_OPERATIONAL_TRANSACTION_SCHEMA = (
    "ProductOperationalTransactionState.v2"
)
PRODUCT_OPERATIONAL_TRANSACTION_FILENAME = (
    "product_operational_transactions.json"
)
class ProductOperationalPhase(str, Enum):
    ADMITTED = "admitted"
    EXECUTING = "executing"
    TERMINAL = "terminal"
    PRESENTATION_ACKNOWLEDGED = "presentation_acknowledged"


@dataclass(frozen=True, slots=True)
class ProductOperationalTransaction:
    request_id: str
    kind: OperationalKind
    request_fingerprint: str
    request_payload: Mapping[str, object]
    phase: ProductOperationalPhase
    admitted_at: float
    updated_at: float
    effect_reference: CoreFrozenPayload
    receipt: OperationalServiceReceipt | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or not self.request_id
            or self.request_id != self.request_id.strip()
        ):
            raise ValueError("product_operational_request_id_required")
        if not isinstance(self.kind, OperationalKind):
            raise TypeError("product_operational_kind_required")
        if (
            type(self.request_fingerprint) is not str
            or len(self.request_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.request_fingerprint
            )
        ):
            raise ValueError(
                "product_operational_request_fingerprint_invalid"
            )
        if not isinstance(self.request_payload, Mapping):
            raise TypeError("product_operational_request_payload_required")
        request = operational_request_from_payload(self.request_payload)
        if operational_request_kind(request) is not self.kind:
            raise ValueError("product_operational_request_kind_mismatch")
        if request.context.request_id != self.request_id:
            raise ValueError("product_operational_request_identity_mismatch")
        if (
            operational_request_fingerprint(request)
            != self.request_fingerprint
        ):
            raise ValueError(
                "product_operational_request_fingerprint_mismatch"
            )
        if not isinstance(self.phase, ProductOperationalPhase):
            raise TypeError("product_operational_phase_required")
        if not isinstance(self.effect_reference, CoreFrozenPayload):
            raise TypeError(
                "product_operational_effect_reference_required"
            )
        for field_name in ("admitted_at", "updated_at"):
            value = getattr(self, field_name)
            if type(value) is not float or value <= 0.0:
                raise ValueError(
                    f"product_operational_{field_name}_invalid"
                )
        if self.updated_at < self.admitted_at:
            raise ValueError("product_operational_time_order_invalid")
        terminal = self.phase in {
            ProductOperationalPhase.TERMINAL,
            ProductOperationalPhase.PRESENTATION_ACKNOWLEDGED,
        }
        if terminal != isinstance(self.receipt, OperationalServiceReceipt):
            raise ValueError(
                "product_operational_terminal_receipt_contract_invalid"
            )
        effect_started = self.phase is not ProductOperationalPhase.ADMITTED
        if effect_started != bool(self.effect_reference.to_mapping()):
            raise ValueError(
                "product_operational_effect_reference_phase_mismatch"
            )
        if self.receipt is not None and (
            self.receipt.request_id != self.request_id
            or self.receipt.kind is not self.kind
            or self.receipt.matrix_identity
            != operational_request_matrix_identity(request)
        ):
            raise ValueError(
                "product_operational_receipt_identity_mismatch"
            )
        reference = self.effect_reference.to_mapping()
        if self.phase is ProductOperationalPhase.EXECUTING:
            if set(reference) != {
                "domain_owner",
                "request_fingerprint",
            }:
                raise ValueError(
                    "product_operational_executing_reference_invalid"
                )
            if (
                not str(reference["domain_owner"] or "").strip()
                or reference["request_fingerprint"]
                != self.request_fingerprint
            ):
                raise ValueError(
                    "product_operational_executing_reference_mismatch"
                )
        elif terminal:
            if set(reference) != {
                "domain_owner",
                "expected_domain_owner",
                "proof_type",
                "receipt_detail_sha256",
                "references",
            }:
                raise ValueError(
                    "product_operational_terminal_reference_invalid"
                )
            if (
                reference["domain_owner"] != self.receipt.owner
                or not str(reference["expected_domain_owner"] or "").strip()
                or not str(reference["proof_type"] or "").strip()
                or reference["receipt_detail_sha256"]
                != self.receipt.detail.sha256
                or not isinstance(reference["references"], Mapping)
                or not reference["references"]
            ):
                raise ValueError(
                    "product_operational_terminal_reference_mismatch"
                )

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "kind": self.kind.value,
            "request_fingerprint": self.request_fingerprint,
            "request_payload": dict(self.request_payload),
            "phase": self.phase.value,
            "admitted_at": self.admitted_at,
            "updated_at": self.updated_at,
            "effect_reference": self.effect_reference.to_payload(),
            "receipt": (
                self.receipt.to_payload()
                if self.receipt is not None
                else None
            ),
        }

    @classmethod
    def from_payload(
        cls,
        raw: Mapping[str, object],
    ) -> "ProductOperationalTransaction":
        if not isinstance(raw, Mapping):
            raise TypeError("product_operational_transaction_mapping_required")
        row = dict(raw)
        if set(row) != {
            "request_id",
            "kind",
            "request_fingerprint",
            "request_payload",
            "phase",
            "admitted_at",
            "updated_at",
            "effect_reference",
            "receipt",
        }:
            raise ValueError("product_operational_transaction_fields_invalid")
        request_payload = row["request_payload"]
        if not isinstance(request_payload, Mapping):
            raise TypeError(
                "product_operational_request_payload_mapping_required"
            )
        receipt_payload = row["receipt"]
        effect_reference = row["effect_reference"]
        if not isinstance(effect_reference, Mapping):
            raise TypeError(
                "product_operational_effect_reference_mapping_required"
            )
        if receipt_payload is not None and not isinstance(
            receipt_payload,
            Mapping,
        ):
            raise TypeError(
                "product_operational_receipt_payload_mapping_required"
            )
        return cls(
            request_id=row["request_id"],
            kind=OperationalKind(row["kind"]),
            request_fingerprint=row["request_fingerprint"],
            request_payload=dict(request_payload),
            phase=ProductOperationalPhase(row["phase"]),
            admitted_at=row["admitted_at"],
            updated_at=row["updated_at"],
            effect_reference=CoreFrozenPayload.from_mapping(
                effect_reference
            ),
            receipt=(
                OperationalServiceReceipt.from_payload(receipt_payload)
                if receipt_payload is not None
                else None
            ),
        )

class ProductOperationalTransactionAuthority:
    """Own every operational request from admission through GUI acknowledgement."""

    def __init__(self, persistence_dir: str | Path) -> None:
        root = Path(persistence_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / PRODUCT_OPERATIONAL_TRANSACTION_FILENAME
        self._lock = RLock()
        self._records: dict[str, ProductOperationalTransaction] = {}
        self._restore()

    @property
    def path(self) -> Path:
        return self._path

    def execute(
        self,
        request: object,
        *,
        effect_owner: str,
        effect: Callable[
            [ProductOperationalEffectPermit],
            OperationalServiceReceipt,
        ],
        reconcile: Callable[
            [
                object,
                ProductOperationalTransaction,
                ProductOperationalEffectPermit,
            ],
            OperationalServiceReceipt | None,
        ]
        | None = None,
        on_effect_failure: Callable[[Exception], None] | None = None,
    ) -> OperationalServiceReceipt:
        if not callable(effect):
            raise TypeError("product_operational_effect_required")
        if (
            type(effect_owner) is not str
            or not effect_owner
            or effect_owner != effect_owner.strip()
        ):
            raise ValueError("product_operational_effect_owner_required")
        request_payload = operational_request_to_payload(request)
        request_id = request.context.request_id
        kind = operational_request_kind(request)
        fingerprint = operational_request_fingerprint(request)
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                now = time()
                record = ProductOperationalTransaction(
                    request_id=request_id,
                    kind=kind,
                    request_fingerprint=fingerprint,
                    request_payload=request_payload,
                    phase=ProductOperationalPhase.ADMITTED,
                    admitted_at=now,
                    updated_at=now,
                    effect_reference=CoreFrozenPayload.empty(),
                )
                self._replace_and_commit(request_id, record)
            else:
                self._require_same_request(
                    record,
                    kind=kind,
                    fingerprint=fingerprint,
                )
            if record.receipt is not None:
                return record.receipt
            if record.phase is ProductOperationalPhase.EXECUTING:
                permit = self._effect_permit(record)
                receipt = (
                    reconcile(request, record, permit)
                    if reconcile is not None
                    else None
                )
                if receipt is None:
                    receipt = self._indeterminate_receipt(
                        request,
                        kind=kind,
                        fingerprint=fingerprint,
                    )
                return self._commit_terminal(record, receipt)
            executing = ProductOperationalTransaction(
                request_id=record.request_id,
                kind=record.kind,
                request_fingerprint=record.request_fingerprint,
                    request_payload=record.request_payload,
                    phase=ProductOperationalPhase.EXECUTING,
                    admitted_at=record.admitted_at,
                    updated_at=time(),
                    effect_reference=CoreFrozenPayload.from_mapping(
                        {
                            "domain_owner": effect_owner,
                            "request_fingerprint": fingerprint,
                        }
                    ),
                )
            self._replace_and_commit(request_id, executing)
            permit = self._effect_permit(executing)
            try:
                receipt = effect(permit)
            except Exception as exc:
                reporting_error = ""
                if on_effect_failure is not None:
                    try:
                        on_effect_failure(exc)
                    except Exception as report_exc:
                        reporting_error = (
                            f"{type(report_exc).__name__}: {report_exc}"
                        )
                receipt = (
                    reconcile(request, executing, permit)
                    if reconcile is not None
                    else None
                )
                if receipt is None:
                    receipt = self._indeterminate_receipt(
                        request,
                        kind=kind,
                        fingerprint=fingerprint,
                        error=exc,
                        reporting_error=reporting_error,
                    )
            if not isinstance(receipt, OperationalServiceReceipt):
                raise TypeError("product_operational_receipt_required")
            return self._commit_terminal(executing, receipt)

    def require_terminal(
        self,
        receipt: OperationalServiceReceipt,
    ) -> ProductOperationalTransaction:
        if not isinstance(receipt, OperationalServiceReceipt):
            raise TypeError("product_operational_receipt_required")
        with self._lock:
            record = self._records.get(receipt.request_id)
            if (
                record is None
                or record.receipt != receipt
                or record.phase
                not in {
                    ProductOperationalPhase.TERMINAL,
                    ProductOperationalPhase.PRESENTATION_ACKNOWLEDGED,
                }
            ):
                raise RuntimeError(
                    "product_operational_terminal_receipt_unowned"
                )
            return record

    def acknowledge_presentation(
        self,
        receipt: OperationalServiceReceipt,
    ) -> ProductOperationalTransaction:
        with self._lock:
            record = self.require_terminal(receipt)
            if (
                record.phase
                is ProductOperationalPhase.PRESENTATION_ACKNOWLEDGED
            ):
                return record
            acknowledged = ProductOperationalTransaction(
                request_id=record.request_id,
                kind=record.kind,
                request_fingerprint=record.request_fingerprint,
                request_payload=record.request_payload,
                phase=ProductOperationalPhase.PRESENTATION_ACKNOWLEDGED,
                admitted_at=record.admitted_at,
                updated_at=time(),
                effect_reference=record.effect_reference,
                receipt=record.receipt,
            )
            self._replace_and_commit(record.request_id, acknowledged)
            return acknowledged

    def _commit_terminal(
        self,
        record: ProductOperationalTransaction,
        receipt: OperationalServiceReceipt,
    ) -> OperationalServiceReceipt:
        request = operational_request_from_payload(record.request_payload)
        if (
            receipt.request_id != record.request_id
            or receipt.kind is not record.kind
            or receipt.matrix_identity
            != operational_request_matrix_identity(request)
        ):
            raise ValueError(
                "product_operational_receipt_identity_mismatch"
            )
        expected_owner = str(
            record.effect_reference.to_mapping().get("domain_owner")
            or ""
        )
        if receipt.owner not in {
            expected_owner,
            PRODUCT_OPERATIONAL_TRANSACTION_OWNER,
        }:
            raise ValueError(
                "product_operational_receipt_domain_owner_mismatch"
            )
        terminal = ProductOperationalTransaction(
            request_id=record.request_id,
            kind=record.kind,
            request_fingerprint=record.request_fingerprint,
            request_payload=record.request_payload,
            phase=ProductOperationalPhase.TERMINAL,
            admitted_at=record.admitted_at,
            updated_at=time(),
            effect_reference=self._effect_reference(
                receipt,
                expected_owner=expected_owner,
            ),
            receipt=receipt,
        )
        self._replace_and_commit(record.request_id, terminal)
        return receipt

    @staticmethod
    def _effect_permit(
        record: ProductOperationalTransaction,
    ) -> ProductOperationalEffectPermit:
        request = operational_request_from_payload(record.request_payload)
        return _issue_product_operational_effect_permit(
            request_id=record.request_id,
            kind=record.kind,
            request_fingerprint=record.request_fingerprint,
            matrix_identity=operational_request_matrix_identity(request),
            issued_at=time(),
        )

    @staticmethod
    def _effect_reference(
        receipt: OperationalServiceReceipt,
        *,
        expected_owner: str,
    ) -> CoreFrozenPayload:
        detail = receipt.detail.to_mapping()
        if receipt.owner == PRODUCT_OPERATIONAL_TRANSACTION_OWNER:
            reason = str(detail.get("reason") or "").strip()
            fingerprint = str(
                detail.get("request_fingerprint") or ""
            ).strip()
            if (
                reason != "indeterminate_requires_reconciliation"
                or len(fingerprint) != 64
            ):
                raise ValueError(
                    "product_operational_indeterminate_proof_invalid"
                )
            proof_type = "product_indeterminate"
            references = {
                "reason": reason,
                "request_fingerprint": fingerprint,
            }
        elif receipt.disposition is OperationalDisposition.REJECTED:
            reason = str(detail.get("reason") or "").strip()
            if not reason:
                raise ValueError(
                    "product_operational_rejection_proof_missing"
                )
            proof_type = "domain_rejection"
            references = {"reason": reason}
        elif receipt.kind is OperationalKind.SET_INTELLIGENCE_AUTOPILOT:
            requested = detail.get("requested")
            control_mode = str(detail.get("control_mode") or "").strip()
            control_reason = str(
                detail.get("control_reason") or ""
            ).strip()
            control_generation = detail.get("control_generation")
            control_available = detail.get("control_available")
            if (
                type(requested) is not bool
                or control_mode
                not in {
                    "CGF_STANDALONE",
                    "INTELLIGENCE_OBSERVE",
                    "INTELLIGENCE_AUTOPILOT",
                }
                or not control_reason
                or type(control_generation) is not int
                or control_generation < 0
                or type(control_available) is not bool
            ):
                raise ValueError(
                    "product_operational_autopilot_effect_proof_missing"
                )
            proof_type = "product_control_policy"
            references = {
                "requested": requested,
                "control_mode": control_mode,
                "control_reason": control_reason,
                "control_generation": control_generation,
                "control_available": control_available,
            }
        elif receipt.kind is OperationalKind.SET_INTELLIGENCE_EXPLORE:
            requested = detail.get("requested")
            session_id = str(detail.get("session_id") or "").strip()
            started_at = detail.get("started_at")
            deadline_at = detail.get("deadline_at")
            reason = str(detail.get("reason") or "").strip()
            control_generation = detail.get("control_generation")
            if (
                type(requested) is not bool
                or type(started_at) not in {int, float}
                or type(deadline_at) not in {int, float}
                or not reason
                or type(control_generation) is not int
                or control_generation < 0
                or (
                    requested
                    and (
                        not session_id
                        or float(started_at) <= 0.0
                        or float(deadline_at) <= float(started_at)
                    )
                )
                or (
                    not requested
                    and (session_id or float(started_at) or float(deadline_at))
                )
            ):
                raise ValueError(
                    "product_operational_explore_effect_proof_missing"
                )
            proof_type = "product_explore_policy"
            references = {
                "requested": requested,
                "session_id": session_id,
                "started_at": float(started_at),
                "deadline_at": float(deadline_at),
                "reason": reason,
                "control_generation": control_generation,
            }
        elif receipt.kind is OperationalKind.READ_DIAGNOSTICS:
            if (receipt.disposition is not OperationalDisposition.ACCEPTED
                    or detail.get("action") not in {"start", "cancel"}
                    or (detail["action"] == "start" and not detail.get("run_id"))):
                raise ValueError("product_operational_read_diagnostic_proof_missing")
            proof_type = "modem_live_read_diagnostic_demand"
            references = {"run_id": detail.get("run_id", ""), "action": detail["action"]}
        elif receipt.kind is OperationalKind.INSPECT_NETWORK_SETTINGS:
            demand_id = detail.get("inspection_demand_id")
            if (receipt.disposition is not OperationalDisposition.ACCEPTED
                    or detail.get("reason") != "inspection_queued"
                    or not isinstance(demand_id, str) or not demand_id.strip()):
                raise ValueError("product_operational_inspection_effect_proof_missing")
            proof_type = "modem_live_inspection_demand"
            references = {"inspection_demand_id": demand_id}
        elif receipt.kind is OperationalKind.WIFI_SET_RADIO:
            command_root_id = str(
                detail.get("command_root_id") or ""
            ).strip()
            physical_root_id = str(
                detail.get("physical_action_root_id") or ""
            ).strip()
            if not command_root_id or not physical_root_id:
                raise ValueError(
                    "product_operational_wifi_effect_proof_missing"
                )
            proof_type = "core_command_dispatch"
            references = {
                "command_root_id": command_root_id,
                "physical_action_root_id": physical_root_id,
            }
        elif receipt.kind is OperationalKind.SET_DOWNSTREAM_HEALTH:
            request_id = str(
                detail.get("configuration_request_id") or ""
            ).strip()
            content_sha = str(
                detail.get("configuration_content_sha256") or ""
            ).strip()
            if (
                request_id != receipt.request_id
                or len(content_sha) != 64
            ):
                raise ValueError(
                    "product_operational_diagnostics_effect_proof_missing"
                )
            proof_type = "core_diagnostics_configuration"
            references = {
                "configuration_request_id": request_id,
                "configuration_content_sha256": content_sha,
            }
        elif receipt.kind is OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL:
            generation_id = str(
                detail.get("config_generation_id") or ""
            ).strip()
            fingerprint = str(
                detail.get("config_fingerprint") or ""
            ).strip()
            if not generation_id or not fingerprint:
                raise ValueError(
                    "product_operational_automation_effect_proof_missing"
                )
            proof_type = "foundation_automation_config_generation"
            references = {
                "config_generation_id": generation_id,
                "config_fingerprint": fingerprint,
            }
        elif receipt.kind is OperationalKind.ANTENNA_GUIDE:
            request_id = str(
                detail.get("operational_request_id") or ""
            ).strip()
            action = str(detail.get("operational_action") or "").strip()
            state_sha = str(
                detail.get("state_content_sha256") or ""
            ).strip()
            if (
                request_id != receipt.request_id
                or not action
                or len(state_sha) != 64
            ):
                raise ValueError(
                    "product_operational_antenna_effect_proof_missing"
                )
            proof_type = "foundation_antenna_state"
            references = {
                "operational_request_id": request_id,
                "operational_action": action,
                "state_content_sha256": state_sha,
            }
        elif receipt.kind in {
            OperationalKind.PLAN_BAND_SCHEDULE,
            OperationalKind.PLAN_BAND_SEQUENCE,
            OperationalKind.CANCEL_BAND_SCHEDULE,
            OperationalKind.CANCEL_BAND_SEQUENCE,
        }:
            revision = detail.get("revision")
            fingerprint_key = (
                "cancellation_request_fingerprint"
                if receipt.kind in {
                    OperationalKind.CANCEL_BAND_SCHEDULE,
                    OperationalKind.CANCEL_BAND_SEQUENCE,
                }
                else "operational_request_fingerprint"
            )
            fingerprint = str(
                detail.get(fingerprint_key) or ""
            ).strip()
            if (
                type(revision) is not int
                or revision <= 0
                or len(fingerprint) != 64
            ):
                raise ValueError(
                    "product_operational_schedule_effect_proof_missing"
                )
            proof_type = "foundation_schedule_state"
            references = {
                "revision": revision,
                fingerprint_key: fingerprint,
            }
        else:
            raise TypeError(
                "product_operational_effect_proof_kind_unhandled"
            )
        return CoreFrozenPayload.from_mapping(
            {
                "domain_owner": receipt.owner,
                "expected_domain_owner": expected_owner,
                "proof_type": proof_type,
                "receipt_detail_sha256": receipt.detail.sha256,
                "references": references,
            }
        )

    @staticmethod
    def _require_same_request(
        record: ProductOperationalTransaction,
        *,
        kind: OperationalKind,
        fingerprint: str,
    ) -> None:
        if (
            record.kind is not kind
            or record.request_fingerprint != fingerprint
        ):
            raise RuntimeError(
                "product_operational_request_id_payload_conflict"
            )

    @staticmethod
    def _indeterminate_receipt(
        request: object,
        *,
        kind: OperationalKind,
        fingerprint: str,
        error: Exception | None = None,
        reporting_error: str = "",
    ) -> OperationalServiceReceipt:
        identity = operational_request_matrix_identity(request)
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=kind,
            owner=PRODUCT_OPERATIONAL_TRANSACTION_OWNER,
            disposition=OperationalDisposition.FAILED,
            message=(
                "Operational effect state is indeterminate after Product "
                "recovery; the effect was not repeated."
            ),
            occurred_at=time(),
            runtime_namespace=identity.namespace,
            matrix_identity=identity,
            detail=CoreFrozenPayload.from_mapping(
                {
                    "reason": "indeterminate_requires_reconciliation",
                    "request_fingerprint": fingerprint,
                    "error_type": (
                        type(error).__name__
                        if error is not None
                        else ""
                    ),
                    "error": str(error) if error is not None else "",
                    "reporting_error": reporting_error,
                }
            ),
        )

    def _replace_and_commit(
        self,
        request_id: str,
        record: ProductOperationalTransaction,
    ) -> None:
        candidate = dict(self._records)
        candidate[request_id] = record
        retained = self._commit(candidate)
        self._records = retained

    def _commit(
        self,
        candidate: Mapping[str, ProductOperationalTransaction],
    ) -> dict[str, ProductOperationalTransaction]:
        # Request IDs are idempotency identities, not a display history.
        # Evicting a completed identity would permit the same ID to actuate
        # again, so correctness records are retained for the lifetime of this
        # Product state.
        retained = list(candidate.values())
        retained_records = {
            record.request_id: record
            for record in retained
        }
        content = {
            "schema": PRODUCT_OPERATIONAL_TRANSACTION_SCHEMA,
            "owner": PRODUCT_OPERATIONAL_TRANSACTION_OWNER,
            "records": [
                record.to_payload()
                for record in sorted(
                    retained,
                    key=lambda item: (item.admitted_at, item.request_id),
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
        return retained_records

    def _restore(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "product_operational_transaction_state_corrupt"
            ) from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError(
                "product_operational_transaction_state_mapping_required"
            )
        row = dict(raw)
        if set(row) != {
            "schema",
            "owner",
            "records",
            "content_sha256",
        }:
            raise RuntimeError(
                "product_operational_transaction_state_fields_invalid"
            )
        checksum = row.pop("content_sha256")
        if (
            row.get("schema") != PRODUCT_OPERATIONAL_TRANSACTION_SCHEMA
            or row.get("owner") != PRODUCT_OPERATIONAL_TRANSACTION_OWNER
            or type(checksum) is not str
            or checksum
            != hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        ):
            raise RuntimeError(
                "product_operational_transaction_state_contract_invalid"
            )
        records = row.get("records")
        if not isinstance(records, list):
            raise RuntimeError(
                "product_operational_transaction_records_list_required"
            )
        restored: dict[str, ProductOperationalTransaction] = {}
        for item in records:
            record = ProductOperationalTransaction.from_payload(item)
            if record.request_id in restored:
                raise RuntimeError(
                    "product_operational_transaction_duplicate_request"
                )
            restored[record.request_id] = record
        self._records = restored


__all__ = (
    "PRODUCT_OPERATIONAL_TRANSACTION_FILENAME",
    "PRODUCT_OPERATIONAL_TRANSACTION_OWNER",
    "PRODUCT_OPERATIONAL_TRANSACTION_SCHEMA",
    "ProductOperationalPhase",
    "ProductOperationalTransaction",
    "ProductOperationalTransactionAuthority",
    "operational_request_fingerprint",
)
