"""Closed typed boundary contracts for Core, Foundation and Intelligence services.

This module owns no execution.  It is the explicit process-boundary vocabulary
that replaces the deleted generic runtime-command string plus params mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.canonical_json import canonical_json_bytes
from arch4.trajectory_contract import MIN_BAND_SEQUENCE_INTERVAL_S


DOWNSTREAM_HEALTH_INTERVAL_MIN_S = 1.0
DOWNSTREAM_HEALTH_INTERVAL_MAX_S = 3600.0


class OperationalDisposition(str, Enum):
    CONFIRMED = "confirmed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    UNATTACHED = "unattached"


class CoreOperationalFreshness(str, Enum):
    OBSERVATION_DEPENDENT = "observation_dependent"
    PERSISTENT_CONFIGURATION = "persistent_configuration"
    CURRENT_OBSERVATION_DIAGNOSTIC = "current_observation_diagnostic"


class OperationalKind(str, Enum):
    ANTENNA_GUIDE = "antenna_guide"
    WIFI_SET_RADIO = "wifi_set_radio"
    READ_DIAGNOSTICS = "read_diagnostics"
    INSPECT_NETWORK_SETTINGS = "inspect_network_settings"
    SET_DOWNSTREAM_HEALTH = "set_downstream_health"
    CONFIGURE_SMS_COMMAND_CONTROL = "configure_sms_command_control"
    PLAN_BAND_SCHEDULE = "plan_band_schedule"
    PLAN_BAND_SEQUENCE = "plan_band_sequence"
    CANCEL_BAND_SCHEDULE = "cancel_band_schedule"
    CANCEL_BAND_SEQUENCE = "cancel_band_sequence"
    SET_INTELLIGENCE_AUTOPILOT = "set_intelligence_autopilot"
    SET_INTELLIGENCE_EXPLORE = "set_intelligence_explore"


_PRODUCT_OPERATIONAL_PERMIT_ISSUER = object()


@dataclass(frozen=True, slots=True)
class ProductOperationalEffectPermit:
    request_id: str
    kind: OperationalKind
    request_fingerprint: str
    matrix_identity: MatrixGenerationIdentity
    issued_at: float
    _issuer: object
    owner: str = "54guc.product.operational_transaction"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required(
                self.request_id,
                "product_operational_permit_request_id_required",
            ),
        )
        if not isinstance(self.kind, OperationalKind):
            raise TypeError("product_operational_permit_kind_required")
        if (
            type(self.request_fingerprint) is not str
            or len(self.request_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.request_fingerprint
            )
        ):
            raise ValueError(
                "product_operational_permit_fingerprint_invalid"
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "product_operational_permit_matrix_identity_required"
            )
        _positive_time(
            self.issued_at,
            "product_operational_permit_issued_at_invalid",
        )
        if self.owner != "54guc.product.operational_transaction":
            raise ValueError("product_operational_permit_owner_invalid")
        if self._issuer is not _PRODUCT_OPERATIONAL_PERMIT_ISSUER:
            raise PermissionError(
                "product_operational_permit_private_issuer_required"
            )


def _issue_product_operational_effect_permit(
    *,
    request_id: str,
    kind: OperationalKind,
    request_fingerprint: str,
    matrix_identity: MatrixGenerationIdentity,
    issued_at: float,
) -> ProductOperationalEffectPermit:
    return ProductOperationalEffectPermit(
        request_id=request_id,
        kind=kind,
        request_fingerprint=request_fingerprint,
        matrix_identity=matrix_identity,
        issued_at=issued_at,
        _issuer=_PRODUCT_OPERATIONAL_PERMIT_ISSUER,
    )


def _required(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    if not value or value != value.strip():
        raise ValueError(reason)
    return value


def _positive_time(value: Any, reason: str) -> float:
    if type(value) is not float:
        raise TypeError(reason)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(reason)
    return value


def _bands(values: Any, *, field_name: str) -> tuple[int, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name}_iterable_required")
    try:
        candidates = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name}_iterable_required") from exc
    normalized: list[int] = []
    for value in candidates:
        if type(value) is not int:
            raise TypeError(f"{field_name}_integer_required")
        band = value
        if band <= 0:
            raise ValueError(f"{field_name}_positive_band_required")
        if band not in normalized:
            normalized.append(band)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class CoreOperatorRequestContext:
    request_id: str
    created_at: float
    expires_at: float
    ingress: CoreCommandIngressContext
    freshness: CoreOperationalFreshness
    client_id: str = "ngui.operator"
    client_role: CoreClientRole = CoreClientRole.OPERATOR
    source: str = "ngui"

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "core_operational_request_id_required"))
        object.__setattr__(self, "client_id", _required(self.client_id, "core_operational_client_id_required"))
        object.__setattr__(self, "source", _required(self.source, "core_operational_source_required"))
        created = _positive_time(self.created_at, "core_operational_positive_created_at_required")
        expires = _positive_time(self.expires_at, "core_operational_positive_expires_at_required")
        if expires <= created:
            raise ValueError("core_operational_expiry_order_invalid")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        if not isinstance(self.ingress, CoreCommandIngressContext):
            raise TypeError("core_operational_typed_ingress_required")
        if not isinstance(self.freshness, CoreOperationalFreshness):
            raise TypeError("core_operational_typed_freshness_required")
        if self.client_role is not CoreClientRole.OPERATOR:
            raise PermissionError("core_operational_operator_role_required")

    @property
    def runtime_namespace(self) -> RuntimeGenerationNamespace:
        return self.ingress.matrix_identity.namespace

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity:
        return self.ingress.matrix_identity


@dataclass(frozen=True, slots=True)
class FoundationOperatorRequestContext:
    request_id: str
    created_at: float
    runtime_namespace: RuntimeGenerationNamespace
    matrix_identity: MatrixGenerationIdentity
    client_id: str = "ngui.operator"
    client_role: CoreClientRole = CoreClientRole.OPERATOR
    source: str = "ngui"

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "foundation_request_id_required"))
        object.__setattr__(self, "client_id", _required(self.client_id, "foundation_client_id_required"))
        object.__setattr__(self, "source", _required(self.source, "foundation_source_required"))
        object.__setattr__(self, "created_at", _positive_time(self.created_at, "foundation_positive_created_at_required"))
        if not isinstance(self.runtime_namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_runtime_namespace_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("foundation_matrix_identity_required")
        if self.matrix_identity.namespace != self.runtime_namespace:
            raise ValueError("foundation_matrix_namespace_mismatch")
        if self.client_role is not CoreClientRole.OPERATOR:
            raise PermissionError("foundation_operator_role_required")


@dataclass(frozen=True, slots=True)
class ProductOperatorRequestContext:
    request_id: str
    created_at: float
    runtime_namespace: RuntimeGenerationNamespace
    matrix_identity: MatrixGenerationIdentity
    client_id: str = "ngui.operator"
    client_role: CoreClientRole = CoreClientRole.OPERATOR
    source: str = "ngui"

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "product_request_id_required"))
        object.__setattr__(self, "client_id", _required(self.client_id, "product_client_id_required"))
        object.__setattr__(self, "source", _required(self.source, "product_source_required"))
        object.__setattr__(self, "created_at", _positive_time(self.created_at, "product_positive_created_at_required"))
        if not isinstance(self.runtime_namespace, RuntimeGenerationNamespace):
            raise TypeError("product_runtime_namespace_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("product_matrix_identity_required")
        if self.matrix_identity.namespace != self.runtime_namespace:
            raise ValueError("product_matrix_namespace_mismatch")
        if self.client_role is not CoreClientRole.OPERATOR:
            raise PermissionError("product_operator_role_required")


@dataclass(frozen=True, slots=True)
class OperationalServiceReceipt:
    request_id: str
    kind: OperationalKind
    owner: str
    disposition: OperationalDisposition
    message: str
    occurred_at: float
    runtime_namespace: RuntimeGenerationNamespace
    matrix_identity: MatrixGenerationIdentity
    detail: CoreFrozenPayload
    schema: str = "OperationalServiceReceipt.v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "operational_receipt_request_id_required"))
        object.__setattr__(self, "owner", _required(self.owner, "operational_receipt_owner_required"))
        object.__setattr__(self, "message", _required(self.message, "operational_receipt_message_required"))
        object.__setattr__(self, "occurred_at", _positive_time(self.occurred_at, "operational_receipt_positive_time_required"))
        if not isinstance(self.kind, OperationalKind):
            raise TypeError("operational_receipt_typed_kind_required")
        if not isinstance(self.disposition, OperationalDisposition):
            raise TypeError("operational_receipt_typed_disposition_required")
        if not isinstance(self.runtime_namespace, RuntimeGenerationNamespace):
            raise TypeError("operational_receipt_runtime_namespace_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("operational_receipt_matrix_identity_required")
        if self.matrix_identity.namespace != self.runtime_namespace:
            raise ValueError("operational_receipt_matrix_namespace_mismatch")
        if not isinstance(self.detail, CoreFrozenPayload):
            raise TypeError("operational_receipt_typed_detail_required")
        if self.schema != "OperationalServiceReceipt.v2":
            raise ValueError("operational_receipt_schema_invalid")

    @property
    def ok(self) -> bool:
        return self.disposition in {
            OperationalDisposition.CONFIRMED,
            OperationalDisposition.ACCEPTED,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "kind": self.kind.value,
            "owner": self.owner,
            "disposition": self.disposition.value,
            "message": self.message,
            "occurred_at": self.occurred_at,
            "runtime_namespace": self.runtime_namespace.as_dict(),
            "matrix_identity": self.matrix_identity.as_dict(),
            "ok": self.ok,
            "detail": self.detail.to_payload(),
        }

    @classmethod
    def from_payload(
        cls,
        raw: Mapping[str, Any],
    ) -> "OperationalServiceReceipt":
        if not isinstance(raw, Mapping):
            raise TypeError("operational_receipt_mapping_required")
        row = dict(raw)
        if set(row) != {
            "schema",
            "request_id",
            "kind",
            "owner",
            "disposition",
            "message",
            "occurred_at",
            "runtime_namespace",
            "matrix_identity",
            "ok",
            "detail",
        }:
            raise ValueError("operational_receipt_fields_invalid")
        namespace_raw = row["runtime_namespace"]
        matrix_raw = row["matrix_identity"]
        detail_raw = row["detail"]
        if not isinstance(namespace_raw, Mapping):
            raise TypeError(
                "operational_receipt_runtime_namespace_mapping_required"
            )
        if not isinstance(matrix_raw, Mapping):
            raise TypeError(
                "operational_receipt_matrix_identity_mapping_required"
            )
        if not isinstance(detail_raw, Mapping):
            raise TypeError("operational_receipt_detail_mapping_required")
        if type(row["kind"]) is not str:
            raise TypeError("operational_receipt_kind_string_required")
        if type(row["disposition"]) is not str:
            raise TypeError("operational_receipt_disposition_string_required")
        value = cls(
            request_id=row["request_id"],
            kind=OperationalKind(row["kind"]),
            owner=row["owner"],
            disposition=OperationalDisposition(row["disposition"]),
            message=row["message"],
            occurred_at=row["occurred_at"],
            runtime_namespace=RuntimeGenerationNamespace.from_mapping(
                namespace_raw
            ),
            matrix_identity=MatrixGenerationIdentity.from_mapping(matrix_raw),
            detail=CoreFrozenPayload.from_mapping(detail_raw),
            schema=row["schema"],
        )
        if type(row["ok"]) is not bool or row["ok"] is not value.ok:
            raise ValueError("operational_receipt_ok_mismatch")
        return value


@dataclass(frozen=True, slots=True)
class RouterReadDiagnosticRequest:
    context: CoreOperatorRequestContext
    action: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, CoreOperatorRequestContext):
            raise TypeError("read_diagnostic_typed_context_required")
        if self.action not in {"start", "cancel"}:
            raise ValueError("read_diagnostic_action_invalid")


@dataclass(frozen=True, slots=True)
class NetworkSettingsInspectionRequest:
    context: CoreOperatorRequestContext

    def __post_init__(self) -> None:
        if not isinstance(self.context, CoreOperatorRequestContext):
            raise TypeError("network_inspection_typed_context_required")


@dataclass(frozen=True, slots=True)
class WifiRadioControlRequest:
    context: CoreOperatorRequestContext
    radio: str
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, CoreOperatorRequestContext):
            raise TypeError("wifi_radio_typed_context_required")
        radio = _required(self.radio, "wifi_radio_required")
        if radio not in {"2.4", "5", "6"}:
            raise ValueError("wifi_radio_must_be_2.4_5_or_6")
        if type(self.enabled) is not bool:
            raise TypeError("wifi_radio_enabled_must_be_bool")
        object.__setattr__(self, "radio", radio)


@dataclass(frozen=True, slots=True)
class DownstreamHealthConfigRequest:
    context: CoreOperatorRequestContext
    enabled: bool
    target: str = ""
    label: str = ""
    ping_count: int = 3
    interval_s: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.context, CoreOperatorRequestContext):
            raise TypeError("downstream_health_typed_context_required")
        if type(self.enabled) is not bool:
            raise TypeError("downstream_health_enabled_must_be_bool")
        if not isinstance(self.target, str) or not isinstance(self.label, str):
            raise TypeError("downstream_health_text_required")
        object.__setattr__(self, "target", self.target.strip())
        object.__setattr__(self, "label", self.label.strip())
        if type(self.ping_count) is not int:
            raise TypeError("downstream_health_ping_count_integer_required")
        count = self.ping_count
        interval = _positive_time(
            self.interval_s,
            "downstream_health_interval_positive_finite_required",
        )
        if count < 1 or count > 10:
            raise ValueError("downstream_health_ping_count_out_of_range")
        if not (
            DOWNSTREAM_HEALTH_INTERVAL_MIN_S
            <= interval
            <= DOWNSTREAM_HEALTH_INTERVAL_MAX_S
        ):
            raise ValueError("downstream_health_interval_out_of_range")
        object.__setattr__(self, "ping_count", count)
        object.__setattr__(self, "interval_s", interval)


@dataclass(frozen=True, slots=True)
class SmsCommandControlConfigRequest:
    context: FoundationOperatorRequestContext
    enabled: bool
    code_word: str = "reboot"
    approved_senders: tuple[str, ...] = ()
    poll_interval_s: float = 121.0
    delete_processed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("sms_command_control_typed_context_required")
        if type(self.enabled) is not bool or type(self.delete_processed) is not bool:
            raise TypeError("sms_command_control_boolean_required")
        code_word = _required(
            self.code_word,
            "sms_command_control_code_word_required",
        ).lower()
        if code_word != "reboot":
            raise ValueError("sms_command_control_only_reboot_supported")
        if isinstance(self.approved_senders, (str, bytes)):
            raise TypeError("sms_command_control_senders_iterable_required")
        if any(not isinstance(value, str) for value in self.approved_senders):
            raise TypeError("sms_command_control_sender_string_required")
        senders = tuple(
            value.strip() for value in self.approved_senders if value.strip()
        )
        object.__setattr__(self, "code_word", code_word)
        object.__setattr__(self, "approved_senders", senders)
        poll_interval = _positive_time(
            self.poll_interval_s,
            "sms_command_control_poll_interval_positive_finite_required",
        )
        if poll_interval < 121.0:
            raise ValueError("sms_command_control_poll_interval_too_short")
        object.__setattr__(self, "poll_interval_s", poll_interval)


@dataclass(frozen=True, slots=True)
class AntennaGuideControlRequest:
    context: FoundationOperatorRequestContext
    action: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("antenna_guide_typed_context_required")
        action = _required(self.action, "antenna_guide_action_required").lower()
        if action not in {"start", "stop"}:
            raise ValueError("antenna_guide_action_invalid")
        object.__setattr__(self, "action", action)


@dataclass(frozen=True, slots=True)
class BandSchedulePlanRequest:
    context: FoundationOperatorRequestContext
    bands: tuple[int, ...] = ()
    operation: str = ""
    activate_at: str = ""
    interval_s: int = 300
    requires_user_approval: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("band_schedule_typed_context_required")
        object.__setattr__(
            self,
            "bands",
            _bands(self.bands, field_name="band_schedule_bands"),
        )
        operation = _required(
            self.operation,
            "band_schedule_operation_required",
        ).lower()
        if operation not in {"add", "remove"}:
            raise ValueError("band_schedule_operation_invalid")
        object.__setattr__(self, "operation", operation)
        if not isinstance(self.activate_at, str):
            raise TypeError("band_schedule_activate_at_string_required")
        object.__setattr__(self, "activate_at", self.activate_at.strip())
        if type(self.interval_s) is not int:
            raise TypeError("band_schedule_interval_integer_required")
        if self.interval_s <= 0:
            raise ValueError("band_schedule_interval_positive_required")
        if type(self.requires_user_approval) is not bool:
            raise TypeError("band_schedule_approval_must_be_bool")


@dataclass(frozen=True, slots=True)
class BandSequencePlanRequest:
    context: FoundationOperatorRequestContext
    bands: tuple[int, ...] = ()
    ca_bands: tuple[int, ...] = ()
    sequence_mode: str = ""
    interval_s: int = MIN_BAND_SEQUENCE_INTERVAL_S
    requires_user_approval: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("band_sequence_typed_context_required")
        object.__setattr__(self, "bands", _bands(self.bands, field_name="band_sequence_bands"))
        object.__setattr__(self, "ca_bands", _bands(self.ca_bands, field_name="band_sequence_ca_bands"))
        if not isinstance(self.sequence_mode, str):
            raise TypeError("band_sequence_mode_string_required")
        mode = self.sequence_mode.strip().lower()
        if mode not in {"", "ca_pool", "pband_sequence", "pband", "anchor_sequence"}:
            raise ValueError("band_sequence_mode_invalid")
        object.__setattr__(self, "sequence_mode", mode)
        if type(self.interval_s) is not int:
            raise TypeError("band_sequence_interval_integer_required")
        if self.interval_s <= 0:
            raise ValueError("band_sequence_interval_positive_required")
        if type(self.requires_user_approval) is not bool:
            raise TypeError("band_sequence_approval_must_be_bool")


@dataclass(frozen=True, slots=True)
class BandScheduleCancelRequest:
    context: FoundationOperatorRequestContext
    reason: str = "operator requested band schedule cancel"

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("band_schedule_cancel_typed_context_required")
        object.__setattr__(self, "reason", _required(self.reason, "band_schedule_cancel_reason_required"))


@dataclass(frozen=True, slots=True)
class BandSequenceCancelRequest:
    context: FoundationOperatorRequestContext
    reason: str = "operator requested band sequence cancel"

    def __post_init__(self) -> None:
        if not isinstance(self.context, FoundationOperatorRequestContext):
            raise TypeError("band_sequence_cancel_typed_context_required")
        object.__setattr__(self, "reason", _required(self.reason, "band_sequence_cancel_reason_required"))


@dataclass(frozen=True, slots=True)
class IntelligenceAutopilotControlRequest:
    context: ProductOperatorRequestContext
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProductOperatorRequestContext):
            raise TypeError("intelligence_autopilot_product_context_required")
        if type(self.enabled) is not bool:
            raise TypeError("intelligence_autopilot_enabled_bool_required")


@dataclass(frozen=True, slots=True)
class IntelligenceExploreControlRequest:
    context: ProductOperatorRequestContext
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProductOperatorRequestContext):
            raise TypeError("intelligence_explore_product_context_required")
        if type(self.enabled) is not bool:
            raise TypeError("intelligence_explore_enabled_bool_required")


TYPED_OPERATION_REQUESTS = (
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
    WifiRadioControlRequest,
    DownstreamHealthConfigRequest,
    SmsCommandControlConfigRequest,
    AntennaGuideControlRequest,
    BandSchedulePlanRequest,
    BandSequencePlanRequest,
    BandScheduleCancelRequest,
    BandSequenceCancelRequest,
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
)

_OPERATIONAL_REQUEST_KIND_BY_TYPE = {
    RouterReadDiagnosticRequest: OperationalKind.READ_DIAGNOSTICS,
    NetworkSettingsInspectionRequest: OperationalKind.INSPECT_NETWORK_SETTINGS,
    WifiRadioControlRequest: OperationalKind.WIFI_SET_RADIO,
    DownstreamHealthConfigRequest: OperationalKind.SET_DOWNSTREAM_HEALTH,
    SmsCommandControlConfigRequest: (
        OperationalKind.CONFIGURE_SMS_COMMAND_CONTROL
    ),
    AntennaGuideControlRequest: OperationalKind.ANTENNA_GUIDE,
    BandSchedulePlanRequest: OperationalKind.PLAN_BAND_SCHEDULE,
    BandSequencePlanRequest: OperationalKind.PLAN_BAND_SEQUENCE,
    BandScheduleCancelRequest: OperationalKind.CANCEL_BAND_SCHEDULE,
    BandSequenceCancelRequest: OperationalKind.CANCEL_BAND_SEQUENCE,
    IntelligenceAutopilotControlRequest: (
        OperationalKind.SET_INTELLIGENCE_AUTOPILOT
    ),
    IntelligenceExploreControlRequest: (
        OperationalKind.SET_INTELLIGENCE_EXPLORE
    ),
}
_OPERATIONAL_REQUEST_TYPE_BY_KIND = {
    kind: request_type
    for request_type, kind in _OPERATIONAL_REQUEST_KIND_BY_TYPE.items()
}


def operational_request_kind(request: object) -> OperationalKind:
    if not isinstance(request, TYPED_OPERATION_REQUESTS):
        raise TypeError("operational_typed_request_required")
    return _OPERATIONAL_REQUEST_KIND_BY_TYPE[type(request)]


def _operator_context_payload(
    context: (
        CoreOperatorRequestContext
        | FoundationOperatorRequestContext
        | ProductOperatorRequestContext
    ),
) -> dict[str, Any]:
    if isinstance(context, CoreOperatorRequestContext):
        return {
            "schema": "CoreOperatorRequestContext.v1",
            "request_id": context.request_id,
            "created_at": context.created_at,
            "expires_at": context.expires_at,
            "ingress": context.ingress.to_payload(),
            "freshness": context.freshness.value,
            "client_id": context.client_id,
            "client_role": context.client_role.value,
            "source": context.source,
        }
    if isinstance(context, FoundationOperatorRequestContext):
        return {
            "schema": "FoundationOperatorRequestContext.v1",
            "request_id": context.request_id,
            "created_at": context.created_at,
            "runtime_namespace": context.runtime_namespace.as_dict(),
            "matrix_identity": context.matrix_identity.as_dict(),
            "client_id": context.client_id,
            "client_role": context.client_role.value,
            "source": context.source,
        }
    if isinstance(context, ProductOperatorRequestContext):
        return {
            "schema": "ProductOperatorRequestContext.v1",
            "request_id": context.request_id,
            "created_at": context.created_at,
            "runtime_namespace": context.runtime_namespace.as_dict(),
            "matrix_identity": context.matrix_identity.as_dict(),
            "client_id": context.client_id,
            "client_role": context.client_role.value,
            "source": context.source,
        }
    raise TypeError("operational_operator_context_required")


def _operator_context_from_payload(
    raw: Mapping[str, Any],
) -> (
    CoreOperatorRequestContext
    | FoundationOperatorRequestContext
    | ProductOperatorRequestContext
):
    if not isinstance(raw, Mapping):
        raise TypeError("operational_operator_context_mapping_required")
    row = dict(raw)
    schema = row.get("schema")
    if schema == "CoreOperatorRequestContext.v1":
        if set(row) != {
            "schema",
            "request_id",
            "created_at",
            "expires_at",
            "ingress",
            "freshness",
            "client_id",
            "client_role",
            "source",
        }:
            raise ValueError("core_operator_context_fields_invalid")
        return CoreOperatorRequestContext(
            request_id=row["request_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            ingress=CoreCommandIngressContext.from_payload(row["ingress"]),
            freshness=CoreOperationalFreshness(row["freshness"]),
            client_id=row["client_id"],
            client_role=CoreClientRole(row["client_role"]),
            source=row["source"],
        )
    if schema == "FoundationOperatorRequestContext.v1":
        if set(row) != {
            "schema",
            "request_id",
            "created_at",
            "runtime_namespace",
            "matrix_identity",
            "client_id",
            "client_role",
            "source",
        }:
            raise ValueError("foundation_operator_context_fields_invalid")
        return FoundationOperatorRequestContext(
            request_id=row["request_id"],
            created_at=row["created_at"],
            runtime_namespace=RuntimeGenerationNamespace.from_mapping(
                row["runtime_namespace"]
            ),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            client_id=row["client_id"],
            client_role=CoreClientRole(row["client_role"]),
            source=row["source"],
        )
    if schema == "ProductOperatorRequestContext.v1":
        if set(row) != {
            "schema",
            "request_id",
            "created_at",
            "runtime_namespace",
            "matrix_identity",
            "client_id",
            "client_role",
            "source",
        }:
            raise ValueError("product_operator_context_fields_invalid")
        return ProductOperatorRequestContext(
            request_id=row["request_id"],
            created_at=row["created_at"],
            runtime_namespace=RuntimeGenerationNamespace.from_mapping(
                row["runtime_namespace"]
            ),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            client_id=row["client_id"],
            client_role=CoreClientRole(row["client_role"]),
            source=row["source"],
        )
    raise ValueError("operational_operator_context_schema_invalid")


def operational_request_to_payload(request: object) -> dict[str, Any]:
    kind = operational_request_kind(request)
    parameters: dict[str, Any]
    if isinstance(request, RouterReadDiagnosticRequest):
        parameters = {"action": request.action}
    elif isinstance(request, NetworkSettingsInspectionRequest):
        parameters = {}
    elif isinstance(request, WifiRadioControlRequest):
        parameters = {"radio": request.radio, "enabled": request.enabled}
    elif isinstance(request, DownstreamHealthConfigRequest):
        parameters = {
            "enabled": request.enabled,
            "target": request.target,
            "label": request.label,
            "ping_count": request.ping_count,
            "interval_s": request.interval_s,
        }
    elif isinstance(request, SmsCommandControlConfigRequest):
        parameters = {
            "enabled": request.enabled,
            "code_word": request.code_word,
            "approved_senders": list(request.approved_senders),
            "poll_interval_s": request.poll_interval_s,
            "delete_processed": request.delete_processed,
        }
    elif isinstance(request, AntennaGuideControlRequest):
        parameters = {"action": request.action}
    elif isinstance(request, BandSchedulePlanRequest):
        parameters = {
            "bands": list(request.bands),
            "operation": request.operation,
            "activate_at": request.activate_at,
            "interval_s": request.interval_s,
            "requires_user_approval": request.requires_user_approval,
        }
    elif isinstance(request, BandSequencePlanRequest):
        parameters = {
            "bands": list(request.bands),
            "ca_bands": list(request.ca_bands),
            "sequence_mode": request.sequence_mode,
            "interval_s": request.interval_s,
            "requires_user_approval": request.requires_user_approval,
        }
    elif isinstance(request, BandScheduleCancelRequest):
        parameters = {"reason": request.reason}
    elif isinstance(request, BandSequenceCancelRequest):
        parameters = {"reason": request.reason}
    elif isinstance(request, IntelligenceAutopilotControlRequest):
        parameters = {"enabled": request.enabled}
    elif isinstance(request, IntelligenceExploreControlRequest):
        parameters = {"enabled": request.enabled}
    else:
        raise TypeError("operational_typed_request_required")
    return {
        "schema": "OperationalRequest.v1",
        "kind": kind.value,
        "context": _operator_context_payload(request.context),
        "parameters": parameters,
    }


def operational_request_from_payload(
    raw: Mapping[str, Any],
) -> object:
    if not isinstance(raw, Mapping):
        raise TypeError("operational_request_mapping_required")
    row = dict(raw)
    if set(row) != {"schema", "kind", "context", "parameters"}:
        raise ValueError("operational_request_fields_invalid")
    if row["schema"] != "OperationalRequest.v1":
        raise ValueError("operational_request_schema_invalid")
    if not isinstance(row["parameters"], Mapping):
        raise TypeError("operational_request_parameters_mapping_required")
    kind = OperationalKind(row["kind"])
    request_type = _OPERATIONAL_REQUEST_TYPE_BY_KIND[kind]
    context = _operator_context_from_payload(row["context"])
    parameters = dict(row["parameters"])
    if request_type in {
        RouterReadDiagnosticRequest,
        NetworkSettingsInspectionRequest,
        WifiRadioControlRequest,
        DownstreamHealthConfigRequest,
    } and not isinstance(context, CoreOperatorRequestContext):
        raise TypeError("operational_core_context_required")
    if request_type in {
        IntelligenceAutopilotControlRequest,
        IntelligenceExploreControlRequest,
    }:
        if not isinstance(context, ProductOperatorRequestContext):
            raise TypeError("operational_product_context_required")
    elif request_type not in {
        RouterReadDiagnosticRequest,
        NetworkSettingsInspectionRequest,
        WifiRadioControlRequest,
        DownstreamHealthConfigRequest,
    } and not isinstance(context, FoundationOperatorRequestContext):
        raise TypeError("operational_foundation_context_required")
    if request_type is SmsCommandControlConfigRequest:
        senders = parameters.get("approved_senders")
        if not isinstance(senders, list):
            raise TypeError("operational_sms_senders_list_required")
        parameters["approved_senders"] = tuple(senders)
    for field_name in ("bands", "ca_bands"):
        if field_name in parameters:
            values = parameters[field_name]
            if not isinstance(values, list):
                raise TypeError(
                    f"operational_{field_name}_list_required"
                )
            parameters[field_name] = tuple(values)
    return request_type(context=context, **parameters)


def operational_request_matrix_identity(
    request: object,
) -> MatrixGenerationIdentity:
    """Return the one typed Matrix identity carried by an operation request."""

    if not isinstance(request, TYPED_OPERATION_REQUESTS):
        raise TypeError("operational_typed_request_required")
    context = request.context
    identity = context.matrix_identity
    if not isinstance(identity, MatrixGenerationIdentity):
        raise TypeError("operational_request_matrix_identity_required")
    if context.runtime_namespace != identity.namespace:
        raise ValueError("operational_request_runtime_namespace_mismatch")
    return identity


def operational_request_fingerprint(request: object) -> str:
    return hashlib.sha256(
        canonical_json_bytes(operational_request_to_payload(request))
    ).hexdigest()


def require_product_operational_effect_permit(
    permit: ProductOperationalEffectPermit,
    request: object,
    *,
    expected_kind: OperationalKind,
) -> None:
    if not isinstance(permit, ProductOperationalEffectPermit):
        raise PermissionError(
            "product_operational_effect_permit_required"
        )
    if operational_request_kind(request) is not expected_kind:
        raise ValueError("operational_effect_expected_kind_mismatch")
    if (
        permit.request_id != request.context.request_id
        or permit.kind is not expected_kind
        or permit.request_fingerprint
        != operational_request_fingerprint(request)
        or permit.matrix_identity
        != operational_request_matrix_identity(request)
    ):
        raise PermissionError(
            "product_operational_effect_permit_identity_mismatch"
        )


__all__ = tuple(cls.__name__ for cls in TYPED_OPERATION_REQUESTS) + (
    "CoreOperationalFreshness",
    "CoreOperatorRequestContext",
    "FoundationOperatorRequestContext",
    "ProductOperatorRequestContext",
    "OperationalDisposition",
    "OperationalKind",
    "OperationalServiceReceipt",
    "ProductOperationalEffectPermit",
    "TYPED_OPERATION_REQUESTS",
    "operational_request_from_payload",
    "operational_request_fingerprint",
    "operational_request_kind",
    "operational_request_matrix_identity",
    "operational_request_to_payload",
    "require_product_operational_effect_permit",
    "DOWNSTREAM_HEALTH_INTERVAL_MIN_S",
    "DOWNSTREAM_HEALTH_INTERVAL_MAX_S",
)
