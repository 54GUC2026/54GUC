"""Atomic NGUI input register bank for several owners."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from threading import Condition, RLock
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping

from arch4.core_contracts.current_state import CoreCurrentFieldState
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from .registry import NGUI_INPUT_SOURCE_REGISTRY, input_source_spec
from .modem_live_matrix_view import (
    MODEM_LIVE_NGUI_CELL_ADDRESSES,
)
from .types import FreshnessState, SourceOwner


class _FrozenSimpleNamespace(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        object.__setattr__(self, "_frozen", False)
        super().__init__(**kwargs)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise TypeError("ngui_input_namespace_is_immutable")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_frozen", False):
            raise TypeError("ngui_input_namespace_is_immutable")
        super().__delattr__(name)


_LOCAL_PUBLISH_OWNERS = frozenset(
    {
        SourceOwner.GUI_UTILITY,
        SourceOwner.GUI_EDIT,
        SourceOwner.GUI_ACTION,
        SourceOwner.GUI_VOICE,
        SourceOwner.GUI_EVENT,
        SourceOwner.PROFILE_SELECTION,
        SourceOwner.PROFILE_DISCOVERY,
        SourceOwner.LOGIN_LIFECYCLE,
        SourceOwner.FOUNDATION_LICENSE,
    }
)
_COMPLETE_LOCAL_OWNER_SOURCES = {
    owner: frozenset(
        source_id
        for source_id, spec in NGUI_INPUT_SOURCE_REGISTRY.items()
        if spec.owner is owner
    )
    for owner in _LOCAL_PUBLISH_OWNERS
}


@dataclass(frozen=True, slots=True)
class InputValue:
    source_id: str
    value: Any
    valid: bool
    sampled_at: float
    sample_sequence: int
    freshness: FreshnessState = FreshnessState.FRESH
    reason: str = ""
    fact_state: CoreCurrentFieldState = CoreCurrentFieldState.AVAILABLE

    def __post_init__(self) -> None:
        source_id = str(self.source_id or "").strip()
        if not source_id:
            raise ValueError("input_value_source_id_required")
        if type(self.valid) is not bool:
            raise TypeError("input_value_valid_bool_required")
        if type(self.sampled_at) not in {int, float}:
            raise TypeError("input_value_sampled_at_number_required")
        sampled_at = float(self.sampled_at)
        if (
            not math.isfinite(sampled_at)
            or sampled_at < 0.0
            or (self.valid and sampled_at <= 0.0)
        ):
            raise ValueError("input_value_sampled_at_invalid")
        if (
            type(self.sample_sequence) is not int
            or self.sample_sequence < 0
            or (self.valid and self.sample_sequence <= 0)
        ):
            raise ValueError("input_value_sample_sequence_invalid")
        if not isinstance(self.freshness, FreshnessState):
            raise TypeError("input_value_freshness_required")
        if not isinstance(self.fact_state, CoreCurrentFieldState):
            raise TypeError("input_value_fact_state_required")
        reason = str(self.reason or "")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(
            self,
            "value",
            freeze_ngui_input_value(
                normalize_ngui_input_value(
                    self.value,
                    path=f"$.{source_id}",
                )
            ),
        )
        object.__setattr__(self, "sampled_at", sampled_at)
        object.__setattr__(self, "reason", reason)

    def __reduce__(self):
        """Rebuild an immutable value after process transport.

        ``MappingProxyType`` prevents in-process mutation, but Windows
        multiprocessing cannot pickle it. Send a plain structural copy. The
        constructor validates and freezes it again in the receiving process.
        """

        return (
            type(self),
            (
                self.source_id,
                _pickle_safe_ngui_input_value(self.value),
                self.valid,
                self.sampled_at,
                self.sample_sequence,
                self.freshness,
                self.reason,
                self.fact_state,
            ),
        )


@dataclass(frozen=True, slots=True)
class OwnerInputBatch:
    owner: SourceOwner
    sequence: int
    published_at: float
    values: tuple[InputValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.owner, SourceOwner):
            raise TypeError("owner batch owner must be SourceOwner")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("owner batch sequence must be positive")
        if type(self.published_at) not in {int, float}:
            raise TypeError("owner batch published_at must be numeric")
        published_at = float(self.published_at)
        if not math.isfinite(published_at) or published_at <= 0:
            raise ValueError("owner batch published_at must be positive")
        values = tuple(self.values)
        if any(not isinstance(value, InputValue) for value in values):
            raise TypeError("owner batch typed input values required")
        source_ids = [value.source_id for value in values]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("owner batch contains duplicate source IDs")
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "values", values)


def owner_input_batch_fingerprint(batch: OwnerInputBatch) -> str:
    """Hash one owner generation with the NGUI value codec."""

    if not isinstance(batch, OwnerInputBatch):
        raise TypeError("owner input fingerprint requires typed batch")
    payload = owner_input_batch_payload(batch)
    # Publication time belongs to transport, not the owner. Replaying the same
    # owner generation later must have the same result.
    payload.pop("published_at")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def normalize_ngui_input_value(value: Any, *, path: str = "$") -> Any:
    """Copy an owner value into the transport-safe NGUI form."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"ngui_input_non_finite_value:{path}")
        return value
    if isinstance(value, Enum):
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key in output:
                raise ValueError(f"ngui_input_key_collision:{path}.{text_key}")
            output[text_key] = normalize_ngui_input_value(
                item,
                path=f"{path}.{text_key}",
            )
        return output
    if isinstance(value, list):
        return [
            normalize_ngui_input_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            normalize_ngui_input_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, set):
        return {
            normalize_ngui_input_value(item, path=f"{path}[]")
            for item in value
        }
    if isinstance(value, frozenset):
        return frozenset(
            normalize_ngui_input_value(item, path=f"{path}[]")
            for item in value
        )
    if isinstance(value, SimpleNamespace):
        return SimpleNamespace(
            **normalize_ngui_input_value(vars(value), path=path)
        )
    if is_dataclass(value) and not isinstance(value, type):
        normalized_fields = {
            field.name: normalize_ngui_input_value(
                getattr(value, field.name),
                path=f"{path}.{field.name}",
            )
            for field in fields(value)
        }
        try:
            return type(value)(**normalized_fields)
        except Exception as exc:
            raise TypeError(
                f"ngui_input_dataclass_reconstruction_failed:{path}:"
                f"{type(value).__name__}"
            ) from exc
    raise TypeError(
        f"ngui_input_value_type_unsupported:{path}:{type(value).__name__}"
    )


