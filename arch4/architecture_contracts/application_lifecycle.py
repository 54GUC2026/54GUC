"""Typed lifecycle identities shared by launcher and Product supervision.

The contracts in this module contain no I/O and own no process, worker, or
device behavior.  They exist so every lifecycle receipt names one exact
generation instead of relying on booleans, a PID, or a lock-file pathname.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    ATOMIC_RUNTIME_CONTRACT_ID,
    ATOMIC_RUNTIME_CONTRACT_VERSION,
    MatrixGenerationIdentity,
    ModemLiveReadyReceipt,
    RuntimeGenerationNamespace,
)


APPLICATION_LEASE_RECEIPT_SCHEMA = "ApplicationLeaseReceipt.v1"
CORE_STARTUP_CANDIDATE_SCHEMA = "CoreStartupCandidate.v2"
CORE_PRIVATE_STARTUP_ROOT_PROOF_SCHEMA = "CorePrivateStartupRootProof.v1"
PRODUCT_STARTUP_RECEIPT_SCHEMA = "ProductStartupReceipt.v3"
PRODUCT_STARTUP_CLEANUP_RECEIPT_SCHEMA = "ProductStartupCleanupReceipt.v1"
PRODUCT_STOP_REQUEST_RECEIPT_SCHEMA = "ProductStopRequestReceipt.v1"
PRODUCT_LIFECYCLE_STATUS_RECEIPT_SCHEMA = "ProductLifecycleStatusReceipt.v1"
PRODUCT_CHILD_TERMINATION_RECEIPT_SCHEMA = "ProductChildTerminationReceipt.v1"
PRODUCT_ZERO_SURVIVOR_RECEIPT_SCHEMA = "ProductZeroSurvivorReceipt.v1"
PRODUCT_SHUTDOWN_RECEIPT_SCHEMA = "ProductShutdownReceipt.v2"
PRODUCT_SUPERVISOR_OWNER = "54guc.product_supervisor"
FOUNDATION_STARTUP_RECEIPT_SCHEMA = "FoundationStartupReceipt.v3"
FOUNDATION_STARTUP_OWNER = "54guc.foundation.composition"
_FOUNDATION_STARTUP_KEYS = frozenset(
    {
        "schema",
        "owner",
        "disposition",
        "prepared_at",
        "namespace",
        "atomic_runtime_contract_id",
        "atomic_runtime_contract_version",
    }
)
_PRODUCT_STARTUP_KEYS = frozenset(
    {
        "schema",
        "owner",
        "disposition",
        "reason",
        "detail",
        "error_type",
        "core_candidate",
        "foundation",
        "intelligence",
        "lifecycle_generation",
        "lifecycle_state",
        "matrix_readiness",
        "classified_disposition",
        "classified_reason",
        "cleanup",
        "root_digest",
        "atomic_runtime_contract_id",
        "atomic_runtime_contract_version",
    }
)
_CORE_STARTUP_CANDIDATE_KEYS = frozenset(
    {
        "schema",
        "owner",
        "lifecycle_generation",
        "prepared_at",
        "config_generation_id",
        "config_fingerprint",
        "matrix_readiness",
        "private_root_proof",
        "candidate_digest",
    }
)
_CORE_PRIVATE_STARTUP_ROOT_PROOF_KEYS = frozenset(
    {
        "schema",
        "owner",
        "startup_attempt_id",
        "namespace",
        "matrix_identity",
        "accepted_root_fingerprint",
        "private_root_created_at",
    }
)
_INTELLIGENCE_STARTUP_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "owner",
        "boundary_id",
        "contract_version",
        "state",
        "customers",
        "attached",
        "last_fault",
    }
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _deep_freeze(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _deep_thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _strict_canonical_freeze(value: Any, *, path: str) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}_non_finite")
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path}_string_key_required")
            output[key] = _strict_canonical_freeze(
                item,
                path=f"{path}.{key}",
            )
        return MappingProxyType(output)
    if isinstance(value, (list, tuple)):
        return tuple(
            _strict_canonical_freeze(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{path}_canonical_value_required")


class ApplicationLeaseState(StrEnum):
    CLAIMED = "claimed"
    STARTING = "starting"
    PROCESS_RUNNING = "process_running"
    PRODUCT_READY = "product_ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            ApplicationLeaseState.STOPPED,
            ApplicationLeaseState.FAILED,
        }


@dataclass(frozen=True, slots=True)
class ApplicationLeaseIdentity:
    lease_id: str
    generation: int
    process_id: int
    process_started_at_unix_ms: int
    profile: str
    executable_path: str
    stage_dir: str

    def __post_init__(self) -> None:
        for field_name in (
            "lease_id",
            "profile",
            "executable_path",
            "stage_dir",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(
                    f"application_lease_identity_{field_name}_string_required"
                )
        for field_name in (
            "generation",
            "process_id",
            "process_started_at_unix_ms",
        ):
            if type(getattr(self, field_name)) is not int:
                raise TypeError(
                    f"application_lease_identity_{field_name}_int_required"
                )
        lease_id = self.lease_id.strip()
        profile = self.profile.strip()
        executable = self.executable_path.strip()
        stage_dir = self.stage_dir.strip()
        if not lease_id:
            raise ValueError("application_lease_id_required")
        if self.generation <= 0:
            raise ValueError("application_lease_generation_invalid")
        if self.process_id <= 0:
            raise ValueError("application_lease_process_id_invalid")
        if self.process_started_at_unix_ms <= 0:
            raise ValueError("application_lease_process_birth_invalid")
        if not profile or not executable or not stage_dir:
            raise ValueError("application_lease_process_identity_required")
        object.__setattr__(self, "lease_id", lease_id)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(
            self,
            "executable_path",
            str(Path(executable).resolve()),
        )
        object.__setattr__(
            self,
            "stage_dir",
            str(Path(stage_dir).resolve()),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "generation": self.generation,
            "process_id": self.process_id,
            "process_started_at_unix_ms": self.process_started_at_unix_ms,
            "profile": self.profile,
            "executable_path": self.executable_path,
            "stage_dir": self.stage_dir,
        }


@dataclass(frozen=True, slots=True)
class ApplicationLeaseReceipt:
    identity: ApplicationLeaseIdentity
    state: ApplicationLeaseState
    sequence: int
    claimed_at: float
    updated_at: float
    detail: Mapping[str, Any]
    schema: str = APPLICATION_LEASE_RECEIPT_SCHEMA
    owner: str = "54guc.application_lease"

    def __post_init__(self) -> None:
        def freeze_detail(value: Any, path: str) -> Any:
            if value is None or type(value) in {str, bool, int}:
                return value
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError(
                        f"application_lease_detail_{path}_finite_required"
                    )
                return value
            if isinstance(value, Mapping):
                frozen: dict[str, Any] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise TypeError(
                            "application_lease_detail_string_key_required"
                        )
                    frozen[key] = freeze_detail(item, f"{path}.{key}")
                return MappingProxyType(frozen)
            if isinstance(value, (list, tuple)):
                return tuple(
                    freeze_detail(item, f"{path}[{index}]")
                    for index, item in enumerate(value)
                )
            raise TypeError("application_lease_detail_json_value_required")

        if not isinstance(self.identity, ApplicationLeaseIdentity):
            raise TypeError("application_lease_identity_required")
        state = ApplicationLeaseState(self.state)
        if type(self.sequence) is not int:
            raise TypeError("application_lease_sequence_int_required")
        if self.sequence <= 0:
            raise ValueError("application_lease_sequence_invalid")
        if type(self.claimed_at) not in {int, float} or type(
            self.updated_at
        ) not in {int, float}:
            raise TypeError("application_lease_timestamp_number_required")
        claimed_at = float(self.claimed_at)
        updated_at = float(self.updated_at)
        if (
            not math.isfinite(claimed_at)
            or not math.isfinite(updated_at)
            or claimed_at <= 0.0
            or updated_at < claimed_at
        ):
            raise ValueError("application_lease_timestamp_invalid")
        if not isinstance(self.detail, Mapping):
            raise TypeError("application_lease_detail_mapping_required")
        if self.schema != APPLICATION_LEASE_RECEIPT_SCHEMA:
            raise ValueError("application_lease_schema_invalid")
        if self.owner != "54guc.application_lease":
            raise ValueError("application_lease_owner_invalid")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "claimed_at", claimed_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self,
            "detail",
            freeze_detail(self.detail, "root"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            **self.identity.to_payload(),
            "state": self.state.value,
            "terminal": self.state.terminal,
            "sequence": int(self.sequence),
            "claimed_at": float(self.claimed_at),
            "updated_at": float(self.updated_at),
            "detail": _deep_thaw(self.detail),
        }


class ProductLifecycleState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    QUIESCING = "quiescing"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DEGRADED = "degraded"

    @property
    def admits_service(self) -> bool:
        return self is ProductLifecycleState.RUNNING

    @property
    def terminal(self) -> bool:
        return self in {
            ProductLifecycleState.STOPPED,
            ProductLifecycleState.DEGRADED,
        }


class ProductStartupDisposition(StrEnum):
    READY = "ready"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class ProductStartupReason(StrEnum):
    STARTED = "started"
    PRODUCT_ALREADY_SHUTDOWN = "product_already_shutdown"
    PRODUCT_LIFECYCLE_NOT_STARTABLE = "product_lifecycle_not_startable"
    AUTHENTICATION_FAILED = "authentication_failed"
    WRONG_TARGET = "wrong_target"
    MODEM_IDENTITY_REQUIRED = "modem_identity_required"
    MODEM_TEMPORARILY_UNAVAILABLE = "modem_temporarily_unavailable"
    CONNECTION_ATTEMPTS_EXHAUSTED = "connection_attempts_exhausted"
    PRODUCT_STARTUP_FAULT = "product_startup_fault"
    PRODUCT_STARTUP_CLEANUP_REQUIRED = "product_startup_cleanup_required"
    STARTUP_CANCELLED_BY_SHUTDOWN = "startup_cancelled_by_shutdown"


class FoundationStartupDisposition(StrEnum):
    PREPARED = "prepared"


class ProductTerminationDisposition(StrEnum):
    COMPLETE = "complete"
    ALREADY_COMPLETE = "already_complete"
    DEGRADED = "degraded"
    RETAINED = "retained"

    @property
    def terminated(self) -> bool:
        return self in {
            ProductTerminationDisposition.COMPLETE,
            ProductTerminationDisposition.ALREADY_COMPLETE,
        }


class ProductShutdownDisposition(StrEnum):
    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"
    DEGRADED = "degraded"


class ProductRuntimeWorkerReplacementReason(StrEnum):
    FOREIGN_RUNTIME_NAMESPACE = "foreign_runtime_namespace"
    PHYSICAL_DEVICE_CHANGED = "physical_device_changed"
    AUTOMATIC_LOGIN_FAILED = "automatic_login_failed"


class ProductRuntimeWorkerReplacementRequired(RuntimeError):
    """Typed signal that replacement must occur at the worker boundary."""

    def __init__(
        self,
        *,
        current_namespace: RuntimeGenerationNamespace,
        requested_namespace: RuntimeGenerationNamespace,
        reason: ProductRuntimeWorkerReplacementReason = (
            ProductRuntimeWorkerReplacementReason.FOREIGN_RUNTIME_NAMESPACE
        ),
    ) -> None:
        if not isinstance(current_namespace, RuntimeGenerationNamespace) or not (
            isinstance(requested_namespace, RuntimeGenerationNamespace)
        ):
            raise TypeError(
                "product_worker_replacement_typed_namespaces_required"
            )
        if not isinstance(reason, ProductRuntimeWorkerReplacementReason):
            raise TypeError("product_worker_replacement_typed_reason_required")
        self.current_namespace = current_namespace
        self.requested_namespace = requested_namespace
        self.reason = reason
        super().__init__(
            "runtime namespace replacement requires new worker generation"
        )


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _deep_thaw(_strict_canonical_freeze(value, path="digest")),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CorePrivateStartupRootProof:
    """Exact identity of the first Core root retained before Product READY."""

    startup_attempt_id: str
    namespace: RuntimeGenerationNamespace
    matrix_identity: MatrixGenerationIdentity
    accepted_root_fingerprint: str
    private_root_created_at: float
    schema: str = CORE_PRIVATE_STARTUP_ROOT_PROOF_SCHEMA
    owner: str = "54guc.core.composition"

    def __post_init__(self) -> None:
        for field_name in ("startup_attempt_id",):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"core_private_startup_root_{field_name}_invalid"
                )
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "core_private_startup_root_namespace_required"
            )
        if not isinstance(
            self.matrix_identity,
            MatrixGenerationIdentity,
        ):
            raise TypeError(
                "core_private_startup_root_matrix_identity_required"
            )
        if self.matrix_identity.namespace != self.namespace:
            raise ValueError(
                "core_private_startup_root_namespace_mismatch"
            )
        fingerprint = self.accepted_root_fingerprint
        if (
            type(fingerprint) is not str
            or len(fingerprint) != 64
            or fingerprint != fingerprint.lower()
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError(
                "core_private_startup_root_fingerprint_invalid"
            )
        if type(self.private_root_created_at) not in {int, float}:
            raise TypeError(
                "core_private_startup_root_created_at_number_required"
            )
        created_at = float(self.private_root_created_at)
        if not math.isfinite(created_at) or created_at <= 0.0:
            raise ValueError(
                "core_private_startup_root_created_at_invalid"
            )
        if self.schema != CORE_PRIVATE_STARTUP_ROOT_PROOF_SCHEMA:
            raise ValueError(
                "core_private_startup_root_schema_invalid"
            )
        if self.owner != "54guc.core.composition":
            raise ValueError(
                "core_private_startup_root_owner_invalid"
            )
        object.__setattr__(
            self,
            "private_root_created_at",
            created_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "startup_attempt_id": self.startup_attempt_id,
            "namespace": self.namespace.as_dict(),
            "matrix_identity": self.matrix_identity.as_dict(),
            "accepted_root_fingerprint": (
                self.accepted_root_fingerprint
            ),
            "private_root_created_at": self.private_root_created_at,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "CorePrivateStartupRootProof":
        if not isinstance(value, Mapping):
            raise TypeError(
                "core_private_startup_root_mapping_required"
            )
        if frozenset(value) != _CORE_PRIVATE_STARTUP_ROOT_PROOF_KEYS:
            raise ValueError(
                "core_private_startup_root_mapping_keys_invalid"
            )
        return cls(
            startup_attempt_id=value["startup_attempt_id"],
            namespace=RuntimeGenerationNamespace.from_mapping(
                value["namespace"]
            ),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                value["matrix_identity"]
            ),
            accepted_root_fingerprint=value[
                "accepted_root_fingerprint"
            ],
            private_root_created_at=value[
                "private_root_created_at"
            ],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (
            _restore_core_private_startup_root_proof,
            (self.as_dict(),),
        )


@dataclass(frozen=True, slots=True)
class CoreStartupCandidate:
    """Exact internally prepared Core root; it grants no external admission."""

    lifecycle_generation: int
    prepared_at: float
    config_generation_id: str
    config_fingerprint: str
    matrix_readiness: ModemLiveReadyReceipt
    private_root_proof: CorePrivateStartupRootProof
    candidate_digest: str = ""
    schema: str = CORE_STARTUP_CANDIDATE_SCHEMA
    owner: str = "54guc.core.composition"

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int:
            raise TypeError("core_startup_candidate_generation_int_required")
        if self.lifecycle_generation <= 0:
            raise ValueError("core_startup_candidate_generation_invalid")
        if type(self.prepared_at) not in {int, float}:
            raise TypeError("core_startup_candidate_prepared_at_number_required")
        prepared_at = float(self.prepared_at)
        if not math.isfinite(prepared_at) or prepared_at <= 0.0:
            raise ValueError("core_startup_candidate_prepared_at_invalid")
        for field_name in ("config_generation_id", "config_fingerprint"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(
                    f"core_startup_candidate_{field_name}_required"
                )
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(
            self.matrix_readiness,
            ModemLiveReadyReceipt,
        ):
            raise TypeError("core_startup_candidate_matrix_readiness_required")
        if not self.matrix_readiness.ready:
            raise ValueError("core_startup_candidate_matrix_not_ready")
        if (
            self.matrix_readiness.lifecycle_generation
            != self.lifecycle_generation
        ):
            raise ValueError("core_startup_candidate_lifecycle_mismatch")
        if not isinstance(
            self.private_root_proof,
            CorePrivateStartupRootProof,
        ):
            raise TypeError(
                "core_startup_candidate_private_root_proof_required"
            )
        if (
            self.private_root_proof.namespace
            != self.matrix_readiness.matrix_identity.namespace
            or self.private_root_proof.matrix_identity
            != self.matrix_readiness.matrix_identity
        ):
            raise ValueError(
                "core_startup_candidate_private_root_matrix_mismatch"
            )
        if (
            self.private_root_proof.startup_attempt_id
            != self.matrix_readiness.startup_sweep.acquisition_attempt_id
        ):
            raise ValueError(
                "core_startup_candidate_private_root_attempt_mismatch"
            )
        if self.schema != CORE_STARTUP_CANDIDATE_SCHEMA:
            raise ValueError("core_startup_candidate_schema_invalid")
        if self.owner != "54guc.core.composition":
            raise ValueError("core_startup_candidate_owner_invalid")
        expected = _canonical_digest(self._digest_material())
        if self.candidate_digest and self.candidate_digest != expected:
            raise ValueError("core_startup_candidate_digest_mismatch")
        object.__setattr__(self, "prepared_at", prepared_at)
        object.__setattr__(self, "candidate_digest", expected)

    def _digest_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "prepared_at": float(self.prepared_at),
            "config_generation_id": self.config_generation_id,
            "config_fingerprint": self.config_fingerprint,
            "matrix_readiness": self.matrix_readiness.as_dict(),
            "private_root_proof": self.private_root_proof.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._digest_material(),
            "candidate_digest": self.candidate_digest,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CoreStartupCandidate":
        if not isinstance(value, Mapping):
            raise TypeError("core_startup_candidate_mapping_required")
        if frozenset(value) != _CORE_STARTUP_CANDIDATE_KEYS:
            raise ValueError("core_startup_candidate_mapping_keys_invalid")
        return cls(
            lifecycle_generation=value["lifecycle_generation"],
            prepared_at=value["prepared_at"],
            config_generation_id=value["config_generation_id"],
            config_fingerprint=value["config_fingerprint"],
            matrix_readiness=ModemLiveReadyReceipt.from_mapping(
                value["matrix_readiness"]
            ),
            private_root_proof=CorePrivateStartupRootProof.from_mapping(
                value["private_root_proof"]
            ),
            candidate_digest=value["candidate_digest"],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (_restore_core_startup_candidate, (self.as_dict(),))


@dataclass(frozen=True, slots=True)
class FoundationStartupReceipt:
    """Exact typed proof that Foundation prepared one runtime namespace."""

    disposition: FoundationStartupDisposition
    namespace: RuntimeGenerationNamespace
    prepared_at: float
    atomic_runtime_contract_id: str = ATOMIC_RUNTIME_CONTRACT_ID
    atomic_runtime_contract_version: int = ATOMIC_RUNTIME_CONTRACT_VERSION
    schema: str = FOUNDATION_STARTUP_RECEIPT_SCHEMA
    owner: str = FOUNDATION_STARTUP_OWNER

    def __post_init__(self) -> None:
        if not isinstance(
            self.disposition,
            FoundationStartupDisposition,
        ):
            raise TypeError(
                "foundation_startup_disposition_typed_required"
            )
        disposition = self.disposition
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_startup_namespace_required")
        if type(self.prepared_at) not in {int, float}:
            raise TypeError("foundation_startup_timestamp_number_required")
        prepared_at = float(self.prepared_at)
        if not math.isfinite(prepared_at) or prepared_at <= 0.0:
            raise ValueError("foundation_startup_timestamp_invalid")
        if self.atomic_runtime_contract_id != ATOMIC_RUNTIME_CONTRACT_ID:
            raise ValueError("foundation_startup_atomic_contract_id_invalid")
        if (
            type(self.atomic_runtime_contract_version) is not int
            or self.atomic_runtime_contract_version
            != ATOMIC_RUNTIME_CONTRACT_VERSION
        ):
            raise ValueError(
                "foundation_startup_atomic_contract_version_invalid"
            )
        if self.schema != FOUNDATION_STARTUP_RECEIPT_SCHEMA:
            raise ValueError("foundation_startup_receipt_schema_invalid")
        if self.owner != FOUNDATION_STARTUP_OWNER:
            raise ValueError("foundation_startup_receipt_owner_invalid")
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "prepared_at", prepared_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "disposition": self.disposition.value,
            "prepared_at": self.prepared_at,
            "namespace": self.namespace.as_dict(),
            "atomic_runtime_contract_id": self.atomic_runtime_contract_id,
            "atomic_runtime_contract_version": (
                self.atomic_runtime_contract_version
            ),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "FoundationStartupReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("foundation_startup_mapping_required")
        if frozenset(value) != _FOUNDATION_STARTUP_KEYS:
            raise ValueError("foundation_startup_mapping_keys_invalid")
        return cls(
            disposition=FoundationStartupDisposition(value["disposition"]),
            namespace=RuntimeGenerationNamespace.from_mapping(
                value["namespace"]
            ),
            prepared_at=value["prepared_at"],
            atomic_runtime_contract_id=value["atomic_runtime_contract_id"],
            atomic_runtime_contract_version=value[
                "atomic_runtime_contract_version"
            ],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (_restore_foundation_startup_receipt, (self.as_dict(),))


@dataclass(frozen=True, slots=True)
class IntelligenceStartupEvidence:
    """Exact proof of the optional Product Intelligence Matrix attachment."""

    boundary_id: str = "54guc.product.intelligence_matrix"
    contract_version: str = "ProductIntelligenceMatrix.v1"
    state: str = "unattached"
    customers: tuple[str, ...] = ()
    attached: bool = False
    last_fault: str = ""
    schema: str = "IntelligenceStartupEvidence.v2"
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if self.schema != "IntelligenceStartupEvidence.v2":
            raise ValueError("intelligence_startup_evidence_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("intelligence_startup_evidence_owner_invalid")
        if self.boundary_id != "54guc.product.intelligence_matrix":
            raise ValueError("intelligence_startup_boundary_id_invalid")
        if self.contract_version != "ProductIntelligenceMatrix.v1":
            raise ValueError("intelligence_startup_contract_version_invalid")
        if self.state not in {
            "unattached",
            "attached",
            "degraded",
            "detached",
        }:
            raise ValueError("intelligence_startup_state_invalid")
        if not isinstance(self.customers, tuple):
            raise TypeError("intelligence_startup_customers_tuple_required")
        if len(self.customers) > 1:
            raise ValueError("intelligence_startup_customer_limit_exceeded")
        expected_attached = bool(
            self.customers and self.state in {"attached", "degraded"}
        )
        if self.attached is not expected_attached:
            raise ValueError("intelligence_startup_attachment_state_mismatch")
        if type(self.last_fault) is not str:
            raise TypeError("intelligence_startup_last_fault_string_required")
        if self.state == "degraded" and not self.last_fault:
            raise ValueError("intelligence_startup_fault_reason_required")
        if self.state != "degraded" and self.last_fault:
            raise ValueError("intelligence_startup_nonfault_reason_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "boundary_id": self.boundary_id,
            "contract_version": self.contract_version,
            "state": self.state,
            "customers": list(self.customers),
            "attached": self.attached,
            "last_fault": self.last_fault,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "IntelligenceStartupEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("intelligence_startup_evidence_mapping_required")
        if frozenset(value) != _INTELLIGENCE_STARTUP_EVIDENCE_KEYS:
            raise ValueError("intelligence_startup_evidence_keys_invalid")
        customers = value["customers"]
        if not isinstance(customers, list):
            raise TypeError("intelligence_startup_customers_list_required")
        if any(type(item) is not str for item in customers):
            raise TypeError("intelligence_startup_customer_string_required")
        return cls(
            boundary_id=value["boundary_id"],
            contract_version=value["contract_version"],
            state=value["state"],
            customers=tuple(customers),
            attached=value["attached"],
            last_fault=value["last_fault"],
            schema=value["schema"],
            owner=value["owner"],
        )


@dataclass(frozen=True, slots=True)
class ProductChildTerminationReceipt:
    child_id: str
    lifecycle_generation: int
    disposition: ProductTerminationDisposition
    survivors: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema: str = PRODUCT_CHILD_TERMINATION_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if type(self.child_id) is not str or not self.child_id.strip():
            raise ValueError("product_child_termination_child_id_required")
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError(
                "product_child_termination_generation_invalid"
            )
        disposition = ProductTerminationDisposition(self.disposition)
        if not isinstance(self.survivors, tuple) or any(
            type(item) is not str or not item.strip()
            for item in self.survivors
        ):
            raise TypeError("product_child_termination_survivors_invalid")
        if not isinstance(self.errors, tuple) or any(
            type(item) is not str or not item.strip() for item in self.errors
        ):
            raise TypeError("product_child_termination_errors_invalid")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("product_child_termination_evidence_mapping_required")
        if disposition.terminated and (self.survivors or self.errors):
            raise ValueError(
                "product_child_termination_complete_with_failure_evidence"
            )
        if self.schema != PRODUCT_CHILD_TERMINATION_RECEIPT_SCHEMA:
            raise ValueError("product_child_termination_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_child_termination_owner_invalid")
        object.__setattr__(self, "child_id", self.child_id.strip())
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(
            self,
            "survivors",
            tuple(item.strip() for item in self.survivors),
        )
        object.__setattr__(
            self,
            "errors",
            tuple(item.strip() for item in self.errors),
        )
        object.__setattr__(
            self,
            "evidence",
            _strict_canonical_freeze(
                self.evidence,
                path="product_child_termination_evidence",
            ),
        )

    @property
    def terminated(self) -> bool:
        return self.disposition.terminated

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "child_id": self.child_id,
            "lifecycle_generation": self.lifecycle_generation,
            "disposition": self.disposition.value,
            "survivors": list(self.survivors),
            "errors": list(self.errors),
            "evidence": _deep_thaw(self.evidence),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProductChildTerminationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("product_child_termination_mapping_required")
        if set(value) != {
            "schema",
            "owner",
            "child_id",
            "lifecycle_generation",
            "disposition",
            "survivors",
            "errors",
            "evidence",
        }:
            raise ValueError("product_child_termination_mapping_keys_invalid")
        if not isinstance(value["survivors"], list) or not isinstance(
            value["errors"], list
        ):
            raise TypeError("product_child_termination_list_required")
        return cls(
            child_id=value["child_id"],
            lifecycle_generation=value["lifecycle_generation"],
            disposition=ProductTerminationDisposition(value["disposition"]),
            survivors=tuple(value["survivors"]),
            errors=tuple(value["errors"]),
            evidence=value["evidence"],
            schema=value["schema"],
            owner=value["owner"],
        )


@dataclass(frozen=True, slots=True)
class ProductZeroSurvivorReceipt:
    lifecycle_generation: int
    survivors: tuple[str, ...]
    complete: bool
    schema: str = PRODUCT_ZERO_SURVIVOR_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError("product_zero_survivor_generation_invalid")
        if not isinstance(self.survivors, tuple) or any(
            type(item) is not str or not item.strip()
            for item in self.survivors
        ):
            raise TypeError("product_zero_survivor_survivors_invalid")
        if type(self.complete) is not bool:
            raise TypeError("product_zero_survivor_complete_bool_required")
        normalized = tuple(sorted(set(item.strip() for item in self.survivors)))
        if self.complete != (not normalized):
            raise ValueError("product_zero_survivor_complete_mismatch")
        if self.schema != PRODUCT_ZERO_SURVIVOR_RECEIPT_SCHEMA:
            raise ValueError("product_zero_survivor_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_zero_survivor_owner_invalid")
        object.__setattr__(self, "survivors", normalized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "survivors": list(self.survivors),
            "complete": self.complete,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProductZeroSurvivorReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "owner",
            "lifecycle_generation",
            "survivors",
            "complete",
        }:
            raise ValueError("product_zero_survivor_mapping_invalid")
        if not isinstance(value["survivors"], list):
            raise TypeError("product_zero_survivor_list_required")
        return cls(
            lifecycle_generation=value["lifecycle_generation"],
            survivors=tuple(value["survivors"]),
            complete=value["complete"],
            schema=value["schema"],
            owner=value["owner"],
        )


@dataclass(frozen=True, slots=True)
class ProductStartupCleanupReceipt:
    lifecycle_generation: int
    children: tuple[ProductChildTerminationReceipt, ...]
    zero_survivor: ProductZeroSurvivorReceipt
    composition_rebuilt: bool = False
    errors: tuple[str, ...] = ()
    schema: str = PRODUCT_STARTUP_CLEANUP_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError("product_startup_cleanup_generation_invalid")
        if not isinstance(self.children, tuple) or any(
            not isinstance(item, ProductChildTerminationReceipt)
            for item in self.children
        ):
            raise TypeError("product_startup_cleanup_children_invalid")
        if not isinstance(self.zero_survivor, ProductZeroSurvivorReceipt):
            raise TypeError("product_startup_cleanup_zero_survivor_required")
        if (
            self.zero_survivor.lifecycle_generation
            != self.lifecycle_generation
        ):
            raise ValueError("product_startup_cleanup_generation_mismatch")
        child_survivors = tuple(
            survivor
            for child in self.children
            for survivor in child.survivors
        )
        if tuple(sorted(set(child_survivors))) != self.zero_survivor.survivors:
            raise ValueError("product_startup_cleanup_survivor_mismatch")
        if type(self.composition_rebuilt) is not bool:
            raise TypeError("product_startup_cleanup_rebuilt_bool_required")
        if not isinstance(self.errors, tuple) or any(
            type(item) is not str or not item.strip() for item in self.errors
        ):
            raise TypeError("product_startup_cleanup_errors_invalid")
        if self.zero_survivor.complete and any(
            not child.terminated for child in self.children
        ):
            raise ValueError("product_startup_cleanup_child_not_terminated")
        if self.schema != PRODUCT_STARTUP_CLEANUP_RECEIPT_SCHEMA:
            raise ValueError("product_startup_cleanup_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_startup_cleanup_owner_invalid")

    @property
    def clean(self) -> bool:
        return self.zero_survivor.complete and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "children": [item.as_dict() for item in self.children],
            "zero_survivor": self.zero_survivor.as_dict(),
            "composition_rebuilt": self.composition_rebuilt,
            "errors": list(self.errors),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProductStartupCleanupReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "owner",
            "lifecycle_generation",
            "children",
            "zero_survivor",
            "composition_rebuilt",
            "errors",
        }:
            raise ValueError("product_startup_cleanup_mapping_invalid")
        if not isinstance(value["children"], list) or not isinstance(
            value["errors"], list
        ):
            raise TypeError("product_startup_cleanup_list_required")
        return cls(
            lifecycle_generation=value["lifecycle_generation"],
            children=tuple(
                ProductChildTerminationReceipt.from_mapping(item)
                for item in value["children"]
            ),
            zero_survivor=ProductZeroSurvivorReceipt.from_mapping(
                value["zero_survivor"]
            ),
            composition_rebuilt=value["composition_rebuilt"],
            errors=tuple(value["errors"]),
            schema=value["schema"],
            owner=value["owner"],
        )


@dataclass(frozen=True, slots=True)
class ProductStartupReceipt:
    """Shared exact Product startup proof consumed by lifecycle boundaries."""

    disposition: ProductStartupDisposition
    reason: ProductStartupReason
    lifecycle_generation: int
    lifecycle_state: str
    matrix_readiness: ModemLiveReadyReceipt | None = None
    detail: str = ""
    error_type: str = ""
    core_candidate: CoreStartupCandidate | None = None
    foundation: FoundationStartupReceipt | None = None
    intelligence: IntelligenceStartupEvidence = field(
        default_factory=IntelligenceStartupEvidence
    )
    classified_disposition: ProductStartupDisposition | None = None
    classified_reason: ProductStartupReason | None = None
    cleanup: ProductStartupCleanupReceipt | None = None
    root_digest: str = ""
    atomic_runtime_contract_id: str = ATOMIC_RUNTIME_CONTRACT_ID
    atomic_runtime_contract_version: int = ATOMIC_RUNTIME_CONTRACT_VERSION
    schema: str = PRODUCT_STARTUP_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if not isinstance(
            self.disposition,
            ProductStartupDisposition,
        ):
            raise TypeError(
                "product_startup_disposition_typed_required"
            )
        if not isinstance(self.reason, ProductStartupReason):
            raise TypeError("product_startup_reason_typed_required")
        disposition = self.disposition
        reason = self.reason
        if type(self.lifecycle_generation) is not int:
            raise TypeError("product_startup_lifecycle_generation_int_required")
        if type(self.lifecycle_state) is not str:
            raise TypeError("product_startup_lifecycle_state_string_required")
        for field_name in (
            "detail",
            "error_type",
            "root_digest",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(
                    f"product_startup_{field_name}_string_required"
                )
        if not isinstance(self.intelligence, IntelligenceStartupEvidence):
            raise TypeError(
                "product_startup_intelligence_evidence_required"
            )
        if (
            self.classified_disposition is not None
            and not isinstance(
                self.classified_disposition,
                ProductStartupDisposition,
            )
        ):
            raise TypeError(
                "product_startup_classified_disposition_typed_required"
            )
        if (
            self.classified_reason is not None
            and not isinstance(
                self.classified_reason,
                ProductStartupReason,
            )
        ):
            raise TypeError(
                "product_startup_classified_reason_typed_required"
            )
        if self.cleanup is not None and not isinstance(
            self.cleanup,
            ProductStartupCleanupReceipt,
        ):
            raise TypeError("product_startup_cleanup_receipt_required")
        generation = self.lifecycle_generation
        if generation <= 0:
            raise ValueError("product_startup_lifecycle_generation_required")
        if self.schema != PRODUCT_STARTUP_RECEIPT_SCHEMA:
            raise ValueError("product_startup_receipt_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_startup_receipt_owner_invalid")
        if self.atomic_runtime_contract_id != ATOMIC_RUNTIME_CONTRACT_ID:
            raise ValueError("product_startup_atomic_contract_id_invalid")
        if (
            type(self.atomic_runtime_contract_version) is not int
            or self.atomic_runtime_contract_version
            != ATOMIC_RUNTIME_CONTRACT_VERSION
        ):
            raise ValueError("product_startup_atomic_contract_version_invalid")
        readiness = self.matrix_readiness
        state = self.lifecycle_state
        if disposition is ProductStartupDisposition.READY:
            if state != ProductLifecycleState.RUNNING.value:
                raise ValueError(
                    "product_startup_ready_requires_running_state"
                )
        elif disposition is ProductStartupDisposition.RETRYABLE:
            if state != ProductLifecycleState.IDLE.value:
                raise ValueError(
                    "product_startup_retryable_requires_idle_state"
                )
        elif state not in {
            ProductLifecycleState.STOPPED.value,
            ProductLifecycleState.DEGRADED.value,
            ProductLifecycleState.STOP_REQUESTED.value,
        }:
            raise ValueError(
                "product_startup_terminal_state_invalid"
            )
        if disposition is ProductStartupDisposition.READY:
            if not isinstance(
                readiness,
                ModemLiveReadyReceipt,
            ):
                raise TypeError(
                    "product_startup_ready_matrix_receipt_required"
                )
            if readiness.lifecycle_generation != generation:
                raise ValueError(
                    "product_startup_matrix_lifecycle_mismatch"
                )
            if not readiness.ready:
                raise ValueError(
                    "product_startup_matrix_monitoring_not_ready"
                )
            foundation = self.foundation
            core_candidate = self.core_candidate
            if not isinstance(core_candidate, CoreStartupCandidate):
                raise ValueError(
                    "product_startup_core_candidate_invalid"
                )
            if core_candidate.matrix_readiness != readiness:
                raise ValueError(
                    "product_startup_core_candidate_matrix_mismatch"
                )
            if not isinstance(foundation, FoundationStartupReceipt):
                raise ValueError(
                    "product_startup_foundation_receipt_invalid"
                )
            if foundation.namespace != readiness.matrix_identity.namespace:
                raise ValueError(
                    "product_startup_foundation_matrix_mismatch"
                )
        elif (
            self.matrix_readiness is not None
            or self.core_candidate is not None
            or self.foundation is not None
        ):
            raise ValueError(
                "product_startup_nonready_matrix_truth_forbidden"
            )
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "lifecycle_generation", generation)
        object.__setattr__(
            self,
            "lifecycle_state",
            state,
        )
        expected_root_digest = (
            _canonical_digest(self._root_material())
            if disposition is ProductStartupDisposition.READY
            else ""
        )
        if self.root_digest and self.root_digest != expected_root_digest:
            raise ValueError("product_startup_root_digest_mismatch")
        object.__setattr__(self, "root_digest", expected_root_digest)

    def _root_material(self) -> dict[str, Any]:
        if (
            self.core_candidate is None
            or self.foundation is None
            or self.matrix_readiness is None
        ):
            raise RuntimeError("product_startup_root_incomplete")
        return {
            "schema": self.schema,
            "owner": self.owner,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_state": self.lifecycle_state,
            "matrix_readiness": self.matrix_readiness.as_dict(),
            "core_candidate": self.core_candidate.as_dict(),
            "foundation": self.foundation.as_dict(),
            "intelligence": self.intelligence.as_dict(),
            "atomic_runtime_contract_id": self.atomic_runtime_contract_id,
            "atomic_runtime_contract_version": (
                self.atomic_runtime_contract_version
            ),
        }

    @property
    def ready(self) -> bool:
        return self.disposition is ProductStartupDisposition.READY

    @property
    def retryable(self) -> bool:
        return self.disposition is ProductStartupDisposition.RETRYABLE

    @property
    def terminal(self) -> bool:
        return self.disposition is ProductStartupDisposition.TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "error_type": self.error_type,
            "core_candidate": (
                self.core_candidate.as_dict()
                if self.core_candidate is not None
                else {}
            ),
            "foundation": (
                self.foundation.as_dict()
                if self.foundation is not None
                else {}
            ),
            "intelligence": self.intelligence.as_dict(),
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_state": self.lifecycle_state,
            "matrix_readiness": (
                self.matrix_readiness.as_dict()
                if self.matrix_readiness is not None
                else {}
            ),
            "classified_disposition": (
                self.classified_disposition.value
                if self.classified_disposition is not None
                else ""
            ),
            "classified_reason": (
                self.classified_reason.value
                if self.classified_reason is not None
                else ""
            ),
            "cleanup": (
                self.cleanup.as_dict() if self.cleanup is not None else {}
            ),
            "root_digest": self.root_digest,
            "atomic_runtime_contract_id": self.atomic_runtime_contract_id,
            "atomic_runtime_contract_version": (
                self.atomic_runtime_contract_version
            ),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProductStartupReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("product_startup_mapping_required")
        if frozenset(value) != _PRODUCT_STARTUP_KEYS:
            raise ValueError("product_startup_mapping_keys_invalid")
        matrix_value = value["matrix_readiness"]
        core_value = value["core_candidate"]
        foundation_value = value["foundation"]
        if not isinstance(matrix_value, Mapping):
            raise TypeError(
                "product_startup_matrix_readiness_mapping_required"
            )
        if not isinstance(foundation_value, Mapping):
            raise TypeError("product_startup_foundation_mapping_required")
        if not isinstance(core_value, Mapping):
            raise TypeError("product_startup_core_candidate_mapping_required")
        if not isinstance(value["intelligence"], Mapping):
            raise TypeError(
                "product_startup_intelligence_evidence_mapping_required"
            )
        if not isinstance(value["cleanup"], Mapping):
            raise TypeError("product_startup_cleanup_mapping_required")
        return cls(
            disposition=ProductStartupDisposition(value["disposition"]),
            reason=ProductStartupReason(value["reason"]),
            lifecycle_generation=value["lifecycle_generation"],
            lifecycle_state=value["lifecycle_state"],
            matrix_readiness=(
                ModemLiveReadyReceipt.from_mapping(matrix_value)
                if matrix_value
                else None
            ),
            detail=value["detail"],
            error_type=value["error_type"],
            core_candidate=(
                CoreStartupCandidate.from_mapping(core_value)
                if core_value
                else None
            ),
            foundation=(
                FoundationStartupReceipt.from_mapping(foundation_value)
                if foundation_value
                else None
            ),
            intelligence=IntelligenceStartupEvidence.from_mapping(
                value["intelligence"]
            ),
            classified_disposition=(
                ProductStartupDisposition(value["classified_disposition"])
                if value["classified_disposition"]
                else None
            ),
            classified_reason=(
                ProductStartupReason(value["classified_reason"])
                if value["classified_reason"]
                else None
            ),
            cleanup=(
                ProductStartupCleanupReceipt.from_mapping(value["cleanup"])
                if value["cleanup"]
                else None
            ),
            root_digest=value["root_digest"],
            atomic_runtime_contract_id=value["atomic_runtime_contract_id"],
            atomic_runtime_contract_version=value[
                "atomic_runtime_contract_version"
            ],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (_restore_product_startup_receipt, (self.as_dict(),))


class ProductRuntimeAdmissionAuthority:
    """The one atomic externally visible Product admission fact."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accepted_root: ProductStartupReceipt | None = None

    def install(self, receipt: ProductStartupReceipt) -> bool:
        if not isinstance(receipt, ProductStartupReceipt) or not receipt.ready:
            raise TypeError("product_admission_ready_root_required")
        if not receipt.root_digest:
            raise ValueError("product_admission_root_digest_required")
        with self._lock:
            current = self._accepted_root
            if current is not None:
                if current == receipt:
                    return False
                raise RuntimeError("product_admission_root_overlap")
            self._accepted_root = receipt
            return True

    def accepted_root(self) -> ProductStartupReceipt:
        with self._lock:
            receipt = self._accepted_root
        if receipt is None:
            raise RuntimeError("product_external_admission_closed")
        return receipt

    def admits(self, receipt: ProductStartupReceipt) -> bool:
        if not isinstance(receipt, ProductStartupReceipt):
            return False
        with self._lock:
            return self._accepted_root == receipt

    def require_runtime_namespace(
        self,
        receipt: ProductStartupReceipt,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        if not isinstance(receipt, ProductStartupReceipt):
            raise TypeError("product_runtime_namespace_root_required")
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("product_runtime_namespace_typed_identity_required")
        with self._lock:
            if self._accepted_root != receipt:
                raise RuntimeError("product_external_admission_closed")
            readiness = receipt.matrix_readiness
            if readiness is None:
                raise RuntimeError("product_runtime_namespace_root_incomplete")
            current = readiness.matrix_identity.namespace
            if namespace != current:
                raise ProductRuntimeWorkerReplacementRequired(
                    current_namespace=current,
                    requested_namespace=namespace,
                )
            return current

    def revoke(self, receipt: ProductStartupReceipt) -> None:
        if not isinstance(receipt, ProductStartupReceipt):
            raise TypeError("product_admission_revoke_root_required")
        with self._lock:
            if self._accepted_root != receipt:
                raise RuntimeError("product_admission_revoke_root_mismatch")
            self._accepted_root = None


@dataclass(frozen=True, slots=True)
class ProductStopRequestReceipt:
    lifecycle_generation: int
    lifecycle_state: ProductLifecycleState
    startup_inflight: bool
    service_inflight: int
    requested_at: float
    schema: str = PRODUCT_STOP_REQUEST_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation < 0:
            raise ValueError("product_stop_request_generation_invalid")
        state = ProductLifecycleState(self.lifecycle_state)
        if type(self.startup_inflight) is not bool:
            raise TypeError("product_stop_request_startup_bool_required")
        if type(self.service_inflight) is not int or self.service_inflight < 0:
            raise ValueError("product_stop_request_service_inflight_invalid")
        if type(self.requested_at) not in {int, float}:
            raise TypeError("product_stop_request_timestamp_number_required")
        requested_at = float(self.requested_at)
        if not math.isfinite(requested_at) or requested_at <= 0.0:
            raise ValueError("product_stop_request_timestamp_invalid")
        if self.schema != PRODUCT_STOP_REQUEST_RECEIPT_SCHEMA:
            raise ValueError("product_stop_request_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_stop_request_owner_invalid")
        object.__setattr__(self, "lifecycle_state", state)
        object.__setattr__(self, "requested_at", requested_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_state": self.lifecycle_state.value,
            "startup_inflight": self.startup_inflight,
            "service_inflight": self.service_inflight,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class ProductLifecycleStatusReceipt:
    lifecycle_generation: int
    lifecycle_state: ProductLifecycleState
    stop_requested: bool
    startup_inflight: bool
    service_inflight: int
    shutdown_coordinator: bool
    accepted_root_digest: str = ""
    schema: str = PRODUCT_LIFECYCLE_STATUS_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation < 0:
            raise ValueError("product_lifecycle_status_generation_invalid")
        state = ProductLifecycleState(self.lifecycle_state)
        for field_name in (
            "stop_requested",
            "startup_inflight",
            "shutdown_coordinator",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"product_lifecycle_status_{field_name}_bool_required")
        if type(self.service_inflight) is not int or self.service_inflight < 0:
            raise ValueError("product_lifecycle_status_service_inflight_invalid")
        if type(self.accepted_root_digest) is not str:
            raise TypeError("product_lifecycle_status_root_digest_string_required")
        if state is ProductLifecycleState.RUNNING and not self.accepted_root_digest:
            raise ValueError("product_lifecycle_status_running_root_required")
        if state is not ProductLifecycleState.RUNNING and self.accepted_root_digest:
            raise ValueError("product_lifecycle_status_nonrunning_root_forbidden")
        if self.schema != PRODUCT_LIFECYCLE_STATUS_RECEIPT_SCHEMA:
            raise ValueError("product_lifecycle_status_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_lifecycle_status_owner_invalid")
        object.__setattr__(self, "lifecycle_state", state)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_state": self.lifecycle_state.value,
            "stop_requested": self.stop_requested,
            "startup_inflight": self.startup_inflight,
            "service_inflight": self.service_inflight,
            "shutdown_coordinator": self.shutdown_coordinator,
            "accepted_root_digest": self.accepted_root_digest,
        }


@dataclass(frozen=True, slots=True)
class ProductShutdownReceipt:
    disposition: ProductShutdownDisposition
    lifecycle_generation: int
    lifecycle_state: ProductLifecycleState
    children: tuple[ProductChildTerminationReceipt, ...]
    zero_survivor: ProductZeroSurvivorReceipt
    errors: tuple[str, ...]
    finished_at: float
    schema: str = PRODUCT_SHUTDOWN_RECEIPT_SCHEMA
    owner: str = PRODUCT_SUPERVISOR_OWNER

    def __post_init__(self) -> None:
        disposition = ProductShutdownDisposition(self.disposition)
        state = ProductLifecycleState(self.lifecycle_state)
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError("product_shutdown_generation_invalid")
        if not isinstance(self.children, tuple) or any(
            not isinstance(item, ProductChildTerminationReceipt)
            for item in self.children
        ):
            raise TypeError("product_shutdown_children_invalid")
        if any(
            item.lifecycle_generation != self.lifecycle_generation
            for item in self.children
        ):
            raise ValueError("product_shutdown_child_generation_mismatch")
        child_ids = tuple(item.child_id for item in self.children)
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("product_shutdown_child_id_duplicate")
        if not isinstance(self.zero_survivor, ProductZeroSurvivorReceipt):
            raise TypeError("product_shutdown_zero_survivor_required")
        if self.zero_survivor.lifecycle_generation != self.lifecycle_generation:
            raise ValueError("product_shutdown_zero_survivor_generation_mismatch")
        child_survivors = tuple(
            survivor
            for child in self.children
            for survivor in child.survivors
        )
        if tuple(sorted(set(child_survivors))) != self.zero_survivor.survivors:
            raise ValueError("product_shutdown_survivor_mismatch")
        if not isinstance(self.errors, tuple) or any(
            type(item) is not str or not item.strip() for item in self.errors
        ):
            raise TypeError("product_shutdown_errors_invalid")
        if type(self.finished_at) not in {int, float}:
            raise TypeError("product_shutdown_finished_at_number_required")
        finished_at = float(self.finished_at)
        if not math.isfinite(finished_at) or finished_at <= 0.0:
            raise ValueError("product_shutdown_finished_at_invalid")
        clean = (
            self.zero_survivor.complete
            and not self.errors
            and all(item.terminated for item in self.children)
        )
        if disposition in {
            ProductShutdownDisposition.STOPPED,
            ProductShutdownDisposition.ALREADY_STOPPED,
        }:
            if not clean or state is not ProductLifecycleState.STOPPED:
                raise ValueError("product_shutdown_stopped_evidence_invalid")
        elif state is not ProductLifecycleState.DEGRADED:
            raise ValueError("product_shutdown_degraded_state_required")
        if self.schema != PRODUCT_SHUTDOWN_RECEIPT_SCHEMA:
            raise ValueError("product_shutdown_schema_invalid")
        if self.owner != PRODUCT_SUPERVISOR_OWNER:
            raise ValueError("product_shutdown_owner_invalid")
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "lifecycle_state", state)
        object.__setattr__(self, "finished_at", finished_at)

    @property
    def clean(self) -> bool:
        return self.disposition in {
            ProductShutdownDisposition.STOPPED,
            ProductShutdownDisposition.ALREADY_STOPPED,
        }

    @property
    def terminated(self) -> bool:
        return self.clean and self.zero_survivor.complete

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "disposition": self.disposition.value,
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_state": self.lifecycle_state.value,
            "children": [item.as_dict() for item in self.children],
            "zero_survivor": self.zero_survivor.as_dict(),
            "errors": list(self.errors),
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ProductShutdownReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "owner",
            "disposition",
            "lifecycle_generation",
            "lifecycle_state",
            "children",
            "zero_survivor",
            "errors",
            "finished_at",
        }:
            raise ValueError("product_shutdown_mapping_invalid")
        if not isinstance(value["children"], list) or not isinstance(
            value["errors"], list
        ):
            raise TypeError("product_shutdown_list_required")
        return cls(
            disposition=ProductShutdownDisposition(value["disposition"]),
            lifecycle_generation=value["lifecycle_generation"],
            lifecycle_state=ProductLifecycleState(value["lifecycle_state"]),
            children=tuple(
                ProductChildTerminationReceipt.from_mapping(item)
                for item in value["children"]
            ),
            zero_survivor=ProductZeroSurvivorReceipt.from_mapping(
                value["zero_survivor"]
            ),
            errors=tuple(value["errors"]),
            finished_at=value["finished_at"],
            schema=value["schema"],
            owner=value["owner"],
        )

    def __reduce__(self):
        return (_restore_product_shutdown_receipt, (self.as_dict(),))


