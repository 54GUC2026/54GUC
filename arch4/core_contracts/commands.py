"""Transport-neutral Core command request and lifecycle contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from threading import Condition, RLock
from typing import Any, Mapping, Protocol

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyRestartPermit,
)
from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreCommandTarget,
    core_command_target_from_payload,
    core_command_target_payload,
)
from arch4.core_contracts.payload import CoreFrozenPayload


BAND_AUTO_COMMAND_KIND = "BAND_AUTO"


def core_command_request_root(request: "CoreCommandRequest") -> str:
    if not isinstance(request, CoreCommandRequest):
        raise TypeError("core_command_request_root_typed_request_required")
    encoded = json.dumps(
        request.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "core-command:" + hashlib.sha256(encoded).hexdigest()[:32]


def _require_exact_fields(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    reason: str,
) -> None:
    if set(payload) != set(expected):
        raise ValueError(reason)


def _exact_positive_int(value: Any, reason: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(reason)
    return value


def _exact_positive_number(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(reason)
    return parsed


class CoreCommandOrigin(str, Enum):
    OPERATOR = "operator"
    OPERATOR_AUTOMATION = "operator_automation"
    AUTONOMOUS_INTELLIGENCE = "autonomous_intelligence"


class CoreCommandLifecycleState(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    HELD = "held"
    QUEUED = "queued"
    EXECUTING = "executing"
    MODEM_ACCEPTED = "modem_accepted"
    MODEM_REJECTED = "modem_rejected"
    TARGET_CONFIRMED = "target_confirmed"
    SETTLING = "settling"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class CoreCommandExecutionGateDisposition(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class CoreCommandExecutionAbortReceipt:
    request_id: str
    matrix_identity: MatrixGenerationIdentity
    reason: str
    worker_terminated: bool
    lifecycle_cancelled: bool
    event_write_barrier_released: bool
    schema: str = "CoreCommandExecutionAbortReceipt.v1"
    owner: str = "54guc.core.command_execution"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required(
                self.request_id,
                "core_command_execution_abort_request_id_required",
            ),
        )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "core_command_execution_abort_matrix_identity_required"
            )
        object.__setattr__(
            self,
            "reason",
            _required(
                self.reason,
                "core_command_execution_abort_reason_required",
            ),
        )
        for field_name in (
            "worker_terminated",
            "lifecycle_cancelled",
            "event_write_barrier_released",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"core_command_execution_abort_{field_name}_bool_required"
                )
        if self.schema != "CoreCommandExecutionAbortReceipt.v1":
            raise ValueError("core_command_execution_abort_schema_invalid")
        if self.owner != "54guc.core.command_execution":
            raise ValueError("core_command_execution_abort_owner_invalid")

    @property
    def terminated(self) -> bool:
        return (
            self.worker_terminated
            and self.lifecycle_cancelled
            and self.event_write_barrier_released
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "reason": self.reason,
            "worker_terminated": self.worker_terminated,
            "lifecycle_cancelled": self.lifecycle_cancelled,
            "event_write_barrier_released": (
                self.event_write_barrier_released
            ),
            "terminated": self.terminated,
        }


class CoreCommandExecutionGate:
    """One exact pre-effect decision: commit or abort, never timeout."""

    schema = "CoreCommandExecutionGate.v2"
    owner = "54guc.core.command_gateway"

    def __init__(
        self,
        *,
        request_id: str,
        matrix_identity: MatrixGenerationIdentity,
        emergency_restart_permit: CoreEmergencyRestartPermit | None,
    ) -> None:
        self.request_id = _required(
            request_id,
            "core_command_execution_gate_request_id_required",
        )
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "core_command_execution_gate_matrix_identity_required"
            )
        self.matrix_identity = matrix_identity
        if (
            emergency_restart_permit is not None
            and not isinstance(
                emergency_restart_permit,
                CoreEmergencyRestartPermit,
            )
        ):
            raise TypeError(
                "core_command_execution_gate_emergency_permit_invalid"
            )
        if (
            emergency_restart_permit is not None
            and (
                emergency_restart_permit.request_id != self.request_id
                or emergency_restart_permit.matrix_identity
                != self.matrix_identity
            )
        ):
            raise ValueError(
                "core_command_execution_gate_emergency_permit_mismatch"
            )
        self.emergency_restart_permit = emergency_restart_permit
        self._condition = Condition(RLock())
        self._disposition = CoreCommandExecutionGateDisposition.PENDING
        self._abort_reason = ""
        self._abort_acknowledgement: tuple[bool, bool] | None = None

    @property
    def disposition(self) -> CoreCommandExecutionGateDisposition:
        with self._condition:
            return self._disposition

    def commit(self) -> None:
        with self._condition:
            if self._disposition is CoreCommandExecutionGateDisposition.ABORTED:
                raise RuntimeError("core_command_execution_gate_already_aborted")
            self._disposition = CoreCommandExecutionGateDisposition.COMMITTED
            self._condition.notify_all()

    def abort(self, *, reason: str) -> None:
        exact_reason = _required(
            reason,
            "core_command_execution_gate_abort_reason_required",
        )
        with self._condition:
            if (
                self._disposition
                is CoreCommandExecutionGateDisposition.COMMITTED
            ):
                raise RuntimeError(
                    "core_command_execution_gate_already_committed"
                )
            if (
                self._disposition
                is CoreCommandExecutionGateDisposition.ABORTED
                and self._abort_reason != exact_reason
            ):
                raise RuntimeError(
                    "core_command_execution_gate_abort_reason_conflict"
                )
            self._disposition = CoreCommandExecutionGateDisposition.ABORTED
            self._abort_reason = exact_reason
            self._condition.notify_all()

    def wait_for_decision(
        self,
    ) -> tuple[CoreCommandExecutionGateDisposition, str]:
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    self._disposition
                    is not CoreCommandExecutionGateDisposition.PENDING
                )
            )
            return self._disposition, self._abort_reason

    def acknowledge_abort(
        self,
        *,
        lifecycle_cancelled: bool,
        event_write_barrier_released: bool,
    ) -> None:
        if (
            type(lifecycle_cancelled) is not bool
            or type(event_write_barrier_released) is not bool
        ):
            raise TypeError(
                "core_command_execution_gate_abort_ack_bool_required"
            )
        with self._condition:
            if self._disposition is not CoreCommandExecutionGateDisposition.ABORTED:
                raise RuntimeError(
                    "core_command_execution_gate_abort_ack_without_abort"
                )
            self._abort_acknowledgement = (
                lifecycle_cancelled,
                event_write_barrier_released,
            )
            self._condition.notify_all()

    def abort_acknowledgement(self) -> tuple[bool, bool] | None:
        with self._condition:
            return self._abort_acknowledgement


@dataclass(frozen=True, slots=True)
class CoreCommandAdmissionStatus:
    """Exact decision-bearing Core command admission status."""

    available: bool
    reason: str
    hard_blocked: bool
    projection_retry_pending: bool
    projection_obligation_pending: bool
    command_protection_active: bool
    command_protection_reason: str
    schema: str = "CoreCommandAdmissionStatus.v1"
    owner: str = "54guc.core.command_lifecycle_service"

    def __post_init__(self) -> None:
        for field_name in (
            "available",
            "hard_blocked",
            "projection_retry_pending",
            "projection_obligation_pending",
            "command_protection_active",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"core_command_admission_{field_name}_bool_required"
                )
        for field_name in ("reason", "command_protection_reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(
                    f"core_command_admission_{field_name}_string_required"
                )
            if value != value.strip():
                raise ValueError(
                    f"core_command_admission_{field_name}_invalid"
                )
        if self.available is not (self.reason == ""):
            raise ValueError("core_command_admission_availability_mismatch")
        if self.command_protection_active is not (
            self.command_protection_reason != ""
        ):
            raise ValueError(
                "core_command_admission_protection_reason_mismatch"
            )
        if self.schema != "CoreCommandAdmissionStatus.v1":
            raise ValueError("core_command_admission_schema_invalid")
        if self.owner != "54guc.core.command_lifecycle_service":
            raise ValueError("core_command_admission_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "available": self.available,
            "reason": self.reason,
            "hard_blocked": self.hard_blocked,
            "projection_retry_pending": self.projection_retry_pending,
            "projection_obligation_pending": (
                self.projection_obligation_pending
            ),
            "command_protection_active": self.command_protection_active,
            "command_protection_reason": self.command_protection_reason,
        }


@dataclass(frozen=True, slots=True)
class CoreCommandGatewayShutdownReceipt:
    """Exact gateway termination proof including blocked submitters."""

    active_submissions: int
    waiting_submissions: int
    already_closed: bool
    closed: bool = True
    schema: str = "CoreCommandGatewayShutdown.v2"
    owner: str = "54guc.core.command_gateway"

    def __post_init__(self) -> None:
        for field_name in ("active_submissions", "waiting_submissions"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(
                    f"core_command_gateway_shutdown_{field_name}_invalid"
                )
        if type(self.already_closed) is not bool:
            raise TypeError(
                "core_command_gateway_shutdown_already_closed_bool_required"
            )
        if type(self.closed) is not bool or self.closed is not True:
            raise TypeError(
                "core_command_gateway_shutdown_closed_true_required"
            )
        if self.schema != "CoreCommandGatewayShutdown.v2":
            raise ValueError("core_command_gateway_shutdown_schema_invalid")
        if self.owner != "54guc.core.command_gateway":
            raise ValueError("core_command_gateway_shutdown_owner_invalid")

    @property
    def terminated(self) -> bool:
        return (
            self.closed
            and self.active_submissions == 0
            and self.waiting_submissions == 0
        )

    @property
    def disposition(self) -> str:
        if not self.terminated:
            return "degraded"
        return "already_closed" if self.already_closed else "closed"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "disposition": self.disposition,
            "closed": self.closed,
            "terminated": self.terminated,
            "already_closed": self.already_closed,
            "active_submissions": self.active_submissions,
            "waiting_submissions": self.waiting_submissions,
        }


@dataclass(frozen=True, slots=True)
class CoreCommandIngressContext:
    matrix_identity: MatrixGenerationIdentity
    identity_code: str
    observed_at: float
    schema: str = "CoreCommandIngressContext.v3"
    owner: str = "54guc.core.command_gateway"

    def __post_init__(self) -> None:
        for field_name in ("identity_code",):
            object.__setattr__(
                self,
                field_name,
                _required(
                    getattr(self, field_name),
                    f"core_command_context_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "core_command_context_matrix_identity_required"
            )
        observed_at = _exact_positive_number(
            self.observed_at,
            "core_command_context_positive_observed_at_required",
        )
        object.__setattr__(self, "observed_at", observed_at)
        if self.schema != "CoreCommandIngressContext.v3":
            raise ValueError("core_command_context_schema_invalid")
        if self.owner != "54guc.core.command_gateway":
            raise ValueError("core_command_context_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_code": self.identity_code,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Any,
    ) -> "CoreCommandIngressContext":
        if not isinstance(payload, Mapping):
            raise TypeError("core_command_context_mapping_required")
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "schema",
                    "owner",
                    "matrix_identity",
                    "identity_code",
                    "observed_at",
                }
            ),
            "core_command_context_fields_invalid",
        )
        return cls(
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                _payload_mapping(
                    payload.get("matrix_identity"),
                    "core_command_context_matrix_identity",
                )
            ),
            identity_code=payload.get("identity_code", ""),
            observed_at=payload.get("observed_at", 0.0),
            schema=payload.get("schema", ""),
            owner=payload.get("owner", ""),
        )

    @property
    def session_id(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation

    @property
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence


_ROLE_ORIGIN = {
    CoreClientRole.OPERATOR: CoreCommandOrigin.OPERATOR,
    CoreClientRole.OPERATOR_AUTOMATION: CoreCommandOrigin.OPERATOR_AUTOMATION,
    CoreClientRole.AUTONOMOUS_INTELLIGENCE: CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE,
}


def _required(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    if not value or value != value.strip():
        raise ValueError(reason)
    return value


@dataclass(frozen=True, slots=True)
class CoreCommandExecutionReceipt:
    """Exact executor-to-gateway result for one typed command request."""

    request_id: str
    command_root_id: str
    matrix_identity: MatrixGenerationIdentity
    submitted: bool
    reason: str
    occurred_at: float
    physical_action_root_id: str = ""
    detail: CoreFrozenPayload = CoreFrozenPayload.empty()
    schema: str = "CoreCommandExecutionReceipt.v2"
    owner: str = "54guc.core.command_execution"

    def __post_init__(self) -> None:
        for field_name in ("request_id", "command_root_id", "reason"):
            object.__setattr__(
                self,
                field_name,
                _required(
                    getattr(self, field_name),
                    f"core_command_execution_{field_name}_required",
                ),
            )
        if type(self.submitted) is not bool:
            raise TypeError("core_command_execution_submitted_bool_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "core_command_execution_matrix_identity_required"
            )
        physical_root = self.physical_action_root_id
        if not isinstance(physical_root, str):
            raise TypeError(
                "core_command_execution_physical_action_root_string_required"
            )
        if physical_root != physical_root.strip():
            raise ValueError(
                "core_command_execution_physical_action_root_invalid"
            )
        if self.submitted and not physical_root:
            raise ValueError(
                "core_command_execution_physical_action_root_required"
            )
        if not self.submitted and physical_root:
            raise ValueError(
                "core_command_execution_rejected_physical_action_root_forbidden"
            )
        object.__setattr__(
            self,
            "occurred_at",
            _exact_positive_number(
                self.occurred_at,
                "core_command_execution_timestamp_invalid",
            ),
        )
        if not isinstance(self.detail, CoreFrozenPayload):
            raise TypeError("core_command_execution_detail_payload_required")
        if self.schema != "CoreCommandExecutionReceipt.v2":
            raise ValueError("core_command_execution_schema_invalid")
        if self.owner != "54guc.core.command_execution":
            raise ValueError("core_command_execution_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "command_root_id": self.command_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "submitted": self.submitted,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
            "physical_action_root_id": self.physical_action_root_id,
            "detail": self.detail.to_payload(),
        }

    @classmethod
    def from_payload(
        cls,
        payload: Any,
    ) -> "CoreCommandExecutionReceipt":
        if not isinstance(payload, Mapping):
            raise TypeError("core_command_execution_mapping_required")
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "schema",
                    "owner",
                    "request_id",
                    "command_root_id",
                    "matrix_identity",
                    "submitted",
                    "reason",
                    "occurred_at",
                    "physical_action_root_id",
                    "detail",
                }
            ),
            "core_command_execution_fields_invalid",
        )
        return cls(
            request_id=payload.get("request_id"),
            command_root_id=payload.get("command_root_id"),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                _payload_mapping(
                    payload.get("matrix_identity"),
                    "core_command_execution_matrix_identity",
                )
            ),
            submitted=payload.get("submitted"),
            reason=payload.get("reason"),
            occurred_at=payload.get("occurred_at"),
            physical_action_root_id=payload.get(
                "physical_action_root_id"
            ),
            detail=CoreFrozenPayload.from_mapping(
                _payload_mapping(
                    payload.get("detail"),
                    "core_command_execution_detail",
                )
            ),
            schema=payload.get("schema"),
            owner=payload.get("owner"),
        )


@dataclass(frozen=True, slots=True)
class CoreCommandRequest:
    request_id: str
    correlation_id: str
    client_id: str
    client_role: CoreClientRole
    origin: CoreCommandOrigin
    matrix_identity: MatrixGenerationIdentity
    identity_code: str
    command_kind: str
    target: CoreCommandTarget
    parameters: CoreFrozenPayload
    capability_reference: str
    created_at: float
    expires_at: float
    confirmed: bool
    safety_context: CoreFrozenPayload
    autonomous_lease: str = ""
    schema: str = "CoreCommandRequest.v2"
    owner: str = "54guc.core.command_request"

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "correlation_id",
            "client_id",
            "identity_code",
            "command_kind",
            "capability_reference",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(
                    getattr(self, field_name),
                    f"core_command_{field_name}_required",
                ),
            )
        if not isinstance(self.client_role, CoreClientRole):
            raise TypeError("core_command_typed_client_role_required")
        if not isinstance(self.origin, CoreCommandOrigin):
            raise TypeError("core_command_typed_origin_required")
        expected_origin = _ROLE_ORIGIN.get(self.client_role)
        if expected_origin is None or expected_origin is not self.origin:
            raise PermissionError("core_command_role_origin_mismatch")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_command_matrix_identity_required")
        if not isinstance(self.target, CORE_COMMAND_TARGET_TYPES):
            raise TypeError("core_command_typed_target_required")
        if self.command_kind != self.target.command_kind:
            raise ValueError("core_command_target_kind_mismatch")
        if not isinstance(self.parameters, CoreFrozenPayload):
            raise TypeError("core_command_parameters_payload_required")
        if not isinstance(self.safety_context, CoreFrozenPayload):
            raise TypeError("core_command_safety_context_payload_required")
        created_at = _exact_positive_number(
            self.created_at,
            "core_command_created_at_invalid",
        )
        expires_at = _exact_positive_number(
            self.expires_at,
            "core_command_expires_at_invalid",
        )
        if (
            not math.isfinite(created_at)
            or not math.isfinite(expires_at)
            or created_at <= 0.0
            or expires_at <= created_at
        ):
            raise ValueError("core_command_expiry_order_invalid")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        if type(self.confirmed) is not bool:
            raise TypeError("core_command_confirmed_must_be_bool")
        if not isinstance(self.autonomous_lease, str):
            raise TypeError("core_command_autonomous_lease_string_required")
        lease = self.autonomous_lease
        if lease != lease.strip():
            raise ValueError("core_command_autonomous_lease_invalid")
        if self.origin is CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE:
            if not lease:
                raise ValueError("core_command_autonomous_lease_required")
        elif lease:
            raise ValueError("core_command_autonomous_lease_forbidden")
        object.__setattr__(self, "autonomous_lease", lease)
        if self.schema != "CoreCommandRequest.v2":
            raise ValueError("core_command_schema_invalid")
        if self.owner != "54guc.core.command_request":
            raise ValueError("core_command_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "client_id": self.client_id,
            "client_role": self.client_role.value,
            "origin": self.origin.value,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_code": self.identity_code,
            "command_kind": self.command_kind,
            "target": core_command_target_payload(self.target),
            "parameters": self.parameters.to_payload(),
            "capability_reference": self.capability_reference,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "confirmed": self.confirmed,
            "safety_context": self.safety_context.to_payload(),
            "autonomous_lease": self.autonomous_lease,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "CoreCommandRequest":
        if not isinstance(payload, Mapping):
            raise TypeError("core_command_request_mapping_required")
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "schema",
                    "owner",
                    "request_id",
                    "correlation_id",
                    "client_id",
                    "client_role",
                    "origin",
                    "matrix_identity",
                    "identity_code",
                    "command_kind",
                    "target",
                    "parameters",
                    "capability_reference",
                    "created_at",
                    "expires_at",
                    "confirmed",
                    "safety_context",
                    "autonomous_lease",
                }
            ),
            "core_command_request_fields_invalid",
        )
        client_role = payload.get("client_role")
        origin = payload.get("origin")
        if not isinstance(client_role, str):
            raise TypeError("core_command_client_role_string_required")
        if not isinstance(origin, str):
            raise TypeError("core_command_origin_string_required")
        return cls(
            request_id=payload.get("request_id", ""),
            correlation_id=payload.get("correlation_id", ""),
            client_id=payload.get("client_id", ""),
            client_role=CoreClientRole(client_role),
            origin=CoreCommandOrigin(origin),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                _payload_mapping(
                    payload.get("matrix_identity"),
                    "core_command_matrix_identity",
                )
            ),
            identity_code=payload.get("identity_code", ""),
            command_kind=payload.get("command_kind", ""),
            target=core_command_target_from_payload(payload.get("target")),
            parameters=CoreFrozenPayload.from_mapping(
                _payload_mapping(
                    payload.get("parameters"),
                    "core_command_parameters",
                )
            ),
            capability_reference=payload.get("capability_reference", ""),
            created_at=payload.get("created_at", 0.0),
            expires_at=payload.get("expires_at", 0.0),
            confirmed=payload.get("confirmed"),
            safety_context=CoreFrozenPayload.from_mapping(
                _payload_mapping(
                    payload.get("safety_context"),
                    "core_command_safety_context",
                )
            ),
            autonomous_lease=payload.get("autonomous_lease", ""),
            schema=payload.get("schema", ""),
            owner=payload.get("owner", ""),
        )

    @property
    def session_id(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation

    @property
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence


def _payload_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name}_mapping_required")
    return value


class CoreCommandClientPort(Protocol):
    """Customer-visible command ingress; exposes no mutable Core owner."""

    def submit(
        self,
        request: CoreCommandRequest,
        *,
        now: float | None = None,
    ) -> Mapping[str, Any]: ...


__all__ = (
    "BAND_AUTO_COMMAND_KIND",
    "CoreCommandAdmissionStatus",
    "CoreCommandExecutionAbortReceipt",
    "CoreCommandExecutionGate",
    "CoreCommandExecutionGateDisposition",
    "CoreCommandGatewayShutdownReceipt",
    "CoreCommandIngressContext",
    "CoreCommandExecutionReceipt",
    "CoreCommandClientPort",
    "CoreCommandLifecycleState",
    "CoreCommandOrigin",
    "CoreCommandRequest",
    "core_command_request_root",
)