def freeze_ngui_input_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): freeze_ngui_input_value(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(freeze_ngui_input_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_ngui_input_value(item) for item in value)
    if isinstance(value, SimpleNamespace):
        return _FrozenSimpleNamespace(
            **{
                key: freeze_ngui_input_value(item)
                for key, item in vars(value).items()
                if key != "_frozen"
            }
        )
    if is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not params.frozen:
            raise TypeError(
                "ngui_input_mutable_dataclass_forbidden:"
                + type(value).__name__
            )
        return type(value)(
            **{
                field.name: freeze_ngui_input_value(
                    getattr(value, field.name)
                )
                for field in fields(value)
            }
        )
    return value


def _pickle_safe_ngui_input_value(value: Any) -> Any:
    """Copy a frozen input value into pickle-safe containers."""

    if isinstance(value, Mapping):
        return {
            str(key): _pickle_safe_ngui_input_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_pickle_safe_ngui_input_value(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(
            _pickle_safe_ngui_input_value(item) for item in value
        )
    if isinstance(value, SimpleNamespace):
        return SimpleNamespace(
            **{
                key: _pickle_safe_ngui_input_value(item)
                for key, item in vars(value).items()
                if key != "_frozen"
            }
        )
    if is_dataclass(value) and not isinstance(value, type):
        return type(value)(
            **{
                field.name: _pickle_safe_ngui_input_value(
                    getattr(value, field.name)
                )
                for field in fields(value)
            }
        )
    return value


def thaw_ngui_input_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): thaw_ngui_input_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [thaw_ngui_input_value(item) for item in value]
    if isinstance(value, frozenset):
        return {
            thaw_ngui_input_value(item)
            for item in value
        }
    if isinstance(value, SimpleNamespace):
        return SimpleNamespace(
            **{
                key: thaw_ngui_input_value(item)
                for key, item in vars(value).items()
                if key != "_frozen"
            }
        )
    return value


def canonical_ngui_input_value(value: Any, *, path: str = "$") -> Any:
    """Encode a valid NGUI value without rebuilding its typed owner."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"ngui_input_non_finite_value:{path}")
        return value
    if isinstance(value, Enum):
        return {
            "__ngui_type__": "enum",
            "class": (
                f"{type(value).__module__}."
                f"{type(value).__qualname__}"
            ),
            "value": canonical_ngui_input_value(
                value.value,
                path=f"{path}.value",
            ),
        }
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key in output:
                raise ValueError(f"ngui_input_key_collision:{path}.{text_key}")
            output[text_key] = canonical_ngui_input_value(
                item,
                path=f"{path}.{text_key}",
            )
        return output
    if isinstance(value, list):
        return [
            canonical_ngui_input_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return {
            "__ngui_type__": "tuple",
            "items": [
                canonical_ngui_input_value(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            ],
        }
    if isinstance(value, (set, frozenset)):
        items = [
            canonical_ngui_input_value(item, path=f"{path}[]")
            for item in value
        ]
        items.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return {
            "__ngui_type__": (
                "frozenset" if isinstance(value, frozenset) else "set"
            ),
            "items": items,
        }
    if isinstance(value, SimpleNamespace):
        return {
            "__ngui_type__": "SimpleNamespace",
            "fields": canonical_ngui_input_value(vars(value), path=path),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__ngui_type__": "dataclass",
            "class": (
                f"{type(value).__module__}."
                f"{type(value).__qualname__}"
            ),
            "fields": {
                field.name: canonical_ngui_input_value(
                    getattr(value, field.name),
                    path=f"{path}.{field.name}",
                )
                for field in fields(value)
            },
        }
    raise TypeError(
        f"ngui_input_canonical_value_unsupported:{path}:"
        f"{type(value).__name__}"
    )


def owner_input_batch_payload(batch: OwnerInputBatch) -> dict[str, Any]:
    """Return the complete owner-generation payload used by the application."""

    if not isinstance(batch, OwnerInputBatch):
        raise TypeError("owner input payload requires typed batch")
    return {
        "owner": batch.owner.value,
        "sequence": int(batch.sequence),
        "published_at": float(batch.published_at),
        "values": [
            {
                "source_id": item.source_id,
                "value": canonical_ngui_input_value(
                    item.value,
                    path=f"$.{item.source_id}",
                ),
                "valid": bool(item.valid),
                "sampled_at": float(item.sampled_at),
                "sample_sequence": int(item.sample_sequence),
                "freshness": item.freshness.value,
                "reason": item.reason,
                "fact_state": item.fact_state.value,
            }
            for item in batch.values
        ],
    }


@dataclass(frozen=True, slots=True)
class InputRegisterSnapshot:
    generation: int
    published_at: float
    values: Mapping[str, InputValue]
    owner_sequences: Mapping[SourceOwner, int]
    owner_fingerprints: Mapping[SourceOwner, str]
    source_owner_sequences: Mapping[str, int]
    runtime_session_scope_key: str
    runtime_source_epoch: str
    accepted_source_sequence: int
    accepted_source_sampled_at: float
    matrix_identity: MatrixGenerationIdentity | None = None


@dataclass(frozen=True, slots=True)
class RuntimeNamespaceBinding:
    changed: bool
    session_changed: bool
    source_epoch_changed: bool
    previous_session_scope_key: str
    previous_source_epoch: str
    session_scope_key: str
    source_epoch: str
    values_cleared: int


@dataclass(frozen=True, slots=True)
class RuntimeFrameInputPublication:
    snapshot: InputRegisterSnapshot
    namespace_binding: RuntimeNamespaceBinding
    applied_owners: tuple[SourceOwner, ...]
    unchanged_owners: tuple[SourceOwner, ...]


class InputRegisterContractError(RuntimeError):
    pass


class NGUIInputRegisterBank:
    """Atomically install local-owner data or a complete runtime frame."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._generation = 0
        self._published_at = 0.0
        self._values: dict[str, InputValue] = {}
        self._owner_sequences: dict[SourceOwner, int] = {}
        self._owner_fingerprints: dict[SourceOwner, str] = {}
        self._source_owner_sequences: dict[str, int] = {}
        self._runtime_session_scope_key = ""
        self._runtime_source_epoch = ""
        self._matrix_identity: MatrixGenerationIdentity | None = None
        self._accepted_source_sampled_at = 0.0

    @classmethod
    def prepare_runtime_frame_candidate(
        cls,
        frame: Any,
        previous: InputRegisterSnapshot | None,
    ) -> InputRegisterSnapshot:
        """Build a complete Input candidate from current owner state."""

        from .runtime_ipc import RuntimeInputFrame

        if not isinstance(frame, RuntimeInputFrame):
            raise TypeError("input_candidate_runtime_frame_required")
        if previous is not None and not isinstance(
            previous,
            InputRegisterSnapshot,
        ):
            raise TypeError("input_candidate_previous_snapshot_required")
        if (
            previous is not None
            and previous.matrix_identity is not None
            and previous.matrix_identity.namespace != frame.namespace
        ):
            raise RuntimeError("input_candidate_explicit_cutover_required")
        candidate = cls()
        if previous is not None:
            candidate._generation = previous.generation
            candidate._published_at = previous.published_at
            candidate._values = dict(previous.values)
            candidate._owner_sequences = dict(previous.owner_sequences)
            candidate._owner_fingerprints = dict(
                previous.owner_fingerprints
            )
            candidate._source_owner_sequences = dict(
                previous.source_owner_sequences
            )
            candidate._runtime_session_scope_key = (
                previous.runtime_session_scope_key
            )
            candidate._runtime_source_epoch = previous.runtime_source_epoch
            candidate._matrix_identity = previous.matrix_identity
            candidate._accepted_source_sampled_at = (
                previous.accepted_source_sampled_at
            )
        publication = candidate.publish_complete_runtime_frame(frame)
        return publication.snapshot

    @classmethod
    def prepare_local_batch_candidate(
        cls,
        batch: OwnerInputBatch,
        previous: InputRegisterSnapshot,
    ) -> InputRegisterSnapshot:
        """Build the next local-owner state without changing live state."""

        if not isinstance(batch, OwnerInputBatch):
            raise TypeError("input_candidate_local_batch_required")
        if not isinstance(previous, InputRegisterSnapshot):
            raise TypeError("input_candidate_previous_snapshot_required")
        candidate = cls()
        candidate._generation = previous.generation
        candidate._published_at = previous.published_at
        candidate._values = dict(previous.values)
        candidate._owner_sequences = dict(previous.owner_sequences)
        candidate._owner_fingerprints = dict(
            previous.owner_fingerprints
        )
        candidate._source_owner_sequences = dict(
            previous.source_owner_sequences
        )
        candidate._runtime_session_scope_key = (
            previous.runtime_session_scope_key
        )
        candidate._runtime_source_epoch = previous.runtime_source_epoch
        candidate._matrix_identity = previous.matrix_identity
        candidate._accepted_source_sampled_at = (
            previous.accepted_source_sampled_at
        )
        candidate._validate_batch(batch)
        with candidate._changed:
            return candidate._apply_local_batch_unlocked(batch)

    @classmethod
    def prepare_local_transaction_candidate(
        cls,
        batches: tuple[OwnerInputBatch, ...],
        previous: InputRegisterSnapshot,
    ) -> InputRegisterSnapshot:
        """Build the next multi-owner state without changing live state."""

        if not batches:
            raise ValueError("input_candidate_local_transaction_required")
        candidate = previous
        seen: set[SourceOwner] = set()
        for batch in batches:
            if batch.owner in seen:
                raise RuntimeError(
                    "input_candidate_local_transaction_owner_duplicate"
                )
            seen.add(batch.owner)
            candidate = cls.prepare_local_batch_candidate(
                batch,
                candidate,
            )
        return candidate

    def publish_complete_runtime_frame(
        self,
        frame: Any,
    ) -> RuntimeFrameInputPublication:
        """Atomically install one complete projected runtime frame."""

        from .runtime_ipc import RuntimeInputFrame

        if not isinstance(frame, RuntimeInputFrame):
            raise TypeError("complete runtime frame required")
        batches = frame.owner_batches
        if tuple(batch.owner for batch in batches) != frame.owner_order:
            raise InputRegisterContractError(
                "complete runtime owner denominator mismatch"
            )
        identity = frame.matrix_identity
        namespace = (
            identity.session_scope_key,
            identity.source_epoch,
        )
        with self._changed:
            previous_namespace = (
                self._runtime_session_scope_key,
                self._runtime_source_epoch,
            )
            if (
                self._matrix_identity is not None
                and self._matrix_identity.namespace != identity.namespace
            ):
                raise InputRegisterContractError(
                    "accepted root namespace requires parent reset"
                )
            if (
                self._matrix_identity is not None
                and identity.matrix_sequence
                < self._matrix_identity.matrix_sequence
            ):
                raise InputRegisterContractError(
                    "accepted root sequence regressed"
                )
            values = dict(self._values)
            source_owner_sequences = dict(self._source_owner_sequences)
            owner_sequences = dict(self._owner_sequences)
            owner_fingerprints = dict(self._owner_fingerprints)
            if frame.bootstrap:
                if frame.owner_order != frame.complete_owner_order:
                    raise InputRegisterContractError(
                        "runtime bootstrap owner denominator incomplete"
                    )
            elif self._matrix_identity is None:
                raise InputRegisterContractError(
                    "runtime owner delta requires accepted bootstrap"
                )
            elif any(
                owner not in owner_sequences
                for owner in frame.complete_owner_order
            ):
                raise InputRegisterContractError(
                    "runtime owner delta base denominator incomplete"
                )
            applied: list[SourceOwner] = []
            for batch, fingerprint in zip(
                batches,
                frame.owner_fingerprints,
                strict=True,
            ):
                previous_sequence = owner_sequences.get(batch.owner, 0)
                if batch.sequence < previous_sequence:
                    raise InputRegisterContractError(
                        "complete runtime owner sequence regressed:"
                        + batch.owner.value
                    )
                if batch.sequence == previous_sequence:
                    if (
                        owner_fingerprints.get(batch.owner)
                        != fingerprint
                    ):
                        raise InputRegisterContractError(
                            "accepted root repeated owner changed:"
                            + batch.owner.value
                        )
                    continue
                previous_source_ids = {
                    source_id
                    for source_id in values
                    if input_source_spec(source_id).owner is batch.owner
                }
                incoming_source_ids = {
                    item.source_id for item in batch.values
                }
                if (
                    batch.owner is SourceOwner.MODEM_MATRIX
                    and incoming_source_ids
                    != set(MODEM_LIVE_NGUI_CELL_ADDRESSES)
                ):
                    raise InputRegisterContractError(
                        "accepted current fact source denominator mismatch"
                    )
                for source_id in previous_source_ids:
                    values.pop(source_id, None)
                    source_owner_sequences.pop(source_id, None)
                for item in batch.values:
                    if input_source_spec(item.source_id).owner is not batch.owner:
                        raise InputRegisterContractError(
                            "accepted root source owner mismatch:"
                            + item.source_id
                        )
                    values[item.source_id] = item
                    source_owner_sequences[item.source_id] = batch.sequence
                owner_sequences[batch.owner] = batch.sequence
                owner_fingerprints[batch.owner] = fingerprint
                applied.append(batch.owner)
            if any(
                owner not in owner_sequences
                for owner in frame.complete_owner_order
            ):
                raise InputRegisterContractError(
                    "runtime projected owner denominator incomplete"
                )
            unchanged = [
                owner
                for owner in frame.complete_owner_order
                if owner not in applied
            ]
            self._values = values
            self._source_owner_sequences = source_owner_sequences
            self._owner_sequences = owner_sequences
            self._owner_fingerprints = owner_fingerprints
            self._runtime_session_scope_key = namespace[0]
            self._runtime_source_epoch = namespace[1]
            self._matrix_identity = identity
            self._accepted_source_sampled_at = (
                frame.accepted_source_sampled_at
            )
            if applied:
                self._generation += 1
                self._published_at = max(
                    self._published_at,
                    frame.published_at,
                )
                self._changed.notify_all()
            binding = RuntimeNamespaceBinding(
                changed=previous_namespace != namespace,
                session_changed=(
                    bool(previous_namespace[0])
                    and previous_namespace[0] != namespace[0]
                ),
                source_epoch_changed=(
                    bool(previous_namespace[1])
                    and previous_namespace[1] != namespace[1]
                ),
                previous_session_scope_key=previous_namespace[0],
                previous_source_epoch=previous_namespace[1],
                session_scope_key=namespace[0],
                source_epoch=namespace[1],
                values_cleared=0,
            )
            return RuntimeFrameInputPublication(
                snapshot=self._snapshot_unlocked(),
                namespace_binding=binding,
                applied_owners=tuple(applied),
                unchanged_owners=tuple(unchanged),
            )

    def install_prepared_runtime_snapshot(
        self,
        previous: InputRegisterSnapshot,
        candidate: InputRegisterSnapshot,
    ) -> InputRegisterSnapshot:
        """Install the prepared accepted-root state with one assignment."""

        if not isinstance(previous, InputRegisterSnapshot) or not isinstance(
            candidate,
            InputRegisterSnapshot,
        ):
            raise TypeError("input_install_prepared_snapshot_required")
        with self._changed:
            current = self._snapshot_unlocked()
            if current != previous:
                raise RuntimeError("input_install_prepared_base_changed")
            if (
                candidate.generation < previous.generation
                or candidate.matrix_identity is None
            ):
                raise RuntimeError(
                    "input_install_prepared_candidate_invalid"
                )
            self._generation = candidate.generation
            self._published_at = candidate.published_at
            self._values = dict(candidate.values)
            self._owner_sequences = dict(candidate.owner_sequences)
            self._owner_fingerprints = dict(
                candidate.owner_fingerprints
            )
            self._source_owner_sequences = dict(
                candidate.source_owner_sequences
            )
            self._runtime_session_scope_key = (
                candidate.runtime_session_scope_key
            )
            self._runtime_source_epoch = candidate.runtime_source_epoch
            self._matrix_identity = candidate.matrix_identity
            self._accepted_source_sampled_at = (
                candidate.accepted_source_sampled_at
            )
            self._changed.notify_all()
            return self._snapshot_unlocked()

    def install_prepared_local_snapshot(
        self,
        previous: InputRegisterSnapshot,
        candidate: InputRegisterSnapshot,
        *,
        batch: OwnerInputBatch,
    ) -> InputRegisterSnapshot:
        """Install local-owner state prepared by the coordinator."""

        if not isinstance(previous, InputRegisterSnapshot) or not isinstance(
            candidate,
            InputRegisterSnapshot,
        ):
            raise TypeError("input_install_prepared_snapshot_required")
        if not isinstance(batch, OwnerInputBatch):
            raise TypeError("input_install_prepared_local_batch_required")
        expected = self.prepare_local_batch_candidate(batch, previous)
        if candidate != expected:
            raise RuntimeError(
                "input_install_prepared_local_candidate_mismatch"
            )
        with self._changed:
            current = self._snapshot_unlocked()
            if current != previous:
                raise RuntimeError("input_install_prepared_base_changed")
            self._generation = candidate.generation
            self._published_at = candidate.published_at
            self._values = dict(candidate.values)
            self._owner_sequences = dict(candidate.owner_sequences)
            self._owner_fingerprints = dict(
                candidate.owner_fingerprints
            )
            self._source_owner_sequences = dict(
                candidate.source_owner_sequences
            )
            self._runtime_session_scope_key = (
                candidate.runtime_session_scope_key
            )
            self._runtime_source_epoch = candidate.runtime_source_epoch
            self._matrix_identity = candidate.matrix_identity
            self._accepted_source_sampled_at = (
                candidate.accepted_source_sampled_at
            )
            self._changed.notify_all()
            return self._snapshot_unlocked()

    def install_prepared_local_transaction(
        self,
        previous: InputRegisterSnapshot,
        candidate: InputRegisterSnapshot,
        *,
        batches: tuple[OwnerInputBatch, ...],
    ) -> InputRegisterSnapshot:
        """Install a multi-owner transaction with one assignment."""

        if not isinstance(previous, InputRegisterSnapshot) or not isinstance(
            candidate,
            InputRegisterSnapshot,
        ):
            raise TypeError("input_install_prepared_snapshot_required")
        expected = self.prepare_local_transaction_candidate(
            batches,
            previous,
        )
        if candidate != expected:
            raise RuntimeError(
                "input_install_prepared_local_transaction_mismatch"
            )
        with self._changed:
            if self._snapshot_unlocked() != previous:
                raise RuntimeError("input_install_prepared_base_changed")
            self._generation = candidate.generation
            self._published_at = candidate.published_at
            self._values = dict(candidate.values)
            self._owner_sequences = dict(candidate.owner_sequences)
            self._owner_fingerprints = dict(
                candidate.owner_fingerprints
            )
            self._source_owner_sequences = dict(
                candidate.source_owner_sequences
            )
            self._runtime_session_scope_key = (
                candidate.runtime_session_scope_key
            )
            self._runtime_source_epoch = candidate.runtime_source_epoch
            self._matrix_identity = candidate.matrix_identity
            self._accepted_source_sampled_at = (
                candidate.accepted_source_sampled_at
            )
            self._changed.notify_all()
            return self._snapshot_unlocked()

    def publish(self, batch: OwnerInputBatch) -> InputRegisterSnapshot:
        """Publish a complete batch for one declared local owner."""

        if batch.owner not in _LOCAL_PUBLISH_OWNERS:
            raise InputRegisterContractError(
                f"{batch.owner.value} must publish through the atomic runtime frame"
            )
        self._validate_batch(batch)
        with self._changed:
            if self._matrix_identity is not None:
                raise InputRegisterContractError(
                    "local owner publication requires runtime generation "
                    "coordinator reentry"
                )
            return self._apply_local_batch_unlocked(batch)

    def _apply_local_batch_unlocked(
        self,
        batch: OwnerInputBatch,
    ) -> InputRegisterSnapshot:
        """Apply a validated local batch while holding the bank lock."""

        previous = self._owner_sequences.get(batch.owner, 0)
        if batch.sequence <= previous:
            raise InputRegisterContractError(
                f"stale {batch.owner.value} sequence {batch.sequence}; current is {previous}"
            )
        replacement = dict(self._values)
        source_owner_sequences = dict(self._source_owner_sequences)
        for item in batch.values:
            replacement[item.source_id] = item
            source_owner_sequences[item.source_id] = batch.sequence
        self._values = replacement
        self._source_owner_sequences = source_owner_sequences
        self._owner_sequences = {**self._owner_sequences, batch.owner: batch.sequence}
        self._owner_fingerprints = {
            **self._owner_fingerprints,
            batch.owner: owner_input_batch_fingerprint(batch),
        }
        self._generation += 1
        self._published_at = max(self._published_at, batch.published_at)
        snapshot = self._snapshot_unlocked()
        self._changed.notify_all()
        return snapshot

    def publish_runtime_frame(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity | None = None,
        session_scope_key: str = "",
        source_epoch: str = "",
        modem_live_matrix: Mapping[str, Any] | None = None,
        owner_batches: tuple[OwnerInputBatch, ...] = (),
    ) -> RuntimeFrameInputPublication:
        """Install an immutable runtime frame and wake presentation once."""

        raise InputRegisterContractError(
            "independent live-owner batch install is retired; "
            "publish one accepted Core root frame"
        )

    def snapshot(self) -> InputRegisterSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def reset_runtime_state(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Clear accepted and local values during a runtime cutover."""

        with self._changed:
            prior = self._reset_observation_unlocked()
            self._generation = 0
            self._published_at = 0.0
            self._values = {}
            self._owner_sequences = {}
            self._owner_fingerprints = {}
            self._source_owner_sequences = {}
            self._runtime_session_scope_key = ""
            self._runtime_source_epoch = ""
            self._matrix_identity = None
            self._accepted_source_sampled_at = 0.0
            result = self._reset_observation_unlocked()
            self._changed.notify_all()
            return MappingProxyType(prior), MappingProxyType(result)

    def wait_for_generation(
        self,
        after_generation: int,
        *,
        timeout_s: float,
    ) -> InputRegisterSnapshot:
        """Wait for a newer input generation or the maintenance timeout."""

        if after_generation < -1:
            raise ValueError("after_generation cannot be less than -1")
        if timeout_s < 0.0:
            raise ValueError("input register wait timeout cannot be negative")
        with self._changed:
            if self._generation <= after_generation:
                self._changed.wait(timeout=float(timeout_s))
            return self._snapshot_unlocked()

    def wake_waiters(self) -> None:
        """Wake lifecycle waiters without creating a register generation."""

        with self._changed:
            self._changed.notify_all()

    @staticmethod
    def _validate_batch(batch: OwnerInputBatch) -> None:
        if batch.owner is SourceOwner.MODEM_MATRIX:
            raise InputRegisterContractError(
                "Modem Live raw truth requires complete matrix publication"
            )
        expected_sources = _COMPLETE_LOCAL_OWNER_SOURCES.get(batch.owner)
        if expected_sources is not None:
            source_ids = frozenset(item.source_id for item in batch.values)
            if source_ids != expected_sources:
                missing = sorted(expected_sources - source_ids)
                extra = sorted(source_ids - expected_sources)
                raise InputRegisterContractError(
                    f"{batch.owner.value} owner batch must be complete:"
                    f"missing={','.join(missing)};extra={','.join(extra)}"
                )
        for item in batch.values:
            spec = input_source_spec(item.source_id)
            if spec.owner is not batch.owner:
                raise InputRegisterContractError(
                    f"{item.source_id} belongs to {spec.owner.value}, not {batch.owner.value}"
                )
            if item.sample_sequence < 0 or (
                item.valid and item.sample_sequence <= 0
            ):
                raise InputRegisterContractError(
                    f"{item.source_id} sample sequence is invalid"
                )
            sampled_at = float(item.sampled_at)
            if (
                not math.isfinite(sampled_at)
                or sampled_at < 0
                or (item.valid and sampled_at <= 0)
                or sampled_at > batch.published_at
            ):
                raise InputRegisterContractError(
                    f"{item.source_id} has invalid sample timestamp"
                )

    def _snapshot_unlocked(self) -> InputRegisterSnapshot:
        return InputRegisterSnapshot(
            generation=self._generation,
            published_at=self._published_at,
            values=MappingProxyType(dict(self._values)),
            owner_sequences=MappingProxyType(dict(self._owner_sequences)),
            owner_fingerprints=MappingProxyType(
                dict(self._owner_fingerprints)
            ),
            source_owner_sequences=MappingProxyType(dict(self._source_owner_sequences)),
            runtime_session_scope_key=self._runtime_session_scope_key,
            runtime_source_epoch=self._runtime_source_epoch,
            accepted_source_sequence=(
                self._matrix_identity.matrix_sequence
                if self._matrix_identity is not None
                else 0
            ),
            accepted_source_sampled_at=(
                self._accepted_source_sampled_at
            ),
            matrix_identity=self._matrix_identity,
        )

    def _reset_observation_unlocked(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "published_at": self._published_at,
            "values": {
                source_id: {
                    "source_id": value.source_id,
                    "value": canonical_ngui_input_value(
                        value.value,
                        path=f"$.reset.{source_id}",
                    ),
                    "valid": value.valid,
                    "sampled_at": value.sampled_at,
                    "sample_sequence": value.sample_sequence,
                    "freshness": value.freshness.value,
                    "reason": value.reason,
                    "fact_state": value.fact_state.value,
                }
                for source_id, value in sorted(self._values.items())
            },
            "owner_sequences": {
                owner.value: sequence
                for owner, sequence in sorted(
                    self._owner_sequences.items(),
                    key=lambda item: item[0].value,
                )
            },
            "owner_fingerprints": {
                owner.value: fingerprint
                for owner, fingerprint in sorted(
                    self._owner_fingerprints.items(),
                    key=lambda item: item[0].value,
                )
            },
            "source_owner_sequences": dict(
                sorted(self._source_owner_sequences.items())
            ),
            "runtime_session_scope_key": self._runtime_session_scope_key,
            "runtime_source_epoch": self._runtime_source_epoch,
            "matrix_identity": (
                self._matrix_identity.as_dict()
                if self._matrix_identity is not None
                else {}
            ),
            "accepted_source_sampled_at": (
                self._accepted_source_sampled_at
            ),
        }
