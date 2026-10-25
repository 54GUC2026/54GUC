"""Typed IPC contract between NGUI and the Engine worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from multiprocessing import connection
import os
from hashlib import sha256
import json
import math
import pickle
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
    ProductRuntimeWorkerReplacementReason,
    ProductRuntimeWorkerReplacementRequired,
    ProductShutdownReceipt,
    ProductStartupReceipt,
)
from arch4.command_transactions import CommandTerminalReceipt
from arch4.core_contracts.commands import CoreCommandRequest
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.operational_service_contracts import (
    AntennaGuideControlRequest,
    BandScheduleCancelRequest,
    BandSchedulePlanRequest,
    BandSequenceCancelRequest,
    BandSequencePlanRequest,
    DownstreamHealthConfigRequest,
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
    OperationalServiceReceipt,
    SmsCommandControlConfigRequest,
    WifiRadioControlRequest,
    RouterReadDiagnosticRequest,
    NetworkSettingsInspectionRequest,
    operational_request_matrix_identity,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from .input_registers import (
    OwnerInputBatch,
    canonical_ngui_input_value,
)
from .runtime_reset_manifest import (
    RuntimeResetApplicationReceipt,
    runtime_reset_manifest_digest,
)
from .types import SourceOwner


MAX_RUNTIME_FRAME_BYTES = 1024 * 1024
RUNTIME_WORKER_GENERATION_IDENTITY_SCHEMA = (
    "RuntimeWorkerGenerationIdentity.v1"
)
RUNTIME_PRODUCT_GENERATION_IDENTITY_SCHEMA = (
    "RuntimeProductGenerationIdentity.v1"
)
RUNTIME_NAMESPACE_GENERATION_IDENTITY_SCHEMA = (
    "RuntimeNamespaceGenerationIdentity.v1"
)
RUNTIME_IPC_IDENTITY_OWNER = "54guc.ngui.runtime_service"


RuntimeWorkerReplacementRequired = ProductRuntimeWorkerReplacementRequired
RuntimeWorkerReplacementReason = ProductRuntimeWorkerReplacementReason


def _runtime_identity_digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception as exc:
        raise TypeError("runtime IPC identity is not canonical") from exc
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeWorkerGenerationIdentity:
    """Application lease and child generation accepted by NGUI."""

    application_lease: ApplicationLeaseIdentity
    worker_generation: int
    reset_application_digest: str
    identity_digest: str = ""
    schema: str = RUNTIME_WORKER_GENERATION_IDENTITY_SCHEMA
    owner: str = RUNTIME_IPC_IDENTITY_OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.application_lease, ApplicationLeaseIdentity):
            raise TypeError("runtime worker application lease identity required")
        if (
            type(self.worker_generation) is not int
            or self.worker_generation <= 0
        ):
            raise ValueError("runtime worker generation identity invalid")
        if type(self.reset_application_digest) is not str:
            raise TypeError(
                "runtime worker reset application digest string required"
            )
        reset_digest = self.reset_application_digest
        if (
            len(reset_digest) != 64
            or reset_digest != reset_digest.lower()
            or any(
                item not in "0123456789abcdef" for item in reset_digest
            )
        ):
            raise ValueError(
                "runtime worker reset application digest invalid"
            )
        if self.schema != RUNTIME_WORKER_GENERATION_IDENTITY_SCHEMA:
            raise ValueError("runtime worker generation schema invalid")
        if self.owner != RUNTIME_IPC_IDENTITY_OWNER:
            raise ValueError("runtime worker generation owner invalid")
        if type(self.identity_digest) is not str:
            raise TypeError("runtime worker generation digest string required")
        expected = _runtime_identity_digest(self._digest_material())
        if (
            self.identity_digest
            and (
                len(self.identity_digest) != 64
                or self.identity_digest != self.identity_digest.lower()
                or any(
                    item not in "0123456789abcdef"
                    for item in self.identity_digest
                )
                or self.identity_digest != expected
            )
        ):
            raise ValueError("runtime worker generation digest mismatch")
        object.__setattr__(self, "identity_digest", expected)

    @classmethod
    def from_reset_application(
        cls,
        *,
        application_lease: ApplicationLeaseIdentity,
        worker_generation: int,
        reset_receipt: RuntimeResetApplicationReceipt,
    ) -> "RuntimeWorkerGenerationIdentity":
        if not isinstance(
            reset_receipt,
            RuntimeResetApplicationReceipt,
        ):
            raise TypeError(
                "runtime worker reset application receipt required"
            )
        if (
            reset_receipt.application_lease != application_lease
            or reset_receipt.worker_generation != worker_generation
        ):
            raise ValueError(
                "runtime worker reset application identity mismatch"
            )
        rotation = reset_receipt.page_truth_rotation
        if (
            rotation.application_lease != application_lease
            or rotation.generation != worker_generation
            or rotation.reset_transaction_digest
            != reset_receipt.transaction_digest
        ):
            raise ValueError(
                "runtime worker PageTruth reset binding mismatch"
            )
        return cls(
            application_lease=application_lease,
            worker_generation=worker_generation,
            reset_application_digest=reset_receipt.receipt_digest,
        )

    def _digest_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "application_lease": self.application_lease.to_payload(),
            "worker_generation": self.worker_generation,
            "reset_application_digest": self.reset_application_digest,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._digest_material(),
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProductGenerationIdentity:
    """Reference to an accepted READY Product root."""

    worker: RuntimeWorkerGenerationIdentity
    lifecycle_generation: int
    runtime_namespace: RuntimeGenerationNamespace
    product_root_digest: str
    identity_digest: str = ""
    schema: str = RUNTIME_PRODUCT_GENERATION_IDENTITY_SCHEMA
    owner: str = RUNTIME_IPC_IDENTITY_OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.worker, RuntimeWorkerGenerationIdentity):
            raise TypeError("runtime Product worker identity required")
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError("runtime Product lifecycle generation invalid")
        if not isinstance(
            self.runtime_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("runtime Product namespace required")
        if (
            self.runtime_namespace.lifecycle_generation
            != self.lifecycle_generation
        ):
            raise ValueError(
                "runtime Product namespace lifecycle mismatch"
            )
        if type(self.product_root_digest) is not str:
            raise TypeError("runtime Product root digest string required")
        product_root_digest = self.product_root_digest
        if (
            len(product_root_digest) != 64
            or product_root_digest != product_root_digest.lower()
            or any(
                item not in "0123456789abcdef"
                for item in product_root_digest
            )
        ):
            raise ValueError("runtime Product root digest invalid")
        if self.schema != RUNTIME_PRODUCT_GENERATION_IDENTITY_SCHEMA:
            raise ValueError("runtime Product generation schema invalid")
        if self.owner != RUNTIME_IPC_IDENTITY_OWNER:
            raise ValueError("runtime Product generation owner invalid")
        if type(self.identity_digest) is not str:
            raise TypeError("runtime Product generation digest string required")
        expected = _runtime_identity_digest(self._digest_material())
        if (
            self.identity_digest
            and (
                len(self.identity_digest) != 64
                or self.identity_digest != self.identity_digest.lower()
                or any(
                    item not in "0123456789abcdef"
                    for item in self.identity_digest
                )
                or self.identity_digest != expected
            )
        ):
            raise ValueError("runtime Product generation digest mismatch")
        object.__setattr__(self, "identity_digest", expected)

    @classmethod
    def from_admitted_root(
        cls,
        *,
        worker: RuntimeWorkerGenerationIdentity,
        product_root: ProductStartupReceipt,
    ) -> "RuntimeProductGenerationIdentity":
        if (
            not isinstance(product_root, ProductStartupReceipt)
            or not product_root.ready
        ):
            raise TypeError("runtime Product READY root required")
        return cls(
            worker=worker,
            lifecycle_generation=product_root.lifecycle_generation,
            runtime_namespace=(
                product_root.matrix_readiness.matrix_identity.namespace
            ),
            product_root_digest=product_root.root_digest,
        )

    def _digest_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "worker": self.worker.as_dict(),
            "lifecycle_generation": self.lifecycle_generation,
            "runtime_namespace": self.runtime_namespace.as_dict(),
            "product_root_digest": self.product_root_digest,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._digest_material(),
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeNamespaceGenerationIdentity:
    """Product and worker root with the accepted Matrix identity."""

    product: RuntimeProductGenerationIdentity
    matrix_identity: MatrixGenerationIdentity
    identity_digest: str = ""
    schema: str = RUNTIME_NAMESPACE_GENERATION_IDENTITY_SCHEMA
    owner: str = RUNTIME_IPC_IDENTITY_OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.product, RuntimeProductGenerationIdentity):
            raise TypeError("runtime namespace Product identity required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("runtime namespace Matrix identity required")
        if self.matrix_identity.namespace != self.product.runtime_namespace:
            raise RuntimeWorkerReplacementRequired(
                current_namespace=self.product.runtime_namespace,
                requested_namespace=self.matrix_identity.namespace,
                reason=(
                    RuntimeWorkerReplacementReason
                    .FOREIGN_RUNTIME_NAMESPACE
                ),
            )
        if self.schema != RUNTIME_NAMESPACE_GENERATION_IDENTITY_SCHEMA:
            raise ValueError("runtime namespace generation schema invalid")
        if self.owner != RUNTIME_IPC_IDENTITY_OWNER:
            raise ValueError("runtime namespace generation owner invalid")
        if type(self.identity_digest) is not str:
            raise TypeError("runtime namespace generation digest string required")
        expected = _runtime_identity_digest(self._digest_material())
        if (
            self.identity_digest
            and (
                len(self.identity_digest) != 64
                or self.identity_digest != self.identity_digest.lower()
                or any(
                    item not in "0123456789abcdef"
                    for item in self.identity_digest
                )
                or self.identity_digest != expected
            )
        ):
            raise ValueError("runtime namespace generation digest mismatch")
        object.__setattr__(self, "identity_digest", expected)

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.matrix_identity.namespace

    def _digest_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "product": self.product.as_dict(),
            "matrix_identity": self.matrix_identity.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._digest_material(),
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeCanonicalMapping(Mapping[str, Any]):
    """Data-only mapping. Never deserialize executable pickle payloads."""

    payload_bytes: bytes
    sha256_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.payload_bytes, bytes):
            raise TypeError("runtime_canonical_mapping_payload_required")
        if (
            type(self.sha256_digest) is not str
            or len(self.sha256_digest) != 64
            or self.sha256_digest != self.sha256_digest.lower()
            or any(
                item not in "0123456789abcdef"
                for item in self.sha256_digest
            )
        ):
            raise ValueError("runtime_canonical_mapping_digest_invalid")
        try:
            value = json.loads(self.payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError("runtime_canonical_mapping_payload_invalid") from exc
        if not isinstance(value, dict):
            raise TypeError("runtime_canonical_mapping_dict_required")
        digest = sha256(self.payload_bytes).hexdigest()
        if self.sha256_digest != digest:
            raise ValueError("runtime_canonical_mapping_digest_mismatch")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeCanonicalMapping":
        if not isinstance(value, Mapping):
            raise TypeError("runtime_canonical_mapping_source_required")
        payload_bytes = json.dumps(
            canonical_ngui_input_value(
                value,
                path="$.runtime_canonical_mapping",
            ),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            payload_bytes=payload_bytes,
            sha256_digest=sha256(payload_bytes).hexdigest(),
        )

    def to_mapping(self) -> dict[str, Any]:
        value = json.loads(self.payload_bytes.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("runtime_canonical_mapping_dict_required")
        return value

    def __getitem__(self, key: str) -> Any:
        return self.to_mapping()[key]

    def __iter__(self):
        return iter(self.to_mapping())

    def __len__(self) -> int:
        return len(self.to_mapping())


@dataclass(frozen=True, slots=True)
class RuntimeSupportHistoryFlushRequest:
    """Read-only export barrier, never a modem or Command Authority request."""
    generation_identity: RuntimeNamespaceGenerationIdentity
    history_path: str
    cutoff: float
    deadline: float
    reply: Any

    def __post_init__(self) -> None:
        if not isinstance(self.generation_identity, RuntimeNamespaceGenerationIdentity):
            raise TypeError("support_history_generation_required")
        if not isinstance(self.history_path, str) or not self.history_path:
            raise ValueError("support_history_path_required")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in (self.cutoff, self.deadline)):
            raise ValueError("support_history_request_bounds_invalid")
        # Windows native Pipes use PipeConnection; POSIX uses Connection.
        reply_types = (connection.Connection,)
        if os.name == "nt":
            reply_types += (connection.PipeConnection,)
        if not isinstance(self.reply, reply_types) or not self.reply.writable or self.reply.readable:
            raise TypeError("support_history_reply_channel_required")


class RuntimeRequestKind(str, Enum):
    CORE_READ_DIAGNOSTICS = "core_read_diagnostics"
    CORE_NETWORK_INSPECTION = "core_network_inspection"
    CORE_WIFI_CONTROL = "core_wifi_control"
    CORE_DOWNSTREAM_DIAGNOSTIC = "core_downstream_diagnostic"
    FOUNDATION_SMS_CONTROL = "foundation_sms_control"
    FOUNDATION_ANTENNA_GUIDE = "foundation_antenna_guide"
    FOUNDATION_BAND_SCHEDULE_PLAN = "foundation_band_schedule_plan"
    FOUNDATION_BAND_SEQUENCE_PLAN = "foundation_band_sequence_plan"
    FOUNDATION_BAND_SCHEDULE_CANCEL = "foundation_band_schedule_cancel"
    FOUNDATION_BAND_SEQUENCE_CANCEL = "foundation_band_sequence_cancel"
    PRODUCT_INTELLIGENCE_AUTOPILOT = "product_intelligence_autopilot"
    PRODUCT_INTELLIGENCE_EXPLORE = "product_intelligence_explore"
    MANUAL_NMI = "manual_nmi"


class RuntimeEventKind(str, Enum):
    PRODUCT_ROOT_ADMISSION = "product_root_admission"
    WORKER_REPLACEMENT_REQUIRED = "worker_replacement_required"
    STATUS = "status"
    MANUAL_NMI_RESULT = "manual_nmi_result"
    CORE_COMMAND_OUTCOME = "core_command_outcome"
    OPERATIONAL_RECEIPT = "operational_receipt"
    OPERATIONAL_PRESENTATION_ACKNOWLEDGED = (
        "operational_presentation_acknowledged"
    )
    SHUTDOWN_RECEIPT = "shutdown_receipt"


class RuntimeEventScope(str, Enum):
    RUNTIME_NAMESPACE = "runtime_namespace"
    APPLICATION_GLOBAL = "application_global"


class RuntimeWorkerStatusState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    IDENTITY_REQUIRED = "identity_required"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RuntimeStartupRouterSerial:
    """One operator-supplied serial for the already-authenticated startup."""

    serial: str

    def __post_init__(self) -> None:
        from arch4.runtime_identity import is_placeholder_identity_text

        if type(self.serial) is not str:
            raise TypeError("router serial string required")
        serial = self.serial.strip()
        if (
            not serial
            or len(serial) > 128
            or any(ord(character) < 32 for character in serial)
            or is_placeholder_identity_text(serial)
            or set(serial) == {"0"}
        ):
            raise ValueError("router serial is invalid")
        object.__setattr__(self, "serial", serial)


@dataclass(frozen=True, slots=True)
class RuntimeWorkerStatusReceipt:
    state: RuntimeWorkerStatusState
    changed_at: float
    message: str
    cycle_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, RuntimeWorkerStatusState):
            raise TypeError("runtime worker status state required")
        if type(self.changed_at) is not float:
            raise TypeError("runtime worker status timestamp float required")
        if not math.isfinite(self.changed_at) or self.changed_at <= 0.0:
            raise ValueError("runtime worker status timestamp invalid")
        if type(self.message) is not str:
            raise TypeError("runtime worker status message string required")
        if type(self.cycle_count) is not int or self.cycle_count < 0:
            raise ValueError("runtime worker status cycle count invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "changed_at": self.changed_at,
            "message": self.message,
            "cycle_count": self.cycle_count,
        }


@dataclass(frozen=True, slots=True)
class RuntimeWorkerReplacementRequiredReceipt:
    """Retire a foreign-device worker or stop after failed automatic login."""

    generation_identity: RuntimeProductGenerationIdentity
    active_namespace: RuntimeGenerationNamespace
    requested_namespace: RuntimeGenerationNamespace
    reason: RuntimeWorkerReplacementReason
    observed_at: float
    expected_reset_manifest_digest: str = field(
        default_factory=runtime_reset_manifest_digest
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.generation_identity,
            RuntimeProductGenerationIdentity,
        ):
            raise TypeError(
                "runtime worker replacement Product identity required"
            )
        if not isinstance(
            self.active_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "runtime worker replacement active namespace required"
            )
        if self.active_namespace != (
            self.generation_identity.runtime_namespace
        ):
            raise ValueError(
                "runtime worker replacement active namespace mismatch"
            )
        if not isinstance(
            self.requested_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "runtime worker replacement requested namespace required"
            )
        if not isinstance(
            self.reason,
            RuntimeWorkerReplacementReason,
        ):
            raise TypeError(
                "runtime worker replacement reason required"
            )
        if self.reason is RuntimeWorkerReplacementReason.AUTOMATIC_LOGIN_FAILED:
            if self.requested_namespace != self.active_namespace:
                raise ValueError("failed login cannot assert a different router")
        elif self.requested_namespace == self.active_namespace:
            raise ValueError("runtime worker replacement foreign namespace required")
        if type(self.observed_at) is not float:
            raise TypeError(
                "runtime worker replacement timestamp float required"
            )
        if not math.isfinite(self.observed_at) or self.observed_at <= 0.0:
            raise ValueError(
                "runtime worker replacement timestamp invalid"
            )
        if type(self.expected_reset_manifest_digest) is not str:
            raise TypeError(
                "runtime worker replacement reset manifest digest required"
            )
        manifest_digest = self.expected_reset_manifest_digest
        if manifest_digest != runtime_reset_manifest_digest():
            raise ValueError(
                "runtime worker replacement reset manifest mismatch"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_identity": self.generation_identity.as_dict(),
            "active_namespace": self.active_namespace.as_dict(),
            "requested_namespace": (
                self.requested_namespace.as_dict()
            ),
            "reason": self.reason.value,
            "observed_at": self.observed_at,
            "expected_reset_manifest_digest": (
                self.expected_reset_manifest_digest
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeProductRootAdmission:
    """Full Product READY proof sent once per worker generation."""

    product_root: ProductStartupReceipt

    def __post_init__(self) -> None:
        if (
            not isinstance(self.product_root, ProductStartupReceipt)
            or not self.product_root.ready
        ):
            raise TypeError("runtime Product root admission READY receipt required")

    @property
    def product_root_digest(self) -> str:
        return self.product_root.root_digest

    def as_dict(self) -> dict[str, Any]:
        return {"product_root": self.product_root.as_dict()}


class RuntimeProductRootAdmissionAuthority:
    """Store the full Product proof accepted for each worker."""

    def __init__(
        self,
        worker_identity: RuntimeWorkerGenerationIdentity,
    ) -> None:
        if not isinstance(worker_identity, RuntimeWorkerGenerationIdentity):
            raise TypeError(
                "runtime Product admission worker identity required"
            )
        self._worker_identity = worker_identity
        self._lock = RLock()
        self._admitted_identity: RuntimeProductGenerationIdentity | None = None
        self._admission: RuntimeProductRootAdmission | None = None

    def admit(
        self,
        event: "RuntimeIPCEvent",
    ) -> RuntimeProductGenerationIdentity:
        if (
            not isinstance(event, RuntimeIPCEvent)
            or event.kind is not RuntimeEventKind.PRODUCT_ROOT_ADMISSION
            or not isinstance(
                event.generation_identity,
                RuntimeProductGenerationIdentity,
            )
            or not isinstance(event.payload, RuntimeProductRootAdmission)
        ):
            raise TypeError("runtime Product root admission event required")
        identity = event.generation_identity
        if identity.worker != self._worker_identity:
            raise RuntimeError(
                "runtime Product root admission worker mismatch"
            )
        with self._lock:
            if self._admitted_identity is None:
                self._admitted_identity = identity
                self._admission = event.payload
            elif (
                identity != self._admitted_identity
                or event.payload != self._admission
            ):
                raise RuntimeError(
                    "runtime Product root immutable for worker"
                )
            return self._admitted_identity

    def require_product(
        self,
        identity: RuntimeProductGenerationIdentity,
    ) -> RuntimeProductRootAdmission:
        if not isinstance(identity, RuntimeProductGenerationIdentity):
            raise TypeError(
                "runtime admitted Product identity required"
            )
        with self._lock:
            if (
                self._admitted_identity != identity
                or self._admission is None
            ):
                raise RuntimeError(
                    "runtime Product root reference not admitted"
                )
            return self._admission

    def require_namespace(
        self,
        identity: RuntimeNamespaceGenerationIdentity,
    ) -> RuntimeProductRootAdmission:
        if not isinstance(identity, RuntimeNamespaceGenerationIdentity):
            raise TypeError(
                "runtime admitted namespace identity required"
            )
        return self.require_product(identity.product)


@dataclass(frozen=True, slots=True)
class RuntimeManualCommandRequest:
    command_request: CoreCommandRequest
    parent_received_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.command_request, CoreCommandRequest):
            raise TypeError("runtime manual Core command request required")
        if type(self.parent_received_at) is not float:
            raise TypeError("runtime manual parent timestamp float required")
        if (
            not math.isfinite(self.parent_received_at)
            or self.parent_received_at <= 0.0
        ):
            raise ValueError("runtime manual parent timestamp invalid")


@dataclass(frozen=True, slots=True)
class RuntimeManualTimingReceipt:
    intent_created_at: float
    parent_received_at: float
    child_received_at: float
    confirm_to_app_ms: float
    app_to_core_ms: float
    confirm_to_core_process_ms: float
    core_submit_ms: float = 0.0
    confirm_to_core_ms: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "intent_created_at",
            "parent_received_at",
            "child_received_at",
        ):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"runtime manual timing {name} float required")
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"runtime manual timing {name} invalid")
        for name in (
            "confirm_to_app_ms",
            "app_to_core_ms",
            "confirm_to_core_process_ms",
            "core_submit_ms",
            "confirm_to_core_ms",
        ):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"runtime manual timing {name} float required")
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"runtime manual timing {name} invalid")

    def as_dict(self) -> dict[str, float]:
        return {
            name: getattr(self, name)
            for name in (
                "intent_created_at",
                "parent_received_at",
                "child_received_at",
                "confirm_to_app_ms",
                "app_to_core_ms",
                "confirm_to_core_process_ms",
                "core_submit_ms",
                "confirm_to_core_ms",
            )
        }


@dataclass(frozen=True, slots=True)
class RuntimeManualResultReceipt:
    sequence: int
    submitted: bool
    reason: str
    correlation_id: str
    gateway_outcome_expected: bool
    timing: RuntimeManualTimingReceipt

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime manual result sequence invalid")
        if type(self.submitted) is not bool:
            raise TypeError("runtime manual result submitted bool required")
        if type(self.gateway_outcome_expected) is not bool:
            raise TypeError(
                "runtime manual result gateway outcome bool required"
            )
        if (
            type(self.reason) is not str
            or not self.reason
            or self.reason != self.reason.strip()
        ):
            raise ValueError("runtime manual result reason required")
        if type(self.correlation_id) is not str:
            raise TypeError("runtime manual result correlation string required")
        if self.correlation_id != self.correlation_id.strip():
            raise ValueError(
                "runtime manual result correlation not canonical"
            )
        if self.gateway_outcome_expected and not self.correlation_id:
            raise ValueError(
                "runtime manual result expected outcome correlation required"
            )
        if not isinstance(self.timing, RuntimeManualTimingReceipt):
            raise TypeError("runtime manual result timing receipt required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "submitted": self.submitted,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "gateway_outcome_expected": self.gateway_outcome_expected,
            "timing": self.timing.as_dict(),
        }


class RuntimeFrameContractError(RuntimeError):
    """Raised when an NGUI frame violates the IPC memory contract."""


@dataclass(frozen=True, slots=True)
class RuntimeFrameDeliveryReceipt:
    """Final parent result for a consumed runtime frame.

    Transport delivery and application acceptance are separate. A rejected
    application is still acknowledged by the transport, so the latest retained
    frame can proceed.
    """

    frame_sequence: int
    accepted_commit_id: str
    matrix_identity: MatrixGenerationIdentity
    front_register_applied: bool
    chart_history_accepted: bool

    def __post_init__(self) -> None:
        if type(self.frame_sequence) is not int or self.frame_sequence <= 0:
            raise ValueError("runtime frame delivery receipt requires sequence")
        if type(self.accepted_commit_id) is not str:
            raise TypeError(
                "runtime frame delivery commit identity string required"
            )
        commit = self.accepted_commit_id
        if not commit or commit != commit.strip():
            raise ValueError(
                "runtime frame delivery receipt requires accepted commit identity"
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "runtime frame delivery receipt requires Matrix identity"
            )
        for name in ("front_register_applied", "chart_history_accepted"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(
                    f"runtime frame delivery {name} bool required"
                )

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.matrix_identity.namespace

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
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def matrix_revision(self) -> str:
        return self.matrix_identity.matrix_revision

    @property
    def frame_applied(self) -> bool:
        """Whether NGUI committed the input and Front.

        Chart history and PageTruth are bounded latest-state effects. Their
        results stay on this receipt, but neither decides whether the live
        Front is accepted.
        """

        return bool(self.front_register_applied)

    @property
    def transport_acknowledged(self) -> bool:
        """The envelope was consumed and received a typed result."""

        return True




@dataclass(frozen=True, slots=True)
class RuntimeInputFrame:
    sequence: int
    published_at: float
    matrix_identity: MatrixGenerationIdentity
    accepted_commit_id: str
    accepted_root_fingerprint: str
    accepted_source_sampled_at: float
    snapshot_sequence: int
    command_context: CoreFrozenPayload
    owner_batches: tuple[OwnerInputBatch, ...]
    owner_order: tuple[SourceOwner, ...]
    complete_owner_order: tuple[SourceOwner, ...]
    owner_fingerprints: tuple[str, ...]
    bootstrap: bool
    extension_vector: tuple[tuple[str, int, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime input frame requires positive sequence")
        if type(self.published_at) is not float:
            raise TypeError("runtime input frame timestamp float required")
        if not math.isfinite(self.published_at) or self.published_at <= 0.0:
            raise ValueError("runtime input frame requires positive timestamp")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("runtime input frame Matrix identity required")
        if (
            type(self.accepted_commit_id) is not str
            or not self.accepted_commit_id
            or self.accepted_commit_id != self.accepted_commit_id.strip()
        ):
            raise ValueError("runtime input frame commit identity required")
        if (
            type(self.accepted_root_fingerprint) is not str
            or len(self.accepted_root_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.accepted_root_fingerprint
            )
        ):
            raise ValueError("runtime input frame root fingerprint invalid")
        if (
            type(self.accepted_source_sampled_at) is not float
            or not math.isfinite(self.accepted_source_sampled_at)
            or self.accepted_source_sampled_at <= 0.0
        ):
            raise ValueError("runtime input frame source timestamp invalid")
        if type(self.snapshot_sequence) is not int or self.snapshot_sequence <= 0:
            raise ValueError("runtime input frame snapshot sequence required")
        if not isinstance(self.command_context, CoreFrozenPayload):
            raise TypeError("runtime input frame command context required")
        if (
            not isinstance(self.owner_batches, tuple)
            or any(
                not isinstance(item, OwnerInputBatch)
                for item in self.owner_batches
            )
        ):
            raise TypeError("runtime input frame projected owner batches required")
        if (
            not isinstance(self.owner_order, tuple)
            or any(not isinstance(item, SourceOwner) for item in self.owner_order)
        ):
            raise TypeError("runtime input frame owner order required")
        if (
            not isinstance(self.complete_owner_order, tuple)
            or not self.complete_owner_order
            or len(self.complete_owner_order) != len(set(self.complete_owner_order))
            or any(
                not isinstance(item, SourceOwner)
                for item in self.complete_owner_order
            )
        ):
            raise TypeError("runtime input frame complete owner order required")
        owners = tuple(item.owner for item in self.owner_batches)
        if owners != self.owner_order or len(owners) != len(set(owners)):
            raise ValueError("runtime input frame owner denominator mismatch")
        if tuple(
            owner for owner in self.complete_owner_order if owner in set(owners)
        ) != owners:
            raise ValueError("runtime input frame owner delta order mismatch")
        if type(self.bootstrap) is not bool:
            raise TypeError("runtime input frame bootstrap bool required")
        if self.bootstrap and owners != self.complete_owner_order:
            raise ValueError("runtime input frame bootstrap denominator incomplete")
        if (
            not isinstance(self.owner_fingerprints, tuple)
            or len(self.owner_fingerprints) != len(self.owner_batches)
            or any(
                type(item) is not str
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
                for item in self.owner_fingerprints
            )
        ):
            raise TypeError("runtime input frame owner fingerprints invalid")
        if (
            not isinstance(self.extension_vector, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 3
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not int
                or item[1] <= 0
                or type(item[2]) is not str
                or len(item[2]) != 64
                for item in self.extension_vector
            )
        ):
            raise TypeError("runtime input frame extension vector invalid")

    @property
    def command_ingress_context(self) -> CoreCommandIngressContext:
        return CoreCommandIngressContext.from_payload(
            self.command_context.to_mapping()
        )

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.matrix_identity.namespace

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
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def matrix_revision(self) -> str:
        return self.matrix_identity.matrix_revision




def runtime_frame_identity(
    frame: RuntimeInputFrame,
) -> tuple[
    int,
    str,
    str,
    str,
    int,
    int,
    str,
]:
    """Return the structural identity of an immutable frame."""

    return (
        int(frame.sequence),
        str(frame.accepted_commit_id),
        str(frame.session_scope_key),
        str(frame.source_epoch),
        int(frame.lifecycle_generation),
        int(frame.source_sequence),
        str(frame.matrix_revision),
    )


def validate_runtime_frame_delivery_receipt(
    frame: RuntimeInputFrame,
    receipt: RuntimeFrameDeliveryReceipt,
) -> None:
    """Require a result for the same structural frame identity."""

    if not isinstance(frame, RuntimeInputFrame):
        raise TypeError("runtime frame receipt validation requires typed frame")
    if not isinstance(receipt, RuntimeFrameDeliveryReceipt):
        raise TypeError("runtime frame receipt validation requires typed receipt")
    if (
        receipt.frame_sequence != frame.sequence
        or receipt.accepted_commit_id != frame.accepted_commit_id
        or receipt.session_scope_key != frame.session_scope_key
        or receipt.source_epoch != frame.source_epoch
        or receipt.lifecycle_generation != frame.lifecycle_generation
        or receipt.source_sequence != frame.source_sequence
        or receipt.matrix_revision != frame.matrix_revision
        or not receipt.transport_acknowledged
    ):
        raise RuntimeError("runtime_frame_acknowledgement_identity_mismatch")


def build_runtime_frame_delivery_receipt(
    frame: RuntimeInputFrame,
    *,
    front_register_applied: bool,
    chart_history_accepted: bool,
) -> RuntimeFrameDeliveryReceipt:
    """Build a receipt for the structural frame identity."""

    if not isinstance(frame, RuntimeInputFrame):
        raise TypeError("runtime frame delivery receipt requires typed frame")
    return RuntimeFrameDeliveryReceipt(
        frame_sequence=frame.sequence,
        accepted_commit_id=frame.accepted_commit_id,
        matrix_identity=frame.matrix_identity,
        front_register_applied=front_register_applied,
        chart_history_accepted=chart_history_accepted,
    )


@dataclass(frozen=True, slots=True)
class RuntimeFrameEnvelope:
    frame: RuntimeInputFrame
    payload_bytes: int
    enqueued_at: float

    def __post_init__(self) -> None:
        if (
            type(self.payload_bytes) is not int
            or self.payload_bytes <= 0
            or self.payload_bytes > MAX_RUNTIME_FRAME_BYTES
        ):
            raise RuntimeFrameContractError(
                f"runtime frame payload outside byte budget: {self.payload_bytes}"
            )
        if type(self.enqueued_at) is not float:
            raise TypeError("runtime frame envelope enqueue time float required")
        if not math.isfinite(self.enqueued_at) or self.enqueued_at <= 0.0:
            raise ValueError("runtime frame envelope requires enqueue timestamp")
        if not isinstance(self.frame, RuntimeInputFrame):
            raise TypeError("runtime frame envelope typed frame required")


def measure_runtime_frame_bytes(frame: RuntimeInputFrame) -> int:
    """Measure the Python IPC payload before multiprocessing retains it."""

    try:
        payload_bytes = len(pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception as exc:
        raise RuntimeFrameContractError(
            f"runtime frame is not serializable: {type(exc).__name__}: {exc}"
        ) from exc
    if payload_bytes > MAX_RUNTIME_FRAME_BYTES:
        raise RuntimeFrameContractError(
            "runtime frame exceeds byte budget: "
            f"{payload_bytes}>{MAX_RUNTIME_FRAME_BYTES};"
            f"accepted_root={frame.accepted_root_fingerprint}"
        )
    return payload_bytes


def validate_runtime_frame_envelope(envelope: RuntimeFrameEnvelope) -> None:
    """Validate the typed envelope once after receiving it."""

    if not isinstance(envelope, RuntimeFrameEnvelope):
        raise TypeError("runtime frame envelope validation requires typed envelope")
    if (
        type(envelope.payload_bytes) is not int
        or envelope.payload_bytes <= 0
        or envelope.payload_bytes > MAX_RUNTIME_FRAME_BYTES
        or not isinstance(envelope.frame, RuntimeInputFrame)
    ):
        raise RuntimeFrameContractError("runtime frame envelope contract invalid")


@dataclass(frozen=True, slots=True)
class RuntimeIPCRequest:
    kind: RuntimeRequestKind
    generation_identity: RuntimeNamespaceGenerationIdentity
    sequence: int
    created_at: float
    payload: (
        RuntimeManualCommandRequest
        | NetworkSettingsInspectionRequest
        | WifiRadioControlRequest
        | DownstreamHealthConfigRequest
        | SmsCommandControlConfigRequest
        | AntennaGuideControlRequest
        | BandSchedulePlanRequest
        | BandSequencePlanRequest
        | BandScheduleCancelRequest
        | BandSequenceCancelRequest
        | IntelligenceAutopilotControlRequest
        | IntelligenceExploreControlRequest
    )

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeRequestKind):
            raise TypeError("runtime IPC request kind required")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime IPC request sequence invalid")
        if type(self.created_at) is not float:
            raise TypeError("runtime IPC request timestamp float required")
        if not math.isfinite(self.created_at) or self.created_at <= 0.0:
            raise ValueError("runtime IPC request timestamp invalid")
        if not isinstance(
            self.generation_identity,
            RuntimeNamespaceGenerationIdentity,
        ):
            raise TypeError(
                "runtime IPC request namespace generation identity required"
            )
        expected_type = {
            RuntimeRequestKind.MANUAL_NMI: RuntimeManualCommandRequest,
            RuntimeRequestKind.CORE_READ_DIAGNOSTICS: RouterReadDiagnosticRequest,
            RuntimeRequestKind.CORE_NETWORK_INSPECTION: NetworkSettingsInspectionRequest,
            RuntimeRequestKind.CORE_WIFI_CONTROL: WifiRadioControlRequest,
            RuntimeRequestKind.CORE_DOWNSTREAM_DIAGNOSTIC: (
                DownstreamHealthConfigRequest
            ),
            RuntimeRequestKind.FOUNDATION_SMS_CONTROL: (
                SmsCommandControlConfigRequest
            ),
            RuntimeRequestKind.FOUNDATION_ANTENNA_GUIDE: (
                AntennaGuideControlRequest
            ),
            RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_PLAN: (
                BandSchedulePlanRequest
            ),
            RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_PLAN: (
                BandSequencePlanRequest
            ),
            RuntimeRequestKind.FOUNDATION_BAND_SCHEDULE_CANCEL: (
                BandScheduleCancelRequest
            ),
            RuntimeRequestKind.FOUNDATION_BAND_SEQUENCE_CANCEL: (
                BandSequenceCancelRequest
            ),
            RuntimeRequestKind.PRODUCT_INTELLIGENCE_AUTOPILOT: (
                IntelligenceAutopilotControlRequest
            ),
            RuntimeRequestKind.PRODUCT_INTELLIGENCE_EXPLORE: (
                IntelligenceExploreControlRequest
            ),
        }[self.kind]
        if not isinstance(self.payload, expected_type):
            raise TypeError(
                "runtime IPC request payload type mismatch:"
                + self.kind.value
            )
        payload_identity = (
            self.payload.command_request.matrix_identity
            if self.kind is RuntimeRequestKind.MANUAL_NMI
            else operational_request_matrix_identity(self.payload)
        )
        if payload_identity != self.generation_identity.matrix_identity:
            raise ValueError(
                "runtime_ipc_request_matrix_identity_mismatch"
            )

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity:
        return self.generation_identity.matrix_identity


@dataclass(frozen=True, slots=True)
class RuntimeIPCEvent:
    kind: RuntimeEventKind
    generation_identity: (
        RuntimeWorkerGenerationIdentity
        | RuntimeProductGenerationIdentity
        | RuntimeNamespaceGenerationIdentity
    )
    payload: (
        RuntimeProductRootAdmission
        | RuntimeWorkerReplacementRequiredReceipt
        | RuntimeWorkerStatusReceipt
        | RuntimeManualResultReceipt
        | CommandTerminalReceipt
        | OperationalServiceReceipt
        | ProductShutdownReceipt
    )

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeEventKind):
            raise TypeError("runtime IPC event requires RuntimeEventKind")
        identity = self.generation_identity
        if self.kind is RuntimeEventKind.PRODUCT_ROOT_ADMISSION:
            if not isinstance(identity, RuntimeProductGenerationIdentity):
                raise TypeError(
                    "runtime Product root admission identity required"
                )
            if not isinstance(self.payload, RuntimeProductRootAdmission):
                raise TypeError(
                    "runtime Product root admission receipt required"
                )
            product_root = self.payload.product_root
            readiness = product_root.matrix_readiness
            if readiness is None:
                raise RuntimeError(
                    "runtime Product root admission readiness unavailable"
                )
            if (
                product_root.lifecycle_generation
                != identity.lifecycle_generation
                or readiness.matrix_identity.namespace
                != identity.runtime_namespace
                or product_root.root_digest != identity.product_root_digest
            ):
                raise ValueError(
                    "runtime Product root admission identity mismatch"
                )
        elif self.kind is RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED:
            if not isinstance(identity, RuntimeProductGenerationIdentity):
                raise TypeError(
                    "runtime worker replacement Product identity required"
                )
            if not isinstance(
                self.payload,
                RuntimeWorkerReplacementRequiredReceipt,
            ):
                raise TypeError(
                    "runtime worker replacement receipt required"
                )
            if self.payload.generation_identity != identity:
                raise ValueError(
                    "runtime worker replacement event identity mismatch"
                )
        elif self.kind is RuntimeEventKind.STATUS:
            if not isinstance(
                identity,
                (
                    RuntimeWorkerGenerationIdentity,
                    RuntimeProductGenerationIdentity,
                ),
            ):
                raise TypeError(
                    "runtime status event worker or Product identity required"
                )
            if not isinstance(self.payload, RuntimeWorkerStatusReceipt):
                raise TypeError("runtime status event receipt required")
            if (
                self.payload.state is RuntimeWorkerStatusState.RUNNING
                and not isinstance(identity, RuntimeProductGenerationIdentity)
            ):
                raise TypeError(
                    "runtime running status Product identity required"
                )
        elif self.kind is RuntimeEventKind.SHUTDOWN_RECEIPT:
            if not isinstance(identity, (RuntimeWorkerGenerationIdentity, RuntimeProductGenerationIdentity)):
                raise TypeError(
                    "runtime shutdown event worker or Product identity required"
                )
            if not isinstance(self.payload, ProductShutdownReceipt):
                raise TypeError("runtime shutdown Product receipt required")
            if (
                isinstance(identity, RuntimeProductGenerationIdentity)
                and self.payload.lifecycle_generation
                != identity.lifecycle_generation
            ):
                raise ValueError(
                    "runtime shutdown Product generation mismatch"
                )
        elif not isinstance(identity, RuntimeNamespaceGenerationIdentity):
            raise TypeError(
                "runtime namespace event generation identity required"
            )
        else:
            expected_type = {
                RuntimeEventKind.MANUAL_NMI_RESULT: (
                    RuntimeManualResultReceipt
                ),
                RuntimeEventKind.CORE_COMMAND_OUTCOME: CommandTerminalReceipt,
                RuntimeEventKind.OPERATIONAL_RECEIPT: OperationalServiceReceipt,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED: (
                    OperationalServiceReceipt
                ),
            }.get(self.kind)
            if expected_type is None or not isinstance(
                self.payload,
                expected_type,
            ):
                raise TypeError(
                    "runtime namespace event payload type mismatch:"
                    + self.kind.value
                )
            if self.kind is RuntimeEventKind.CORE_COMMAND_OUTCOME:
                payload_identity = (
                    self.payload.transaction.matrix_identity
                )
                if payload_identity != identity.matrix_identity:
                    raise ValueError(
                        "runtime_command_outcome_matrix_identity_mismatch"
                    )
            elif self.kind in {
                RuntimeEventKind.OPERATIONAL_RECEIPT,
                RuntimeEventKind.OPERATIONAL_PRESENTATION_ACKNOWLEDGED,
            }:
                if (
                    self.payload.runtime_namespace
                    != identity.namespace
                    or self.payload.matrix_identity
                    != identity.matrix_identity
                ):
                    raise ValueError(
                        "runtime_operational_receipt_matrix_identity_mismatch"
                    )

    @property
    def scope(self) -> RuntimeEventScope:
        if isinstance(
            self.generation_identity,
            RuntimeNamespaceGenerationIdentity,
        ):
            return RuntimeEventScope.RUNTIME_NAMESPACE
        return RuntimeEventScope.APPLICATION_GLOBAL

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity | None:
        identity = self.generation_identity
        if isinstance(identity, RuntimeNamespaceGenerationIdentity):
            return identity.matrix_identity
        return None


def runtime_ipc_event_digest(event: RuntimeIPCEvent) -> str:
    if not isinstance(event, RuntimeIPCEvent):
        raise TypeError("runtime lifecycle digest requires RuntimeIPCEvent")
    try:
        encoded = json.dumps(
            {
                "kind": event.kind.value,
                "scope": event.scope.value,
                "generation_identity": event.generation_identity.as_dict(),
                "matrix_identity": (
                    event.matrix_identity.as_dict()
                    if event.matrix_identity is not None
                    else None
                ),
                "payload": _runtime_event_payload(event.payload),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception as exc:
        raise RuntimeFrameContractError(
            "runtime lifecycle event is not serializable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return sha256(encoded).hexdigest()


def _runtime_event_payload(
    payload: (
        RuntimeWorkerStatusReceipt
        | RuntimeProductRootAdmission
        | RuntimeWorkerReplacementRequiredReceipt
        | RuntimeManualResultReceipt
        | CommandTerminalReceipt
        | OperationalServiceReceipt
        | ProductShutdownReceipt
    ),
) -> Mapping[str, Any]:
    if isinstance(
        payload,
        (
            RuntimeWorkerStatusReceipt,
            RuntimeProductRootAdmission,
            RuntimeWorkerReplacementRequiredReceipt,
            RuntimeManualResultReceipt,
            ProductShutdownReceipt,
        ),
    ):
        return payload.as_dict()
    if isinstance(payload, CommandTerminalReceipt):
        return payload.as_dict()
    if isinstance(payload, OperationalServiceReceipt):
        return payload.to_payload()
    raise TypeError("runtime IPC event payload catalog mismatch")


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleEnvelope:
    """Replayable lifecycle delivery from child to parent."""

    sequence: int
    event: RuntimeIPCEvent
    enqueued_at: float
    event_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime lifecycle envelope requires sequence")
        if not isinstance(self.event, RuntimeIPCEvent):
            raise TypeError("runtime lifecycle envelope requires typed event")
        if self.event.kind is RuntimeEventKind.STATUS:
            raise ValueError("runtime status is illegal on lifecycle transport")
        if type(self.enqueued_at) is not float:
            raise TypeError(
                "runtime lifecycle envelope enqueue time float required"
            )
        if not math.isfinite(self.enqueued_at) or self.enqueued_at <= 0.0:
            raise ValueError("runtime lifecycle envelope requires enqueue time")
        object.__setattr__(self, "event_digest", runtime_ipc_event_digest(self.event))


@dataclass(frozen=True, slots=True)
class RuntimeProductEventApplicationReceipt:
    """Typed receiver proof for a Product lifecycle event."""

    generation_identity: RuntimeWorkerGenerationIdentity | RuntimeProductGenerationIdentity
    sequence: int
    event_kind: RuntimeEventKind
    event_digest: str
    applied_at: float
    application_key: str

    def __post_init__(self) -> None:
        worker_shutdown = (self.event_kind is RuntimeEventKind.SHUTDOWN_RECEIPT
                           and isinstance(self.generation_identity, RuntimeWorkerGenerationIdentity))
        if not worker_shutdown and not isinstance(self.generation_identity, RuntimeProductGenerationIdentity):
            raise TypeError(
                "runtime Product event application identity required"
            )
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError(
                "runtime Product event application sequence invalid"
            )
        if not isinstance(self.event_kind, RuntimeEventKind):
            raise TypeError(
                "runtime Product event application kind required"
            )
        if self.event_kind not in {
            RuntimeEventKind.PRODUCT_ROOT_ADMISSION,
            RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED,
            RuntimeEventKind.SHUTDOWN_RECEIPT,
        }:
            raise ValueError(
                "runtime Product event application kind invalid"
            )
        if type(self.event_digest) is not str:
            raise TypeError(
                "runtime Product event application digest string required"
            )
        digest = self.event_digest
        if (
            len(digest) != 64
            or digest != digest.lower()
            or any(
                item not in "0123456789abcdef" for item in digest
            )
        ):
            raise ValueError(
                "runtime Product event application digest invalid"
            )
        if type(self.applied_at) is not float:
            raise TypeError(
                "runtime Product event application timestamp float required"
            )
        if not math.isfinite(self.applied_at) or self.applied_at <= 0.0:
            raise ValueError(
                "runtime Product event application timestamp invalid"
            )
        if (
            type(self.application_key) is not str
            or not self.application_key
            or self.application_key != self.application_key.strip()
        ):
            raise ValueError(
                "runtime Product event application key required"
            )


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleAcknowledgement:
    """Parent acceptance of a lifecycle envelope."""

    sequence: int
    event_kind: RuntimeEventKind
    generation_identity: (
        RuntimeWorkerGenerationIdentity
        | RuntimeProductGenerationIdentity
        | RuntimeNamespaceGenerationIdentity
    )
    event_digest: str
    accepted: bool
    reason: str = ""
    application_receipt: (
        RuntimeLifecycleApplicationReceipt
        | RuntimeProductEventApplicationReceipt
        | None
    ) = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime lifecycle acknowledgement requires sequence")
        if not isinstance(self.event_kind, RuntimeEventKind):
            raise TypeError(
                "runtime lifecycle acknowledgement requires RuntimeEventKind"
            )
        if self.event_kind is RuntimeEventKind.STATUS:
            raise ValueError("runtime status cannot be lifecycle-acknowledged")
        identity = self.generation_identity
        if self.event_kind in {
            RuntimeEventKind.PRODUCT_ROOT_ADMISSION,
            RuntimeEventKind.WORKER_REPLACEMENT_REQUIRED,
            RuntimeEventKind.SHUTDOWN_RECEIPT,
        }:
            worker_shutdown = (self.event_kind is RuntimeEventKind.SHUTDOWN_RECEIPT
                               and isinstance(identity, RuntimeWorkerGenerationIdentity))
            if not worker_shutdown and not isinstance(identity, RuntimeProductGenerationIdentity):
                raise TypeError(
                    "runtime lifecycle Product-scoped identity required"
                )
        elif not isinstance(identity, RuntimeNamespaceGenerationIdentity):
            raise TypeError(
                "runtime lifecycle namespace identity required"
            )
        if type(self.event_digest) is not str:
            raise TypeError(
                "runtime lifecycle acknowledgement digest string required"
            )
        digest = self.event_digest
        if (
            len(digest) != 64
            or digest != digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
        ):
            raise ValueError("runtime lifecycle acknowledgement digest invalid")
        if type(self.accepted) is not bool:
            raise TypeError(
                "runtime lifecycle acknowledgement accepted bool required"
            )
        if type(self.reason) is not str:
            raise TypeError(
                "runtime lifecycle acknowledgement detail string required"
            )
        reason = self.reason
        if reason != reason.strip():
            raise ValueError(
                "runtime lifecycle acknowledgement detail not canonical"
            )
        if self.accepted:
            if reason:
                raise ValueError(
                    "accepted runtime lifecycle acknowledgement cannot "
                    "carry rejection reason"
                )
            if self.application_receipt is None:
                raise ValueError(
                    "accepted runtime lifecycle acknowledgement requires "
                    "typed application proof"
                )
            expected_receipt_type = (
                RuntimeProductEventApplicationReceipt
                if isinstance(identity, (RuntimeWorkerGenerationIdentity, RuntimeProductGenerationIdentity))
                else RuntimeLifecycleApplicationReceipt
            )
            if not isinstance(
                self.application_receipt,
                expected_receipt_type,
            ):
                raise TypeError(
                    "runtime lifecycle acknowledgement application "
                    "receipt scope mismatch"
                )
            if (
                self.application_receipt.sequence != self.sequence
                or self.application_receipt.event_kind is not self.event_kind
                or self.application_receipt.generation_identity
                != self.generation_identity
                or self.application_receipt.event_digest != digest
            ):
                raise ValueError(
                    "runtime lifecycle acknowledgement application mismatch"
                )
        elif not reason:
            raise ValueError(
                "rejected runtime lifecycle acknowledgement requires reason"
            )
        elif self.application_receipt is not None:
            raise ValueError(
                "rejected runtime lifecycle acknowledgement cannot carry "
                "application proof"
            )

    @property
    def event_scope(self) -> RuntimeEventScope:
        if isinstance(
            self.generation_identity,
            RuntimeNamespaceGenerationIdentity,
        ):
            return RuntimeEventScope.RUNTIME_NAMESPACE
        return RuntimeEventScope.APPLICATION_GLOBAL

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity | None:
        identity = self.generation_identity
        if isinstance(identity, RuntimeNamespaceGenerationIdentity):
            return identity.matrix_identity
        return None


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleClaim:
    """Lifecycle event held by the parent until it becomes visible."""

    generation_identity: (
        RuntimeProductGenerationIdentity
        | RuntimeNamespaceGenerationIdentity
    )
    sequence: int
    event_kind: RuntimeEventKind
    event_digest: str
    event: RuntimeIPCEvent

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime lifecycle claim requires sequence")
        if not isinstance(self.event_kind, RuntimeEventKind):
            raise TypeError("runtime lifecycle claim requires event kind")
        if type(self.event_digest) is not str:
            raise TypeError("runtime lifecycle claim digest string required")
        if (
            len(self.event_digest) != 64
            or self.event_digest != self.event_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in self.event_digest
            )
        ):
            raise ValueError("runtime lifecycle claim digest invalid")
        if not isinstance(self.event, RuntimeIPCEvent):
            raise TypeError("runtime lifecycle claim requires typed event")
        if not isinstance(
            self.generation_identity,
            (
                RuntimeProductGenerationIdentity,
                RuntimeNamespaceGenerationIdentity,
            ),
        ):
            raise TypeError(
                "runtime lifecycle claim generation identity required"
            )
        if self.generation_identity != self.event.generation_identity:
            raise ValueError("runtime lifecycle claim generation identity mismatch")
        if self.event.kind is not self.event_kind:
            raise ValueError("runtime lifecycle claim event kind mismatch")
        if runtime_ipc_event_digest(self.event) != self.event_digest:
            raise ValueError("runtime lifecycle claim event digest mismatch")

    @property
    def runtime_generation(self) -> int:
        identity = self.generation_identity
        if isinstance(identity, RuntimeNamespaceGenerationIdentity):
            return identity.product.worker.worker_generation
        return identity.worker.worker_generation

    @property
    def event_scope(self) -> RuntimeEventScope:
        return self.event.scope

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity | None:
        return self.event.matrix_identity


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleApplicationReceipt:
    """Proof that a claimed event reached the accepted Front."""

    generation_identity: RuntimeNamespaceGenerationIdentity
    sequence: int
    event_kind: RuntimeEventKind
    event_digest: str
    input_generation: int
    front_sequence: int
    application_key: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.generation_identity,
            RuntimeNamespaceGenerationIdentity,
        ):
            raise TypeError(
                "runtime lifecycle application namespace identity required"
            )
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("runtime lifecycle application requires sequence")
        if not isinstance(self.event_kind, RuntimeEventKind):
            raise TypeError(
                "runtime lifecycle application requires event kind"
            )
        if type(self.event_digest) is not str:
            raise TypeError(
                "runtime lifecycle application digest string required"
            )
        digest = self.event_digest
        if (
            len(digest) != 64
            or digest != digest.lower()
            or any(
                item not in "0123456789abcdef" for item in digest
            )
        ):
            raise ValueError(
                "runtime lifecycle application requires event digest"
            )
        if type(self.input_generation) is not int or self.input_generation <= 0:
            raise ValueError(
                "runtime lifecycle application requires input generation"
            )
        if type(self.front_sequence) is not int or self.front_sequence <= 0:
            raise ValueError(
                "runtime lifecycle application requires front sequence"
            )
        if type(self.application_key) is not str:
            raise TypeError(
                "runtime lifecycle application key string required"
            )
        key = self.application_key
        if not key or key != key.strip():
            raise ValueError(
                "runtime lifecycle application requires application key"
            )

    @property
    def runtime_generation(self) -> int:
        return self.generation_identity.product.worker.worker_generation

    @property
    def event_scope(self) -> RuntimeEventScope:
        return RuntimeEventScope.RUNTIME_NAMESPACE

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity:
        return self.generation_identity.matrix_identity


__all__ = [
    "MAX_RUNTIME_FRAME_BYTES",
    "RuntimeEventKind",
    "RuntimeEventScope",
    "RuntimeFrameContractError",
    "RuntimeFrameDeliveryReceipt",
    "RuntimeFrameEnvelope",
    "RuntimeInputFrame",
    "RuntimeCanonicalMapping",
    "RuntimeIPCEvent",
    "RuntimeLifecycleApplicationReceipt",
    "RuntimeLifecycleAcknowledgement",
    "RuntimeLifecycleClaim",
    "RuntimeLifecycleEnvelope",
    "RuntimeManualCommandRequest",
    "RuntimeManualResultReceipt",
    "RuntimeManualTimingReceipt",
    "RuntimeNamespaceGenerationIdentity",
    "RuntimeIPCRequest",
    "RuntimeProductEventApplicationReceipt",
    "RuntimeProductGenerationIdentity",
    "RuntimeProductRootAdmission",
    "RuntimeProductRootAdmissionAuthority",
    "RuntimeRequestKind",
    "RuntimeStartupRouterSerial",
    "RuntimeWorkerGenerationIdentity",
    "RuntimeWorkerReplacementRequired",
    "RuntimeWorkerReplacementReason",
    "RuntimeWorkerReplacementRequiredReceipt",
    "RuntimeWorkerStatusReceipt",
    "RuntimeWorkerStatusState",
    "build_runtime_frame_delivery_receipt",
    "measure_runtime_frame_bytes",
    "runtime_frame_identity",
    "runtime_ipc_event_digest",
    "validate_runtime_frame_envelope",
    "validate_runtime_frame_delivery_receipt",
]
