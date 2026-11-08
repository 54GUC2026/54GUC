"""Atomically own one complete accepted NGUI runtime generation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
import math
from threading import Condition, RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    ModemLiveReadyReceipt,
    RuntimeGenerationNamespace,
)

from .edit_register import EditRegisterSnapshot
from .front_register import FrontRegisterSnapshot
from .input_registers import InputRegisterSnapshot
from .runtime_ipc import (
    RuntimeCanonicalMapping,
    RuntimeFrameDeliveryReceipt,
    RuntimeInputFrame,
    runtime_frame_identity,
)
from .input_registers import canonical_ngui_input_value


NGUI_ACCEPTED_GENERATION_SCHEMA = "NGUIAcceptedGenerationState.v2"
NGUI_ACCEPTED_GENERATION_OWNER = "54guc.ngui.runtime_generation_coordinator"
NGUI_OUTBOX_MAX_PENDING_PER_KIND = 32
class NGUIAcceptedCauseKind(StrEnum):
    RUNTIME_FRAME = "runtime_frame"
    FOUNDATION_UPDATE = "foundation_update"
    LOCAL_OWNER_UPDATE = "local_owner_update"
    CHART_RESULT = "chart_result"
    ASYNC_COMPLETION = "async_completion"
    CUTOVER = "cutover"


class NGUIOutboxKind(StrEnum):
    CHART_WORK = "chart_work"
    PAGE_TRUTH = "page_truth"
    TERMINAL_DISPOSITION = "terminal_disposition"
    NOTIFICATION = "notification"
    SPEECH = "speech"


class NGUIOutboxTerminalDisposition(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


class NGUICauseEntryDisposition(StrEnum):
    ATOMIC_REENTRY = "atomic_reentry"
    EXPLICIT_NON_G02_ADAPTER_REQUIRED = "explicit_non_g02_adapter_required"


class NGUIRetiredCauseQuarantined(RuntimeError):
    """Final typed result for work from a retired namespace."""


@dataclass(frozen=True, slots=True)
class NGUIFrozenValue:
    """Immutable value retained by the accepted root."""

    encoded: bytes
    digest: str

    @classmethod
    def from_value(cls, value: Any) -> "NGUIFrozenValue":
        encoded = json.dumps(
            canonical_ngui_input_value(value, path="$.accepted_root"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return cls(encoded=encoded, digest=sha256(encoded).hexdigest())

    def __post_init__(self) -> None:
        if not isinstance(self.encoded, bytes):
            raise TypeError("ngui_frozen_value_bytes_required")
        if (
            type(self.digest) is not str
            or len(self.digest) != 64
            or self.digest != self.digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in self.digest
            )
        ):
            raise ValueError("ngui_frozen_value_digest_invalid")
        if sha256(self.encoded).hexdigest() != self.digest:
            raise ValueError("ngui_frozen_value_digest_mismatch")


@dataclass(frozen=True, slots=True)
class NGUIOutboxAdmission:
    """Full-root evidence carried by every asynchronous intent."""

    matrix_identity: MatrixGenerationIdentity
    input_generation: int
    front_sequence: int
    full_root_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("ngui_outbox_admission_matrix_identity_required")
        for field_name in ("input_generation", "front_sequence"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(
                    "ngui_outbox_admission_component_sequence_required"
                )
        if type(self.full_root_required) is not bool or not self.full_root_required:
            raise ValueError("ngui_outbox_admission_full_root_required")


@dataclass(frozen=True, slots=True)
class NGUIAcceptedGenerationIdentity:
    matrix_identity: MatrixGenerationIdentity
    coordinator_sequence: int
    cause_kind: NGUIAcceptedCauseKind
    cause_identity: str
    cause_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("ngui_generation_matrix_identity_required")
        if (
            type(self.coordinator_sequence) is not int
            or self.coordinator_sequence <= 0
        ):
            raise ValueError("ngui_generation_sequence_positive_required")
        if not isinstance(self.cause_kind, NGUIAcceptedCauseKind):
            raise TypeError("ngui_generation_cause_kind_required")
        identity = self.cause_identity
        digest = self.cause_digest
        if (
            type(identity) is not str
            or not identity
            or identity != identity.strip()
        ):
            raise ValueError("ngui_generation_cause_identity_required")
        if (
            type(digest) is not str
            or len(digest) != 64
            or digest != digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
        ):
            raise ValueError("ngui_generation_cause_digest_invalid")

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.matrix_identity.namespace

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.matrix_identity.as_dict(),
            "coordinator_sequence": self.coordinator_sequence,
            "cause_kind": self.cause_kind.value,
            "cause_identity": self.cause_identity,
            "cause_digest": self.cause_digest,
        }


@dataclass(frozen=True, slots=True)
class NGUIOutboxIntent:
    identity: NGUIAcceptedGenerationIdentity
    kind: NGUIOutboxKind
    sequence: int
    payload: NGUIOutboxAdmission
    front: FrontRegisterSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.identity, NGUIAcceptedGenerationIdentity):
            raise TypeError("ngui_outbox_generation_identity_required")
        if not isinstance(self.kind, NGUIOutboxKind):
            raise TypeError("ngui_outbox_kind_required")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("ngui_outbox_sequence_positive_required")
        if self.sequence != self.identity.coordinator_sequence:
            raise ValueError("ngui_outbox_sequence_identity_mismatch")
        if not isinstance(self.payload, NGUIOutboxAdmission):
            raise TypeError("ngui_outbox_typed_admission_required")
        if not isinstance(self.front, FrontRegisterSnapshot):
            raise TypeError("ngui_outbox_front_snapshot_required")
        if self.payload.matrix_identity != self.identity.matrix_identity:
            raise ValueError("ngui_outbox_admission_identity_mismatch")
        if (
            self.front.matrix_identity != self.identity.matrix_identity
            or self.front.sequence != self.payload.front_sequence
            or self.front.input_generation != self.payload.input_generation
        ):
            raise ValueError("ngui_outbox_front_identity_mismatch")


@dataclass(frozen=True, slots=True)
class NGUILocalCauseCommitReceipt:
    matrix_identity: MatrixGenerationIdentity
    cause_kind: NGUIAcceptedCauseKind
    cause_identity: str
    cause_digest: str
    coordinator_sequence: int
    committed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("ngui_local_cause_matrix_identity_required")
        if self.cause_kind not in {
            NGUIAcceptedCauseKind.FOUNDATION_UPDATE,
            NGUIAcceptedCauseKind.LOCAL_OWNER_UPDATE,
            NGUIAcceptedCauseKind.CHART_RESULT,
            NGUIAcceptedCauseKind.ASYNC_COMPLETION,
        }:
            raise ValueError("ngui_local_cause_kind_invalid")
        if (
            type(self.cause_identity) is not str
            or not self.cause_identity
            or self.cause_identity != self.cause_identity.strip()
        ):
            raise ValueError("ngui_local_cause_identity_required")
        if (
            type(self.cause_digest) is not str
            or len(self.cause_digest) != 64
            or self.cause_digest != self.cause_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in self.cause_digest
            )
        ):
            raise ValueError("ngui_local_cause_digest_invalid")
        if (
            type(self.coordinator_sequence) is not int
            or self.coordinator_sequence <= 0
        ):
            raise ValueError("ngui_local_cause_sequence_invalid")
        if (
            type(self.committed_at) is not float
            or not math.isfinite(self.committed_at)
            or self.committed_at <= 0.0
        ):
            raise ValueError("ngui_local_cause_timestamp_invalid")


@dataclass(frozen=True, slots=True)
class NGUIAcceptedCauseReceipt:
    matrix_identity: MatrixGenerationIdentity
    cause_kind: NGUIAcceptedCauseKind
    cause_identity: str
    cause_digest: str
    committed_coordinator_sequence: int
    delivery_receipt: (
        RuntimeFrameDeliveryReceipt | NGUILocalCauseCommitReceipt
    )

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("ngui_accepted_cause_matrix_identity_required")
        if not isinstance(self.cause_kind, NGUIAcceptedCauseKind):
            raise TypeError("ngui_accepted_cause_kind_required")
        if (
            type(self.cause_identity) is not str
            or not self.cause_identity
            or self.cause_identity != self.cause_identity.strip()
        ):
            raise ValueError("ngui_accepted_cause_identity_required")
        if (
            type(self.cause_digest) is not str
            or len(self.cause_digest) != 64
            or self.cause_digest != self.cause_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in self.cause_digest
            )
        ):
            raise ValueError("ngui_accepted_cause_digest_invalid")
        if (
            type(self.committed_coordinator_sequence) is not int
            or self.committed_coordinator_sequence <= 0
        ):
            raise ValueError(
                "ngui_accepted_cause_coordinator_sequence_required"
            )
        if not isinstance(
            self.delivery_receipt,
            (RuntimeFrameDeliveryReceipt, NGUILocalCauseCommitReceipt),
        ):
            raise TypeError("ngui_accepted_cause_receipt_required")
        if (
            self.cause_kind is NGUIAcceptedCauseKind.RUNTIME_FRAME
            and (
                not isinstance(
                    self.delivery_receipt,
                    RuntimeFrameDeliveryReceipt,
                )
                or self.delivery_receipt.matrix_identity
                != self.matrix_identity
            )
        ):
            raise TypeError(
                "ngui_runtime_cause_delivery_receipt_required"
            )
        if (
            self.cause_kind is not NGUIAcceptedCauseKind.RUNTIME_FRAME
            and (
                not isinstance(
                    self.delivery_receipt,
                    NGUILocalCauseCommitReceipt,
                )
                or self.delivery_receipt.cause_kind is not self.cause_kind
                or self.delivery_receipt.cause_identity
                != self.cause_identity
                or self.delivery_receipt.cause_digest
                != self.cause_digest
                or self.delivery_receipt.coordinator_sequence
                != self.committed_coordinator_sequence
                or self.delivery_receipt.matrix_identity
                != self.matrix_identity
            )
        ):
            raise ValueError("ngui_local_cause_receipt_identity_mismatch")


@dataclass(frozen=True, slots=True)
class NGUIOutboxClaim:
    """Opaque claim issued by the coordinator for a pending intent."""

    identity: NGUIAcceptedGenerationIdentity
    kind: NGUIOutboxKind
    sequence: int
    claim_sequence: int
    claimed_at: float
    intent: NGUIOutboxIntent
    _issuer: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, NGUIAcceptedGenerationIdentity):
            raise TypeError("ngui_outbox_claim_generation_identity_required")
        if self.kind not in {
            NGUIOutboxKind.CHART_WORK,
            NGUIOutboxKind.PAGE_TRUTH,
        }:
            raise ValueError("ngui_outbox_claim_kind_invalid")
        for field_name in ("sequence", "claim_sequence"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError("ngui_outbox_claim_sequence_invalid")
        if self.sequence != self.identity.coordinator_sequence:
            raise ValueError("ngui_outbox_claim_intent_sequence_mismatch")
        if (
            not isinstance(self.intent, NGUIOutboxIntent)
            or self.intent.identity != self.identity
            or self.intent.kind is not self.kind
            or self.intent.sequence != self.sequence
        ):
            raise ValueError("ngui_outbox_claim_intent_mismatch")
        if (
            type(self.claimed_at) is not float
            or not math.isfinite(self.claimed_at)
            or self.claimed_at <= 0.0
        ):
            raise ValueError("ngui_outbox_claim_timestamp_invalid")


@dataclass(frozen=True, slots=True)
class NGUIOutboxTerminalReceipt:
    """Final result for a coordinator-owned outbox claim."""

    identity: NGUIAcceptedGenerationIdentity
    kind: NGUIOutboxKind
    sequence: int
    claim_sequence: int
    disposition: NGUIOutboxTerminalDisposition
    completed_at: float
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, NGUIAcceptedGenerationIdentity):
            raise TypeError("ngui_outbox_terminal_generation_identity_required")
        if self.kind not in {
            NGUIOutboxKind.CHART_WORK,
            NGUIOutboxKind.PAGE_TRUTH,
        }:
            raise ValueError("ngui_outbox_terminal_kind_invalid")
        for field_name in ("sequence", "claim_sequence"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError("ngui_outbox_terminal_sequence_invalid")
        if self.sequence != self.identity.coordinator_sequence:
            raise ValueError("ngui_outbox_terminal_intent_sequence_mismatch")
        if not isinstance(
            self.disposition,
            NGUIOutboxTerminalDisposition,
        ):
            raise TypeError("ngui_outbox_terminal_disposition_required")
        if (
            type(self.completed_at) is not float
            or not math.isfinite(self.completed_at)
            or self.completed_at <= 0.0
        ):
            raise ValueError("ngui_outbox_terminal_timestamp_invalid")
        if (
            type(self.detail) is not str
            or self.detail != self.detail.strip()
        ):
            raise ValueError("ngui_outbox_terminal_detail_not_canonical")
        if (
            self.disposition is NGUIOutboxTerminalDisposition.APPLIED
            and self.detail
        ):
            raise ValueError("ngui_outbox_terminal_applied_detail_forbidden")
        if (
            self.disposition is NGUIOutboxTerminalDisposition.REJECTED
            and not self.detail
        ):
            raise ValueError("ngui_outbox_terminal_rejection_detail_required")


@dataclass(frozen=True, slots=True)
class NGUIAcceptedGenerationState:
    """Current synchronous state visible to NGUI components."""

    identity: NGUIAcceptedGenerationIdentity
    input_snapshot: InputRegisterSnapshot
    presentation_policy_state: NGUIFrozenValue
    front: FrontRegisterSnapshot
    edits: EditRegisterSnapshot
    lifecycle_state: RuntimeCanonicalMapping
    command_context: RuntimeCanonicalMapping
    chart_intake_cursor: int
    chart_outbox: tuple[NGUIOutboxIntent, ...]
    page_truth_outbox: tuple[NGUIOutboxIntent, ...]
    completion_outbox: tuple[NGUIOutboxIntent, ...]
    outbox_terminal_receipts: tuple[NGUIOutboxTerminalReceipt, ...]
    delivery_receipt: RuntimeFrameDeliveryReceipt
    accepted_causes: tuple[NGUIAcceptedCauseReceipt, ...]
    schema: str = NGUI_ACCEPTED_GENERATION_SCHEMA
    owner: str = NGUI_ACCEPTED_GENERATION_OWNER

    def __post_init__(self) -> None:
        if self.schema != NGUI_ACCEPTED_GENERATION_SCHEMA:
            raise ValueError("ngui_accepted_generation_schema_invalid")
        if self.owner != NGUI_ACCEPTED_GENERATION_OWNER:
            raise ValueError("ngui_accepted_generation_owner_invalid")
        if not isinstance(self.identity, NGUIAcceptedGenerationIdentity):
            raise TypeError("ngui_accepted_generation_identity_required")
        if not isinstance(self.input_snapshot, InputRegisterSnapshot):
            raise TypeError("ngui_accepted_generation_input_required")
        if not isinstance(self.front, FrontRegisterSnapshot):
            raise TypeError("ngui_accepted_generation_front_required")
        if not isinstance(self.edits, EditRegisterSnapshot):
            raise TypeError("ngui_accepted_generation_edits_required")
        if not isinstance(self.delivery_receipt, RuntimeFrameDeliveryReceipt):
            raise TypeError("ngui_accepted_generation_receipt_required")
        if self.delivery_receipt.matrix_identity != self.identity.matrix_identity:
            raise ValueError("ngui_accepted_generation_receipt_identity_mismatch")
        if (
            self.input_snapshot.matrix_identity != self.identity.matrix_identity
            or self.front.matrix_identity != self.identity.matrix_identity
        ):
            raise ValueError("ngui_accepted_generation_component_identity_mismatch")
        if not isinstance(self.presentation_policy_state, NGUIFrozenValue):
            raise TypeError("ngui_accepted_generation_presentation_frozen_required")
        if not isinstance(self.lifecycle_state, RuntimeCanonicalMapping):
            raise TypeError("ngui_accepted_generation_lifecycle_frozen_required")
        if not isinstance(self.command_context, RuntimeCanonicalMapping):
            raise TypeError("ngui_accepted_generation_command_frozen_required")
        if (
            type(self.chart_intake_cursor) is not int
            or self.chart_intake_cursor <= 0
            or self.chart_intake_cursor
            != self.identity.coordinator_sequence
        ):
            raise ValueError(
                "ngui_accepted_generation_chart_cursor_invalid"
            )
        for name in (
            "chart_outbox",
            "page_truth_outbox",
            "completion_outbox",
        ):
            intents = getattr(self, name)
            if not isinstance(intents, tuple):
                raise TypeError(
                    f"ngui_accepted_generation_{name}_tuple_required"
                )
            if any(
                not isinstance(intent, NGUIOutboxIntent)
                or intent.identity.namespace != self.identity.namespace
                or intent.identity.coordinator_sequence
                > self.identity.coordinator_sequence
                for intent in intents
            ):
                raise ValueError(f"ngui_accepted_generation_{name}_invalid")
        pending = (
            *self.chart_outbox,
            *self.page_truth_outbox,
            *self.completion_outbox,
        )
        pending_keys = tuple(
            (intent.identity, intent.kind, intent.sequence)
            for intent in pending
        )
        if len(pending_keys) != len(set(pending_keys)):
            raise ValueError("ngui_accepted_generation_outbox_duplicate")
        if (
            len(self.chart_outbox) > NGUI_OUTBOX_MAX_PENDING_PER_KIND
            or len(self.page_truth_outbox)
            > NGUI_OUTBOX_MAX_PENDING_PER_KIND
        ):
            raise ValueError("ngui_accepted_generation_outbox_capacity_invalid")
        if not isinstance(self.outbox_terminal_receipts, tuple):
            raise TypeError(
                "ngui_accepted_generation_terminal_receipts_tuple_required"
            )
        terminals = self.outbox_terminal_receipts
        if len(terminals) > 64 or any(
            not isinstance(item, NGUIOutboxTerminalReceipt)
            or item.identity.namespace != self.identity.namespace
            or item.identity.coordinator_sequence
            > self.identity.coordinator_sequence
            for item in terminals
        ):
            raise ValueError(
                "ngui_accepted_generation_terminal_receipts_invalid"
            )
        terminal_keys = tuple(
            (item.identity, item.kind, item.sequence)
            for item in terminals
        )
        if (
            len(terminal_keys) != len(set(terminal_keys))
            or set(terminal_keys) & set(pending_keys)
        ):
            raise ValueError(
                "ngui_accepted_generation_outbox_terminal_overlap"
            )
        if not isinstance(self.accepted_causes, tuple):
            raise TypeError(
                "ngui_accepted_generation_cause_ledger_tuple_required"
            )
        causes = self.accepted_causes
        if len(causes) > 32 or any(
            not isinstance(item, NGUIAcceptedCauseReceipt) for item in causes
        ):
            raise ValueError("ngui_accepted_generation_cause_ledger_invalid")
        cause_ids = tuple(item.cause_identity for item in causes)
        if len(cause_ids) != len(set(cause_ids)):
            raise ValueError("ngui_accepted_generation_cause_duplicate")
        if not causes:
            raise ValueError("ngui_accepted_generation_cause_required")
        newest = causes[-1]
        if (
            newest.matrix_identity != self.identity.matrix_identity
            or newest.cause_kind is not self.identity.cause_kind
            or newest.cause_identity != self.identity.cause_identity
            or newest.cause_digest != self.identity.cause_digest
            or newest.committed_coordinator_sequence
            != self.identity.coordinator_sequence
        ):
            raise ValueError(
                "ngui_accepted_generation_current_cause_mismatch"
            )
        if (
            newest.cause_kind is NGUIAcceptedCauseKind.RUNTIME_FRAME
            and newest.delivery_receipt != self.delivery_receipt
        ):
            raise ValueError(
                "ngui_accepted_generation_runtime_receipt_mismatch"
            )


@dataclass(frozen=True, slots=True)
class _NGUIRuntimeGenerationCandidate:
    """Opaque, single-use prepare receipt issued by the coordinator."""

    base_coordinator_sequence: int
    frame_identity: tuple[Any, ...]
    state: NGUIAcceptedGenerationState
    replay_receipt: RuntimeFrameDeliveryReceipt | None = None
    _issuer: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if (
            type(self.base_coordinator_sequence) is not int
            or self.base_coordinator_sequence < 0
        ):
            raise ValueError("ngui_runtime_generation_candidate_base_invalid")
        if not isinstance(self.frame_identity, tuple):
            raise TypeError("ngui_runtime_generation_candidate_frame_identity_required")
        if not isinstance(self.state, NGUIAcceptedGenerationState):
            raise TypeError("ngui_runtime_generation_candidate_state_required")
        if self.replay_receipt is not None and not isinstance(
            self.replay_receipt,
            RuntimeFrameDeliveryReceipt,
        ):
            raise TypeError("ngui_runtime_generation_candidate_replay_invalid")


@dataclass(frozen=True, slots=True)
class _NGUILocalCauseCandidate:
    base_coordinator_sequence: int
    state: NGUIAcceptedGenerationState
    _issuer: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if (
            type(self.base_coordinator_sequence) is not int
            or self.base_coordinator_sequence <= 0
        ):
            raise ValueError("ngui_local_candidate_base_invalid")
        if not isinstance(self.state, NGUIAcceptedGenerationState):
            raise TypeError("ngui_local_candidate_state_required")


@dataclass(frozen=True, slots=True)
class NGUIRuntimeGenerationPreparedComponents:
    """Frozen participant snapshots with no callbacks to live owners."""

    input_snapshot: InputRegisterSnapshot
    presentation_policy_state: NGUIFrozenValue
    front: FrontRegisterSnapshot
    edits: EditRegisterSnapshot
    lifecycle_state: RuntimeCanonicalMapping
    command_context: RuntimeCanonicalMapping

    def __post_init__(self) -> None:
        expected = (
            (self.input_snapshot, InputRegisterSnapshot, "input"),
            (self.presentation_policy_state, NGUIFrozenValue, "presentation"),
            (self.front, FrontRegisterSnapshot, "front"),
            (self.edits, EditRegisterSnapshot, "edits"),
            (self.lifecycle_state, RuntimeCanonicalMapping, "lifecycle"),
            (self.command_context, RuntimeCanonicalMapping, "command_context"),
        )
        for value, expected_type, name in expected:
            if not isinstance(value, expected_type):
                raise TypeError(
                    f"ngui_generation_prepared_{name}_type_invalid"
                )
        if (
            self.input_snapshot.matrix_identity
            != self.front.matrix_identity
        ):
            raise ValueError(
                "ngui_generation_prepared_matrix_identity_mismatch"
            )


class NGUIRuntimeGenerationCoordinator:
    """Prepare accepted NGUI state, then commit it with one assignment."""

    def __init__(
        self,
        modem_live_ready: ModemLiveReadyReceipt,
    ) -> None:
        if not isinstance(
            modem_live_ready,
            ModemLiveReadyReceipt,
        ):
            raise TypeError("ngui_runtime_generation_modem_live_ready_required")
        self._modem_live_ready = modem_live_ready
        self._active_namespace = modem_live_ready.matrix_identity.namespace
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._state: NGUIAcceptedGenerationState | None = None
        self._candidate_issuer = object()
        self._prepared_candidates: dict[
            int,
            _NGUIRuntimeGenerationCandidate,
        ] = {}
        self._local_candidate_issuer = object()
        self._prepared_local_candidates: dict[
            int,
            _NGUILocalCauseCandidate,
        ] = {}
        self._outbox_claim_issuer = object()
        self._outbox_claim_sequence = 0
        self._outbox_claims: dict[
            tuple[NGUIAcceptedGenerationIdentity, NGUIOutboxKind, int],
            NGUIOutboxClaim,
        ] = {}

    def _issue_candidate(
        self,
        *,
        base_coordinator_sequence: int,
        frame_identity: tuple[Any, ...],
        state: NGUIAcceptedGenerationState,
        replay_receipt: RuntimeFrameDeliveryReceipt | None = None,
    ) -> _NGUIRuntimeGenerationCandidate:
        candidate = _NGUIRuntimeGenerationCandidate(
            base_coordinator_sequence=base_coordinator_sequence,
            frame_identity=frame_identity,
            state=state,
            replay_receipt=replay_receipt,
            _issuer=self._candidate_issuer,
        )
        with self._lock:
            self._prepared_candidates[id(candidate)] = candidate
            while len(self._prepared_candidates) > 64:
                oldest = next(iter(self._prepared_candidates))
                self._prepared_candidates.pop(oldest, None)
        return candidate

    @staticmethod
    def cause_entry_disposition(
        kind: NGUIAcceptedCauseKind,
    ) -> NGUICauseEntryDisposition:
        if kind in {
            NGUIAcceptedCauseKind.RUNTIME_FRAME,
            NGUIAcceptedCauseKind.LOCAL_OWNER_UPDATE,
            NGUIAcceptedCauseKind.CHART_RESULT,
            NGUIAcceptedCauseKind.CUTOVER,
        }:
            return NGUICauseEntryDisposition.ATOMIC_REENTRY
        return NGUICauseEntryDisposition.EXPLICIT_NON_G02_ADAPTER_REQUIRED

    def prepare_runtime_frame(
        self,
        frame: RuntimeInputFrame,
        prepared: NGUIRuntimeGenerationPreparedComponents,
        *,
        accepted_at: float,
    ) -> _NGUIRuntimeGenerationCandidate:
        """Build synchronous state and outboxes without changing live state."""

        if not isinstance(frame, RuntimeInputFrame):
            raise TypeError("ngui_runtime_generation_frame_required")
        if not isinstance(
            prepared,
            NGUIRuntimeGenerationPreparedComponents,
        ):
            raise TypeError(
                "ngui_runtime_generation_prepared_components_required"
            )
        if type(accepted_at) is not float:
            raise TypeError("ngui_runtime_generation_timestamp_float_required")
        if not math.isfinite(accepted_at) or accepted_at <= 0.0:
            raise ValueError("ngui_runtime_generation_timestamp_required")
        with self._lock:
            previous = self._state
            base_sequence = (
                previous.identity.coordinator_sequence
                if previous is not None
                else 0
            )
            cause_key = _runtime_frame_cause_key(frame)
            digest = cause_key.removeprefix("runtime_frame:")
            if frame.namespace != self._active_namespace:
                raise NGUIRetiredCauseQuarantined(
                    "ngui_runtime_generation_foreign_namespace_quarantined"
                )
            if previous is not None:
                previous_matrix = previous.identity.matrix_identity
                if (
                    frame.matrix_identity.matrix_sequence
                    < previous_matrix.matrix_sequence
                ):
                    raise NGUIRetiredCauseQuarantined(
                        "ngui_runtime_generation_matrix_sequence_retired"
                    )
                if (
                    frame.matrix_identity.matrix_sequence
                    == previous_matrix.matrix_sequence
                    and frame.matrix_identity != previous_matrix
                ):
                    raise RuntimeError(
                        "ngui_runtime_generation_repeated_matrix_changed"
                    )
                prior = next(
                    (
                        item
                        for item in previous.accepted_causes
                        if item.cause_identity == cause_key
                    ),
                    None,
                )
                if prior is not None:
                    if prior.cause_digest != digest:
                        raise RuntimeError(
                            "ngui_runtime_generation_repeated_identity_changed"
                        )
                    return self._issue_candidate(
                        base_coordinator_sequence=base_sequence,
                        frame_identity=runtime_frame_identity(frame),
                        state=previous,
                        replay_receipt=prior.delivery_receipt,
                    )

        input_snapshot = prepared.input_snapshot
        presentation_policy = prepared.presentation_policy_state
        front = prepared.front
        if (
            front.matrix_identity != frame.matrix_identity
            or input_snapshot.matrix_identity != frame.matrix_identity
        ):
            raise RuntimeError("ngui_runtime_generation_front_identity_mismatch")
        edits = prepared.edits
        lifecycle_state = prepared.lifecycle_state
        command_context = prepared.command_context
        identity = NGUIAcceptedGenerationIdentity(
            matrix_identity=frame.matrix_identity,
            coordinator_sequence=base_sequence + 1,
            cause_kind=NGUIAcceptedCauseKind.RUNTIME_FRAME,
            cause_identity=cause_key,
            cause_digest=digest,
        )
        receipt = _prebuilt_receipt(frame)
        outbox_admission = NGUIOutboxAdmission(
            matrix_identity=frame.matrix_identity,
            input_generation=input_snapshot.generation,
            front_sequence=front.sequence,
        )
        chart_intent = NGUIOutboxIntent(
            identity=identity,
            kind=NGUIOutboxKind.CHART_WORK,
            sequence=identity.coordinator_sequence,
            payload=outbox_admission,
            front=front,
        )
        page_truth_intent = NGUIOutboxIntent(
            identity=identity,
            kind=NGUIOutboxKind.PAGE_TRUTH,
            sequence=identity.coordinator_sequence,
            payload=outbox_admission,
            front=front,
        )
        accepted_causes = (
            previous.accepted_causes
            if previous is not None
            and previous.identity.namespace == frame.namespace
            else ()
        )
        accepted_causes = (
            *accepted_causes[-31:],
            NGUIAcceptedCauseReceipt(
                matrix_identity=frame.matrix_identity,
                cause_kind=NGUIAcceptedCauseKind.RUNTIME_FRAME,
                cause_identity=cause_key,
                cause_digest=digest,
                committed_coordinator_sequence=(
                    identity.coordinator_sequence
                ),
                delivery_receipt=receipt,
            ),
        )
        state = NGUIAcceptedGenerationState(
            identity=identity,
            input_snapshot=input_snapshot,
            presentation_policy_state=presentation_policy,
            front=front,
            edits=edits,
            lifecycle_state=lifecycle_state,
            command_context=command_context,
            chart_intake_cursor=identity.coordinator_sequence,
            # Chart and PageTruth keep only latest state. A failed older intent
            # must not block the live Front.
            chart_outbox=(chart_intent,),
            page_truth_outbox=(page_truth_intent,),
            completion_outbox=(),
            outbox_terminal_receipts=(
                previous.outbox_terminal_receipts[-63:]
                if previous is not None
                else ()
            ),
            delivery_receipt=receipt,
            accepted_causes=accepted_causes,
        )
        return self._issue_candidate(
            base_coordinator_sequence=base_sequence,
            frame_identity=runtime_frame_identity(frame),
            state=state,
        )

    def commit_runtime_frame(
        self,
        candidate: _NGUIRuntimeGenerationCandidate,
    ) -> RuntimeFrameDeliveryReceipt:
        """Revalidate the base, then commit with one non-throwing assignment."""

        if not isinstance(candidate, _NGUIRuntimeGenerationCandidate):
            raise TypeError("ngui_runtime_generation_candidate_required")
        with self._changed:
            registered = self._prepared_candidates.pop(id(candidate), None)
            if (
                registered is not candidate
                or candidate._issuer is not self._candidate_issuer
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_candidate_not_coordinator_issued"
                )
            current_sequence = (
                self._state.identity.coordinator_sequence
                if self._state is not None
                else 0
            )
            if candidate.state is self._state:
                if candidate.replay_receipt is None:
                    raise RuntimeError(
                        "ngui_runtime_generation_replay_receipt_missing"
                    )
                return candidate.replay_receipt
            if candidate.base_coordinator_sequence != current_sequence:
                raise RuntimeError("ngui_runtime_generation_candidate_stale")
            receipt = _committed_receipt(
                candidate.state.delivery_receipt
            )
            causes = (
                *candidate.state.accepted_causes[:-1],
                replace(
                    candidate.state.accepted_causes[-1],
                    delivery_receipt=receipt,
                ),
            )
            self._state = replace(
                candidate.state,
                delivery_receipt=receipt,
                accepted_causes=causes,
            )
            self._changed.notify_all()
            return receipt

    def prepare_local_owner_update(
        self,
        *,
        cause_identity: str,
        cause_digest: str,
        prepared: NGUIRuntimeGenerationPreparedComponents,
        accepted_at: float,
    ) -> _NGUILocalCauseCandidate:
        """Build the next local-owner state without changing live state."""

        if not isinstance(
            prepared,
            NGUIRuntimeGenerationPreparedComponents,
        ):
            raise TypeError(
                "ngui_local_generation_prepared_components_required"
            )
        if (
            type(cause_identity) is not str
            or not cause_identity
            or cause_identity != cause_identity.strip()
        ):
            raise ValueError("ngui_local_generation_cause_identity_required")
        if (
            type(cause_digest) is not str
            or len(cause_digest) != 64
            or cause_digest != cause_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in cause_digest
            )
        ):
            raise ValueError("ngui_local_generation_cause_digest_invalid")
        if (
            type(accepted_at) is not float
            or not math.isfinite(accepted_at)
            or accepted_at <= 0.0
        ):
            raise ValueError("ngui_local_generation_timestamp_invalid")
        with self._lock:
            previous = self._state
            if previous is None:
                raise RuntimeError("ngui_local_generation_runtime_root_required")
            if any(
                item.cause_identity == cause_identity
                for item in previous.accepted_causes
            ):
                raise RuntimeError("ngui_local_generation_cause_replayed")
            base_sequence = previous.identity.coordinator_sequence
            matrix_identity = previous.identity.matrix_identity
            if (
                prepared.input_snapshot.matrix_identity != matrix_identity
                or prepared.front.matrix_identity != matrix_identity
            ):
                raise RuntimeError(
                    "ngui_local_generation_matrix_identity_mismatch"
                )
            if (
                prepared.input_snapshot.generation
                < previous.input_snapshot.generation
                or prepared.front.input_generation
                != prepared.input_snapshot.generation
            ):
                raise RuntimeError(
                    "ngui_local_generation_component_regression"
                )
            identity = NGUIAcceptedGenerationIdentity(
                matrix_identity=matrix_identity,
                coordinator_sequence=base_sequence + 1,
                cause_kind=NGUIAcceptedCauseKind.LOCAL_OWNER_UPDATE,
                cause_identity=cause_identity,
                cause_digest=cause_digest,
            )
            local_receipt = NGUILocalCauseCommitReceipt(
                matrix_identity=matrix_identity,
                cause_kind=identity.cause_kind,
                cause_identity=identity.cause_identity,
                cause_digest=identity.cause_digest,
                coordinator_sequence=identity.coordinator_sequence,
                committed_at=accepted_at,
            )
            admission = NGUIOutboxAdmission(
                matrix_identity=matrix_identity,
                input_generation=prepared.input_snapshot.generation,
                front_sequence=prepared.front.sequence,
            )
            chart_intent = NGUIOutboxIntent(
                identity=identity,
                kind=NGUIOutboxKind.CHART_WORK,
                sequence=identity.coordinator_sequence,
                payload=admission,
                front=prepared.front,
            )
            page_truth_intent = NGUIOutboxIntent(
                identity=identity,
                kind=NGUIOutboxKind.PAGE_TRUTH,
                sequence=identity.coordinator_sequence,
                payload=admission,
                front=prepared.front,
            )
            cause = NGUIAcceptedCauseReceipt(
                matrix_identity=matrix_identity,
                cause_kind=identity.cause_kind,
                cause_identity=identity.cause_identity,
                cause_digest=identity.cause_digest,
                committed_coordinator_sequence=(
                    identity.coordinator_sequence
                ),
                delivery_receipt=local_receipt,
            )
            state = replace(
                previous,
                identity=identity,
                input_snapshot=prepared.input_snapshot,
                presentation_policy_state=(
                    prepared.presentation_policy_state
                ),
                front=prepared.front,
                edits=prepared.edits,
                lifecycle_state=prepared.lifecycle_state,
                command_context=prepared.command_context,
                chart_intake_cursor=identity.coordinator_sequence,
                chart_outbox=(chart_intent,),
                page_truth_outbox=(page_truth_intent,),
                accepted_causes=(
                    *previous.accepted_causes[-31:],
                    cause,
                ),
            )
            return self._issue_local_candidate(
                base_coordinator_sequence=base_sequence,
                state=state,
            )

    def prepare_chart_result(
        self,
        *,
        cause_identity: str,
        cause_digest: str,
        prepared: NGUIRuntimeGenerationPreparedComponents,
        accepted_at: float,
    ) -> _NGUILocalCauseCandidate:
        """Build a chart-result successor that only affects PageTruth."""

        if not isinstance(
            prepared,
            NGUIRuntimeGenerationPreparedComponents,
        ):
            raise TypeError(
                "ngui_chart_generation_prepared_components_required"
            )
        if (
            type(cause_identity) is not str
            or not cause_identity
            or cause_identity != cause_identity.strip()
        ):
            raise ValueError("ngui_chart_generation_cause_identity_required")
        if (
            type(cause_digest) is not str
            or len(cause_digest) != 64
            or cause_digest != cause_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in cause_digest
            )
        ):
            raise ValueError("ngui_chart_generation_cause_digest_invalid")
        if (
            type(accepted_at) is not float
            or not math.isfinite(accepted_at)
            or accepted_at <= 0.0
        ):
            raise ValueError("ngui_chart_generation_timestamp_invalid")
        with self._lock:
            previous = self._state
            if previous is None:
                raise RuntimeError("ngui_chart_generation_runtime_root_required")
            if any(
                item.cause_identity == cause_identity
                for item in previous.accepted_causes
            ):
                raise RuntimeError("ngui_chart_generation_cause_replayed")
            if (
                prepared.input_snapshot != previous.input_snapshot
                or prepared.presentation_policy_state
                != previous.presentation_policy_state
                or prepared.lifecycle_state != previous.lifecycle_state
                or prepared.command_context != previous.command_context
            ):
                raise RuntimeError(
                    "ngui_chart_generation_non_chart_component_changed"
                )
            if (
                prepared.front.matrix_identity
                != previous.identity.matrix_identity
                or prepared.front.input_generation
                != previous.input_snapshot.generation
                or prepared.front.sequence != previous.front.sequence + 1
                or prepared.front.fields != previous.front.fields
                or prepared.front.pages != previous.front.pages
            ):
                raise RuntimeError(
                    "ngui_chart_generation_front_successor_invalid"
                )
            base_sequence = previous.identity.coordinator_sequence
            identity = NGUIAcceptedGenerationIdentity(
                matrix_identity=previous.identity.matrix_identity,
                coordinator_sequence=base_sequence + 1,
                cause_kind=NGUIAcceptedCauseKind.CHART_RESULT,
                cause_identity=cause_identity,
                cause_digest=cause_digest,
            )
            local_receipt = NGUILocalCauseCommitReceipt(
                matrix_identity=identity.matrix_identity,
                cause_kind=identity.cause_kind,
                cause_identity=identity.cause_identity,
                cause_digest=identity.cause_digest,
                coordinator_sequence=identity.coordinator_sequence,
                committed_at=accepted_at,
            )
            admission = NGUIOutboxAdmission(
                matrix_identity=identity.matrix_identity,
                input_generation=prepared.input_snapshot.generation,
                front_sequence=prepared.front.sequence,
            )
            page_truth_intent = NGUIOutboxIntent(
                identity=identity,
                kind=NGUIOutboxKind.PAGE_TRUTH,
                sequence=identity.coordinator_sequence,
                payload=admission,
                front=prepared.front,
            )
            cause = NGUIAcceptedCauseReceipt(
                matrix_identity=identity.matrix_identity,
                cause_kind=identity.cause_kind,
                cause_identity=identity.cause_identity,
                cause_digest=identity.cause_digest,
                committed_coordinator_sequence=(
                    identity.coordinator_sequence
                ),
                delivery_receipt=local_receipt,
            )
            state = replace(
                previous,
                identity=identity,
                front=prepared.front,
                edits=prepared.edits,
                chart_intake_cursor=identity.coordinator_sequence,
                page_truth_outbox=(page_truth_intent,),
                accepted_causes=(
                    *previous.accepted_causes[-31:],
                    cause,
                ),
            )
            return self._issue_local_candidate(
                base_coordinator_sequence=base_sequence,
                state=state,
            )

    def _issue_local_candidate(
        self,
        *,
        base_coordinator_sequence: int,
        state: NGUIAcceptedGenerationState,
    ) -> _NGUILocalCauseCandidate:
        candidate = _NGUILocalCauseCandidate(
            base_coordinator_sequence=base_coordinator_sequence,
            state=state,
            _issuer=self._local_candidate_issuer,
        )
        self._prepared_local_candidates[id(candidate)] = candidate
        while len(self._prepared_local_candidates) > 64:
            oldest = next(iter(self._prepared_local_candidates))
            self._prepared_local_candidates.pop(oldest, None)
        return candidate

    def commit_local_owner_update(
        self,
        candidate: _NGUILocalCauseCandidate,
    ) -> NGUILocalCauseCommitReceipt:
        """Commit the prepared local-owner state."""

        if (
            not isinstance(candidate, _NGUILocalCauseCandidate)
            or candidate.state.identity.cause_kind
            is not NGUIAcceptedCauseKind.LOCAL_OWNER_UPDATE
        ):
            raise TypeError("ngui_local_generation_candidate_required")
        return self.commit_atomic_reentry(candidate)

    def commit_atomic_reentry(
        self,
        candidate: _NGUILocalCauseCandidate,
    ) -> NGUILocalCauseCommitReceipt:
        """Commit non-runtime state with one root assignment."""

        if not isinstance(candidate, _NGUILocalCauseCandidate):
            raise TypeError("ngui_atomic_reentry_candidate_required")
        with self._changed:
            registered = self._prepared_local_candidates.pop(
                id(candidate),
                None,
            )
            if (
                registered is not candidate
                or candidate._issuer is not self._local_candidate_issuer
            ):
                raise RuntimeError(
                    "ngui_local_generation_candidate_not_coordinator_issued"
                )
            if self._state is None or (
                self._state.identity.coordinator_sequence
                != candidate.base_coordinator_sequence
            ):
                raise RuntimeError("ngui_local_generation_candidate_stale")
            receipt = candidate.state.accepted_causes[-1].delivery_receipt
            if not isinstance(receipt, NGUILocalCauseCommitReceipt):
                raise RuntimeError(
                    "ngui_local_generation_commit_receipt_missing"
                )
            self._state = candidate.state
            self._changed.notify_all()
            return receipt

    def claim_outbox(
        self,
        kind: NGUIOutboxKind,
        *,
        claimed_at: float,
    ) -> NGUIOutboxClaim | None:
        """Claim the oldest free chart or PageTruth intent once."""

        if kind not in {
            NGUIOutboxKind.CHART_WORK,
            NGUIOutboxKind.PAGE_TRUTH,
        }:
            raise ValueError("ngui_runtime_generation_claim_kind_invalid")
        if (
            type(claimed_at) is not float
            or not math.isfinite(claimed_at)
            or claimed_at <= 0.0
        ):
            raise ValueError(
                "ngui_runtime_generation_claim_timestamp_invalid"
            )
        with self._lock:
            if self._state is None:
                raise RuntimeError("ngui_runtime_generation_uncommitted")
            pending = (
                self._state.chart_outbox
                if kind is NGUIOutboxKind.CHART_WORK
                else self._state.page_truth_outbox
            )
            for intent in pending:
                key = (intent.identity, intent.kind, intent.sequence)
                if key in self._outbox_claims:
                    continue
                self._outbox_claim_sequence += 1
                claim = NGUIOutboxClaim(
                    identity=intent.identity,
                    kind=intent.kind,
                    sequence=intent.sequence,
                    claim_sequence=self._outbox_claim_sequence,
                    claimed_at=claimed_at,
                    intent=intent,
                    _issuer=self._outbox_claim_issuer,
                )
                self._outbox_claims[key] = claim
                return claim
            return None

    def complete_outbox(
        self,
        claim: NGUIOutboxClaim,
        *,
        disposition: NGUIOutboxTerminalDisposition,
        completed_at: float,
        detail: str = "",
    ) -> NGUIOutboxTerminalReceipt:
        """Remove one claimed intent and keep its final receipt."""

        if not isinstance(claim, NGUIOutboxClaim):
            raise TypeError("ngui_runtime_generation_outbox_claim_required")
        if not isinstance(
            disposition,
            NGUIOutboxTerminalDisposition,
        ):
            raise TypeError(
                "ngui_runtime_generation_terminal_disposition_required"
            )
        terminal = NGUIOutboxTerminalReceipt(
            identity=claim.identity,
            kind=claim.kind,
            sequence=claim.sequence,
            claim_sequence=claim.claim_sequence,
            disposition=disposition,
            completed_at=completed_at,
            detail=detail,
        )
        key = (claim.identity, claim.kind, claim.sequence)
        with self._changed:
            registered = self._outbox_claims.get(key)
            if (
                registered is not claim
                or claim._issuer is not self._outbox_claim_issuer
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_outbox_claim_not_issued"
                )
            if self._state is None:
                raise RuntimeError("ngui_runtime_generation_uncommitted")
            source = (
                self._state.chart_outbox
                if claim.kind is NGUIOutboxKind.CHART_WORK
                else self._state.page_truth_outbox
            )
            matched = tuple(
                intent
                for intent in source
                if (
                    intent.identity == claim.identity
                    and intent.kind is claim.kind
                    and intent.sequence == claim.sequence
                    and intent is claim.intent
                )
            )
            if len(matched) != 1:
                raise RuntimeError(
                    "ngui_runtime_generation_claimed_intent_missing"
                )
            remaining = tuple(
                intent for intent in source if intent is not matched[0]
            )
            chart_outbox = (
                remaining
                if claim.kind is NGUIOutboxKind.CHART_WORK
                else self._state.chart_outbox
            )
            page_truth_outbox = (
                remaining
                if claim.kind is NGUIOutboxKind.PAGE_TRUTH
                else self._state.page_truth_outbox
            )
            causes = list(self._state.accepted_causes)
            cause_index = next(
                (
                    index
                    for index, cause in enumerate(causes)
                    if (
                        cause.committed_coordinator_sequence
                        == claim.identity.coordinator_sequence
                        and cause.cause_identity
                        == claim.identity.cause_identity
                        and cause.cause_digest
                        == claim.identity.cause_digest
                    )
                ),
                -1,
            )
            if cause_index < 0:
                raise RuntimeError(
                    "ngui_runtime_generation_claim_cause_missing"
                )
            cause_receipt = causes[cause_index].delivery_receipt
            retained_terminals = (
                *self._state.outbox_terminal_receipts[-63:],
                terminal,
            )
            identity_pending = any(
                intent.identity == claim.identity
                for intent in (*chart_outbox, *page_truth_outbox)
            )
            identity_terminals = {
                item.kind: item
                for item in retained_terminals
                if item.identity == claim.identity
            }
            all_effects_applied = bool(
                not identity_pending
                and set(identity_terminals)
                == {
                    NGUIOutboxKind.CHART_WORK,
                    NGUIOutboxKind.PAGE_TRUTH,
                }
                and all(
                    item.disposition
                    is NGUIOutboxTerminalDisposition.APPLIED
                    for item in identity_terminals.values()
                )
            )
            if all_effects_applied and isinstance(
                cause_receipt,
                RuntimeFrameDeliveryReceipt,
            ):
                cause_receipt = replace(
                    cause_receipt,
                    chart_history_accepted=True,
                )
                causes[cause_index] = replace(
                    causes[cause_index],
                    delivery_receipt=cause_receipt,
                )
            delivery_receipt = (
                cause_receipt
                if (
                    claim.identity == self._state.identity
                    and isinstance(
                        cause_receipt,
                        RuntimeFrameDeliveryReceipt,
                    )
                )
                else self._state.delivery_receipt
            )
            self._state = replace(
                self._state,
                chart_outbox=chart_outbox,
                page_truth_outbox=page_truth_outbox,
                outbox_terminal_receipts=retained_terminals,
                delivery_receipt=delivery_receipt,
                accepted_causes=tuple(causes),
            )
            self._outbox_claims.pop(key, None)
            self._changed.notify_all()
            return terminal

    def release_outbox_claim(
        self,
        claim: NGUIOutboxClaim,
    ) -> None:
        """Release retryable work without losing its intent."""

        if not isinstance(claim, NGUIOutboxClaim):
            raise TypeError("ngui_runtime_generation_outbox_claim_required")
        key = (claim.identity, claim.kind, claim.sequence)
        with self._lock:
            registered = self._outbox_claims.get(key)
            if (
                registered is not claim
                or claim._issuer is not self._outbox_claim_issuer
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_outbox_claim_not_issued"
                )
            self._outbox_claims.pop(key, None)

    def terminal_delivery_receipt(
        self,
        identity: NGUIAcceptedGenerationIdentity,
    ) -> RuntimeFrameDeliveryReceipt:
        """Return a frame receipt after chart and PageTruth are final."""

        if not isinstance(identity, NGUIAcceptedGenerationIdentity):
            raise TypeError(
                "ngui_runtime_generation_delivery_identity_required"
            )
        with self._lock:
            if self._state is None:
                raise RuntimeError("ngui_runtime_generation_uncommitted")
            if any(
                intent.identity == identity
                for intent in (
                    *self._state.chart_outbox,
                    *self._state.page_truth_outbox,
                )
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_delivery_pending"
                )
            terminals = {
                item.kind: item
                for item in self._state.outbox_terminal_receipts
                if item.identity == identity
            }
            if (
                set(terminals)
                != {
                    NGUIOutboxKind.CHART_WORK,
                    NGUIOutboxKind.PAGE_TRUTH,
                }
                or any(
                    item.disposition
                    is not NGUIOutboxTerminalDisposition.APPLIED
                    for item in terminals.values()
                )
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_delivery_not_applied"
                )
            cause = next(
                (
                    item
                    for item in self._state.accepted_causes
                    if (
                        item.committed_coordinator_sequence
                        == identity.coordinator_sequence
                        and item.cause_identity == identity.cause_identity
                        and item.cause_digest == identity.cause_digest
                    )
                ),
                None,
            )
            if (
                cause is None
                or not isinstance(
                    cause.delivery_receipt,
                    RuntimeFrameDeliveryReceipt,
                )
                or not cause.delivery_receipt.frame_applied
            ):
                raise RuntimeError(
                    "ngui_runtime_generation_delivery_receipt_incomplete"
                )
            return cause.delivery_receipt

    def snapshot(self) -> NGUIAcceptedGenerationState:
        with self._lock:
            if self._state is None:
                raise RuntimeError("ngui_runtime_generation_uncommitted")
            return self._state

    def snapshot_for(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> NGUIAcceptedGenerationState:
        state = self.snapshot()
        if state.identity.namespace != namespace:
            raise RuntimeError("ngui_runtime_generation_namespace_not_active")
        return state


class NGUIInputRegisterView:
    """Read-only Input view over coordinator state."""

    def __init__(self, coordinator: NGUIRuntimeGenerationCoordinator) -> None:
        self._coordinator = coordinator

    def snapshot(self) -> InputRegisterSnapshot:
        return self._coordinator.snapshot().input_snapshot


class NGUIFrontRegisterView:
    """Read-only Front view over coordinator state."""

    def __init__(self, coordinator: NGUIRuntimeGenerationCoordinator) -> None:
        self._coordinator = coordinator

    def snapshot(self) -> FrontRegisterSnapshot:
        return self._coordinator.snapshot().front


def _runtime_frame_cause_key(frame: RuntimeInputFrame) -> str:
    identity = {
        "frame_sequence": frame.sequence,
        "accepted_commit_id": frame.accepted_commit_id,
        "matrix_identity": frame.matrix_identity.as_dict(),
        "accepted_root_fingerprint": (
            frame.accepted_root_fingerprint
        ),
        "core_snapshot_sequence": frame.snapshot_sequence,
        "foundation_extensions": tuple(
            (provider_id, sequence)
            for provider_id, sequence, _digest in frame.extension_vector
        ),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "runtime_frame:" + sha256(encoded).hexdigest()


def _prebuilt_receipt(frame: RuntimeInputFrame) -> RuntimeFrameDeliveryReceipt:
    from .runtime_ipc import build_runtime_frame_delivery_receipt

    return build_runtime_frame_delivery_receipt(
        frame,
        front_register_applied=False,
        chart_history_accepted=False,
    )


def _committed_receipt(
    prepared: RuntimeFrameDeliveryReceipt,
) -> RuntimeFrameDeliveryReceipt:
    """Commit synchronous state while the chart remains pending."""

    if not isinstance(prepared, RuntimeFrameDeliveryReceipt):
        raise TypeError("ngui_runtime_generation_prepared_receipt_required")
    return replace(
        prepared,
        front_register_applied=True,
        chart_history_accepted=False,
    )


__all__ = (
    "NGUI_ACCEPTED_GENERATION_OWNER",
    "NGUI_ACCEPTED_GENERATION_SCHEMA",
    "NGUIAcceptedCauseKind",
    "NGUIAcceptedCauseReceipt",
    "NGUIAcceptedGenerationIdentity",
    "NGUIAcceptedGenerationState",
    "NGUICauseEntryDisposition",
    "NGUIFrozenValue",
    "NGUIFrontRegisterView",
    "NGUIInputRegisterView",
    "NGUILocalCauseCommitReceipt",
    "NGUIOutboxIntent",
    "NGUIOutboxAdmission",
    "NGUIOutboxClaim",
    "NGUIOutboxKind",
    "NGUIOutboxTerminalDisposition",
    "NGUIOutboxTerminalReceipt",
    "NGUIRuntimeGenerationPreparedComponents",
    "NGUIRuntimeGenerationCoordinator",
    "NGUIRetiredCauseQuarantined",
)
