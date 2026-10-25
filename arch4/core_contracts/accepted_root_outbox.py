"""Customer delivery contracts referencing immutable accepted Core roots."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.accepted_generation_commit import (
    CustomerCursorState,
)
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.payload import CoreFrozenPayload


class CoreAcceptedRootResyncRequired(RuntimeError):
    """The optional customer fell behind the bounded accepted-root outbox."""

    def __init__(self, state: CustomerCursorState) -> None:
        if not isinstance(state, CustomerCursorState) or not state.resync_required:
            raise TypeError("core_accepted_root_resync_state_required")
        self.state = state
        loss = state.loss_range
        assert loss is not None
        super().__init__(
            "core_accepted_root_resync_required:"
            f"{loss.session_scope_key}:{loss.source_epoch}:"
            f"{loss.lifecycle_generation}:"
            f"{loss.first_source_sequence}-{loss.last_source_sequence}"
        )


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootOutboxRecord:
    key: str
    root: CoreAcceptedGenerationRoot

    def __post_init__(self) -> None:
        if (
            type(self.key) is not str
            or not self.key
            or self.key != self.key.strip()
        ):
            raise ValueError("core_accepted_root_outbox_key_required")
        if not isinstance(self.root, CoreAcceptedGenerationRoot):
            raise TypeError("core_accepted_root_outbox_root_required")


def _validate_fingerprint(value: str, *, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TypeError(f"{label}_invalid")


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootTerminalObligation:
    key: str
    matrix_identity: MatrixGenerationIdentity
    root_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key or self.key != self.key.strip():
            raise TypeError("core_terminal_obligation_key_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_terminal_obligation_matrix_identity_required")
        _validate_fingerprint(
            self.root_fingerprint,
            label="core_terminal_obligation_root_fingerprint",
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "matrix_identity": self.matrix_identity.as_dict(),
            "root_fingerprint": self.root_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootCustomerTerminalDisposition:
    lifecycle_generation: int
    shutdown_started_at: float
    client_id: str
    reason: str
    obligations: tuple[CoreAcceptedRootTerminalObligation, ...]
    inflight_key: str
    disposed_at: float
    schema: str = "CoreAcceptedRootCustomerTerminalDisposition.v2"
    owner: str = "54guc.core.accepted_root_outbox"

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation <= 0:
            raise TypeError("core_terminal_disposition_lifecycle_invalid")
        for name, value in (
            ("shutdown_started_at", self.shutdown_started_at),
            ("disposed_at", self.disposed_at),
        ):
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise TypeError(f"core_terminal_disposition_{name}_invalid")
        if self.disposed_at < self.shutdown_started_at:
            raise ValueError("core_terminal_disposition_time_order_invalid")
        if (
            type(self.client_id) is not str
            or not self.client_id
            or self.client_id != self.client_id.strip()
        ):
            raise TypeError("core_terminal_disposition_client_id_invalid")
        if self.reason != "product_runtime_shutdown_after_service_revocation":
            raise ValueError("core_terminal_disposition_reason_invalid")
        if not isinstance(self.obligations, tuple) or any(
            not isinstance(item, CoreAcceptedRootTerminalObligation)
            for item in self.obligations
        ):
            raise TypeError("core_terminal_disposition_obligations_invalid")
        if type(self.inflight_key) is not str:
            raise TypeError("core_terminal_disposition_inflight_key_invalid")
        if (
            self.schema != "CoreAcceptedRootCustomerTerminalDisposition.v2"
            or self.owner != "54guc.core.accepted_root_outbox"
        ):
            raise ValueError("core_terminal_disposition_owner_schema_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "client_id": self.client_id,
            "reason": self.reason,
            "obligations": [
                obligation.to_payload() for obligation in self.obligations
            ],
            "inflight_key": self.inflight_key,
            "disposed_at": self.disposed_at,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootTerminalDispositionBatch:
    lifecycle_generation: int
    shutdown_started_at: float
    reason: str
    customers: tuple[CoreAcceptedRootCustomerTerminalDisposition, ...]
    schema: str = "CoreAcceptedRootTerminalDispositionBatch.v2"
    owner: str = "54guc.core.accepted_root_outbox"

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation <= 0:
            raise TypeError("core_terminal_batch_lifecycle_invalid")
        if (
            type(self.shutdown_started_at) is not float
            or not math.isfinite(self.shutdown_started_at)
            or self.shutdown_started_at <= 0.0
        ):
            raise TypeError("core_terminal_batch_shutdown_time_invalid")
        if self.reason != "product_runtime_shutdown_after_service_revocation":
            raise ValueError("core_terminal_batch_reason_invalid")
        if not isinstance(self.customers, tuple) or any(
            not isinstance(item, CoreAcceptedRootCustomerTerminalDisposition)
            for item in self.customers
        ):
            raise TypeError("core_terminal_batch_customers_invalid")
        if any(
            item.lifecycle_generation != self.lifecycle_generation
            or item.shutdown_started_at != self.shutdown_started_at
            or item.reason != self.reason
            for item in self.customers
        ):
            raise ValueError("core_terminal_batch_customer_identity_mismatch")
        if len({item.client_id for item in self.customers}) != len(self.customers):
            raise ValueError("core_terminal_batch_duplicate_customer")
        if (
            self.schema != "CoreAcceptedRootTerminalDispositionBatch.v2"
            or self.owner != "54guc.core.accepted_root_outbox"
        ):
            raise ValueError("core_terminal_batch_owner_schema_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "reason": self.reason,
            "customers": {
                item.client_id: item.to_payload() for item in self.customers
            },
        }


@dataclass(frozen=True, slots=True)
class CorePrivateStartupRootTerminalDisposition:
    lifecycle_generation: int
    shutdown_started_at: float
    matrix_identity: MatrixGenerationIdentity
    root_fingerprint: str
    disposed_at: float
    reason: str = "product_shutdown_before_root_publication"
    schema: str = "CorePrivateStartupRootDisposition.v2"
    owner: str = "runtime.modem_sample_publication"

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation <= 0:
            raise TypeError("core_private_disposition_lifecycle_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_private_disposition_matrix_identity_required")
        _validate_fingerprint(
            self.root_fingerprint,
            label="core_private_disposition_root_fingerprint",
        )
        for name, value in (
            ("shutdown_started_at", self.shutdown_started_at),
            ("disposed_at", self.disposed_at),
        ):
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise TypeError(f"core_private_disposition_{name}_invalid")
        if self.disposed_at < self.shutdown_started_at:
            raise ValueError("core_private_disposition_time_order_invalid")
        if self.reason != "product_shutdown_before_root_publication":
            raise ValueError("core_private_disposition_reason_invalid")
        if (
            self.schema != "CorePrivateStartupRootDisposition.v2"
            or self.owner != "runtime.modem_sample_publication"
        ):
            raise ValueError("core_private_disposition_owner_schema_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "matrix_identity": self.matrix_identity.as_dict(),
            "root_fingerprint": self.root_fingerprint,
            "reason": self.reason,
            "disposed_at": self.disposed_at,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootOutboxNamespaceBindingReceipt:
    namespace: RuntimeGenerationNamespace
    changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("core_outbox_binding_namespace_required")
        if type(self.changed) is not bool:
            raise TypeError("core_outbox_binding_changed_bool_required")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "CoreAcceptedRootOutboxNamespaceBinding.v2",
            "owner": "54guc.core.accepted_root_outbox",
            **self.namespace.as_dict(),
            "changed": self.changed,
            "retired_records": 0,
            "retired_cursors": 0,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootOutboxDrainStatus:
    admission_open: bool
    admission_closed_at: float
    latest_source_sequence: int
    attached_clients: tuple[str, ...]
    pending_clients: tuple[str, ...]
    inflight_count: int
    retained_record_count: int
    retained_cursor_count: int
    current_root_retained: bool

    def __post_init__(self) -> None:
        if type(self.admission_open) is not bool:
            raise TypeError("core_outbox_drain_admission_bool_required")
        if (
            type(self.admission_closed_at) is not float
            or not math.isfinite(self.admission_closed_at)
            or self.admission_closed_at < 0.0
        ):
            raise TypeError("core_outbox_drain_closed_at_invalid")
        for name in (
            "latest_source_sequence",
            "inflight_count",
            "retained_record_count",
            "retained_cursor_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"core_outbox_drain_{name}_invalid")
        if type(self.current_root_retained) is not bool:
            raise TypeError(
                "core_outbox_drain_current_root_retained_invalid"
            )
        for name in ("attached_clients", "pending_clients"):
            value = getattr(self, name)
            if (
                not isinstance(value, tuple)
                or any(
                    type(item) is not str
                    or not item
                    or item != item.strip()
                    for item in value
                )
            ):
                raise TypeError(f"core_outbox_drain_{name}_invalid")
        if self.admission_open and self.admission_closed_at != 0.0:
            raise ValueError("core_outbox_drain_open_with_close_time")

    @property
    def drained(self) -> bool:
        return bool(
            not self.admission_open
            and not self.pending_clients
            and self.inflight_count == 0
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "CoreAcceptedRootOutboxDrainStatus.v2",
            "owner": "54guc.core.accepted_root_outbox",
            "admission_open": self.admission_open,
            "admission_closed_at": self.admission_closed_at,
            "latest_source_sequence": self.latest_source_sequence,
            "attached_clients": list(self.attached_clients),
            "pending_clients": list(self.pending_clients),
            "inflight_count": self.inflight_count,
            "retained_record_count": self.retained_record_count,
            "retained_cursor_count": self.retained_cursor_count,
            "current_root_retained": self.current_root_retained,
            "drained": self.drained,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootOutboxTerminationReceipt:
    retired_records: int
    retired_cursors: int
    terminal_dispositions: CoreFrozenPayload
    terminated_at: float

    def __post_init__(self) -> None:
        for name in ("retired_records", "retired_cursors"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"core_outbox_termination_{name}_invalid")
        if not isinstance(self.terminal_dispositions, CoreFrozenPayload):
            raise TypeError(
                "core_outbox_termination_dispositions_required"
            )
        if (
            type(self.terminated_at) is not float
            or not math.isfinite(self.terminated_at)
            or self.terminated_at <= 0.0
        ):
            raise TypeError("core_outbox_termination_time_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "CoreAcceptedRootOutboxTerminationReceipt.v2",
            "owner": "54guc.core.accepted_root_outbox",
            "retired_records": self.retired_records,
            "retired_cursors": self.retired_cursors,
            "terminal_dispositions": (
                self.terminal_dispositions.to_mapping()
            ),
            "remaining_records": 0,
            "remaining_cursors": 0,
            "remaining_inflight": 0,
            "current_root_retained": False,
            "terminated_at": self.terminated_at,
        }


class CoreAcceptedRootOutboxPort(Protocol):
    def current(self) -> CoreAcceptedGenerationRoot | None: ...

    def claim_next(
        self,
        client_id: str,
    ) -> CoreAcceptedRootOutboxRecord | None: ...

    def acknowledge(self, client_id: str, *, key: str) -> None: ...

    def release(
        self,
        client_id: str,
        *,
        key: str,
        error: BaseException,
    ) -> None: ...

    def resync_required(
        self,
        client_id: str,
    ) -> CustomerCursorState | None: ...

    def acknowledge_resync(
        self,
        client_id: str,
        *,
        through_source_sequence: int,
    ) -> CustomerCursorState: ...

    def status(self) -> dict[str, Any]: ...

    def close_admission(self) -> CoreAcceptedRootOutboxDrainStatus: ...

    def drain_status(self) -> CoreAcceptedRootOutboxDrainStatus: ...


__all__ = (
    "CoreAcceptedRootCustomerTerminalDisposition",
    "CoreAcceptedRootOutboxPort",
    "CoreAcceptedRootOutboxRecord",
    "CoreAcceptedRootResyncRequired",
    "CoreAcceptedRootOutboxDrainStatus",
    "CoreAcceptedRootOutboxNamespaceBindingReceipt",
    "CoreAcceptedRootOutboxTerminationReceipt",
    "CoreAcceptedRootTerminalDispositionBatch",
    "CoreAcceptedRootTerminalObligation",
    "CorePrivateStartupRootTerminalDisposition",
)
