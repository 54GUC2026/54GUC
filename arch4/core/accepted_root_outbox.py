"""The only bounded customer outbox of immutable accepted-Core root references."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from threading import RLock
from time import time
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core_contracts.clients import (
    CoreClientManifest,
    CoreClientRole,
)
from arch4.core_contracts.accepted_generation_commit import (
    CustomerCursorState,
    GenerationLossRange,
)
from arch4.core_contracts.accepted_generation_root import (
    CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.accepted_root_outbox import (
    CoreAcceptedRootCustomerTerminalDisposition,
    CoreAcceptedRootOutboxDrainStatus,
    CoreAcceptedRootOutboxNamespaceBindingReceipt,
    CoreAcceptedRootOutboxRecord,
    CoreAcceptedRootResyncRequired,
    CoreAcceptedRootOutboxTerminationReceipt,
    CoreAcceptedRootTerminalDispositionBatch,
    CoreAcceptedRootTerminalObligation,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.hooks import CoreHookAttachmentMode


ACCEPTED_ROOT_HOOK_ID = "accepted_observation"
OWNER = "54guc.core.accepted_root_outbox"
CORE_REGISTER_PROJECTION_CLIENT_ID = (
    "54guc.core.runtime_register_projection"
)
CORE_COMMAND_LIFECYCLE_CLIENT_ID = "54guc.core.command_lifecycle"
REQUIRED_CORE_ROOT_CLIENT_IDS = (
    CORE_REGISTER_PROJECTION_CLIENT_ID,
    CORE_COMMAND_LIFECYCLE_CLIENT_ID,
)


def _require_exact_text(value: object, *, reason: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        raise TypeError(reason)
    return value


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootNamespaceBindingPlan:
    namespace: RuntimeGenerationNamespace
    previous_namespace: RuntimeGenerationNamespace | None

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("core_accepted_root_binding_plan_namespace_required")
        if self.previous_namespace is not None and not isinstance(
            self.previous_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "core_accepted_root_binding_plan_previous_namespace_invalid"
            )


class CoreAcceptedRootOutbox:
    """Retain one reference per accepted root and customer cursors only."""

    def __init__(
        self,
        hook_registry: CoreExtensionHookRegistry,
        *,
        max_records: int = 128,
    ) -> None:
        if not isinstance(hook_registry, CoreExtensionHookRegistry):
            raise TypeError("core_accepted_root_outbox_hooks_required")
        retained = int(max_records)
        if retained < 8:
            raise ValueError("core_accepted_root_outbox_retention_invalid")
        self._hooks = hook_registry
        self._max_records = retained
        self._lock = RLock()
        self._records: OrderedDict[
            str,
            CoreAcceptedRootOutboxRecord,
        ] = OrderedDict()
        self._latest_sequence: dict[RuntimeGenerationNamespace, int] = {}
        self._inflight: dict[str, str] = {}
        self._cursors: dict[str, CustomerCursorState] = {}
        self._terminal_dispositions: dict[
            str,
            CoreAcceptedRootCustomerTerminalDisposition,
        ] = {}
        self._evicted_total = 0
        self._live_namespace: RuntimeGenerationNamespace | None = None
        self._admission_open = True
        self._admission_closed_at = 0.0
        self._current: CoreAcceptedGenerationRoot | None = None
        for client_id in REQUIRED_CORE_ROOT_CLIENT_IDS:
            self._hooks.attach(
                CoreClientManifest(
                    client_id=client_id,
                    role=CoreClientRole.FOUNDATION_SERVICE,
                    contract_versions=(
                        CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
                    ),
                    queue_budget=self._max_records,
                    required=True,
                ),
                hook_id=ACCEPTED_ROOT_HOOK_ID,
                mode=CoreHookAttachmentMode.SUBSCRIBER,
            )

    def _pristine_initial_binding_state_locked(self) -> bool:
        """Return whether every private state cell is at initial admission."""

        return bool(
            self._live_namespace is None
            and not self._records
            and not self._latest_sequence
            and not self._inflight
            and not self._cursors
            and not self._terminal_dispositions
            and self._evicted_total == 0
            and self._admission_open
            and self._admission_closed_at == 0.0
            and self._current is None
        )

    @staticmethod
    def identity_key(identity: MatrixGenerationIdentity) -> str:
        if not isinstance(identity, MatrixGenerationIdentity):
            raise TypeError("core_accepted_root_matrix_identity_required")
        digest = hashlib.sha256(
            identity.session_scope_key.encode("utf-8")
        ).hexdigest()[:16]
        return (
            f"{digest}:{identity.source_epoch}:"
            f"{identity.lifecycle_generation}:{identity.matrix_sequence}:"
            f"{identity.matrix_revision}"
        )

    def prepare_live_namespace_binding(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> CoreAcceptedRootNamespaceBindingPlan:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("core_accepted_root_live_namespace_required")
        with self._lock:
            previous = self._live_namespace
            if previous == namespace:
                return CoreAcceptedRootNamespaceBindingPlan(
                    namespace,
                    previous,
                )
            if previous is not None:
                raise RuntimeError(
                    "core_accepted_root_outbox_replacement_requires_product_cutover"
                )
            if not self._pristine_initial_binding_state_locked():
                raise RuntimeError(
                    "core_accepted_root_outbox_namespace_bind_not_pristine"
                )
            return CoreAcceptedRootNamespaceBindingPlan(
                namespace,
                previous,
            )

    def commit_live_namespace_binding(
        self,
        plan: CoreAcceptedRootNamespaceBindingPlan,
    ) -> CoreAcceptedRootOutboxNamespaceBindingReceipt:
        if not isinstance(plan, CoreAcceptedRootNamespaceBindingPlan):
            raise TypeError(
                "core_accepted_root_namespace_binding_plan_required"
            )
        namespace = plan.namespace
        with self._lock:
            previous = self._live_namespace
            if previous == namespace:
                changed = False
            elif (
                previous is None
                and self._pristine_initial_binding_state_locked()
            ):
                self._live_namespace = namespace
                changed = True
            else:
                raise RuntimeError(
                    "core_accepted_root_namespace_binding_plan_stale"
                )
            return CoreAcceptedRootOutboxNamespaceBindingReceipt(
                namespace=namespace,
                changed=changed,
            )

    def bind_live_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> dict[str, Any]:
        del namespace
        raise RuntimeError(
            "direct_outbox_namespace_binding_illegal:"
            "use_modem_sample_publication_atomic_bind"
        )

    def rollback_live_namespace_binding(
        self,
        plan: CoreAcceptedRootNamespaceBindingPlan,
    ) -> None:
        if not isinstance(plan, CoreAcceptedRootNamespaceBindingPlan):
            raise TypeError(
                "core_accepted_root_namespace_binding_plan_required"
            )
        with self._lock:
            if plan.previous_namespace is not None:
                return
            if self._live_namespace is None:
                return
            if (
                self._live_namespace != plan.namespace
                or self._records
                or self._latest_sequence
                or self._inflight
                or self._cursors
                or self._terminal_dispositions
                or self._evicted_total != 0
                or not self._admission_open
                or self._admission_closed_at != 0.0
                or self._current is not None
            ):
                raise RuntimeError(
                    "core_accepted_root_namespace_rollback_state_mismatch"
                )
            self._live_namespace = None

    def publish(
        self,
        root: CoreAcceptedGenerationRoot,
    ) -> CoreAcceptedRootOutboxRecord:
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise TypeError("core_accepted_root_outbox_typed_root_required")
        identity = root.matrix_identity
        key = self.identity_key(identity)
        namespace = identity.namespace
        with self._lock:
            if not self._admission_open:
                raise RuntimeError(
                    "core_accepted_root_outbox_admission_closed"
                )
            if namespace != self._live_namespace:
                raise RuntimeError(
                    "core_accepted_root_foreign_namespace_rejected"
                )
            existing = self._records.get(key)
            if existing is not None:
                if (
                    existing.root.root_fingerprint
                    != root.root_fingerprint
                ):
                    raise RuntimeError(
                        "core_accepted_root_key_payload_mismatch"
                    )
                return existing
            latest = self._latest_sequence.get(namespace, 0)
            if identity.matrix_sequence <= latest:
                raise ValueError(
                    "core_accepted_root_sequence_not_monotonic"
                )
            record = CoreAcceptedRootOutboxRecord(key=key, root=root)
            self._records[key] = record
            self._latest_sequence[namespace] = identity.matrix_sequence
            self._current = root
            self._trim_locked()
            return record

    def current(self) -> CoreAcceptedGenerationRoot | None:
        with self._lock:
            return self._current

    def claim_next(
        self,
        client_id: str,
    ) -> CoreAcceptedRootOutboxRecord | None:
        identity = _require_exact_text(
            client_id,
            reason="core_accepted_root_client_id_required",
        )
        self._hooks.require_attachment(
            identity,
            hook_id=ACCEPTED_ROOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        with self._lock:
            if identity in self._terminal_dispositions:
                raise RuntimeError(
                    "core_accepted_root_customer_terminally_dispositioned"
                )
            if identity in self._inflight:
                return None
            cursor = self._cursors.get(identity)
            if cursor is not None and cursor.resync_required:
                raise CoreAcceptedRootResyncRequired(cursor)
            if cursor is not None and cursor.acknowledged_source_sequence > 0:
                first = self._first_for_namespace_locked(
                    RuntimeGenerationNamespace(
                        session_scope_key=cursor.session_scope_key,
                        source_epoch=cursor.source_epoch,
                        lifecycle_generation=cursor.lifecycle_generation,
                    )
                )
                if (
                    first is not None
                    and first.root.matrix_identity.matrix_sequence
                    > cursor.acknowledged_source_sequence + 1
                ):
                    cursor = self._require_resync_locked(
                        identity,
                        root=first.root,
                        through_source_sequence=(
                            first.root.matrix_identity.matrix_sequence - 1
                        ),
                        reason="bounded_accepted_root_cursor_gap",
                    )
                    raise CoreAcceptedRootResyncRequired(cursor)
            for key, record in self._records.items():
                root_identity = record.root.matrix_identity
                if root_identity.namespace != self._live_namespace:
                    raise RuntimeError(
                        "core_accepted_root_foreign_record_retained"
                    )
                if cursor is not None and cursor.acknowledged_source_sequence > 0:
                    same_namespace = (
                        root_identity.session_scope_key
                        == cursor.session_scope_key
                        and root_identity.source_epoch == cursor.source_epoch
                        and root_identity.lifecycle_generation
                        == cursor.lifecycle_generation
                    )
                    if (
                        same_namespace
                        and root_identity.matrix_sequence
                        <= cursor.acknowledged_source_sequence
                    ):
                        continue
                self._inflight[identity] = key
                return record
            return None

    def acknowledge(self, client_id: str, *, key: str) -> None:
        identity = _require_exact_text(
            client_id,
            reason="core_accepted_root_client_id_required",
        )
        self._hooks.require_attachment(
            identity,
            hook_id=ACCEPTED_ROOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        expected = _require_exact_text(
            key,
            reason="core_accepted_root_key_required",
        )
        with self._lock:
            record = self._records.get(expected)
            if record is None:
                raise RuntimeError(
                    "core_accepted_root_outbox_ack_record_missing"
                )
            root_identity = record.root.matrix_identity
            previous = self._cursors.get(identity)
            if self._inflight.get(identity) != expected:
                if (
                    previous is not None
                    and previous.session_scope_key
                    == root_identity.session_scope_key
                    and previous.source_epoch == root_identity.source_epoch
                    and previous.lifecycle_generation
                    == root_identity.lifecycle_generation
                    and previous.acknowledged_source_sequence
                    >= root_identity.matrix_sequence
                ):
                    return
                raise RuntimeError("core_accepted_root_outbox_ack_mismatch")
            self._inflight.pop(identity, None)
            self._cursors[identity] = CustomerCursorState(
                customer_id=identity,
                session_scope_key=root_identity.session_scope_key,
                source_epoch=root_identity.source_epoch,
                lifecycle_generation=root_identity.lifecycle_generation,
                acknowledged_source_sequence=root_identity.matrix_sequence,
                fault_count=(
                    previous.fault_count if previous is not None else 0
                ),
                last_error="",
            )

    def release(
        self,
        client_id: str,
        *,
        key: str,
        error: BaseException,
    ) -> None:
        identity = _require_exact_text(
            client_id,
            reason="core_accepted_root_client_id_required",
        )
        self._hooks.require_attachment(
            identity,
            hook_id=ACCEPTED_ROOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        expected = _require_exact_text(
            key,
            reason="core_accepted_root_key_required",
        )
        if not isinstance(error, BaseException):
            raise TypeError(
                "core_accepted_root_outbox_release_exception_required"
            )
        with self._lock:
            if self._inflight.get(identity) != expected:
                raise RuntimeError(
                    "core_accepted_root_outbox_release_mismatch"
                )
            self._inflight.pop(identity, None)
            previous = self._cursors.get(identity)
            record = self._records.get(expected)
            root_identity = (
                record.root.matrix_identity if record is not None else None
            )
            self._cursors[identity] = CustomerCursorState(
                customer_id=identity,
                session_scope_key=(
                    previous.session_scope_key
                    if previous is not None
                    else (
                        root_identity.session_scope_key
                        if root_identity is not None
                        else ""
                    )
                ),
                source_epoch=(
                    previous.source_epoch
                    if previous is not None
                    else (
                        root_identity.source_epoch
                        if root_identity is not None
                        else ""
                    )
                ),
                lifecycle_generation=(
                    previous.lifecycle_generation
                    if previous is not None
                    else (
                        root_identity.lifecycle_generation
                        if root_identity is not None
                        else 0
                    )
                ),
                acknowledged_source_sequence=(
                    previous.acknowledged_source_sequence
                    if previous is not None
                    else 0
                ),
                fault_count=(
                    previous.fault_count if previous is not None else 0
                )
                + 1,
                last_error=f"{type(error).__name__}: {error}",
            )

    def close_admission(self) -> CoreAcceptedRootOutboxDrainStatus:
        """Forbid new roots while allowing attached customers to drain."""

        with self._lock:
            if self._admission_open:
                self._admission_open = False
                self._admission_closed_at = time()
            return self._drain_status_locked()

    def drain_status(self) -> CoreAcceptedRootOutboxDrainStatus:
        with self._lock:
            return self._drain_status_locked()

    def finalize_termination(
        self,
    ) -> CoreAcceptedRootOutboxTerminationReceipt:
        """Release drained roots and confirm that none remain."""

        with self._lock:
            drain = self._drain_status_locked()
            if not drain.drained:
                raise RuntimeError(
                    "core_accepted_root_outbox_finalize_before_drain"
                )
            retired_records = len(self._records)
            retired_cursors = len(self._cursors)
            terminal_dispositions = {
                client_id: disposition.to_payload()
                for client_id, disposition in (
                    self._terminal_dispositions.items()
                )
            }
            self._records.clear()
            self._latest_sequence.clear()
            self._inflight.clear()
            self._cursors.clear()
            self._terminal_dispositions.clear()
            self._current = None
            self._live_namespace = None
            return CoreAcceptedRootOutboxTerminationReceipt(
                retired_records=retired_records,
                retired_cursors=retired_cursors,
                terminal_dispositions=CoreFrozenPayload.from_mapping(
                    terminal_dispositions
                ),
                terminated_at=float(time()),
            )

    def terminally_disposition_pending_customers(
        self,
        *,
        reason: str,
        lifecycle_generation: int,
        shutdown_started_at: float,
    ) -> CoreAcceptedRootTerminalDispositionBatch:
        """Record shutdown loss for each customer that did not acknowledge."""

        if type(reason) is not str:
            raise TypeError(
                "core_accepted_root_terminal_disposition_reason_required"
            )
        with self._lock:
            if self._admission_open:
                raise RuntimeError(
                    "core_accepted_root_terminal_disposition_before_close"
                )
            drain = self._drain_status_locked()
            pending = drain.pending_clients
            dispositions: list[
                CoreAcceptedRootCustomerTerminalDisposition
            ] = []
            for client_id in pending:
                cursor = self._cursors.get(client_id)
                acknowledged = (
                    cursor.acknowledged_source_sequence
                    if cursor is not None
                    else 0
                )
                obligations: list[CoreAcceptedRootTerminalObligation] = []
                for key, record in self._records.items():
                    identity = record.root.matrix_identity
                    same_namespace = bool(
                        cursor is None
                        or (
                            cursor.session_scope_key
                            == identity.session_scope_key
                            and cursor.source_epoch
                            == identity.source_epoch
                            and cursor.lifecycle_generation
                            == identity.lifecycle_generation
                        )
                    )
                    if (
                        same_namespace
                        and identity.matrix_sequence > acknowledged
                    ):
                        obligations.append(
                            CoreAcceptedRootTerminalObligation(
                                key=key,
                                matrix_identity=identity,
                                root_fingerprint=(
                                    record.root.root_fingerprint
                                ),
                            )
                        )
                disposition = CoreAcceptedRootCustomerTerminalDisposition(
                    lifecycle_generation=lifecycle_generation,
                    shutdown_started_at=shutdown_started_at,
                    client_id=client_id,
                    reason=reason,
                    obligations=tuple(obligations),
                    inflight_key=self._inflight.pop(client_id, ""),
                    disposed_at=float(time()),
                )
                self._terminal_dispositions[client_id] = disposition
                dispositions.append(disposition)
            return CoreAcceptedRootTerminalDispositionBatch(
                lifecycle_generation=lifecycle_generation,
                shutdown_started_at=shutdown_started_at,
                reason=reason,
                customers=tuple(dispositions),
            )

    def resync_required(
        self,
        client_id: str,
    ) -> CustomerCursorState | None:
        identity = _require_exact_text(
            client_id,
            reason="core_accepted_root_client_id_required",
        )
        with self._lock:
            state = self._cursors.get(identity)
            return (
                state
                if state is not None and state.resync_required
                else None
            )

    def acknowledge_resync(
        self,
        client_id: str,
        *,
        through_source_sequence: int,
    ) -> CustomerCursorState:
        identity = _require_exact_text(
            client_id,
            reason="core_accepted_root_client_id_required",
        )
        self._hooks.require_attachment(
            identity,
            hook_id=ACCEPTED_ROOT_HOOK_ID,
            mode=CoreHookAttachmentMode.SUBSCRIBER,
        )
        with self._lock:
            current = self._cursors.get(identity)
            if current is None or not current.resync_required:
                raise RuntimeError(
                    "core_accepted_root_resync_not_required"
                )
            loss = current.loss_range
            assert loss is not None
            if type(through_source_sequence) is not int or through_source_sequence <= 0:
                raise TypeError(
                    "core_accepted_root_resync_sequence_required"
                )
            if (
                loss.last_source_sequence != through_source_sequence
            ):
                raise RuntimeError(
                    "core_accepted_root_resync_ack_mismatch"
                )
            updated = CustomerCursorState(
                customer_id=identity,
                session_scope_key=loss.session_scope_key,
                source_epoch=loss.source_epoch,
                lifecycle_generation=loss.lifecycle_generation,
                acknowledged_source_sequence=loss.last_source_sequence,
                fault_count=current.fault_count,
                last_error="",
            )
            self._cursors[identity] = updated
            return updated

    def latest(self) -> CoreAcceptedRootOutboxRecord | None:
        with self._lock:
            return next(reversed(self._records.values()), None)

    def exact(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity,
    ) -> CoreAcceptedRootOutboxRecord | None:
        with self._lock:
            return self._records.get(self.identity_key(matrix_identity))

    def latest_and_exact(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity,
    ) -> tuple[
        CoreAcceptedRootOutboxRecord | None,
        CoreAcceptedRootOutboxRecord | None,
    ]:
        key = self.identity_key(matrix_identity)
        with self._lock:
            latest = next(reversed(self._records.values()), None)
            return latest, self._records.get(key)

    def status(self) -> dict[str, Any]:
        with self._lock:
            latest = next(reversed(self._records.values()), None)
            return {
                "schema": "CoreAcceptedRootOutboxStatus.v1",
                "owner": OWNER,
                "records": len(self._records),
                "max_records": self._max_records,
                "latest_key": latest.key if latest is not None else "",
                "current_root_fingerprint": (
                    self._current.root_fingerprint
                    if self._current is not None
                    else ""
                ),
                "evicted_total": self._evicted_total,
                "admission_open": self._admission_open,
                "admission_closed_at": self._admission_closed_at,
                "live_namespace": (
                    self._live_namespace.as_dict()
                    if self._live_namespace is not None
                    else {}
                ),
                "inflight": dict(self._inflight),
                "customer_cursors": {
                    client_id: cursor.to_payload()
                    for client_id, cursor in self._cursors.items()
                },
                "terminal_dispositions": {
                    client_id: disposition.to_payload()
                    for client_id, disposition in (
                        self._terminal_dispositions.items()
                    )
                },
                "drain": self._drain_status_locked().to_payload(),
            }

    def _drain_status_locked(self) -> CoreAcceptedRootOutboxDrainStatus:
        hook_status = self._hooks.status(ACCEPTED_ROOT_HOOK_ID)
        subscribers = tuple(hook_status.subscribers)
        latest = next(reversed(self._records.values()), None)
        latest_sequence = (
            latest.root.matrix_identity.matrix_sequence
            if latest is not None
            else 0
        )
        pending_clients: list[str] = []
        for client_id in subscribers:
            if client_id in self._terminal_dispositions:
                continue
            if client_id in self._inflight:
                pending_clients.append(client_id)
                continue
            cursor = self._cursors.get(client_id)
            if cursor is not None and cursor.resync_required:
                pending_clients.append(client_id)
                continue
            acknowledged = (
                cursor.acknowledged_source_sequence
                if cursor is not None
                and latest is not None
                and cursor.session_scope_key
                == latest.root.matrix_identity.session_scope_key
                and cursor.source_epoch
                == latest.root.matrix_identity.source_epoch
                and cursor.lifecycle_generation
                == latest.root.matrix_identity.lifecycle_generation
                else 0
            )
            if acknowledged < latest_sequence:
                pending_clients.append(client_id)
        return CoreAcceptedRootOutboxDrainStatus(
            admission_open=self._admission_open,
            admission_closed_at=float(self._admission_closed_at),
            latest_source_sequence=latest_sequence,
            attached_clients=subscribers,
            pending_clients=tuple(pending_clients),
            inflight_count=len(self._inflight),
            retained_record_count=len(self._records),
            retained_cursor_count=len(self._cursors),
            current_root_retained=self._current is not None,
        )

    def _trim_locked(self) -> None:
        while len(self._records) > self._max_records:
            key, record = self._records.popitem(last=False)
            self._evicted_total += 1
            for client_id, inflight_key in tuple(self._inflight.items()):
                if inflight_key == key:
                    self._inflight.pop(client_id, None)
                    self._require_resync_locked(
                        client_id,
                        root=record.root,
                        through_source_sequence=(
                            record.root.matrix_identity.matrix_sequence
                        ),
                        reason="bounded_accepted_root_eviction",
                    )

    def _first_for_namespace_locked(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> CoreAcceptedRootOutboxRecord | None:
        for record in self._records.values():
            if record.root.matrix_identity.namespace == namespace:
                return record
        return None

    def _require_resync_locked(
        self,
        client_id: str,
        *,
        root: CoreAcceptedGenerationRoot,
        through_source_sequence: int,
        reason: str,
    ) -> CustomerCursorState:
        identity = root.matrix_identity
        previous = self._cursors.get(client_id)
        first = (
            previous.acknowledged_source_sequence + 1
            if (
                previous is not None
                and previous.session_scope_key == identity.session_scope_key
                and previous.source_epoch == identity.source_epoch
                and previous.lifecycle_generation
                == identity.lifecycle_generation
            )
            else identity.matrix_sequence
        )
        last = max(first, int(through_source_sequence))
        loss = GenerationLossRange(
            session_scope_key=identity.session_scope_key,
            source_epoch=identity.source_epoch,
            lifecycle_generation=identity.lifecycle_generation,
            first_source_sequence=first,
            last_source_sequence=last,
            reason=reason,
        )
        state = CustomerCursorState(
            customer_id=client_id,
            session_scope_key=identity.session_scope_key,
            source_epoch=identity.source_epoch,
            lifecycle_generation=identity.lifecycle_generation,
            acknowledged_source_sequence=max(0, first - 1),
            resync_required=True,
            loss_range=loss,
            fault_count=(
                previous.fault_count if previous is not None else 0
            ),
            last_error=(
                previous.last_error if previous is not None else ""
            ),
        )
        self._cursors[client_id] = state
        return state


def require_core_accepted_root_outbox(port: Any) -> CoreAcceptedRootOutbox:
    outbox = getattr(port, "accepted_root_outbox", None)
    if not isinstance(outbox, CoreAcceptedRootOutbox):
        raise RuntimeError("core_accepted_root_outbox_unavailable")
    return outbox


__all__ = (
    "ACCEPTED_ROOT_HOOK_ID",
    "CORE_COMMAND_LIFECYCLE_CLIENT_ID",
    "CORE_REGISTER_PROJECTION_CLIENT_ID",
    "CoreAcceptedRootNamespaceBindingPlan",
    "CoreAcceptedRootOutbox",
    "CoreAcceptedRootResyncRequired",
    "OWNER",
    "REQUIRED_CORE_ROOT_CLIENT_IDS",
    "require_core_accepted_root_outbox",
)
