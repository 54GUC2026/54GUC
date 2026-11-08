"""Immutable receipt for one internally committed accepted modem generation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.payload import CoreFrozenPayload


ACCEPTED_GENERATION_COMMIT_SCHEMA = "AcceptedGenerationCommit.v3"
ACCEPTED_GENERATION_COMMIT_OWNER = "54guc.core.accepted_generation_commit"


def _required(value: Any, reason: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(reason)
    return value


def _positive_time(value: Any, reason: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(reason)
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(reason)
    return result


def _exact_nonnegative_int(value: Any, reason: str) -> int:
    if type(value) is not int:
        raise TypeError(reason)
    if value < 0:
        raise ValueError(reason)
    return value


def _exact_positive_int(value: Any, reason: str) -> int:
    result = _exact_nonnegative_int(value, reason)
    if result == 0:
        raise ValueError(reason)
    return result


def _exact_optional_string(value: Any, reason: str) -> str:
    if type(value) is not str or value != value.strip():
        raise TypeError(reason)
    return value


@dataclass(frozen=True, slots=True)
class GenerationLossRange:
    session_scope_key: str
    source_epoch: str
    lifecycle_generation: int
    first_source_sequence: int
    last_source_sequence: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "session_scope_key",
            _required(
                self.session_scope_key,
                "generation_loss_session_scope_required",
            ),
        )
        object.__setattr__(
            self,
            "source_epoch",
            _required(self.source_epoch, "generation_loss_source_epoch_required"),
        )
        lifecycle = _exact_positive_int(
            self.lifecycle_generation,
            "generation_loss_lifecycle_generation_required",
        )
        object.__setattr__(self, "lifecycle_generation", lifecycle)
        first = _exact_positive_int(
            self.first_source_sequence,
            "generation_loss_range_invalid",
        )
        last = _exact_positive_int(
            self.last_source_sequence,
            "generation_loss_range_invalid",
        )
        if last < first:
            raise ValueError("generation_loss_range_invalid")
        object.__setattr__(self, "first_source_sequence", first)
        object.__setattr__(self, "last_source_sequence", last)
        object.__setattr__(
            self,
            "reason",
            _required(self.reason, "generation_loss_reason_required"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_scope_key": self.session_scope_key,
            "source_epoch": self.source_epoch,
            "lifecycle_generation": self.lifecycle_generation,
            "first_source_sequence": self.first_source_sequence,
            "last_source_sequence": self.last_source_sequence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CustomerCursorState:
    customer_id: str
    session_scope_key: str = ""
    source_epoch: str = ""
    lifecycle_generation: int = 0
    acknowledged_source_sequence: int = 0
    resync_required: bool = False
    loss_range: GenerationLossRange | None = None
    fault_count: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "customer_id",
            _required(self.customer_id, "customer_cursor_id_required"),
        )
        sequence = _exact_nonnegative_int(
            self.acknowledged_source_sequence,
            "customer_cursor_sequence_invalid",
        )
        scope = _exact_optional_string(
            self.session_scope_key,
            "customer_cursor_session_scope_invalid",
        )
        epoch = _exact_optional_string(
            self.source_epoch,
            "customer_cursor_source_epoch_invalid",
        )
        lifecycle = _exact_nonnegative_int(
            self.lifecycle_generation,
            "customer_cursor_lifecycle_invalid",
        )
        if type(self.resync_required) is not bool:
            raise TypeError("customer_cursor_resync_required_invalid")
        if sequence > 0 and (not scope or not epoch or lifecycle <= 0):
            raise ValueError("customer_cursor_namespace_required")
        if sequence == 0 and lifecycle < 0:
            raise ValueError("customer_cursor_lifecycle_invalid")
        if self.resync_required and self.loss_range is None:
            raise ValueError("customer_cursor_loss_range_required")
        if not self.resync_required and self.loss_range is not None:
            raise ValueError("customer_cursor_unexpected_loss_range")
        faults = _exact_nonnegative_int(
            self.fault_count,
            "customer_cursor_fault_count_invalid",
        )
        if type(self.last_error) is not str:
            raise TypeError("customer_cursor_last_error_invalid")
        object.__setattr__(self, "session_scope_key", scope)
        object.__setattr__(self, "source_epoch", epoch)
        object.__setattr__(self, "lifecycle_generation", lifecycle)
        object.__setattr__(self, "acknowledged_source_sequence", sequence)
        object.__setattr__(self, "fault_count", faults)
        object.__setattr__(self, "last_error", self.last_error)

    def to_payload(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "session_scope_key": self.session_scope_key,
            "source_epoch": self.source_epoch,
            "lifecycle_generation": self.lifecycle_generation,
            "acknowledged_source_sequence": self.acknowledged_source_sequence,
            "resync_required": self.resync_required,
            "loss_range": (
                self.loss_range.to_payload() if self.loss_range is not None else {}
            ),
            "fault_count": self.fault_count,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class AcceptedGenerationCommit:
    commit_id: str
    matrix_identity: MatrixGenerationIdentity
    matrix_source_time: float
    matrix_published_at: float
    accepted_at: float
    committed_at: float
    derived_batch: CoreFrozenPayload
    observation_identity: CoreFrozenPayload
    transition_attribution: CoreFrozenPayload
    prepared_components: CoreFrozenPayload
    schema: str = ACCEPTED_GENERATION_COMMIT_SCHEMA
    owner: str = ACCEPTED_GENERATION_COMMIT_OWNER

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "commit_id",
            _required(self.commit_id, "accepted_commit_id_required"),
        )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("accepted_commit_matrix_identity_required")
        sequence = self.matrix_identity.matrix_sequence
        lifecycle = self.matrix_identity.lifecycle_generation
        sampled = _positive_time(
            self.matrix_source_time,
            "accepted_commit_matrix_source_time_invalid",
        )
        published = _positive_time(
            self.matrix_published_at,
            "accepted_commit_matrix_published_at_invalid",
        )
        accepted = _positive_time(
            self.accepted_at,
            "accepted_commit_accepted_at_invalid",
        )
        committed = _positive_time(
            self.committed_at,
            "accepted_commit_committed_at_invalid",
        )
        if not sampled <= published <= accepted <= committed:
            raise ValueError("accepted_commit_timestamp_order_invalid")
        object.__setattr__(self, "matrix_source_time", sampled)
        object.__setattr__(self, "matrix_published_at", published)
        object.__setattr__(self, "accepted_at", accepted)
        object.__setattr__(self, "committed_at", committed)
        for field_name in (
            "derived_batch",
            "observation_identity",
            "transition_attribution",
            "prepared_components",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(f"accepted_commit_{field_name}_payload_required")
        derived = self.derived_batch.to_mapping()
        if set(derived) != {
            "latency",
            "quality",
            "ca",
            "prepared_registers",
        }:
            raise ValueError("accepted_commit_complete_derived_batch_required")
        for name in ("latency", "quality", "ca"):
            row = derived.get(name)
            value = row.get("value") if isinstance(row, Mapping) else None
            if (
                not isinstance(value, Mapping)
                or type(value.get("sample_sequence")) is not int
                or value.get("sample_sequence") != sequence
                or type(value.get("source_epoch")) is not str
                or value.get("source_epoch")
                != self.matrix_identity.source_epoch
                or type(value.get("session_scope_key")) is not str
                or value.get("session_scope_key")
                != self.matrix_identity.session_scope_key
                or type(value.get("lifecycle_generation")) is not int
                or value.get("lifecycle_generation")
                != lifecycle
                or type(value.get("matrix_revision")) is not str
                or value.get("matrix_revision")
                != self.matrix_identity.matrix_revision
            ):
                raise ValueError(
                    f"accepted_commit_derived_{name}_identity_mismatch"
                )
        prepared_registers = derived.get("prepared_registers")
        if (
            not isinstance(prepared_registers, Mapping)
            or set(prepared_registers) != {"names"}
            or set(prepared_registers.get("names") or ())
            != {"latency", "quality", "ca"}
        ):
            raise ValueError("accepted_commit_prepared_registers_invalid")
        identity = self.observation_identity.to_mapping()
        if (
            type(identity.get("accepted_commit_id")) is not str
            or identity.get("accepted_commit_id") != self.commit_id
            or type(identity.get("session_id")) is not str
            or identity.get("session_id")
            != self.matrix_identity.session_scope_key
            or type(identity.get("source_epoch")) is not str
            or identity.get("source_epoch")
            != self.matrix_identity.source_epoch
            or type(identity.get("lifecycle_generation")) is not int
            or identity.get("lifecycle_generation")
            != lifecycle
            or type(identity.get("source_sequence")) is not int
            or identity.get("source_sequence") != sequence
            or type(identity.get("matrix_revision")) is not str
            or identity.get("matrix_revision")
            != self.matrix_identity.matrix_revision
            or type(identity.get("sampled_at")) not in {int, float}
            or float(identity.get("sampled_at")) != sampled
            or type(identity.get("accepted_at")) not in {int, float}
            or float(identity.get("accepted_at")) != accepted
            or type(identity.get("published_at")) not in {int, float}
            or not (
                accepted
                <= float(identity.get("published_at"))
                <= committed
            )
        ):
            raise ValueError("accepted_commit_observation_identity_mismatch")
        transition = self.transition_attribution.to_mapping()
        transition_identity = transition.get("matrix_identity")
        if (
            type(transition.get("source_generation")) is not int
            or transition.get("source_generation") != self.matrix_sequence
            or not isinstance(transition_identity, Mapping)
            or MatrixGenerationIdentity.from_mapping(transition_identity)
            != self.matrix_identity
        ):
            raise ValueError("accepted_commit_transition_sequence_mismatch")
        prepared = self.prepared_components.to_mapping()
        if set(prepared) != {
            "register_transaction",
            "observation_frame",
            "runtime_view",
            "current_facts",
            "radio_transition",
        }:
            raise ValueError("accepted_commit_prepared_components_incomplete")
        for component, fingerprint in prepared.items():
            if (
                type(fingerprint) is not str
                or len(fingerprint) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in fingerprint
                )
            ):
                raise ValueError(
                    f"accepted_commit_prepared_{component}_fingerprint_invalid"
                )
        if self.schema != ACCEPTED_GENERATION_COMMIT_SCHEMA:
            raise ValueError("accepted_commit_schema_invalid")
        if self.owner != ACCEPTED_GENERATION_COMMIT_OWNER:
            raise ValueError("accepted_commit_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "commit_id": self.commit_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "matrix_source_time": self.matrix_source_time,
            "matrix_published_at": self.matrix_published_at,
            "accepted_at": self.accepted_at,
            "committed_at": self.committed_at,
            "derived_batch": self.derived_batch.to_payload(),
            "observation_identity": self.observation_identity.to_payload(),
            "transition_attribution": self.transition_attribution.to_payload(),
            "prepared_components": self.prepared_components.to_payload(),
        }

    @property
    def session_scope_key(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation

    @property
    def matrix_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def matrix_revision(self) -> str:
        return self.matrix_identity.matrix_revision


__all__ = (
    "ACCEPTED_GENERATION_COMMIT_OWNER",
    "ACCEPTED_GENERATION_COMMIT_SCHEMA",
    "AcceptedGenerationCommit",
    "CustomerCursorState",
    "GenerationLossRange",
)
