"""The only mutable owner of Core customer-hook attachment lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import time
from typing import Any, Iterable

from arch4.core_contracts.clients import CoreClientManifest, CoreClientRole
from arch4.command_transactions import COMMAND_TERMINAL_RECEIPT_SCHEMA
from arch4.core_contracts.hooks import (
    CoreExtensionHookSpec,
    CoreExtensionHookStatus,
    CoreHookAttachmentMode,
    CoreHookAttachmentReceipt,
    CoreHookDirection,
    CoreHookPayloadKind,
    CoreHookState,
)
from arch4.core_contracts.accepted_generation_root import (
    CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
)
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class _Attachment:
    client_id: str
    role: CoreClientRole
    mode: CoreHookAttachmentMode
    payload_schema: str


def default_core_extension_hook_specs() -> tuple[CoreExtensionHookSpec, ...]:
    observation_roles = (
        CoreClientRole.OBSERVER,
        CoreClientRole.FOUNDATION_SERVICE,
        CoreClientRole.DIAGNOSTIC,
    )
    command_outcome_roles = (
        CoreClientRole.OPERATOR,
        CoreClientRole.OPERATOR_AUTOMATION,
        *observation_roles,
        CoreClientRole.PRODUCT_BOUNDARY,
    )
    return (
        CoreExtensionHookSpec(
            hook_id="accepted_observation",
            direction=CoreHookDirection.CORE_TO_CUSTOMER,
            payload_kind=CoreHookPayloadKind.OBSERVATION,
            payload_schema=CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
            allowed_roles=observation_roles,
            max_providers=0,
            max_subscribers=64,
            forbidden_core_truth=("modem_io", "accepted_generation", "derived_facts"),
        ),
        CoreExtensionHookSpec(
            hook_id=CORE_RUNTIME_SNAPSHOT_HOOK_ID,
            direction=CoreHookDirection.CORE_TO_CUSTOMER,
            payload_kind=CoreHookPayloadKind.OBSERVATION,
            payload_schema=CORE_RUNTIME_SNAPSHOT_SCHEMA,
            allowed_roles=(
                CoreClientRole.OBSERVER,
                CoreClientRole.PRODUCT_BOUNDARY,
                CoreClientRole.DIAGNOSTIC,
            ),
            max_providers=0,
            max_subscribers=8,
            forbidden_core_truth=("mutable_register_bank", "modem_io"),
        ),
        CoreExtensionHookSpec(
            hook_id="clock_wake_50ms",
            direction=CoreHookDirection.CORE_TO_CUSTOMER,
            payload_kind=CoreHookPayloadKind.CLOCK_WAKE,
            payload_schema="CoreClockWake.v1",
            allowed_roles=(
                CoreClientRole.DIAGNOSTIC,
            ),
            max_providers=0,
            max_subscribers=8,
            clock_lane="50ms",
            forbidden_core_truth=("runtime_clock",),
        ),
        CoreExtensionHookSpec(
            hook_id="clock_wake_100ms",
            direction=CoreHookDirection.CORE_TO_CUSTOMER,
            payload_kind=CoreHookPayloadKind.CLOCK_WAKE,
            payload_schema="CoreClockWake.v1",
            allowed_roles=(
                CoreClientRole.FOUNDATION_SERVICE,
                CoreClientRole.DIAGNOSTIC,
            ),
            max_providers=0,
            max_subscribers=32,
            clock_lane="100ms",
            forbidden_core_truth=("runtime_clock",),
        ),
        CoreExtensionHookSpec(
            hook_id="command_outcome",
            direction=CoreHookDirection.CORE_TO_CUSTOMER,
            payload_kind=CoreHookPayloadKind.COMMAND_OUTCOME,
            payload_schema=COMMAND_TERMINAL_RECEIPT_SCHEMA,
            allowed_roles=command_outcome_roles,
            max_providers=0,
            max_subscribers=64,
            forbidden_core_truth=("command_lifecycle",),
        ),
        CoreExtensionHookSpec(
            hook_id="foundation_ngui_extension",
            direction=CoreHookDirection.CUSTOMER_TO_CORE,
            payload_kind=CoreHookPayloadKind.PRESENTATION_EXTENSION,
            payload_schema="FoundationNGUIExtension.v1",
            allowed_roles=(CoreClientRole.FOUNDATION_SERVICE,),
            max_providers=1,
            max_subscribers=0,
            forbidden_core_truth=("ngui_core_frame", "core_registers"),
        ),
    )


class CoreExtensionHookRegistry:
    """Own declarations and attachment state; never customer execution."""

    def __init__(self, specs: Iterable[CoreExtensionHookSpec]) -> None:
        declared = tuple(specs)
        if not declared:
            raise ValueError("core_hook_specs_required")
        for spec in declared:
            if not isinstance(spec, CoreExtensionHookSpec):
                raise TypeError("core_hook_typed_spec_required")
        by_id = {spec.hook_id: spec for spec in declared}
        if len(by_id) != len(declared):
            raise ValueError("core_hook_id_duplicate")
        self._lock = RLock()
        self._specs = by_id
        self._attachments: dict[str, dict[str, _Attachment]] = {
            hook_id: {} for hook_id in by_id
        }
        self._states: dict[str, CoreHookState] = {
            hook_id: CoreHookState.UNATTACHED for hook_id in by_id
        }
        self._updated_at: dict[str, float] = {hook_id: 0.0 for hook_id in by_id}
        self._last_fault: dict[str, str] = {hook_id: "" for hook_id in by_id}
        self._faulted_clients: dict[str, set[str]] = {
            hook_id: set() for hook_id in by_id
        }
        self._sequence = 0
        self._hook_sequences: dict[str, int] = {hook_id: 0 for hook_id in by_id}

    @classmethod
    def with_default_catalog(cls) -> "CoreExtensionHookRegistry":
        return cls(default_core_extension_hook_specs())

    def _spec(self, hook_id: str) -> CoreExtensionHookSpec:
        identity = str(hook_id or "").strip()
        spec = self._specs.get(identity)
        if spec is None:
            raise KeyError(f"core_hook_unknown:{identity or 'missing'}")
        return spec

    def _next_sequence(self, hook_id: str) -> int:
        self._sequence += 1
        self._hook_sequences[hook_id] = self._sequence
        return self._sequence

    @staticmethod
    def _validate_mode(spec: CoreExtensionHookSpec, mode: CoreHookAttachmentMode) -> None:
        if not isinstance(mode, CoreHookAttachmentMode):
            raise TypeError("core_hook_typed_attachment_mode_required")
        if spec.direction is CoreHookDirection.CORE_TO_CUSTOMER:
            if mode is not CoreHookAttachmentMode.SUBSCRIBER:
                raise ValueError("core_hook_requires_subscriber")
            return
        if mode is not CoreHookAttachmentMode.PROVIDER:
            raise ValueError("core_hook_requires_provider")

    def attach(
        self,
        manifest: CoreClientManifest,
        *,
        hook_id: str,
        mode: CoreHookAttachmentMode,
        now: float | None = None,
    ) -> CoreHookAttachmentReceipt:
        if not isinstance(manifest, CoreClientManifest):
            raise TypeError("core_hook_client_manifest_required")
        observed_at = float(time() if now is None else now)
        with self._lock:
            spec = self._spec(hook_id)
            self._validate_mode(spec, mode)
            if manifest.role not in spec.allowed_roles:
                raise PermissionError(
                    f"core_hook_role_forbidden:{spec.hook_id}:{manifest.role.value}"
                )
            if not manifest.supports(spec.payload_schema):
                raise ValueError(
                    f"core_hook_payload_schema_unsupported:{spec.hook_id}:"
                    f"{spec.payload_schema}"
                )
            if self._states[spec.hook_id] is CoreHookState.FAULTED:
                raise RuntimeError(f"core_hook_fault_must_be_cleared:{spec.hook_id}")
            current = self._attachments[spec.hook_id]
            if manifest.client_id in current:
                raise RuntimeError(
                    f"core_hook_client_already_attached:{spec.hook_id}:"
                    f"{manifest.client_id}"
                )
            providers = sum(
                item.mode is CoreHookAttachmentMode.PROVIDER
                for item in current.values()
            )
            subscribers = sum(
                item.mode is CoreHookAttachmentMode.SUBSCRIBER
                for item in current.values()
            )
            if mode is CoreHookAttachmentMode.PROVIDER and providers >= spec.max_providers:
                raise RuntimeError(f"core_hook_provider_capacity:{spec.hook_id}")
            if (
                mode is CoreHookAttachmentMode.SUBSCRIBER
                and subscribers >= spec.max_subscribers
            ):
                raise RuntimeError(f"core_hook_subscriber_capacity:{spec.hook_id}")
            current[manifest.client_id] = _Attachment(
                client_id=manifest.client_id,
                role=manifest.role,
                mode=mode,
                payload_schema=spec.payload_schema,
            )
            sequence = self._next_sequence(spec.hook_id)
            self._states[spec.hook_id] = CoreHookState.ATTACHED
            self._updated_at[spec.hook_id] = observed_at
            self._last_fault[spec.hook_id] = ""
            return CoreHookAttachmentReceipt(
                sequence=sequence,
                hook_id=spec.hook_id,
                client_id=manifest.client_id,
                mode=mode,
                state=CoreHookState.ATTACHED,
                accepted=True,
                reason="attached",
                occurred_at=observed_at,
            )

    def detach(
        self,
        client_id: str,
        *,
        hook_id: str,
        now: float | None = None,
    ) -> CoreHookAttachmentReceipt:
        identity = str(client_id or "").strip()
        if not identity:
            raise ValueError("core_hook_detach_client_id_required")
        observed_at = float(time() if now is None else now)
        with self._lock:
            spec = self._spec(hook_id)
            current = self._attachments[spec.hook_id]
            attachment = current.pop(identity, None)
            if attachment is None:
                raise KeyError(
                    f"core_hook_client_not_attached:{spec.hook_id}:{identity}"
                )
            self._faulted_clients[spec.hook_id].discard(identity)
            state = (
                CoreHookState.FAULTED
                if current
                and len(self._faulted_clients[spec.hook_id]) == len(current)
                else CoreHookState.ATTACHED
                if current
                else CoreHookState.DETACHED
            )
            sequence = self._next_sequence(spec.hook_id)
            self._states[spec.hook_id] = state
            self._updated_at[spec.hook_id] = observed_at
            self._last_fault[spec.hook_id] = ""
            return CoreHookAttachmentReceipt(
                sequence=sequence,
                hook_id=spec.hook_id,
                client_id=identity,
                mode=attachment.mode,
                state=state,
                accepted=True,
                reason="detached",
                occurred_at=observed_at,
            )

    def mark_fault(
        self,
        client_id: str,
        *,
        hook_id: str,
        error: BaseException,
        now: float | None = None,
    ) -> CoreHookAttachmentReceipt:
        identity = str(client_id or "").strip()
        if not identity:
            raise ValueError("core_hook_fault_client_id_required")
        if not isinstance(error, BaseException):
            raise TypeError("core_hook_fault_exception_required")
        observed_at = float(time() if now is None else now)
        with self._lock:
            spec = self._spec(hook_id)
            attachment = self._attachments[spec.hook_id].get(identity)
            if attachment is None:
                raise KeyError(
                    f"core_hook_client_not_attached:{spec.hook_id}:{identity}"
                )
            sequence = self._next_sequence(spec.hook_id)
            self._faulted_clients[spec.hook_id].add(identity)
            self._states[spec.hook_id] = (
                CoreHookState.FAULTED
                if len(self._faulted_clients[spec.hook_id])
                == len(self._attachments[spec.hook_id])
                else CoreHookState.ATTACHED
            )
            self._updated_at[spec.hook_id] = observed_at
            self._last_fault[spec.hook_id] = f"{type(error).__name__}: {error}"
            return CoreHookAttachmentReceipt(
                sequence=sequence,
                hook_id=spec.hook_id,
                client_id=identity,
                mode=attachment.mode,
                state=CoreHookState.FAULTED,
                accepted=False,
                reason="client_fault",
                occurred_at=observed_at,
            )

    def clear_fault(
        self,
        *,
        hook_id: str,
        now: float | None = None,
    ) -> CoreExtensionHookStatus:
        observed_at = float(time() if now is None else now)
        with self._lock:
            spec = self._spec(hook_id)
            if self._states[spec.hook_id] is not CoreHookState.FAULTED:
                raise RuntimeError(f"core_hook_not_faulted:{spec.hook_id}")
            self._next_sequence(spec.hook_id)
            self._faulted_clients[spec.hook_id].clear()
            self._states[spec.hook_id] = (
                CoreHookState.ATTACHED
                if self._attachments[spec.hook_id]
                else CoreHookState.DETACHED
            )
            self._updated_at[spec.hook_id] = observed_at
            self._last_fault[spec.hook_id] = ""
            return self._status_locked(spec)

    def clear_client_fault(
        self,
        client_id: str,
        *,
        hook_id: str,
        now: float | None = None,
    ) -> CoreExtensionHookStatus:
        """Recover one isolated customer without altering another attachment."""

        identity = str(client_id or "").strip()
        if not identity:
            raise ValueError("core_hook_clear_client_id_required")
        observed_at = float(time() if now is None else now)
        with self._lock:
            spec = self._spec(hook_id)
            if identity not in self._attachments[spec.hook_id]:
                raise KeyError(
                    f"core_hook_client_not_attached:{spec.hook_id}:{identity}"
                )
            if identity not in self._faulted_clients[spec.hook_id]:
                raise RuntimeError(
                    f"core_hook_client_not_faulted:{spec.hook_id}:{identity}"
                )
            self._faulted_clients[spec.hook_id].remove(identity)
            self._next_sequence(spec.hook_id)
            self._states[spec.hook_id] = CoreHookState.ATTACHED
            self._updated_at[spec.hook_id] = observed_at
            if not self._faulted_clients[spec.hook_id]:
                self._last_fault[spec.hook_id] = ""
            return self._status_locked(spec)

    def require_attachment(
        self,
        client_id: str,
        *,
        hook_id: str,
        mode: CoreHookAttachmentMode,
    ) -> None:
        """Fail unless one healthy client is attached in the declared mode."""

        identity = str(client_id or "").strip()
        if not identity:
            raise ValueError("core_hook_client_id_required")
        if not isinstance(mode, CoreHookAttachmentMode):
            raise TypeError("core_hook_typed_attachment_mode_required")
        with self._lock:
            spec = self._spec(hook_id)
            attachment = self._attachments[spec.hook_id].get(identity)
            if attachment is None:
                raise PermissionError(
                    f"core_hook_client_not_attached:{spec.hook_id}:{identity}"
                )
            if attachment.mode is not mode:
                raise PermissionError(
                    f"core_hook_client_mode_mismatch:{spec.hook_id}:{identity}"
                )
            if identity in self._faulted_clients[spec.hook_id]:
                raise RuntimeError(
                    f"core_hook_client_faulted:{spec.hook_id}:{identity}"
                )

    def _status_locked(self, spec: CoreExtensionHookSpec) -> CoreExtensionHookStatus:
        attachments = self._attachments[spec.hook_id]
        return CoreExtensionHookStatus(
            spec=spec,
            state=self._states[spec.hook_id],
            providers=tuple(
                sorted(
                    item.client_id
                    for item in attachments.values()
                    if item.mode is CoreHookAttachmentMode.PROVIDER
                )
            ),
            subscribers=tuple(
                sorted(
                    item.client_id
                    for item in attachments.values()
                    if item.mode is CoreHookAttachmentMode.SUBSCRIBER
                )
            ),
            sequence=self._hook_sequences[spec.hook_id],
            updated_at=self._updated_at[spec.hook_id],
            last_fault=self._last_fault[spec.hook_id],
        )

    def status(self, hook_id: str) -> CoreExtensionHookStatus:
        with self._lock:
            return self._status_locked(self._spec(hook_id))

    def snapshot(self) -> tuple[CoreExtensionHookStatus, ...]:
        with self._lock:
            return tuple(
                self._status_locked(self._specs[hook_id])
                for hook_id in sorted(self._specs)
            )


def require_core_extension_hook_registry(port: Any) -> CoreExtensionHookRegistry:
    registry = getattr(port, "extension_hooks", None)
    if not isinstance(registry, CoreExtensionHookRegistry):
        raise RuntimeError("core_extension_hook_registry_unavailable")
    return registry


__all__ = (
    "CoreExtensionHookRegistry",
    "default_core_extension_hook_specs",
    "require_core_extension_hook_registry",
)
