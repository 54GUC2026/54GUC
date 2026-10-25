"""Immutable identities for one CGF runtime and Modem Live Matrix.

Modem Live owns startup and Matrix semantics.  Other CGF components receive
one small ready receipt after the real startup acquisition has completed; they
do not reclassify profile families or Matrix cells.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping


_REVISION = re.compile(r"^[0-9a-f]{64}$")
ATOMIC_RUNTIME_CONTRACT_ID = "cgf.atomic_runtime_generation"
ATOMIC_RUNTIME_CONTRACT_VERSION = 1
MODEM_LIVE_READY_OWNER = "54guc.modem_live.ready"
MODEM_LIVE_READY_SCHEMA = "ModemLiveReadyReceipt.v1"

_MATRIX_IDENTITY_KEYS = frozenset(
    {
        "session_scope_key",
        "source_epoch",
        "lifecycle_generation",
        "matrix_sequence",
        "matrix_revision",
    }
)
_STARTUP_SWEEP_KEYS = frozenset(
    {
        *_MATRIX_IDENTITY_KEYS,
        "acquisition_attempt_id",
        "acquisition_attempt_ids",
        "source_lane",
        "started_at",
        "finished_at",
        "completed",
    }
)
_MODEM_LIVE_READY_KEYS = frozenset(
    {
        "schema",
        "owner",
        "startup_sweep",
        "ready_at",
    }
)


def _finite_number(value: Any, error: str, *, positive: bool = False) -> float:
    if type(value) not in {int, float}:
        raise TypeError(error)
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise ValueError(error)
    return result


def _canonical_nonempty_text(value: Any, error: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(error)
    return value


def _positive_int(value: Any, error: str) -> int:
    if type(value) is not int:
        raise TypeError(error)
    if value <= 0:
        raise ValueError(error)
    return value


@dataclass(frozen=True, slots=True)
class RuntimeGenerationNamespace:
    """One Product lifecycle bound to one Modem Live device session."""

    session_scope_key: str
    source_epoch: str
    lifecycle_generation: int

    def __post_init__(self) -> None:
        scope = _canonical_nonempty_text(
            self.session_scope_key,
            "runtime_generation_session_scope_required",
        )
        epoch = _canonical_nonempty_text(
            self.source_epoch,
            "runtime_generation_source_epoch_required",
        )
        lifecycle = _positive_int(
            self.lifecycle_generation,
            "runtime_generation_lifecycle_positive_required",
        )
        object.__setattr__(self, "session_scope_key", scope)
        object.__setattr__(self, "source_epoch", epoch)
        object.__setattr__(self, "lifecycle_generation", lifecycle)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "RuntimeGenerationNamespace":
        if not isinstance(value, Mapping):
            raise TypeError("runtime_generation_namespace_mapping_required")
        if frozenset(value) != frozenset(
            {
                "session_scope_key",
                "source_epoch",
                "lifecycle_generation",
            }
        ):
            raise ValueError("runtime_generation_namespace_keys_invalid")
        return cls(
            session_scope_key=value["session_scope_key"],
            source_epoch=value["source_epoch"],
            lifecycle_generation=value["lifecycle_generation"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_scope_key": self.session_scope_key,
            "source_epoch": self.source_epoch,
            "lifecycle_generation": self.lifecycle_generation,
        }


@dataclass(frozen=True, slots=True)
class MatrixGenerationIdentity:
    """Exact immutable identity of one committed Modem Live Matrix."""

    namespace: RuntimeGenerationNamespace
    matrix_sequence: int
    matrix_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("matrix_generation_namespace_required")
        sequence = _positive_int(
            self.matrix_sequence,
            "matrix_generation_sequence_positive_required",
        )
        if type(self.matrix_revision) is not str:
            raise TypeError("matrix_generation_revision_invalid")
        revision = self.matrix_revision
        if (
            revision != revision.strip().lower()
            or not _REVISION.fullmatch(revision)
        ):
            raise ValueError("matrix_generation_revision_invalid")
        object.__setattr__(self, "matrix_sequence", sequence)
        object.__setattr__(self, "matrix_revision", revision)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "MatrixGenerationIdentity":
        if not isinstance(value, Mapping):
            raise TypeError("matrix_generation_identity_mapping_required")
        if frozenset(value) != _MATRIX_IDENTITY_KEYS:
            raise ValueError("matrix_generation_identity_keys_invalid")
        return cls(
            namespace=RuntimeGenerationNamespace.from_mapping(
                {
                    key: value[key]
                    for key in (
                        "session_scope_key",
                        "source_epoch",
                        "lifecycle_generation",
                    )
                }
            ),
            matrix_sequence=value["matrix_sequence"],
            matrix_revision=value["matrix_revision"],
        )

    @classmethod
    def from_truth_matrix(
        cls,
        value: Mapping[str, Any],
    ) -> "MatrixGenerationIdentity":
        """Strict boundary from the sole ModemLiveTruthMatrix owner."""

        if not isinstance(value, Mapping):
            raise TypeError("matrix_generation_truth_matrix_required")
        if value.get("schema") != "ModemLiveTruthMatrix.v4":
            raise ValueError("matrix_generation_truth_matrix_schema_invalid")
        if value.get("owner") != "54guc.modem_live.truth_matrix":
            raise ValueError("matrix_generation_truth_matrix_owner_invalid")
        if "matrix_sequence" in value or "matrix_revision" in value:
            raise ValueError("matrix_generation_truth_matrix_mixed_keys_forbidden")
        return cls(
            namespace=RuntimeGenerationNamespace.from_mapping(
                {
                    key: value[key]
                    for key in (
                        "session_scope_key",
                        "source_epoch",
                        "lifecycle_generation",
                    )
                }
            ),
            matrix_sequence=value["sequence"],
            matrix_revision=value["revision"],
        )

    @property
    def session_scope_key(self) -> str:
        return self.namespace.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.namespace.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.namespace.lifecycle_generation

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.namespace.as_dict(),
            "matrix_sequence": self.matrix_sequence,
            "matrix_revision": self.matrix_revision,
        }


@dataclass(frozen=True, slots=True)
class StartupAcquisitionSweepReceipt:
    """Physical startup transaction bound to one committed Matrix."""

    matrix_identity: MatrixGenerationIdentity
    acquisition_attempt_id: str
    acquisition_attempt_ids: tuple[str, ...]
    source_lane: str
    started_at: float
    finished_at: float
    completed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("startup_sweep_matrix_identity_required")
        attempt = _canonical_nonempty_text(
            self.acquisition_attempt_id,
            "startup_sweep_physical_evidence_required",
        )
        if (
            type(self.acquisition_attempt_ids) is not tuple
            or not self.acquisition_attempt_ids
        ):
            raise TypeError("startup_sweep_physical_attempt_ids_required")
        attempt_ids = tuple(
            _canonical_nonempty_text(
                item,
                "startup_sweep_physical_attempt_ids_required",
            )
            for item in self.acquisition_attempt_ids
        )
        if len(attempt_ids) != len(set(attempt_ids)) or attempt not in attempt_ids:
            raise ValueError("startup_sweep_physical_attempt_ids_invalid")
        lane = _canonical_nonempty_text(
            self.source_lane,
            "startup_sweep_physical_evidence_required",
        )
        started = _finite_number(
            self.started_at,
            "startup_sweep_timestamp_order_invalid",
        )
        finished = _finite_number(
            self.finished_at,
            "startup_sweep_timestamp_order_invalid",
        )
        if started <= 0.0 or finished < started:
            raise ValueError("startup_sweep_timestamp_order_invalid")
        if type(self.completed) is not bool:
            raise TypeError("startup_sweep_completed_bool_required")
        object.__setattr__(self, "acquisition_attempt_id", attempt)
        object.__setattr__(self, "acquisition_attempt_ids", attempt_ids)
        object.__setattr__(self, "source_lane", lane)
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "finished_at", finished)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.matrix_identity.as_dict(),
            "acquisition_attempt_id": self.acquisition_attempt_id,
            "acquisition_attempt_ids": list(self.acquisition_attempt_ids),
            "source_lane": self.source_lane,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "completed": self.completed,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "StartupAcquisitionSweepReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("startup_sweep_mapping_required")
        if frozenset(value) != _STARTUP_SWEEP_KEYS:
            raise ValueError("startup_sweep_mapping_keys_invalid")
        attempt_ids = value["acquisition_attempt_ids"]
        if type(attempt_ids) is not list:
            raise TypeError("startup_sweep_physical_attempt_ids_list_required")
        return cls(
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                {key: value[key] for key in _MATRIX_IDENTITY_KEYS}
            ),
            acquisition_attempt_id=value["acquisition_attempt_id"],
            acquisition_attempt_ids=tuple(attempt_ids),
            source_lane=value["source_lane"],
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            completed=value["completed"],
        )


@dataclass(frozen=True, slots=True)
class ModemLiveReadyReceipt:
    """One-time Modem Live proof that startup produced Matrix truth."""

    startup_sweep: StartupAcquisitionSweepReceipt
    ready_at: float
    schema: str = MODEM_LIVE_READY_SCHEMA
    owner: str = MODEM_LIVE_READY_OWNER

    def __post_init__(self) -> None:
        if self.schema != MODEM_LIVE_READY_SCHEMA:
            raise ValueError("modem_live_ready_schema_invalid")
        if self.owner != MODEM_LIVE_READY_OWNER:
            raise ValueError("modem_live_ready_owner_invalid")
        if not isinstance(self.startup_sweep, StartupAcquisitionSweepReceipt):
            raise TypeError("modem_live_ready_startup_sweep_required")
        if not self.startup_sweep.completed:
            raise ValueError("modem_live_ready_startup_incomplete")
        ready_at = _finite_number(
            self.ready_at,
            "modem_live_ready_timestamp_invalid",
            positive=True,
        )
        if ready_at < self.startup_sweep.finished_at:
            raise ValueError("modem_live_ready_timestamp_order_invalid")
        object.__setattr__(self, "ready_at", ready_at)

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity:
        return self.startup_sweep.matrix_identity

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

    @property
    def ready(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "startup_sweep": self.startup_sweep.as_dict(),
            "ready_at": self.ready_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModemLiveReadyReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("modem_live_ready_mapping_required")
        if frozenset(value) != _MODEM_LIVE_READY_KEYS:
            raise ValueError("modem_live_ready_mapping_keys_invalid")
        return cls(
            startup_sweep=StartupAcquisitionSweepReceipt.from_mapping(
                value["startup_sweep"]
            ),
            ready_at=value["ready_at"],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (_restore_modem_live_ready, (self.as_dict(),))


def _restore_modem_live_ready(
    value: Mapping[str, Any],
) -> ModemLiveReadyReceipt:
    return ModemLiveReadyReceipt.from_mapping(value)


__all__ = (
    "ATOMIC_RUNTIME_CONTRACT_ID",
    "ATOMIC_RUNTIME_CONTRACT_VERSION",
    "MODEM_LIVE_READY_OWNER",
    "MODEM_LIVE_READY_SCHEMA",
    "MatrixGenerationIdentity",
    "ModemLiveReadyReceipt",
    "RuntimeGenerationNamespace",
    "StartupAcquisitionSweepReceipt",
)