def _restore_foundation_startup_receipt(
    value: Mapping[str, Any],
) -> FoundationStartupReceipt:
    return FoundationStartupReceipt.from_mapping(value)


def _restore_core_startup_candidate(
    value: Mapping[str, Any],
) -> CoreStartupCandidate:
    return CoreStartupCandidate.from_mapping(value)


def _restore_core_private_startup_root_proof(
    value: Mapping[str, Any],
) -> CorePrivateStartupRootProof:
    return CorePrivateStartupRootProof.from_mapping(value)


def _restore_product_startup_receipt(
    value: Mapping[str, Any],
) -> ProductStartupReceipt:
    return ProductStartupReceipt.from_mapping(value)


def _restore_product_shutdown_receipt(
    value: Mapping[str, Any],
) -> ProductShutdownReceipt:
    return ProductShutdownReceipt.from_mapping(value)


__all__ = (
    "APPLICATION_LEASE_RECEIPT_SCHEMA",
    "ApplicationLeaseIdentity",
    "ApplicationLeaseReceipt",
    "ApplicationLeaseState",
    "CORE_STARTUP_CANDIDATE_SCHEMA",
    "CORE_PRIVATE_STARTUP_ROOT_PROOF_SCHEMA",
    "CorePrivateStartupRootProof",
    "CoreStartupCandidate",
    "FOUNDATION_STARTUP_OWNER",
    "FOUNDATION_STARTUP_RECEIPT_SCHEMA",
    "FoundationStartupDisposition",
    "FoundationStartupReceipt",
    "IntelligenceStartupEvidence",
    "PRODUCT_CHILD_TERMINATION_RECEIPT_SCHEMA",
    "PRODUCT_LIFECYCLE_STATUS_RECEIPT_SCHEMA",
    "PRODUCT_SHUTDOWN_RECEIPT_SCHEMA",
    "PRODUCT_STARTUP_CLEANUP_RECEIPT_SCHEMA",
    "PRODUCT_STOP_REQUEST_RECEIPT_SCHEMA",
    "PRODUCT_ZERO_SURVIVOR_RECEIPT_SCHEMA",
    "ProductChildTerminationReceipt",
    "ProductLifecycleState",
    "ProductLifecycleStatusReceipt",
    "ProductRuntimeAdmissionAuthority",
    "ProductRuntimeWorkerReplacementReason",
    "ProductRuntimeWorkerReplacementRequired",
    "ProductShutdownDisposition",
    "ProductShutdownReceipt",
    "ProductStartupDisposition",
    "ProductStartupCleanupReceipt",
    "ProductStartupReason",
    "ProductStartupReceipt",
    "ProductStopRequestReceipt",
    "ProductTerminationDisposition",
    "ProductZeroSurvivorReceipt",
    "PRODUCT_STARTUP_RECEIPT_SCHEMA",
    "PRODUCT_SUPERVISOR_OWNER",
)
