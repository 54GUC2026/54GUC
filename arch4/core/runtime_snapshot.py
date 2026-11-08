"""The only immutable customer-snapshot publisher over the Core register bank."""

from __future__ import annotations

from threading import RLock
from time import time
from typing import Any, Mapping

from arch4.core.accepted_root_outbox import CoreAcceptedRootOutbox
from arch4.core.command_context import build_core_command_ingress_context
from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES,
    CoreRuntimeSnapshot,
)
from arch4.runtime_register_contract_names import (
    CORE_CUSTOMER_REGISTER_NAMES,
)
from arch4.runtime_registers import RuntimeRegisterBank


class CoreRuntimeSnapshotPublisher:
    """Read the Core catalog without exposing mutable registers."""

    def __init__(
        self,
        accepted_root_outbox: CoreAcceptedRootOutbox,
        hook_registry: CoreExtensionHookRegistry,
        register_bank: RuntimeRegisterBank,
    ) -> None:
        if not isinstance(accepted_root_outbox, CoreAcceptedRootOutbox):
            raise TypeError("core_runtime_snapshot_root_outbox_required")
        if not isinstance(hook_registry, CoreExtensionHookRegistry):
            raise TypeError("core_runtime_snapshot_hook_registry_required")
        if not isinstance(register_bank, RuntimeRegisterBank):
            raise TypeError("core_runtime_snapshot_register_bank_required")
        self._accepted_root_outbox = accepted_root_outbox
        self._hooks = hook_registry
        self._register_bank = register_bank
        self._lock = RLock()
        self._sequence = 0
        self._register_payload_sequence = -1
        self._register_payload: CoreFrozenPayload | None = None
        self._command_root_fingerprint = ""
        self._command_matrix_sequence = -1
        self._command_payload: CoreFrozenPayload | None = None
        self._empty_diagnostics_payload = CoreFrozenPayload.from_mapping(
            {
                "schema": "CoreRuntimeSnapshotDiagnostics.v1",
                "owner": "54guc.core.runtime_snapshot",
                "decision_inert": True,
                "health": {},
                "metadata": {},
            }
        )

    def _cached_payloads(
        self,
        root: CoreAcceptedGenerationRoot,
        register_sequence: int,
    ) -> tuple[CoreFrozenPayload | None, CoreFrozenPayload | None]:
        with self._lock:
            registers = (
                self._register_payload
                if register_sequence == self._register_payload_sequence
                else None
            )
            command = (
                self._command_payload
                if root.root_fingerprint == self._command_root_fingerprint
                else None
            )
        return registers, command

    def _current_register_payload(
        self,
        observed_sequence: int,
        cached: CoreFrozenPayload | None,
    ) -> tuple[int, CoreFrozenPayload]:
        if cached is not None:
            return observed_sequence, cached
        sequence, registers = self._register_bank.snapshot_generation(
            CORE_CUSTOMER_REGISTER_NAMES
        )
        if set(registers) != set(CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES):
            raise RuntimeError(
                "core_runtime_snapshot_root_register_catalog_mismatch"
            )
        return sequence, CoreFrozenPayload.from_mapping(registers)

    @staticmethod
    def _current_command_payload(
        root: CoreAcceptedGenerationRoot,
        cached: CoreFrozenPayload | None,
    ) -> CoreFrozenPayload:
        if cached is not None:
            return cached
        return CoreFrozenPayload.from_mapping(
            build_core_command_ingress_context(root.runtime_view).to_payload()
        )

    def _diagnostics_payload(
        self,
        health: Mapping[str, Any] | None,
        metadata: Mapping[str, Any] | None,
    ) -> CoreFrozenPayload:
        if not health and not metadata:
            return self._empty_diagnostics_payload
        return CoreFrozenPayload.from_mapping(
            {
                "schema": "CoreRuntimeSnapshotDiagnostics.v1",
                "owner": "54guc.core.runtime_snapshot",
                "decision_inert": True,
                "health": dict(health or {}),
                "metadata": dict(metadata or {}),
            }
        )

    def _commit_snapshot(
        self,
        *,
        root: CoreAcceptedGenerationRoot,
        observed_at: float,
        register_sequence: int,
        register_payload: CoreFrozenPayload,
        command_payload: CoreFrozenPayload,
        diagnostics_payload: CoreFrozenPayload,
    ) -> CoreRuntimeSnapshot:
        with self._lock:
            if register_sequence >= self._register_payload_sequence:
                self._register_payload_sequence = register_sequence
                self._register_payload = register_payload
            if root.matrix_identity.matrix_sequence >= self._command_matrix_sequence:
                self._command_root_fingerprint = root.root_fingerprint
                self._command_matrix_sequence = root.matrix_identity.matrix_sequence
                self._command_payload = command_payload
            sequence = self._sequence + 1
            snapshot = CoreRuntimeSnapshot(
                matrix_identity=root.matrix_identity,
                accepted_commit_id=root.commit.commit_id,
                accepted_root_fingerprint=root.root_fingerprint,
                accepted_source_sampled_at=root.commit.matrix_source_time,
                accepted_current_facts=root.current_facts,
                sequence=sequence,
                published_at=observed_at,
                registers=register_payload,
                command_context=command_payload,
                diagnostics=diagnostics_payload,
            )
            self._sequence = sequence
            return snapshot

    def snapshot_for(
        self,
        client_id: str,
        *,
        accepted_root: CoreAcceptedGenerationRoot | None = None,
        health: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        published_at: float | None = None,
    ) -> CoreRuntimeSnapshot:
        if (
            type(client_id) is not str
            or not client_id
            or client_id != client_id.strip()
        ):
            raise TypeError("core_runtime_snapshot_client_id_invalid")
        identity = client_id
        self._hooks.require_attachment(
            identity,
            hook_id=CORE_RUNTIME_SNAPSHOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        observed_at = time() if published_at is None else published_at
        if type(observed_at) is not float or observed_at <= 0.0:
            raise ValueError("core_runtime_snapshot_positive_timestamp_required")
        root = (
            self._accepted_root_outbox.current()
            if accepted_root is None
            else accepted_root
        )
        if root is None:
            raise RuntimeError("core_runtime_snapshot_accepted_root_unavailable")
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise TypeError("core_runtime_snapshot_accepted_root_invalid")
        if health is not None and not isinstance(health, Mapping):
            raise TypeError("core_runtime_snapshot_health_mapping_required")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("core_runtime_snapshot_metadata_mapping_required")
        observed_register_sequence = self._register_bank.sequence
        register_payload, command_payload = self._cached_payloads(
            root,
            observed_register_sequence,
        )
        register_sequence, register_payload = self._current_register_payload(
            observed_register_sequence,
            register_payload,
        )
        command_payload = self._current_command_payload(root, command_payload)
        return self._commit_snapshot(
            root=root,
            observed_at=observed_at,
            register_sequence=register_sequence,
            register_payload=register_payload,
            command_payload=command_payload,
            diagnostics_payload=self._diagnostics_payload(health, metadata),
        )


def require_core_runtime_snapshot_publisher(port: Any) -> CoreRuntimeSnapshotPublisher:
    publisher = getattr(port, "runtime_snapshot_publisher", None)
    if not isinstance(publisher, CoreRuntimeSnapshotPublisher):
        raise RuntimeError("core_runtime_snapshot_publisher_unavailable")
    return publisher


__all__ = (
    "CoreRuntimeSnapshotPublisher",
    "require_core_runtime_snapshot_publisher",
)
