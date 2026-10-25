"""Single presentation publisher for typed operational service receipts."""

from __future__ import annotations

from threading import RLock
from typing import Any

from arch4.operational_service_contracts import OperationalServiceReceipt
from arch4.operational_service_status_contract import (
    operational_service_status_with_receipt,
)
from arch4.runtime_register_contract_names import REGISTER_OPERATIONAL_STATUS
from arch4.runtime_registers import require_core_runtime_register_bank


_STATUS_LOCK = RLock()


def publish_operational_service_status(
    engine: Any,
    receipt: OperationalServiceReceipt,
) -> dict[str, Any]:
    if not isinstance(receipt, OperationalServiceReceipt):
        raise TypeError("typed_operational_service_receipt_required")
    bank = require_core_runtime_register_bank(engine)
    with _STATUS_LOCK:
        current = bank.read(REGISTER_OPERATIONAL_STATUS)
        value = operational_service_status_with_receipt(
            current.value if current.valid else None,
            receipt,
        )
        register = bank.write_operational_status(
            value=value,
            source="operational_service_status",
            updated_at=receipt.occurred_at,
            valid=True,
            stale=False,
            dirty=False,
            trust="derived",
            source_kind="derived",
            validity="valid",
            transition=False,
        )
    return register.as_dict()


__all__ = ("publish_operational_service_status",)
