"""Bounded per-operation presentation truth for operational service receipts."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.operational_service_contracts import (
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
)


OPERATIONAL_SERVICE_STATUS_FILENAME = "operational_service_status.json"
OPERATIONAL_SERVICE_STATUS_SCHEMA = "OperationalServiceStatus.v3"
OPERATIONAL_SERVICE_STATUS_OWNER = "54guc.presentation.operational_service_status"

_RECEIPT_FIELDS = frozenset(
    {
        "request_id",
        "operation",
        "service_owner",
        "status",
        "message",
        "occurred_at",
        "runtime_namespace",
        "matrix_identity",
        "ok",
        "detail",
    }
)


def normalize_operational_service_receipt(value: Any) -> dict[str, Any]:
    if isinstance(value, OperationalServiceReceipt):
        receipt = value
    elif isinstance(value, Mapping):
        receipt = OperationalServiceReceipt.from_payload(value)
    else:
        raise TypeError("typed_operational_service_receipt_required")
    return {
        "request_id": receipt.request_id,
        "operation": receipt.kind.value,
        "service_owner": receipt.owner,
        "status": receipt.disposition.value,
        "message": receipt.message,
        "occurred_at": receipt.occurred_at,
        "runtime_namespace": receipt.runtime_namespace.as_dict(),
        "matrix_identity": receipt.matrix_identity.as_dict(),
        "ok": receipt.ok,
        "detail": receipt.detail.to_payload(),
    }


def normalize_operational_service_status(value: Any) -> dict[str, Any]:
    """Validate the sole register shape; receipt-shaped register writes are illegal."""

    if not isinstance(value, Mapping):
        raise TypeError("operational_service_status_mapping_required")
    payload = dict(value)
    if payload.get("schema") != OPERATIONAL_SERVICE_STATUS_SCHEMA:
        raise ValueError("operational_service_status_schema_invalid")
    if payload.get("owner") != OPERATIONAL_SERVICE_STATUS_OWNER:
        raise ValueError("operational_service_status_owner_invalid")
    if set(payload) != {"schema", "owner", "latest_operation", "latest", "by_operation"}:
        raise ValueError("operational_service_status_fields_invalid")
    latest_operation = str(payload.get("latest_operation") or "").strip()
    latest = _normalize_status_entry(payload.get("latest"))
    by_operation_raw = payload.get("by_operation")
    if not isinstance(by_operation_raw, Mapping):
        raise TypeError("operational_service_status_by_operation_mapping_required")
    by_operation: dict[str, dict[str, Any]] = {}
    for operation, entry in by_operation_raw.items():
        operation_name = str(operation or "").strip()
        try:
            typed_operation = OperationalKind(operation_name)
        except ValueError as exc:
            raise ValueError(
                f"operational_service_status_operation_invalid:{operation_name}"
            ) from exc
        normalized = _normalize_status_entry(entry)
        if normalized["operation"] != typed_operation.value:
            raise ValueError(
                f"operational_service_status_operation_key_mismatch:{operation_name}"
            )
        by_operation[operation_name] = normalized
    if not latest_operation or latest_operation not in by_operation:
        raise ValueError("operational_service_status_latest_operation_invalid")
    if latest != by_operation[latest_operation]:
        raise ValueError("operational_service_status_latest_entry_mismatch")
    return {
        "schema": OPERATIONAL_SERVICE_STATUS_SCHEMA,
        "owner": OPERATIONAL_SERVICE_STATUS_OWNER,
        "latest_operation": latest_operation,
        "latest": latest,
        "by_operation": by_operation,
    }


def operational_service_status_with_receipt(
    current: Any,
    receipt: Any,
) -> dict[str, Any]:
    entry = normalize_operational_service_receipt(receipt)
    if current is None:
        by_operation: dict[str, dict[str, Any]] = {}
    else:
        by_operation = dict(
            normalize_operational_service_status(current)["by_operation"]
        )
    operation = str(entry["operation"])
    by_operation[operation] = entry
    return normalize_operational_service_status(
        {
            "schema": OPERATIONAL_SERVICE_STATUS_SCHEMA,
            "owner": OPERATIONAL_SERVICE_STATUS_OWNER,
            "latest_operation": operation,
            "latest": entry,
            "by_operation": by_operation,
        }
    )


def _normalize_status_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("operational_service_status_entry_mapping_required")
    payload = dict(value)
    if set(payload) != _RECEIPT_FIELDS:
        raise ValueError("operational_service_status_entry_fields_invalid")
    receipt = OperationalServiceReceipt.from_payload(
        {
            "schema": "OperationalServiceReceipt.v2",
            "request_id": payload["request_id"],
            "kind": payload["operation"],
            "owner": payload["service_owner"],
            "disposition": payload["status"],
            "message": payload["message"],
            "occurred_at": payload["occurred_at"],
            "runtime_namespace": payload["runtime_namespace"],
            "matrix_identity": payload["matrix_identity"],
            "ok": payload["ok"],
            "detail": payload["detail"],
        }
    )
    return normalize_operational_service_receipt(receipt)


__all__ = (
    "OPERATIONAL_SERVICE_STATUS_FILENAME",
    "OPERATIONAL_SERVICE_STATUS_OWNER",
    "OPERATIONAL_SERVICE_STATUS_SCHEMA",
    "normalize_operational_service_receipt",
    "normalize_operational_service_status",
    "operational_service_status_with_receipt",
)
