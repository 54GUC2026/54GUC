"""Immutable contracts for Core-owned inactive customer attachment hooks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from arch4.core_contracts.clients import CoreClientRole


CORE_EXTENSION_HOOK_SCHEMA = "CoreExtensionHook.v1"
CORE_EXTENSION_HOOK_OWNER = "54guc.core.extension_hook_registry"


class CoreHookDirection(str, Enum):
    CORE_TO_CUSTOMER = "core_to_customer"
    CUSTOMER_TO_CORE = "customer_to_core"
    REQUEST_RESPONSE = "request_response"


class CoreHookAttachmentMode(str, Enum):
    PROVIDER = "provider"
    SUBSCRIBER = "subscriber"


class CoreHookPayloadKind(str, Enum):
    OBSERVATION = "observation"
    CLOCK_WAKE = "clock_wake"
    COMMAND_PROPOSAL = "command_proposal"
    POLICY_EVALUATION = "policy_evaluation"
    COMMAND_OUTCOME = "command_outcome"
    CONFIG_PROPOSAL = "config_proposal"
    PRESENTATION_EXTENSION = "presentation_extension"


class CoreHookState(str, Enum):
    UNATTACHED = "unattached"
    ATTACHED = "attached"
    FAULTED = "faulted"
    DETACHED = "detached"


def _required_text(value: Any, *, reason: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(reason)
    return text


def _unique_roles(values: Any) -> tuple[CoreClientRole, ...]:
    try:
        candidates = tuple(values or ())
    except TypeError as exc:
        raise TypeError("core_hook_allowed_roles_must_be_iterable") from exc
    roles: list[CoreClientRole] = []
    for value in candidates:
        if not isinstance(value, CoreClientRole):
            raise TypeError("core_hook_typed_allowed_role_required")
        if value in roles:
            raise ValueError(f"core_hook_allowed_role_duplicate:{value.value}")
        roles.append(value)
    if not roles:
        raise ValueError("core_hook_allowed_roles_required")
    return tuple(roles)


def _unique_text(values: Any, *, reason: str) -> tuple[str, ...]:
    try:
        candidates = tuple(values or ())
    except TypeError as exc:
        raise TypeError(reason) from exc
    normalized: list[str] = []
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            raise ValueError(reason)
        if text in normalized:
            raise ValueError(f"{reason}:{text}")
        normalized.append(text)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class CoreExtensionHookSpec:
    hook_id: str
    direction: CoreHookDirection
    payload_kind: CoreHookPayloadKind
    payload_schema: str
    allowed_roles: tuple[CoreClientRole, ...]
    max_providers: int
    max_subscribers: int
    clock_lane: str = ""
    forbidden_core_truth: tuple[str, ...] = ()
    schema: str = CORE_EXTENSION_HOOK_SCHEMA
    owner: str = CORE_EXTENSION_HOOK_OWNER

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hook_id",
            _required_text(self.hook_id, reason="core_hook_id_required"),
        )
        if not isinstance(self.direction, CoreHookDirection):
            raise TypeError("core_hook_typed_direction_required")
        if not isinstance(self.payload_kind, CoreHookPayloadKind):
            raise TypeError("core_hook_typed_payload_kind_required")
        object.__setattr__(
            self,
            "payload_schema",
            _required_text(
                self.payload_schema,
                reason="core_hook_payload_schema_required",
            ),
        )
        object.__setattr__(self, "allowed_roles", _unique_roles(self.allowed_roles))
        providers = int(self.max_providers)
        subscribers = int(self.max_subscribers)
        if providers < 0 or subscribers < 0:
            raise ValueError("core_hook_cardinality_must_be_nonnegative")
        if self.direction is CoreHookDirection.CORE_TO_CUSTOMER:
            if providers != 0 or subscribers <= 0:
                raise ValueError("core_hook_core_to_customer_cardinality_invalid")
        elif self.direction is CoreHookDirection.CUSTOMER_TO_CORE:
            if providers <= 0 or subscribers != 0:
                raise ValueError("core_hook_customer_to_core_cardinality_invalid")
        elif providers <= 0 or subscribers != 0:
            raise ValueError("core_hook_request_response_cardinality_invalid")
        object.__setattr__(self, "max_providers", providers)
        object.__setattr__(self, "max_subscribers", subscribers)
        object.__setattr__(self, "clock_lane", str(self.clock_lane or "").strip())
        object.__setattr__(
            self,
            "forbidden_core_truth",
            _unique_text(
                self.forbidden_core_truth,
                reason="core_hook_forbidden_truth_invalid",
            ),
        )
        if self.schema != CORE_EXTENSION_HOOK_SCHEMA:
            raise ValueError("core_hook_schema_invalid")
        if self.owner != CORE_EXTENSION_HOOK_OWNER:
            raise ValueError("core_hook_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "hook_id": self.hook_id,
            "direction": self.direction.value,
            "payload_kind": self.payload_kind.value,
            "payload_schema": self.payload_schema,
            "allowed_roles": [role.value for role in self.allowed_roles],
            "max_providers": self.max_providers,
            "max_subscribers": self.max_subscribers,
            "clock_lane": self.clock_lane,
            "forbidden_core_truth": list(self.forbidden_core_truth),
        }


@dataclass(frozen=True, slots=True)
class CoreHookAttachmentReceipt:
    sequence: int
    hook_id: str
    client_id: str
    mode: CoreHookAttachmentMode
    state: CoreHookState
    accepted: bool
    reason: str
    occurred_at: float
    schema: str = "CoreHookAttachmentReceipt.v1"
    owner: str = CORE_EXTENSION_HOOK_OWNER

    def __post_init__(self) -> None:
        if int(self.sequence) <= 0:
            raise ValueError("core_hook_receipt_positive_sequence_required")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(
            self,
            "hook_id",
            _required_text(self.hook_id, reason="core_hook_receipt_hook_id_required"),
        )
        object.__setattr__(
            self,
            "client_id",
            _required_text(
                self.client_id,
                reason="core_hook_receipt_client_id_required",
            ),
        )
        if not isinstance(self.mode, CoreHookAttachmentMode):
            raise TypeError("core_hook_receipt_typed_mode_required")
        if not isinstance(self.state, CoreHookState):
            raise TypeError("core_hook_receipt_typed_state_required")
        if type(self.accepted) is not bool:
            raise TypeError("core_hook_receipt_accepted_must_be_bool")
        object.__setattr__(
            self,
            "reason",
            _required_text(self.reason, reason="core_hook_receipt_reason_required"),
        )
        object.__setattr__(self, "occurred_at", float(self.occurred_at))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": self.sequence,
            "hook_id": self.hook_id,
            "client_id": self.client_id,
            "mode": self.mode.value,
            "state": self.state.value,
            "accepted": self.accepted,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True, slots=True)
class CoreExtensionHookStatus:
    spec: CoreExtensionHookSpec
    state: CoreHookState
    providers: tuple[str, ...]
    subscribers: tuple[str, ...]
    sequence: int
    updated_at: float
    last_fault: str = ""
    schema: str = "CoreExtensionHookStatus.v1"
    owner: str = CORE_EXTENSION_HOOK_OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.spec, CoreExtensionHookSpec):
            raise TypeError("core_hook_status_spec_required")
        if not isinstance(self.state, CoreHookState):
            raise TypeError("core_hook_status_typed_state_required")
        object.__setattr__(
            self,
            "providers",
            _unique_text(self.providers, reason="core_hook_status_provider_invalid"),
        )
        object.__setattr__(
            self,
            "subscribers",
            _unique_text(
                self.subscribers,
                reason="core_hook_status_subscriber_invalid",
            ),
        )
        if int(self.sequence) < 0:
            raise ValueError("core_hook_status_sequence_invalid")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "updated_at", float(self.updated_at))
        object.__setattr__(self, "last_fault", str(self.last_fault or ""))
        if self.state is CoreHookState.UNATTACHED and (
            self.providers or self.subscribers or self.last_fault
        ):
            raise ValueError("core_hook_unattached_status_contains_customer_truth")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "hook": self.spec.to_payload(),
            "state": self.state.value,
            "providers": list(self.providers),
            "subscribers": list(self.subscribers),
            "sequence": self.sequence,
            "updated_at": self.updated_at,
            "last_fault": self.last_fault,
        }


__all__ = (
    "CORE_EXTENSION_HOOK_OWNER",
    "CORE_EXTENSION_HOOK_SCHEMA",
    "CoreExtensionHookSpec",
    "CoreExtensionHookStatus",
    "CoreHookAttachmentMode",
    "CoreHookAttachmentReceipt",
    "CoreHookDirection",
    "CoreHookPayloadKind",
    "CoreHookState",
)
