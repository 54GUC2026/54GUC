"""Single runtime owner for Modem Live generation promotion and fan-out."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from threading import Event, RLock, Thread
from time import perf_counter, sleep, time
from typing import Any, Callable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.accepted_modem_generation import (
    AcceptedModemGeneration,
    SignalProcessingMatrix,
    RawModemGeneration,
)
from arch4.core_contracts.accepted_generation_commit import (
    AcceptedGenerationCommit,
)
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFacts,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.current_health import (
    RuntimeHealthNamespaceBindingReceipt,
)
from arch4.controller_frames import (
    RadioTransitionFrame,
    radio_transition_frame_to_mapping,
)
from arch4.core.observation_adapter import (
    build_core_accepted_current_facts,
    build_core_accepted_runtime_view,
    build_core_observation_frame,
)
from arch4.core.command_lifecycle_service import CoreCommandLifecycleService
from arch4.core.action_source_state_authority import (
    require_action_source_state_authority,
)
from arch4.core.accepted_root_outbox import (
    CORE_COMMAND_LIFECYCLE_CLIENT_ID,
    CORE_REGISTER_PROJECTION_CLIENT_ID,
    CoreAcceptedRootOutbox,
)
from arch4.core_contracts.accepted_root_outbox import (
    CoreAcceptedRootOutboxDrainStatus,
    CoreAcceptedRootOutboxNamespaceBindingReceipt,
    CoreAcceptedRootOutboxTerminationReceipt,
    CoreAcceptedRootTerminalDispositionBatch,
    CorePrivateStartupRootTerminalDisposition,
)
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    require_fault_supervisor,
)
from arch4.modem_live.publication import (
    ModemLivePublicationAdmissionReceipt,
    ModemLivePublicationNotice,
    ModemLivePublicationSinkDetachReceipt,
)
from arch4.rtt_observation import MatrixBoundRttObservation
from arch4.runtime_registers import (
    SignalProcessingRegisterTransaction,
    RuntimeRegisterBank,
    prepare_signal_processing_registers,
    publish_modem_live_matrix_generation,
    publish_signal_processing_registers,
    require_core_runtime_register_bank,
)
from arch4.runtime_register_contract_names import (
    REGISTER_CA,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
)
from arch4.signal_tuning_surface import signal_tuning_surface_from_owner


PUBLICATION_WORKER_EXIT_RESERVE_S = 0.25


@dataclass(frozen=True, slots=True)
class ModemSamplePublicationResult:
    ran: bool
    published: bool
    sequence: int
    sequence_gap: int
    missed_total: int
    reason: str
    duration_ms: float
    last_error: str = ""
    sampled_at: float = 0.0
    modem_published_at: float = 0.0
    runtime_promoted_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "modem_sample_publication_status.v1",
            "owner": "runtime.modem_sample_publication",
            "ran": self.ran,
            "published": self.published,
            "sequence": self.sequence,
            "sequence_gap": self.sequence_gap,
            "missed_total": self.missed_total,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "last_error": self.last_error,
            "sampled_at": self.sampled_at,
            "modem_published_at": self.modem_published_at,
            "runtime_promoted_at": self.runtime_promoted_at,
        }


@dataclass(frozen=True, slots=True)
class ModemSamplePublicationAdmissionClosureReceipt:
    admission_closed_at: float
    retained_notice_identity: CoreFrozenPayload

    def __post_init__(self) -> None:
        if (
            type(self.admission_closed_at) is not float
            or self.admission_closed_at <= 0.0
        ):
            raise ValueError(
                "modem_sample_admission_closure_timestamp_invalid"
            )
        if not isinstance(
            self.retained_notice_identity,
            CoreFrozenPayload,
        ):
            raise TypeError(
                "modem_sample_admission_closure_identity_required"
            )


@dataclass(frozen=True, slots=True)
class ModemSamplePublicationDrainStatus:
    admission_open: bool
    admission_closed_at: float
    retained_notice_identity: CoreFrozenPayload
    retained_transaction_identity: CoreFrozenPayload
    internal_pending_count: int
    public_pending_count: int
    accepted_root_outbox: CoreAcceptedRootOutboxDrainStatus

    def __post_init__(self) -> None:
        if type(self.admission_open) is not bool:
            raise TypeError("modem_sample_drain_admission_bool_required")
        if (
            type(self.admission_closed_at) is not float
            or self.admission_closed_at < 0.0
        ):
            raise ValueError(
                "modem_sample_drain_admission_timestamp_invalid"
            )
        for field_name in (
            "retained_notice_identity",
            "retained_transaction_identity",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"modem_sample_drain_{field_name}_required"
                )
        for field_name in (
            "internal_pending_count",
            "public_pending_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"modem_sample_drain_{field_name}_invalid"
                )
        if not isinstance(
            self.accepted_root_outbox,
            CoreAcceptedRootOutboxDrainStatus,
        ):
            raise TypeError("modem_sample_drain_outbox_status_required")
        if self.admission_open and self.admission_closed_at != 0.0:
            raise ValueError(
                "modem_sample_drain_open_with_close_timestamp"
            )
        if not self.admission_open and self.admission_closed_at <= 0.0:
            raise ValueError(
                "modem_sample_drain_closed_without_timestamp"
            )

    @property
    def drained(self) -> bool:
        return bool(
            not self.admission_open
            and not self.retained_notice_identity.to_mapping()
            and not self.retained_transaction_identity.to_mapping()
            and self.internal_pending_count == 0
            and self.accepted_root_outbox.drained
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "admission_open": self.admission_open,
            "admission_closed_at": self.admission_closed_at,
            "retained_notice_identity": (
                self.retained_notice_identity.to_mapping()
            ),
            "retained_transaction_identity": (
                self.retained_transaction_identity.to_mapping()
            ),
            "internal_pending_count": self.internal_pending_count,
            "public_pending_count": self.public_pending_count,
            "drained": self.drained,
            "accepted_root_outbox": (
                self.accepted_root_outbox.to_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class ModemSamplePublicationNamespaceBindingReceipt:
    namespace: RuntimeGenerationNamespace
    changed: bool
    accepted_root_outbox: CoreAcceptedRootOutboxNamespaceBindingReceipt
    compute_namespace: RuntimeGenerationNamespace
    current_health: RuntimeHealthNamespaceBindingReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "modem_sample_namespace_binding_namespace_required"
            )
        if type(self.changed) is not bool:
            raise TypeError(
                "modem_sample_namespace_binding_changed_bool_required"
            )
        if not isinstance(
            self.accepted_root_outbox,
            CoreAcceptedRootOutboxNamespaceBindingReceipt,
        ):
            raise TypeError(
                "modem_sample_namespace_binding_outbox_receipt_required"
            )
        if not isinstance(
            self.compute_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "modem_sample_namespace_binding_compute_receipt_required"
            )
        if not isinstance(
            self.current_health,
            RuntimeHealthNamespaceBindingReceipt,
        ):
            raise TypeError(
                "modem_sample_namespace_binding_health_receipt_required"
            )
        if (
            self.accepted_root_outbox.namespace != self.namespace
            or self.compute_namespace != self.namespace
            or self.current_health.namespace != self.namespace
        ):
            raise ValueError(
                "modem_sample_namespace_binding_participant_mismatch"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "ModemSamplePublicationNamespaceBindingReceipt.v1",
            "owner": "runtime.modem_sample_publication",
            **self.namespace.as_dict(),
            "changed": self.changed,
            "accepted_root_outbox_binding": (
                self.accepted_root_outbox.to_payload()
            ),
            "compute_namespace": self.compute_namespace.as_dict(),
            "current_health_namespace": self.current_health.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class ModemSamplePublicationTerminationReceipt:
    lifecycle_generation: int
    shutdown_started_at: float
    admission_closed_at: float
    producer_detached: bool
    producer_inflight_callbacks: int
    retained_notice_identity: CoreFrozenPayload
    retained_transaction_identity: CoreFrozenPayload
    internal_pending_count: int
    public_pending_count: int
    outbox_remaining_records: int
    outbox_remaining_cursors: int
    outbox_remaining_inflight: int
    outbox_current_root_retained: bool
    authority_inflight_sequence: int
    private_root_disposition: CorePrivateStartupRootTerminalDisposition | None
    customer_terminal_dispositions: (
        CoreAcceptedRootTerminalDispositionBatch | None
    )
    authority_evidence: CoreFrozenPayload
    outbox_termination_evidence: CoreFrozenPayload
    outbox_status_evidence: CoreFrozenPayload
    error: str
    finished_at: float

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation <= 0:
            raise TypeError(
                "modem_sample_termination_lifecycle_generation_invalid"
            )
        if (
            type(self.shutdown_started_at) is not float
            or self.shutdown_started_at <= 0.0
            or type(self.finished_at) is not float
            or self.finished_at < self.shutdown_started_at
        ):
            raise TypeError("modem_sample_termination_timestamps_invalid")
        if type(self.admission_closed_at) is not float:
            raise TypeError(
                "modem_sample_termination_admission_timestamp_invalid"
            )
        for field_name in (
            "producer_inflight_callbacks",
            "internal_pending_count",
            "public_pending_count",
            "outbox_remaining_records",
            "outbox_remaining_cursors",
            "outbox_remaining_inflight",
            "authority_inflight_sequence",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(
                    f"modem_sample_termination_{field_name}_invalid"
                )
        for field_name in (
            "retained_notice_identity",
            "retained_transaction_identity",
            "authority_evidence",
            "outbox_termination_evidence",
            "outbox_status_evidence",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"modem_sample_termination_{field_name}_invalid"
                )
        if self.private_root_disposition is not None and not isinstance(
            self.private_root_disposition,
            CorePrivateStartupRootTerminalDisposition,
        ):
            raise TypeError(
                "modem_sample_termination_private_disposition_invalid"
            )
        if (
            self.customer_terminal_dispositions is not None
            and not isinstance(
                self.customer_terminal_dispositions,
                CoreAcceptedRootTerminalDispositionBatch,
            )
        ):
            raise TypeError(
                "modem_sample_termination_customer_dispositions_invalid"
            )
        for disposition in (
            self.private_root_disposition,
            self.customer_terminal_dispositions,
        ):
            if disposition is not None and (
                disposition.lifecycle_generation != self.lifecycle_generation
                or disposition.shutdown_started_at != self.shutdown_started_at
            ):
                raise ValueError(
                    "modem_sample_termination_disposition_identity_mismatch"
                )

    @property
    def terminated(self) -> bool:
        return bool(
            self.producer_detached
            and self.producer_inflight_callbacks == 0
            and not self.retained_notice_identity.to_mapping()
            and not self.retained_transaction_identity.to_mapping()
            and self.internal_pending_count == 0
            and self.public_pending_count == 0
            and self.outbox_remaining_records == 0
            and self.outbox_remaining_cursors == 0
            and self.outbox_remaining_inflight == 0
            and not self.outbox_current_root_retained
            and self.authority_inflight_sequence == 0
            and not self.error
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "ModemSamplePublicationTerminationReceipt.v2",
            "owner": "runtime.modem_sample_publication",
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "admission_closed_at": self.admission_closed_at,
            "producer_detached": self.producer_detached,
            "producer_inflight_callbacks": (
                self.producer_inflight_callbacks
            ),
            "retained_notice_identity": (
                self.retained_notice_identity.to_mapping()
            ),
            "retained_transaction_identity": (
                self.retained_transaction_identity.to_mapping()
            ),
            "internal_pending_count": self.internal_pending_count,
            "public_pending_count": self.public_pending_count,
            "outbox_remaining_records": self.outbox_remaining_records,
            "outbox_remaining_cursors": self.outbox_remaining_cursors,
            "outbox_remaining_inflight": self.outbox_remaining_inflight,
            "outbox_current_root_retained": (
                self.outbox_current_root_retained
            ),
            "authority_inflight_sequence": (
                self.authority_inflight_sequence
            ),
            "private_root_disposition": (
                self.private_root_disposition.to_payload()
                if self.private_root_disposition is not None
                else {}
            ),
            "customer_terminal_dispositions": (
                self.customer_terminal_dispositions.to_payload()
                if self.customer_terminal_dispositions is not None
                else {}
            ),
            "signal_processing_matrix": self.authority_evidence.to_mapping(),
            "outbox_termination": (
                self.outbox_termination_evidence.to_mapping()
            ),
            "accepted_root_outbox": (
                self.outbox_status_evidence.to_mapping()
            ),
            "error": self.error,
            "finished_at": self.finished_at,
            "terminated": self.terminated,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedRootPublicationReceipt:
    matrix_identity: MatrixGenerationIdentity
    root_fingerprint: str
    published_at: float
    disposition: str
    schema: str = "CoreAcceptedRootPublicationReceipt.v1"
    owner: str = "54guc.core.accepted_root_outbox"

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_root_publication_identity_required")
        if (
            type(self.root_fingerprint) is not str
            or len(self.root_fingerprint) != 64
            or any(
                item not in "0123456789abcdef"
                for item in self.root_fingerprint
            )
        ):
            raise TypeError("core_root_publication_fingerprint_invalid")
        if (
            type(self.published_at) is not float
            or self.published_at <= 0.0
        ):
            raise TypeError("core_root_publication_timestamp_invalid")
        if self.disposition not in {
            "published_after_product_ready",
            "already_published",
        }:
            raise ValueError("core_root_publication_disposition_invalid")


@dataclass(frozen=True, slots=True)
class _AcceptedGenerationPublicationTransaction:
    notice: ModemLivePublicationNotice
    accepted: AcceptedModemGeneration
    current_facts: CoreAcceptedCurrentFacts
    radio_transition: RadioTransitionFrame
    sequence_gap: int
    missed_total: int

    def __post_init__(self) -> None:
        if not isinstance(self.notice, ModemLivePublicationNotice):
            raise TypeError("accepted_publication_notice_required")
        if not isinstance(self.accepted, AcceptedModemGeneration):
            raise TypeError("accepted_publication_generation_required")
        if (
            not isinstance(self.current_facts, CoreAcceptedCurrentFacts)
            or self.current_facts.matrix_identity
            != self.accepted.raw.matrix_identity
        ):
            raise TypeError("accepted_publication_current_facts_required")
        if not isinstance(self.radio_transition, RadioTransitionFrame):
            raise TypeError("accepted_publication_transition_required")
        if type(self.sequence_gap) is not int or self.sequence_gap < 0:
            raise TypeError("accepted_publication_sequence_gap_invalid")
        if type(self.missed_total) is not int or self.missed_total < 0:
            raise TypeError("accepted_publication_missed_total_invalid")
        if (
            self.notice.matrix_identity
            != self.accepted.raw.matrix_identity
        ):
            raise ValueError(
                "accepted_publication_notice_generation_identity_mismatch"
            )
        if (
            self.radio_transition.source_generation
            != self.accepted.source_sequence
        ):
            raise ValueError(
                "accepted_publication_transition_generation_mismatch"
            )


def _accepted_commit_id(
    accepted: AcceptedModemGeneration,
) -> str:
    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("accepted_commit_generation_required")
    return _matrix_commit_id(accepted.raw)


def _matrix_commit_id(raw: RawModemGeneration) -> str:
    if not isinstance(raw, RawModemGeneration):
        raise TypeError("matrix_commit_raw_generation_required")
    identity = raw.matrix_identity
    scope_digest = hashlib.sha256(
        identity.session_scope_key.encode("utf-8")
    ).hexdigest()[:16]
    return (
        f"{scope_digest}:{identity.source_epoch}:"
        f"{identity.lifecycle_generation}:{identity.matrix_sequence}:"
        f"{identity.matrix_revision}"
    )


class ModemSamplePublicationFabric:
    """Coalesce notices and fan one canonical generation to every consumer."""

    def __init__(
        self,
        signal_processing_matrix: SignalProcessingMatrix,
        accepted_root_outbox: CoreAcceptedRootOutbox,
        register_bank: RuntimeRegisterBank,
    ) -> None:
        if not isinstance(signal_processing_matrix, SignalProcessingMatrix):
            raise TypeError(
                "ModemSamplePublicationFabric requires SignalProcessingMatrix"
            )
        if not isinstance(accepted_root_outbox, CoreAcceptedRootOutbox):
            raise TypeError(
                "ModemSamplePublicationFabric requires CoreAcceptedRootOutbox"
            )
        if not isinstance(register_bank, RuntimeRegisterBank):
            raise TypeError(
                "ModemSamplePublicationFabric requires RuntimeRegisterBank"
            )
        self._lock = RLock()
        self._drain_lock = RLock()
        self._admission_open = True
        self._admission_closed_at = 0.0
        self._signal_processing_matrix = signal_processing_matrix
        self._accepted_root_outbox = accepted_root_outbox
        self._register_bank = register_bank
        self._pending: ModemLivePublicationNotice | None = None
        self._unstaged_transaction: (
            _AcceptedGenerationPublicationTransaction | None
        ) = None
        self._active_namespace: RuntimeGenerationNamespace | None = None
        self._namespace_binding_receipt: (
            ModemSamplePublicationNamespaceBindingReceipt | None
        ) = None
        self._last_sequence = 0
        self._missed_total = 0
        self._wake = Event()
        self._wake_listener: Callable[[], None] | None = None
        self._wake_listener_last_error = ""
        self._public_root_admission_open = False
        self._private_startup_root: CoreAcceptedGenerationRoot | None = None
        self._live_namespace_binding_status: dict[str, Any] = {}
        self._completed_termination: ModemSamplePublicationTerminationReceipt | None = None

    def private_startup_root(self) -> CoreAcceptedGenerationRoot | None:
        with self._lock:
            return self._private_startup_root

    @property
    def active_namespace(self) -> RuntimeGenerationNamespace | None:
        with self._lock:
            return self._active_namespace

    def open_accepted_root_publication(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity,
        root_fingerprint: str,
    ) -> CoreAcceptedRootPublicationReceipt:
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_root_publication_identity_required")
        if type(root_fingerprint) is not str:
            raise TypeError("core_root_publication_fingerprint_required")
        with self._lock:
            retained = self._private_startup_root
            if retained is None:
                current = self._accepted_root_outbox.current()
                if (
                    self._public_root_admission_open
                    and current is not None
                    and current.matrix_identity == matrix_identity
                    and current.root_fingerprint == root_fingerprint
                ):
                    return CoreAcceptedRootPublicationReceipt(
                        matrix_identity=matrix_identity,
                        root_fingerprint=root_fingerprint,
                        published_at=float(time()),
                        disposition="already_published",
                    )
                raise RuntimeError("core_private_startup_root_missing")
            if (
                retained.matrix_identity != matrix_identity
                or retained.root_fingerprint != root_fingerprint
            ):
                raise RuntimeError(
                    "core_private_startup_root_proof_mismatch"
                )
            if self._public_root_admission_open:
                raise RuntimeError(
                    "core_root_publication_admission_already_open"
                )
            published = self._accepted_root_outbox.publish(retained).root
            self._public_root_admission_open = True
            self._private_startup_root = None
            wake_listener = self._wake_listener
        if wake_listener is not None:
            try:
                wake_listener()
                with self._lock:
                    self._wake_listener_last_error = ""
            except Exception as exc:
                # Publication is already committed.  A payload-free wake is
                # advisory and cannot roll back or falsify the accepted root.
                with self._lock:
                    self._wake_listener_last_error = (
                        f"{type(exc).__name__}: {exc}"
                    )
        self._wake.set()
        return CoreAcceptedRootPublicationReceipt(
            matrix_identity=published.matrix_identity,
            root_fingerprint=published.root_fingerprint,
            published_at=float(time()),
            disposition="published_after_product_ready",
        )

    def set_wake_listener(
        self,
        listener: Callable[[], None] | None,
    ) -> None:
        """Bind an optional payload-free presentation wake notification."""

        if listener is not None and not callable(listener):
            raise TypeError(
                "modem_sample_publication_wake_listener_callable_required"
            )
        with self._lock:
            self._wake_listener = listener

    def set_register_listener(
        self,
        listener: Any,
    ) -> None:
        del listener
        raise RuntimeError(
            "modem_sample_publication_mapping_listener_illegal_use_wake_listener"
        )

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> ModemSamplePublicationNamespaceBindingReceipt:
        """Bind this worker once to its exact startup-admitted namespace."""

        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "modem_sample_publication_runtime_namespace_required"
            )
        with self._lock:
            if self._active_namespace == namespace:
                receipt = self._namespace_binding_receipt
                if receipt is None:
                    raise RuntimeError(
                        "modem_sample_namespace_binding_receipt_missing"
                    )
                return replace(receipt, changed=False)
            if self._active_namespace is not None:
                raise RuntimeError(
                    "modem_sample_publication_worker_namespace_replacement_forbidden"
                )
            if self._fanout_transition_busy_locked():
                raise RuntimeError(
                    "modem_sample_publication_namespace_binding_busy"
                )
            outbox_plan = (
                self._accepted_root_outbox.prepare_live_namespace_binding(
                    namespace
                )
            )
            compute_plan = (
                self._signal_processing_matrix.prepare_runtime_namespace_binding(
                    namespace
                )
            )
            health_plan = (
                self._register_bank
                .prepare_current_health_namespace_binding(namespace)
            )
            compute_binding = None
            outbox_binding: (
                CoreAcceptedRootOutboxNamespaceBindingReceipt | None
            ) = None
            health_binding: RuntimeHealthNamespaceBindingReceipt | None = None
            compute_commit_attempted = False
            outbox_commit_attempted = False
            health_commit_attempted = False
            try:
                compute_commit_attempted = True
                compute_binding = (
                    self._signal_processing_matrix
                    .commit_runtime_namespace_binding(compute_plan)
                )
                outbox_commit_attempted = True
                outbox_binding = (
                    self._accepted_root_outbox
                    .commit_live_namespace_binding(outbox_plan)
                )
                health_commit_attempted = True
                health_binding = (
                    self._register_bank
                    .commit_current_health_namespace_binding(health_plan)
                )
            except Exception as commit_error:
                rollback_errors: list[str] = []
                if health_commit_attempted:
                    try:
                        self._register_bank.rollback_current_health_namespace_binding(
                            health_plan
                        )
                    except Exception as rollback_error:
                        rollback_errors.append(
                            "current_health:"
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                if outbox_commit_attempted:
                    try:
                        self._accepted_root_outbox.rollback_live_namespace_binding(
                            outbox_plan
                        )
                    except Exception as rollback_error:
                        rollback_errors.append(
                            "accepted_root_outbox:"
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                if compute_commit_attempted:
                    try:
                        self._signal_processing_matrix.rollback_runtime_namespace_binding(
                            compute_plan
                        )
                    except Exception as rollback_error:
                        rollback_errors.append(
                            "signal_processing_matrix:"
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                if rollback_errors:
                    raise RuntimeError(
                        "modem_sample_publication_namespace_rollback_failed:"
                        + ";".join(rollback_errors)
                    ) from commit_error
                raise
            if (
                compute_binding is None
                or outbox_binding is None
                or health_binding is None
            ):
                raise RuntimeError(
                    "modem_sample_publication_namespace_commit_incomplete"
                )
            self._reset_fanout_namespace_locked()
            self._live_namespace_binding_status = {
                "schema": "ModemSamplePublicationNamespaceBinding.v1",
                "owner": "runtime.modem_sample_publication",
                **namespace.as_dict(),
            }
            self._active_namespace = namespace
            receipt = ModemSamplePublicationNamespaceBindingReceipt(
                namespace=namespace,
                changed=True,
                accepted_root_outbox=outbox_binding,
                compute_namespace=compute_binding,
                current_health=health_binding,
            )
            self._namespace_binding_receipt = receipt
        return receipt

    def notify(
        self,
        notice: ModemLivePublicationNotice,
    ) -> ModemLivePublicationAdmissionReceipt:
        if not isinstance(notice, ModemLivePublicationNotice):
            raise TypeError("modem_sample_publication_notice_required")
        changed = False
        disposition = "retained"
        with self._lock:
            if not self._admission_open:
                raise RuntimeError(
                    "modem_sample_publication_admission_closed"
                )
            active_namespace = self._active_namespace
            if active_namespace is None:
                raise RuntimeError(
                    "modem_sample_publication_namespace_unbound"
                )
            notice_identity = notice.matrix_identity.namespace
            if notice_identity != active_namespace:
                raise RuntimeError(
                    "modem_sample_publication_namespace_mismatch"
                )
            if notice.sequence <= self._last_sequence:
                disposition = "already_consumed"
            elif self._pending is None or notice.sequence > self._pending.sequence:
                self._pending = notice
                changed = True
            else:
                disposition = "already_retained"
        if changed:
            self._wake.set()
        return ModemLivePublicationAdmissionReceipt(
            matrix_identity=notice.matrix_identity,
            disposition=disposition,
            accepted_at=float(time()),
        )

    def close_admission(
        self,
    ) -> ModemSamplePublicationAdmissionClosureReceipt:
        """Close raw notice admission; exact accepted work remains retained."""

        with self._lock:
            if self._admission_open:
                self._admission_open = False
                self._admission_closed_at = time()
            retained = self._pending
            closed_at = self._admission_closed_at
        self._wake.set()
        return ModemSamplePublicationAdmissionClosureReceipt(
            admission_closed_at=float(closed_at),
            retained_notice_identity=CoreFrozenPayload.from_mapping(
                (
                    self._notice_identity(retained)
                    if retained is not None
                    else {}
                )
            ),
        )

    def shutdown_status(self) -> ModemSamplePublicationDrainStatus:
        with self._lock:
            pending = self._pending
            transaction = self._unstaged_transaction
            admission_open = self._admission_open
            closed_at = self._admission_closed_at
        internal_pending = 1 if transaction is not None else 0
        outbox_drain = self._accepted_root_outbox.drain_status()
        public_pending = len(outbox_drain.pending_clients)
        retained_transaction = (
            {
                "session_scope_key": (
                    transaction.accepted.raw.matrix_identity.session_scope_key
                ),
                "source_epoch": (
                    transaction.accepted.raw.matrix_identity.source_epoch
                ),
                "source_sequence": transaction.accepted.source_sequence,
            }
            if transaction is not None
            else {}
        )
        return ModemSamplePublicationDrainStatus(
            admission_open=admission_open,
            admission_closed_at=float(closed_at),
            retained_notice_identity=CoreFrozenPayload.from_mapping(
                (
                    self._notice_identity(pending)
                    if pending is not None
                    else {}
                )
            ),
            retained_transaction_identity=CoreFrozenPayload.from_mapping(
                retained_transaction
            ),
            internal_pending_count=internal_pending,
            public_pending_count=public_pending,
            accepted_root_outbox=outbox_drain,
        )

    def terminate_core_owned(
        self,
        engine: Any,
        *,
        input_module: Any,
        lifecycle_generation: int,
        timeout_s: float,
    ) -> ModemSamplePublicationTerminationReceipt:
        """Close admission and prove every Core-owned generation obligation."""

        if type(lifecycle_generation) is not int or lifecycle_generation <= 0:
            raise TypeError(
                "modem_sample_termination_lifecycle_generation_required"
            )
        completed = self._completed_termination
        if completed is not None:
            if completed.lifecycle_generation != lifecycle_generation:
                raise RuntimeError("modem_sample_termination_generation_mismatch")
            return completed
        shutdown_started_at = float(time())
        deadline = shutdown_started_at + max(0.0, float(timeout_s))
        detach = getattr(
            input_module,
            "detach_live_register_publication_sink",
            None,
        )
        if not callable(detach):
            raise RuntimeError(
                "modem_live_publication_sink_detach_port_unavailable"
            )
        detach_receipt = detach(
            self.notify,
            timeout_s=max(0.0, deadline - time()),
        )
        if not isinstance(
            detach_receipt,
            ModemLivePublicationSinkDetachReceipt,
        ):
            raise TypeError(
                "modem_live_publication_sink_detach_receipt_required"
            )
        producer_detached = detach_receipt.detached
        if not producer_detached:
            readiness = self._signal_processing_matrix.termination_readiness()
            current_status = self.shutdown_status()
            outbox_status = dict(self._accepted_root_outbox.status())
            return ModemSamplePublicationTerminationReceipt(
                lifecycle_generation=lifecycle_generation,
                shutdown_started_at=shutdown_started_at,
                admission_closed_at=0.0,
                producer_detached=False,
                producer_inflight_callbacks=(
                    detach_receipt.inflight_callbacks
                ),
                retained_notice_identity=(
                    current_status.retained_notice_identity
                ),
                retained_transaction_identity=(
                    current_status.retained_transaction_identity
                ),
                internal_pending_count=(
                    current_status.internal_pending_count
                ),
                public_pending_count=(
                    current_status.public_pending_count
                ),
                outbox_remaining_records=(
                    current_status.accepted_root_outbox
                    .retained_record_count
                ),
                outbox_remaining_cursors=(
                    current_status.accepted_root_outbox
                    .retained_cursor_count
                ),
                outbox_remaining_inflight=(
                    current_status.accepted_root_outbox.inflight_count
                ),
                outbox_current_root_retained=(
                    current_status.accepted_root_outbox
                    .current_root_retained
                ),
                authority_inflight_sequence=(
                    readiness.inflight_sequence
                ),
                private_root_disposition=None,
                customer_terminal_dispositions=None,
                authority_evidence=CoreFrozenPayload.from_mapping(
                    readiness.to_payload()
                ),
                outbox_termination_evidence=CoreFrozenPayload.empty(),
                outbox_status_evidence=CoreFrozenPayload.from_mapping(
                    outbox_status
                ),
                error=(
                    "modem_live_publication_producer_detach_incomplete"
                ),
                finished_at=float(time()),
            )
        closure: (
            ModemSamplePublicationAdmissionClosureReceipt | None
        ) = None
        private_disposition: (
            CorePrivateStartupRootTerminalDisposition | None
        ) = None
        outbox_termination: (
            CoreAcceptedRootOutboxTerminationReceipt | None
        ) = None
        customer_terminal_dispositions: (
            CoreAcceptedRootTerminalDispositionBatch | None
        ) = None
        closure = self.close_admission()
        while True:
            with self._lock:
                private = self._private_startup_root
                public_open = self._public_root_admission_open
                if private is not None and not public_open:
                    private_disposition = (
                        CorePrivateStartupRootTerminalDisposition(
                            lifecycle_generation=lifecycle_generation,
                            shutdown_started_at=shutdown_started_at,
                            matrix_identity=private.matrix_identity,
                            root_fingerprint=private.root_fingerprint,
                            disposed_at=float(time()),
                        )
                    )
                    self._private_startup_root = None
            self.drain(engine, now=float(time()))
            status = self.shutdown_status()
            if status.drained:
                break
            outbox = status.accepted_root_outbox
            if (
                not status.retained_notice_identity.to_mapping()
                and not status.retained_transaction_identity.to_mapping()
                and status.internal_pending_count == 0
                and status.public_pending_count > 0
                and not outbox.admission_open
                and outbox.inflight_count == 0
            ):
                customer_terminal_dispositions = (
                    self._accepted_root_outbox
                    .terminally_disposition_pending_customers(
                        reason=(
                            "product_runtime_shutdown_after_service_revocation"
                        ),
                        lifecycle_generation=lifecycle_generation,
                        shutdown_started_at=shutdown_started_at,
                    )
                )
                if self.shutdown_status().drained:
                    break
            remaining = deadline - time()
            if remaining <= 0.0:
                break
            sleep(min(0.01, remaining))
        status = self.shutdown_status()
        if (
            not status.retained_notice_identity.to_mapping()
            and not status.retained_transaction_identity.to_mapping()
            and status.internal_pending_count == 0
            and status.public_pending_count > 0
        ):
            customer_terminal_dispositions = (
                self._accepted_root_outbox
                .terminally_disposition_pending_customers(
                    reason=(
                        "product_runtime_shutdown_after_service_revocation"
                    ),
                    lifecycle_generation=lifecycle_generation,
                    shutdown_started_at=shutdown_started_at,
                )
            )
        if self.shutdown_status().drained:
            outbox_termination = (
                self._accepted_root_outbox.finalize_termination()
            )
        authority_close = self._signal_processing_matrix.close()
        final_status = self.shutdown_status()
        outbox_status = dict(self._accepted_root_outbox.status())
        pending_disposition = authority_close.pending_disposition
        authority_evidence = {
            "closed_at": authority_close.closed_at,
            "namespace": (
                authority_close.namespace.as_dict()
                if authority_close.namespace is not None
                else {}
            ),
            "inflight_sequence": authority_close.inflight_sequence,
            "pending_disposition": (
                {
                    "identity": (
                        pending_disposition.identity.as_dict()
                    ),
                    "disposed_at": pending_disposition.disposed_at,
                    "reason": pending_disposition.reason,
                }
                if pending_disposition is not None
                else {}
            ),
        }
        termination_complete = bool(
            producer_detached
            and final_status.drained
            and not final_status.retained_notice_identity.to_mapping()
            and not final_status.retained_transaction_identity.to_mapping()
            and final_status.accepted_root_outbox.inflight_count == 0
            and not final_status.accepted_root_outbox.pending_clients
            and authority_close.inflight_sequence == 0
        )
        termination_error = (
            ""
            if termination_complete
            else "modem_sample_publication_zero_survivor_unproven"
        )
        receipt = ModemSamplePublicationTerminationReceipt(
            lifecycle_generation=lifecycle_generation,
            shutdown_started_at=shutdown_started_at,
            admission_closed_at=(
                closure.admission_closed_at
                if closure is not None
                else 0.0
            ),
            producer_detached=producer_detached,
            producer_inflight_callbacks=int(
                detach_receipt.inflight_callbacks
            ),
            retained_notice_identity=(
                final_status.retained_notice_identity
            ),
            retained_transaction_identity=(
                final_status.retained_transaction_identity
            ),
            internal_pending_count=final_status.internal_pending_count,
            public_pending_count=final_status.public_pending_count,
            outbox_remaining_records=int(
                0
                if outbox_termination is not None
                else final_status.accepted_root_outbox.retained_record_count
            ),
            outbox_remaining_cursors=int(
                0
                if outbox_termination is not None
                else final_status.accepted_root_outbox.retained_cursor_count
            ),
            outbox_remaining_inflight=int(
                0
                if outbox_termination is not None
                else final_status.accepted_root_outbox.inflight_count
            ),
            outbox_current_root_retained=bool(
                False
                if outbox_termination is not None
                else final_status.accepted_root_outbox.current_root_retained
            ),
            authority_inflight_sequence=int(
                authority_close.inflight_sequence
            ),
            private_root_disposition=private_disposition,
            customer_terminal_dispositions=(
                customer_terminal_dispositions
            ),
            authority_evidence=CoreFrozenPayload.from_mapping(
                authority_evidence
            ),
            outbox_termination_evidence=CoreFrozenPayload.from_mapping(
                (
                    outbox_termination.to_payload()
                    if outbox_termination is not None
                    else {}
                )
            ),
            outbox_status_evidence=CoreFrozenPayload.from_mapping(
                outbox_status
            ),
            error=termination_error,
            finished_at=float(time()),
        )
        if receipt.terminated:
            self._completed_termination = receipt
        return receipt

    @staticmethod
    def _notice_identity(
        notice: ModemLivePublicationNotice,
    ) -> dict[str, Any]:
        return {
            "session_scope_key": notice.matrix_identity.session_scope_key,
            "source_epoch": notice.source_epoch,
            "lifecycle_generation": notice.lifecycle_generation,
            "source_sequence": notice.sequence,
            "matrix_revision": notice.matrix_revision,
        }

    def wait_for_notice(self, timeout_s: float) -> bool:
        if timeout_s < 0.0:
            raise ValueError("modem sample publication wait cannot be negative")
        with self._lock:
            if (
                self._pending is not None
                or self._unstaged_transaction is not None
            ):
                return True
        self._wake.wait(float(timeout_s))
        self._wake.clear()
        with self._lock:
            return (
                self._pending is not None
                or self._unstaged_transaction is not None
            )

    def wake(self) -> None:
        self._wake.set()

    def _fanout_transition_busy_locked(self) -> bool:
        return bool(
            self._pending is not None
            or self._private_startup_root is not None
            or self._unstaged_transaction is not None
        )

    def _fanout_enrichment_busy_locked(self) -> bool:
        return self._fanout_transition_busy_locked()

    def _reset_fanout_namespace_locked(self) -> None:
        self._last_sequence = 0
        self._missed_total = 0

    def adopt_source_epoch(
        self,
        *,
        session_scope_key: str,
        source_epoch: str,
        expected_current_source_epoch: str,
    ) -> Mapping[str, Any]:
        del (
            session_scope_key,
            source_epoch,
            expected_current_source_epoch,
        )
        raise RuntimeError(
            "modem_sample_publication_epoch_adoption_forbidden_use_readiness_bind"
        )

    def adopt_session(
        self,
        session_scope_key: str,
        *,
        expected_current_session_scope_key: str,
    ) -> Mapping[str, Any]:
        del session_scope_key, expected_current_session_scope_key
        raise RuntimeError(
            "modem_sample_publication_session_adoption_forbidden_use_readiness_bind"
        )

    def accepted_generation_status(self) -> Mapping[str, Any]:
        authority = self._signal_processing_matrix
        authority_status = authority.status()
        with self._lock:
            namespace = self._active_namespace
            live_namespace_binding = dict(self._live_namespace_binding_status)
            unstaged = self._unstaged_transaction
            wake_listener_last_error = self._wake_listener_last_error
        accepted_root = self._accepted_root_outbox.current()
        return {
            "schema_version": "accepted_modem_generation_status.v4",
            "owner": "54guc.core.accepted_generation_root",
            "composed": True,
            "core_observation_last_delivery_identity": (
                accepted_root.commit.observation_identity.to_mapping()
                if accepted_root is not None
                else {}
            ),
            "active_namespace": (
                namespace.as_dict() if namespace is not None else {}
            ),
            "accepted_root_outbox": self._accepted_root_outbox.status(),
            "wake_listener_last_error": wake_listener_last_error,
            "live_namespace_binding": live_namespace_binding,
            "unstaged_transaction": (
                {
                    "session_scope_key": (
                        unstaged.accepted.raw.matrix_identity.session_scope_key
                    ),
                    "source_epoch": (
                        unstaged.accepted.raw.matrix_identity.source_epoch
                    ),
                    "source_sequence": unstaged.accepted.source_sequence,
                    "sequence_gap": unstaged.sequence_gap,
                    "missed_total": unstaged.missed_total,
                }
                if unstaged is not None
                else {}
            ),
            "authority": dict(authority_status or {}),
            "accepted_root": (
                {
                    **accepted_root.matrix_identity.as_dict(),
                    "commit_id": accepted_root.commit.commit_id,
                    "root_fingerprint": accepted_root.root_fingerprint,
                }
                if accepted_root is not None
                else {}
            ),
        }

    @property
    def accepted_root_outbox(self) -> CoreAcceptedRootOutbox:
        return self._accepted_root_outbox

    def drain(self, engine: Any, *, now: float) -> ModemSamplePublicationResult:
        with self._drain_lock:
            return self._drain_serialized(engine, now=now)

    def _drain_serialized(
        self,
        engine: Any,
        *,
        now: float,
    ) -> ModemSamplePublicationResult:
        started = perf_counter()
        with self._lock:
            if (
                self._private_startup_root is not None
                and not self._public_root_admission_open
            ):
                return self._result(
                    engine,
                    started,
                    ran=False,
                    published=False,
                    sequence=self._last_sequence,
                    sequence_gap=0,
                    missed_total=self._missed_total,
                    reason="private_startup_root_awaiting_product_ready",
                )
        root_consumer_failures = self._drain_required_root_consumers(
            engine
        )
        with self._lock:
            unstaged = self._unstaged_transaction
            notice = None if unstaged is not None else self._pending
            if unstaged is None:
                self._pending = None
            last_sequence = self._last_sequence
            missed_total = self._missed_total
            active_namespace = self._active_namespace
        if unstaged is not None:
            try:
                reason, last_error = self._stage_accepted_transaction(
                    engine,
                    unstaged,
                    now=now,
                )
            except Exception as exc:
                self._wake.set()
                return self._result(
                    engine,
                    started,
                    ran=True,
                    published=False,
                    sequence=last_sequence,
                    sequence_gap=0,
                    missed_total=missed_total,
                    reason="accepted_generation_stage_retry_pending",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            published = reason == "accepted"
            with self._lock:
                committed_sequence = self._last_sequence
                committed_missed = self._missed_total
            result = self._result(
                engine,
                started,
                ran=True,
                published=published,
                sequence=committed_sequence,
                sequence_gap=unstaged.sequence_gap,
                missed_total=committed_missed,
                reason=reason,
                last_error=last_error,
                sampled_at=unstaged.notice.sampled_at,
                modem_published_at=unstaged.notice.published_at,
                runtime_promoted_at=now,
            )
            return result
        if notice is None:
            # Projection retries retain customer obligations, not raw work.
            # Shutdown must close root admission before disposing those
            # obligations, even when their last projection could not finish.
            self._close_root_outbox_after_internal_drain(
                namespace=active_namespace
            )
            if root_consumer_failures:
                self._wake.set()
                return self._result(
                    engine,
                    started,
                    ran=True,
                    published=False,
                    sequence=last_sequence,
                    sequence_gap=0,
                    missed_total=missed_total,
                    reason="accepted_root_consumer_retry_pending",
                    last_error="; ".join(root_consumer_failures),
                )
            return self._result(
                engine,
                started,
                ran=True,
                published=False,
                sequence=last_sequence,
                sequence_gap=0,
                missed_total=missed_total,
                reason="no_new_modem_sample",
            )

        promoted = False
        sequence = last_sequence
        sequence_gap = 0
        try:
            matrix = notice.truth_matrix_reader()
            matrix_identity = matrix.identity
            if matrix_identity != notice.matrix_identity:
                raise RuntimeError("modem_live_notice_matrix_identity_mismatch")
            matrix_sequence = matrix_identity.matrix_sequence
            if matrix.metadata("source") != notice.source:
                raise RuntimeError("modem_live_notice_source_mismatch")
            if matrix.metadata("sampled_at") != notice.sampled_at:
                raise RuntimeError("modem_live_notice_sample_timestamp_mismatch")
            raw_generation = RawModemGeneration.capture_owner_validated(
                matrix=matrix,
                published_at=notice.published_at,
            )
            matrix_rtt = notice.matrix_rtt
            if not isinstance(matrix_rtt, MatrixBoundRttObservation):
                raise TypeError("modem_live_matrix_rtt_boundary_invalid")
            if matrix_rtt.matrix_identity != notice.matrix_identity:
                raise ValueError("modem_live_matrix_rtt_identity_mismatch")
            with self._lock:
                active_namespace = self._active_namespace
                if active_namespace is None:
                    raise RuntimeError(
                        "modem_sample_publication_namespace_unbound"
                    )
                if raw_generation.matrix_identity.namespace != active_namespace:
                    raise RuntimeError(
                        "modem_sample_publication_generation_namespace_mismatch"
                    )
            sequence = matrix_sequence
            if sequence <= last_sequence:
                return self._result(
                    engine,
                    started,
                    ran=True,
                    published=False,
                    sequence=last_sequence,
                    sequence_gap=0,
                    missed_total=missed_total,
                    reason="generation_already_consumed",
                )

            sequence_gap = max(0, sequence - last_sequence - 1) if last_sequence else 0
            missed_total += sequence_gap
            acceptance_reason = "ok"
            acceptance_error = ""
            acceptance_reason, acceptance_error = self._accept_raw_generation(
                engine,
                raw=raw_generation,
                notice=notice,
                matrix_rtt=matrix_rtt,
                now=now,
                sequence_gap=sequence_gap,
                missed_total=missed_total,
            )
            promoted = acceptance_reason == "accepted"
            with self._lock:
                committed_sequence = self._last_sequence
                committed_missed_total = self._missed_total
            return self._result(
                engine,
                started,
                ran=True,
                published=promoted,
                sequence=committed_sequence,
                sequence_gap=sequence_gap,
                missed_total=committed_missed_total,
                reason=acceptance_reason,
                last_error=acceptance_error,
                sampled_at=notice.sampled_at,
                modem_published_at=notice.published_at,
                runtime_promoted_at=now,
            )
        except Exception as exc:
            if not promoted:
                with self._lock:
                    if self._pending is None or notice.sequence > self._pending.sequence:
                        self._pending = notice
            return self._result(
                engine,
                started,
                ran=promoted,
                published=promoted,
                sequence=sequence if promoted else last_sequence,
                sequence_gap=sequence_gap if promoted else 0,
                missed_total=missed_total,
                reason=(
                    "runtime_consumer_publication_failed"
                    if promoted
                    else "modem_sample_publication_failed"
                ),
                last_error=f"{type(exc).__name__}: {exc}",
                sampled_at=notice.sampled_at if promoted else 0.0,
                modem_published_at=notice.published_at if promoted else 0.0,
                runtime_promoted_at=now if promoted else 0.0,
            )

    def _accept_raw_generation(
        self,
        engine: Any,
        *,
        raw: RawModemGeneration,
        notice: ModemLivePublicationNotice,
        matrix_rtt: MatrixBoundRttObservation,
        now: float,
        sequence_gap: int,
        missed_total: int,
    ) -> tuple[str, str]:
        authority = self._signal_processing_matrix
        advisory_errors: list[str] = []
        try:
            if not isinstance(matrix_rtt, MatrixBoundRttObservation):
                raise TypeError("modem_live_matrix_rtt_boundary_invalid")
            if matrix_rtt.matrix_identity != raw.matrix_identity:
                raise ValueError("modem_live_matrix_rtt_identity_mismatch")
            rtt_observation = matrix_rtt.observation
            times = (
                now,
                raw.published_at,
                rtt_observation.snapshot_at,
                rtt_observation.observed_at,
                rtt_observation.last_probe_at,
            )
            if any(type(value) not in {int, float} for value in times):
                raise TypeError(
                    "accepted_generation_evidence_timestamp_invalid"
                )
            accepted_at = max(times)
            publish_modem_live_matrix_generation(
                require_core_runtime_register_bank(engine),
                raw,
                commit_id=_matrix_commit_id(raw),
            )
            offered = authority.offer(
                raw,
                matrix_rtt=matrix_rtt,
            )
            if not offered.queued:
                return str(offered.reason or "generation_not_queued"), "; ".join(advisory_errors)
            processed = authority.process_latest(
                accepted_at=accepted_at
            )
            accepted = processed.accepted
            if accepted is None:
                quarantine = processed.quarantine
                error = ""
                if quarantine is not None:
                    error = f"{quarantine.error_type}: {quarantine.error_message}"
                return str(processed.status or "generation_not_accepted"), error
            transition_authority = getattr(
                engine,
                "radio_transition_authority",
                None,
            )
            observe_transition = getattr(
                transition_authority,
                "observe_accepted",
                None,
            )
            if not callable(observe_transition):
                raise RuntimeError("core_radio_transition_authority_unavailable")
            current_facts = build_core_accepted_current_facts(accepted)
            radio_transition = observe_transition(
                accepted=accepted,
                current_facts=current_facts,
                command_authority=engine.command_authority,
            )
            require_action_source_state_authority(
                engine
            ).note_accepted_radio_transition(
                radio_transition,
                observed_at=accepted.accepted_at,
            )
            transaction = _AcceptedGenerationPublicationTransaction(
                notice=notice,
                accepted=accepted,
                current_facts=current_facts,
                radio_transition=radio_transition,
                sequence_gap=sequence_gap,
                missed_total=missed_total,
            )
            with self._lock:
                if self._unstaged_transaction is not None:
                    raise RuntimeError(
                        "accepted_generation_unstaged_transaction_busy"
                    )
                self._unstaged_transaction = transaction
            try:
                reason, error = self._stage_accepted_transaction(
                    engine,
                    transaction,
                    now=now,
                )
                if advisory_errors:
                    error = "; ".join(
                        item for item in (*advisory_errors, error) if item
                    )
                return reason, error
            except Exception as exc:
                self._wake.set()
                return (
                    "accepted_generation_stage_retry_pending",
                    f"{type(exc).__name__}: {exc}",
                )
        except Exception as exc:
            self._record_consumer_fault(
                engine,
                operation="accepted_generation_processing",
                generation=raw.source_sequence,
                error=exc,
                retryable=False,
            )
            return "accepted_generation_processing_failed", f"{type(exc).__name__}: {exc}"

    def _stage_accepted_transaction(
        self,
        engine: Any,
        transaction: _AcceptedGenerationPublicationTransaction,
        *,
        now: float,
    ) -> tuple[str, str]:
        """Prepare and publish one complete immutable accepted-Core root."""

        if not isinstance(
            transaction,
            _AcceptedGenerationPublicationTransaction,
        ):
            raise TypeError(
                "accepted_generation_publication_transaction_required"
            )
        with self._lock:
            if self._unstaged_transaction is not transaction:
                raise RuntimeError(
                    "accepted_generation_publication_transaction_mismatch"
                )
        accepted_root = self._prepare_accepted_root(
            engine,
            transaction,
            now=now,
        )
        publish_signal_processing_registers(
            require_core_runtime_register_bank(engine),
            accepted_root.register_transaction,
        )
        published_root = self._publish_prepared_root(
            engine,
            accepted_root,
        )
        with self._lock:
            if self._unstaged_transaction is not transaction:
                raise RuntimeError(
                    "accepted_generation_root_commit_transaction_mismatch"
                )
            self._unstaged_transaction = None
            self._last_sequence = (
                published_root.matrix_identity.matrix_sequence
            )
            self._missed_total = transaction.missed_total
        return "accepted", ""

    def _prepare_accepted_root(
        self,
        engine: Any,
        transaction: _AcceptedGenerationPublicationTransaction,
        *,
        now: float,
    ) -> CoreAcceptedGenerationRoot:
        accepted = transaction.accepted
        commit_id = _accepted_commit_id(accepted)
        bank = require_core_runtime_register_bank(engine)
        tuning_surface = signal_tuning_surface_from_owner(
            self._signal_processing_matrix.signal_facade
        )
        register_transaction = prepare_signal_processing_registers(
            bank,
            accepted,
            commit_id=commit_id,
            signal_tuning=tuning_surface,
        )
        current_facts = transaction.current_facts
        published_at = max(float(now), accepted.accepted_at)
        frame = build_core_observation_frame(
            accepted,
            accepted_commit_id=commit_id,
            published_at=published_at,
            radio_transition=transaction.radio_transition,
            current_facts=current_facts,
        )
        runtime_view = build_core_accepted_runtime_view(
            accepted,
            frame=frame,
            current_facts=current_facts,
        )
        derived_evidence = CoreFrozenPayload.from_mapping(
            register_transaction.prepared_derived_evidence()
        )
        register_material = {
            "commit_id": register_transaction.commit_id,
            "matrix_identity": (
                register_transaction.matrix_identity.as_dict()
            ),
            "writes": [
                {
                    "name": write.name,
                    "owner": write.owner,
                    "source": write.source,
                    "updated_at": write.updated_at,
                    "valid": write.valid,
                    "stale": write.stale,
                    "dirty": write.dirty,
                    "trust": write.trust,
                    "source_kind": write.source_kind,
                    "validity": write.validity,
                    "transition": write.transition,
                    "error": write.error,
                    "signature": write.signature,
                    "force": write.force,
                    "value_sha256": write.value.sha256,
                    "value_size_bytes": write.value.size_bytes,
                }
                for write in register_transaction.writes
            ],
        }
        prepared_components = CoreFrozenPayload.from_mapping(
            {
                "register_transaction": (
                    CoreFrozenPayload.from_mapping(
                        register_material
                    ).sha256
                ),
                "observation_frame": (
                    CoreFrozenPayload.from_mapping(
                        frame.to_payload()
                    ).sha256
                ),
                "runtime_view": (
                    CoreFrozenPayload.from_mapping(
                        runtime_view.to_payload()
                    ).sha256
                ),
                "current_facts": (
                    CoreFrozenPayload.from_mapping(
                        current_facts.to_payload()
                    ).sha256
                ),
                "radio_transition": (
                    CoreFrozenPayload.from_mapping(
                        radio_transition_frame_to_mapping(
                            transaction.radio_transition
                        )
                    ).sha256
                ),
            }
        )
        commit = AcceptedGenerationCommit(
            commit_id=commit_id,
            matrix_identity=accepted.raw.matrix_identity,
            matrix_source_time=accepted.raw.sampled_at,
            matrix_published_at=accepted.raw.published_at,
            accepted_at=accepted.accepted_at,
            committed_at=published_at,
            derived_batch=derived_evidence,
            observation_identity=CoreFrozenPayload.from_mapping(
                {
                    "key": commit_id,
                    "accepted_commit_id": frame.accepted_commit_id,
                    "session_id": frame.session_id,
                    "identity_code": frame.identity_code,
                    "identity_status": frame.identity_status,
                    "source_epoch": frame.source_epoch,
                    "lifecycle_generation": frame.lifecycle_generation,
                    "source_sequence": frame.source_sequence,
                    "matrix_revision": frame.matrix_revision,
                    "sampled_at": frame.sampled_at,
                    "accepted_at": frame.accepted_at,
                    "published_at": frame.published_at,
                }
            ),
            transition_attribution=CoreFrozenPayload.from_mapping(
                radio_transition_frame_to_mapping(
                    transaction.radio_transition
                )
            ),
            prepared_components=prepared_components,
        )
        return CoreAcceptedGenerationRoot.prepare(
            commit=commit,
            register_transaction=register_transaction,
            observation_frame=frame,
            runtime_view=runtime_view,
            current_facts=current_facts,
            radio_transition=transaction.radio_transition,
            prepared_at=published_at,
        )

    def _publish_prepared_root(
        self,
        engine: Any,
        accepted_root: CoreAcceptedGenerationRoot,
    ) -> CoreAcceptedGenerationRoot:
        """Retain pre-READY privately or publish the exact root once."""

        if not isinstance(accepted_root, CoreAcceptedGenerationRoot):
            raise TypeError("core_accepted_generation_root_required")
        with self._lock:
            public_open = self._public_root_admission_open
            retained = self._private_startup_root
            if not public_open:
                if retained is not None:
                    if (
                        retained.matrix_identity
                        != accepted_root.matrix_identity
                        or retained.root_fingerprint
                        != accepted_root.root_fingerprint
                    ):
                        raise RuntimeError(
                            "core_private_startup_root_already_retained"
                        )
                    return retained
                self._private_startup_root = accepted_root
                return accepted_root
        published_root = self._accepted_root_outbox.publish(
            accepted_root
        ).root
        with self._lock:
            wake_listener = self._wake_listener
        if wake_listener is not None:
            try:
                wake_listener()
            except Exception as exc:
                self._record_consumer_fault(
                    engine,
                    operation="accepted_root_wake_listener",
                    generation=(
                        published_root.matrix_identity.matrix_sequence
                    ),
                    error=exc,
                    retryable=True,
                )
        return published_root

    def _drain_required_root_consumers(
        self,
        engine: Any,
    ) -> list[str]:
        """Advance independent retained projections from exact roots."""

        failures: list[str] = []
        for client_id in (
            CORE_REGISTER_PROJECTION_CLIENT_ID,
            CORE_COMMAND_LIFECYCLE_CLIENT_ID,
        ):
            while True:
                record = self._accepted_root_outbox.claim_next(
                    client_id
                )
                if record is None:
                    break
                try:
                    if client_id == CORE_REGISTER_PROJECTION_CLIENT_ID:
                        publish_signal_processing_registers(
                            require_core_runtime_register_bank(engine),
                            record.root.register_transaction,
                        )
                    else:
                        command_lifecycle = getattr(
                            engine,
                            "command_lifecycle",
                            None,
                        )
                        if not isinstance(
                            command_lifecycle,
                            CoreCommandLifecycleService,
                        ):
                            raise RuntimeError(
                                "core_command_lifecycle_root_consumer_unavailable"
                            )
                        result = (
                            command_lifecycle.observe_committed_generation(
                                record.root
                            )
                        )
                        if (
                            not isinstance(result, Mapping)
                            or result.get("fully_projected") is not True
                            or type(result.get("source_sequence")) is not int
                            or result.get("source_sequence")
                            != record.root.matrix_identity.matrix_sequence
                        ):
                            raise RuntimeError(
                                "core_command_lifecycle_root_projection_incomplete"
                            )
                    self._accepted_root_outbox.acknowledge(
                        client_id,
                        key=record.key,
                    )
                except Exception as exc:
                    self._accepted_root_outbox.release(
                        client_id,
                        key=record.key,
                        error=exc,
                    )
                    self._record_consumer_fault(
                        engine,
                        operation=f"accepted_root_consumer:{client_id}",
                        generation=(
                            record.root.matrix_identity.matrix_sequence
                        ),
                        error=exc,
                        retryable=True,
                    )
                    failures.append(
                        f"{client_id}:{record.key}:"
                        f"{type(exc).__name__}: {exc}"
                    )
                    break
        return failures

    def _close_root_outbox_after_internal_drain(
        self,
        *,
        namespace: RuntimeGenerationNamespace | None,
    ) -> None:
        """Close root admission after raw intake and pre-root work end.

        A constructed-but-never-started fabric has no namespace and no root
        records.  That empty state is still a real termination state; keeping
        admission open would retain Core and Foundation forever.
        """

        with self._lock:
            admission_open = self._admission_open
            retained_notice = self._pending
            retained_transaction = self._unstaged_transaction
        if (
            admission_open
            or retained_notice is not None
            or retained_transaction is not None
        ):
            return
        if namespace is None:
            outbox = self._accepted_root_outbox.drain_status()
            if (
                outbox.retained_record_count
                or outbox.retained_cursor_count
                or outbox.inflight_count
                or outbox.current_root_retained
            ):
                raise RuntimeError(
                    "unbound_modem_sample_outbox_content_forbidden"
                )
        self._accepted_root_outbox.close_admission()

    @staticmethod
    def _record_consumer_fault(
        engine: Any,
        *,
        operation: str,
        generation: int,
        error: BaseException,
        retryable: bool,
    ) -> None:
        try:
            supervisor = require_fault_supervisor(engine)
        except RuntimeError:
            return
        supervisor.record(
            boundary=FaultBoundaryClass.WORKER,
            owner="runtime.modem_sample_publication",
            operation=operation,
            generation=generation,
            severity=FaultSeverity.ERROR,
            retryable=retryable,
            degraded_behavior="consumer_receipt_retained",
            error=error,
            suppression_budget=3,
        )

    @staticmethod
    def _result(
        engine: Any,
        started: float,
        *,
        ran: bool,
        published: bool,
        sequence: int,
        sequence_gap: int,
        missed_total: int,
        reason: str,
        last_error: str = "",
        sampled_at: float = 0.0,
        modem_published_at: float = 0.0,
        runtime_promoted_at: float = 0.0,
    ) -> ModemSamplePublicationResult:
        result = ModemSamplePublicationResult(
            ran=ran,
            published=published,
            sequence=sequence,
            sequence_gap=sequence_gap,
            missed_total=missed_total,
            reason=reason,
            duration_ms=round((perf_counter() - started) * 1000.0, 3),
            last_error=last_error,
            sampled_at=float(sampled_at or 0.0),
            modem_published_at=float(modem_published_at or 0.0),
            runtime_promoted_at=float(runtime_promoted_at or 0.0),
        )
        del engine
        return result


class ModemSamplePublicationService:
    """Dedicated event-driven owner of Modem Live runtime promotion."""

    def __init__(
        self,
        *,
        core: Any,
        is_running: Callable[[], bool],
        report_error: Callable[[str], None] | None = None,
    ) -> None:
        self._core = core
        self._is_running = is_running
        self._report_error = report_error
        self._stop = Event()
        self._thread: Thread | None = None
        self._lifecycle_lock = RLock()
        self._lifecycle_state = "never_started"
        self._termination_namespace: RuntimeGenerationNamespace | None = None
        self._termination_receipt: (
            ModemSamplePublicationTerminationReceipt | None
        ) = None
        from arch4.worker_health import (
            WorkerExecutionKind,
            require_worker_health_authority,
        )

        self._worker_health = require_worker_health_authority(core)
        self._worker_id = "modem_sample_publication"
        self._worker_health.register(
            self._worker_id,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=True,
            retryable=True,
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None or self._lifecycle_state in {
                "starting",
                "running",
            }:
                raise RuntimeError(
                    "modem sample publication service is already running"
                )
            fabric = require_modem_sample_publication_fabric(self._core)
            if fabric.active_namespace is None:
                raise RuntimeError(
                    "modem_sample_publication_start_namespace_unbound"
                )
            self._termination_namespace = fabric.active_namespace
            self._termination_receipt = None
            self._stop.clear()
            candidate = Thread(
                target=self._run,
                name="runtime-modem-sample-publication",
                daemon=True,
            )
            self._lifecycle_state = "starting"
            self._thread = candidate
            self._worker_health.mark_started(self._worker_id)
            try:
                candidate.start()
            except BaseException:
                self._stop.set()
                self._thread = None
                self._lifecycle_state = "never_started"
                self._worker_health.mark_stopped(
                    self._worker_id,
                    reason="thread_start_failed_rolled_back",
                )
                raise
            if not candidate.is_alive():
                self._stop.set()
                self._thread = None
                self._lifecycle_state = "never_started"
                self._worker_health.mark_stopped(
                    self._worker_id,
                    reason="thread_failed_to_enter_running_state",
                )
                raise RuntimeError(
                    "modem_sample_publication_thread_not_running"
                )
            self._lifecycle_state = "running"

    def stop(
        self,
        *,
        timeout_s: float = 6.0,
    ) -> ModemSamplePublicationTerminationReceipt:
        timeout = max(0.0, float(timeout_s))
        deadline = time() + timeout
        with self._lifecycle_lock:
            state = self._lifecycle_state
            thread = self._thread
            cached = self._termination_receipt
            if (
                state == "stopped"
                and cached is not None
                and cached.terminated
            ):
                return cached
            if state == "never_started" and thread is None:
                raise RuntimeError(
                    "modem_sample_publication_service_never_started"
                )
            if state not in {"running", "stopping", "stopped"}:
                raise RuntimeError(
                    f"modem_sample_publication_service_state_invalid:{state}"
                )
            self._lifecycle_state = "stopping"
            self._stop.set()
        fabric = require_modem_sample_publication_fabric(self._core)
        fabric.wake()
        namespace = (
            fabric.active_namespace or self._termination_namespace
        )
        if namespace is None:
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time()))
            survived = bool(thread is not None and thread.is_alive())
            with self._lifecycle_lock:
                if not survived:
                    self._thread = None
                    self._lifecycle_state = "stopped"
            raise RuntimeError(
                "modem_sample_publication_runtime_namespace_unbound:"
                f"worker_terminated={str(not survived).lower()}"
            )
        join_reserve = min(PUBLICATION_WORKER_EXIT_RESERVE_S, timeout)
        receipt = fabric.terminate_core_owned(
            self._core,
            input_module=getattr(self._core, "input_module", None),
            lifecycle_generation=namespace.lifecycle_generation,
            timeout_s=max(0.0, deadline - time() - join_reserve),
        )
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time()))
        survived = bool(thread is not None and thread.is_alive())
        with self._lifecycle_lock:
            if receipt.terminated and not survived:
                self._thread = None
                self._lifecycle_state = "stopped"
                self._termination_receipt = receipt
            else:
                self._lifecycle_state = "stopping"
        if survived:
            receipt = replace(
                receipt,
                error=(
                    receipt.error + ";"
                    if receipt.error
                    else ""
                )
                + "modem_sample_publication_worker_survived",
                finished_at=float(time()),
            )
        with self._lifecycle_lock:
            self._termination_receipt = receipt
        return receipt

    def _run(self) -> None:
        fabric = require_modem_sample_publication_fabric(self._core)
        try:
            while True:
                stopping = self._stop.is_set() or not self._is_running()
                if stopping and fabric.active_namespace is None:
                    break
                if stopping and fabric.shutdown_status().drained:
                    break
                try:
                    if not fabric.wait_for_notice(0.1):
                        continue
                    while True:
                        stopping = (
                            self._stop.is_set() or not self._is_running()
                        )
                        if (
                            stopping
                            and fabric.shutdown_status().drained
                        ):
                            break
                        result = fabric.drain(self._core, now=time())
                        if result.last_error:
                            error = RuntimeError(result.last_error)
                            current = self._worker_health.record(self._worker_id)
                            retry_s = min(
                                5.0,
                                0.1 * (2 ** min(current.failure_streak + 1, 6)),
                            )
                            health = self._worker_health.mark_failure(
                                self._worker_id,
                                error,
                                next_retry_at=time() + retry_s,
                            )
                            require_fault_supervisor(self._core).record(
                                boundary=FaultBoundaryClass.WORKER,
                                owner=self._worker_id,
                                operation="drain_generation",
                                generation=max(0, int(result.sequence)),
                                severity=FaultSeverity.ERROR,
                                retryable=True,
                                degraded_behavior="sample_publication_retries_no_false_generation",
                                error=error,
                                suppression_budget=3,
                            )
                            if self._report_error is not None:
                                self._report_error(result.last_error)
                            wait_s = max(0.01, health.next_retry_at - time())
                            if stopping:
                                sleep(wait_s)
                            else:
                                self._stop.wait(wait_s)
                            break
                        if result.published:
                            self._worker_health.mark_success(self._worker_id)
                            continue
                        if result.reason == "generation_not_yet_readable":
                            if stopping:
                                sleep(0.01)
                            else:
                                self._stop.wait(0.01)
                            continue
                        if not result.ran and self._report_error is not None:
                            self._report_error(result.reason)
                        break
                except Exception as exc:
                    current = self._worker_health.record(self._worker_id)
                    retry_s = min(5.0, 0.1 * (2 ** min(current.failure_streak + 1, 6)))
                    self._worker_health.mark_failure(
                        self._worker_id,
                        exc,
                        next_retry_at=time() + retry_s,
                    )
                    require_fault_supervisor(self._core).record(
                        boundary=FaultBoundaryClass.WORKER,
                        owner=self._worker_id,
                        operation="publication_loop",
                        generation=0,
                        severity=FaultSeverity.ERROR,
                        retryable=True,
                        degraded_behavior="sample_publication_retries_no_false_generation",
                        error=exc,
                        suppression_budget=3,
                    )
                    if self._report_error is not None:
                        self._report_error(f"{type(exc).__name__}: {exc}")
                    if stopping:
                        sleep(retry_s)
                    else:
                        self._stop.wait(retry_s)
        finally:
            self._worker_health.mark_stopped(
                self._worker_id,
                reason="service_stop_requested"
                if self._stop.is_set()
                else "runtime_owner_stopped",
            )


def require_modem_sample_publication_fabric(core: Any) -> ModemSamplePublicationFabric:
    fabric = getattr(core, "modem_sample_publication", None)
    if not isinstance(fabric, ModemSamplePublicationFabric):
        raise RuntimeError("core_modem_sample_publication_fabric_unavailable")
    return fabric


__all__ = [
    "CoreAcceptedRootPublicationReceipt",
    "ModemSamplePublicationAdmissionClosureReceipt",
    "ModemSamplePublicationDrainStatus",
    "ModemSamplePublicationNamespaceBindingReceipt",
    "ModemSamplePublicationFabric",
    "ModemSamplePublicationResult",
    "ModemSamplePublicationService",
    "ModemSamplePublicationTerminationReceipt",
    "require_modem_sample_publication_fabric",
]
