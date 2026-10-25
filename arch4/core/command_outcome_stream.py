"""Per-customer durable cursors over immutable CommandAuthority receipts."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.command_transactions import CommandAuthority
from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.outcomes import CoreCommandOutcomeRecord
from arch4.memory_store.event_journal import JsonlEventJournal


CORE_COMMAND_OUTCOME_STREAM_OWNER = "54guc.core.command_outcome_stream"
CORE_COMMAND_OUTCOME_HOOK_ID = "command_outcome"
CORE_COMMAND_OUTCOME_CURSOR_SCHEMA = "CoreCommandOutcomeCursor.v4"
CORE_COMMAND_OUTCOME_CURSOR_FILENAME = "core_command_outcome_cursors_v4.jsonl"


class CoreCommandOutcomeStream:
    """Deliver final receipts without skipping or inventing history."""

    def __init__(
        self,
        authority: CommandAuthority,
        hook_registry: CoreExtensionHookRegistry,
        *,
        persistence_dir: str | Path | None = None,
    ) -> None:
        if not isinstance(authority, CommandAuthority):
            raise TypeError("core_command_outcome_authority_required")
        if not isinstance(hook_registry, CoreExtensionHookRegistry):
            raise TypeError("core_command_outcome_hook_registry_required")
        self._authority = authority
        self._hooks = hook_registry
        self._lock = RLock()
        self._inflight: dict[
            tuple[str, RuntimeGenerationNamespace],
            int,
        ] = {}
        self._acked_sequence: dict[
            tuple[str, RuntimeGenerationNamespace],
            int,
        ] = {}
        self._fault_count: dict[
            tuple[str, RuntimeGenerationNamespace],
            int,
        ] = {}
        self._last_error: dict[
            tuple[str, RuntimeGenerationNamespace],
            str,
        ] = {}
        self._persistence_available = True
        self._persistence_error = ""
        self._journal = (
            JsonlEventJournal(
                Path(persistence_dir),
                CORE_COMMAND_OUTCOME_CURSOR_FILENAME,
            )
            if persistence_dir is not None
            else None
        )
        if self._journal is not None:
            self._restore_cursors()

    @staticmethod
    def _key(sequence: int) -> str:
        if type(sequence) is not int or sequence <= 0:
            raise TypeError("core_command_outcome_positive_sequence_required")
        return f"command-authority-receipt:{sequence}"

    @staticmethod
    def _cursor_row(
        client_id: str,
        namespace: RuntimeGenerationNamespace,
        sequence: int,
    ) -> dict[str, Any]:
        return {
            "schema": CORE_COMMAND_OUTCOME_CURSOR_SCHEMA,
            "owner": CORE_COMMAND_OUTCOME_STREAM_OWNER,
            "client_id": client_id,
            "runtime_namespace": namespace.as_dict(),
            "acked_sequence": sequence,
        }

    def _restore_cursors(self) -> None:
        assert self._journal is not None
        if self._journal.malformed_count():
            self._persistence_available = False
            self._persistence_error = "core_command_outcome_cursor_corrupt"
            return
        try:
            for row in self._journal.read_all():
                if not isinstance(row, dict) or set(row) != {
                    "schema",
                    "owner",
                    "client_id",
                    "runtime_namespace",
                    "acked_sequence",
                }:
                    raise ValueError("core_command_outcome_cursor_fields_invalid")
                if (
                    row.get("schema") != CORE_COMMAND_OUTCOME_CURSOR_SCHEMA
                    or row.get("owner") != CORE_COMMAND_OUTCOME_STREAM_OWNER
                ):
                    raise ValueError("core_command_outcome_cursor_invalid")
                client_id = row.get("client_id")
                namespace = RuntimeGenerationNamespace.from_mapping(
                    row.get("runtime_namespace")
                )
                sequence = row.get("acked_sequence")
                cursor = (client_id, namespace)
                if (
                    not isinstance(client_id, str)
                    or not client_id
                    or client_id != client_id.strip()
                    or type(sequence) is not int
                    or sequence <= 0
                    or sequence <= self._acked_sequence.get(
                        cursor, 0
                    )
                ):
                    raise ValueError("core_command_outcome_cursor_not_monotonic")
                self._acked_sequence[cursor] = sequence
        except Exception as exc:
            self._acked_sequence.clear()
            self._persistence_available = False
            self._persistence_error = (
                "core_command_outcome_cursor_restore_failed:"
                + type(exc).__name__
                + ":"
                + str(exc)
            )

    def _persist_ack_locked(
        self,
        client_id: str,
        namespace: RuntimeGenerationNamespace,
        sequence: int,
    ) -> None:
        if self._journal is None:
            return
        if not self._persistence_available:
            raise RuntimeError(
                "core_command_outcome_cursor_authority_unavailable"
            )
        try:
            self._journal.append(
                self._cursor_row(client_id, namespace, sequence)
            )
        except Exception as exc:
            self._persistence_available = False
            self._persistence_error = (
                "core_command_outcome_cursor_append_failed:"
                + type(exc).__name__
                + ":"
                + str(exc)
            )
            raise RuntimeError(
                "core_command_outcome_cursor_persistence_failed"
            ) from exc

    def _require_client(self, client_id: str) -> str:
        if (
            not isinstance(client_id, str)
            or not client_id
            or client_id != client_id.strip()
        ):
            raise TypeError("core_command_outcome_client_id_required")
        identity = client_id
        self._hooks.require_attachment(
            identity,
            hook_id=CORE_COMMAND_OUTCOME_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        return identity

    @staticmethod
    def _require_namespace(
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "core_command_outcome_runtime_namespace_required"
            )
        return namespace

    @staticmethod
    def _cursor_label(
        cursor: tuple[str, RuntimeGenerationNamespace],
    ) -> str:
        client_id, namespace = cursor
        return (
            f"{client_id}|{namespace.session_scope_key}|"
            f"{namespace.source_epoch}|{namespace.lifecycle_generation}"
        )

    def claim_next(
        self,
        client_id: str,
        *,
        runtime_namespace: RuntimeGenerationNamespace,
    ) -> CoreCommandOutcomeRecord | None:
        identity = self._require_client(client_id)
        namespace = self._require_namespace(runtime_namespace)
        cursor = (identity, namespace)
        with self._lock:
            if not self._persistence_available:
                raise RuntimeError(
                    "core_command_outcome_cursor_authority_unavailable"
                )
            if cursor in self._inflight:
                return None
            acknowledged = int(self._acked_sequence.get(cursor, 0))
            retention = self._authority.terminal_receipt_retention()
            if (
                retention["evicted_through_sequence"] > 0
                and acknowledged < retention["evicted_through_sequence"]
            ):
                raise RuntimeError("core_command_outcome_history_loss")
            receipts = self._authority.terminal_receipts(
                after_sequence=acknowledged
            )
            if not receipts:
                return None
            matching = tuple(
                receipt
                for receipt in receipts
                if receipt.transaction.matrix_identity.namespace == namespace
            )
            if not matching:
                last_sequence = receipts[-1].receipt_sequence
                self._persist_ack_locked(
                    identity,
                    namespace,
                    last_sequence,
                )
                self._acked_sequence[cursor] = last_sequence
                return None
            first = matching[0]
            if first.receipt_sequence <= acknowledged:
                raise RuntimeError("core_command_outcome_history_loss")
            self._inflight[cursor] = first.receipt_sequence
            return CoreCommandOutcomeRecord(
                key=self._key(first.receipt_sequence),
                receipt=first,
            )

    def acknowledge(
        self,
        client_id: str,
        *,
        runtime_namespace: RuntimeGenerationNamespace,
        key: str,
    ) -> None:
        identity = self._require_client(client_id)
        namespace = self._require_namespace(runtime_namespace)
        cursor = (identity, namespace)
        if not isinstance(key, str) or not key or key != key.strip():
            raise TypeError("core_command_outcome_ack_key_required")
        expected = key
        with self._lock:
            sequence = self._inflight.get(cursor)
            if sequence is None or self._key(sequence) != expected:
                raise RuntimeError("core_command_outcome_ack_mismatch")
            receipts = self._authority.terminal_receipts(
                after_sequence=sequence - 1
            )
            if not receipts or receipts[0].receipt_sequence != sequence:
                raise RuntimeError("core_command_outcome_ack_receipt_missing")
            if (
                receipts[0].transaction.matrix_identity.namespace
                != namespace
            ):
                raise RuntimeError(
                    "core_command_outcome_ack_namespace_mismatch"
                )
            self._persist_ack_locked(identity, namespace, sequence)
            self._inflight.pop(cursor, None)
            self._acked_sequence[cursor] = sequence

    def release(
        self,
        client_id: str,
        *,
        runtime_namespace: RuntimeGenerationNamespace,
        key: str,
        error: BaseException,
    ) -> None:
        identity = self._require_client(client_id)
        namespace = self._require_namespace(runtime_namespace)
        cursor = (identity, namespace)
        if not isinstance(key, str) or not key or key != key.strip():
            raise TypeError("core_command_outcome_release_key_required")
        expected = key
        if not isinstance(error, BaseException):
            raise TypeError("core_command_outcome_release_exception_required")
        with self._lock:
            sequence = self._inflight.get(cursor)
            if sequence is None or self._key(sequence) != expected:
                raise RuntimeError("core_command_outcome_release_mismatch")
            self._inflight.pop(cursor, None)
            self._fault_count[cursor] = self._fault_count.get(cursor, 0) + 1
            self._last_error[cursor] = f"{type(error).__name__}: {error}"

    def synchronize_to_live_edge(self, client_id: str) -> None:
        raise RuntimeError(
            "core_command_outcome_live_edge_skip_illegal:"
            + str(client_id or "").strip()
        )

    def recover_client(self, client_id: str) -> None:
        raise RuntimeError(
            "core_command_outcome_ad_hoc_recovery_illegal:"
            + str(client_id or "").strip()
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            receipts = self._authority.terminal_receipts()
            return {
                "schema": "CoreCommandOutcomeStreamStatus.v4",
                "owner": CORE_COMMAND_OUTCOME_STREAM_OWNER,
                "authority_owner": "arch4.command_transactions.CommandAuthority",
                "terminal_receipts": len(receipts),
                "latest_sequence": (
                    receipts[-1].receipt_sequence if receipts else 0
                ),
                "inflight": {
                    self._cursor_label(cursor): self._key(sequence)
                    for cursor, sequence in self._inflight.items()
                },
                "acked_sequence": {
                    self._cursor_label(cursor): sequence
                    for cursor, sequence in self._acked_sequence.items()
                },
                "fault_count": {
                    self._cursor_label(cursor): count
                    for cursor, count in self._fault_count.items()
                },
                "last_error": {
                    self._cursor_label(cursor): error
                    for cursor, error in self._last_error.items()
                },
                "cursor_persistent": self._journal is not None,
                "persistence_available": self._persistence_available,
                "persistence_error": self._persistence_error,
            }


__all__ = (
    "CORE_COMMAND_OUTCOME_STREAM_OWNER",
    "CoreCommandOutcomeStream",
)
