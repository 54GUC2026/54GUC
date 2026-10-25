"""Reset rules for state retained in the NGUI parent process."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
)
from .debug_surface import PageTruthGenerationRotationReceipt


RESET_MANIFEST_SCHEMA = "NGUIRuntimeResetManifest.v1"
RESET_MANIFEST_OWNER = "54guc.ngui.runtime_generation_coordinator"


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("runtime_reset_mapping_string_key_required")
            output[key] = _deep_freeze(item)
        return MappingProxyType(output)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


class RuntimeStateScope(StrEnum):
    APPLICATION_GLOBAL = "application_global"
    RUNTIME_NAMESPACE = "runtime_namespace"
    SAME_NAMESPACE_EVIDENCE = "same_namespace_evidence"


class RuntimeCutoverDisposition(StrEnum):
    RESET_TO_NEUTRAL = "reset_to_neutral"
    RETAIN_APPLICATION_GLOBAL = "retain_application_global"
    RETAIN_SAME_NAMESPACE = "retain_same_namespace"
    TERMINAL_QUARANTINE = "terminal_quarantine"


class RuntimeLateWorkDisposition(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    TERMINAL_CANCEL = "terminal_cancel"
    TERMINAL_QUARANTINE = "terminal_quarantine"


@dataclass(frozen=True, slots=True)
class RuntimeResetManifestRow:
    state_id: str
    scope_class: RuntimeStateScope
    cutover_disposition: RuntimeCutoverDisposition
    neutral_seed: Any
    late_work_disposition: RuntimeLateWorkDisposition
    verification_gate: str

    def __post_init__(self) -> None:
        if type(self.state_id) is not str:
            raise TypeError("runtime_reset_manifest_state_id_string_required")
        if type(self.verification_gate) is not str:
            raise TypeError(
                "runtime_reset_manifest_verification_gate_string_required"
            )
        if (
            not self.state_id
            or self.state_id != self.state_id.strip()
            or not self.verification_gate
            or self.verification_gate != self.verification_gate.strip()
        ):
            raise ValueError("runtime_reset_manifest_identity_required")
        object.__setattr__(self, "neutral_seed", _deep_freeze(self.neutral_seed))
        if (
            self.scope_class is RuntimeStateScope.APPLICATION_GLOBAL
            and self.cutover_disposition
            is not RuntimeCutoverDisposition.RETAIN_APPLICATION_GLOBAL
        ):
            raise ValueError("application_global_state_must_be_retained")
        if (
            self.cutover_disposition
            is RuntimeCutoverDisposition.RETAIN_APPLICATION_GLOBAL
            and self.scope_class is not RuntimeStateScope.APPLICATION_GLOBAL
        ):
            raise ValueError("runtime_state_cannot_use_global_retention")
        if (
            self.cutover_disposition
            is RuntimeCutoverDisposition.RETAIN_SAME_NAMESPACE
            and self.scope_class
            is not RuntimeStateScope.SAME_NAMESPACE_EVIDENCE
        ):
            raise ValueError("same_namespace_retention_scope_required")


def _row(
    state_id: str,
    *,
    scope: RuntimeStateScope = RuntimeStateScope.RUNTIME_NAMESPACE,
    cutover: RuntimeCutoverDisposition = (
        RuntimeCutoverDisposition.RESET_TO_NEUTRAL
    ),
    neutral: Any = None,
    late: RuntimeLateWorkDisposition = (
        RuntimeLateWorkDisposition.TERMINAL_QUARANTINE
    ),
    gate: str,
) -> RuntimeResetManifestRow:
    return RuntimeResetManifestRow(
        state_id=state_id,
        scope_class=scope,
        cutover_disposition=cutover,
        neutral_seed=neutral,
        late_work_disposition=late,
        verification_gate=f"{gate}:{state_id}:behavior",
    )


_ROWS = (
    _row(
        "ngui.owner.operator_connection_draft",
        scope=RuntimeStateScope.APPLICATION_GLOBAL,
        cutover=(
            RuntimeCutoverDisposition.RETAIN_APPLICATION_GLOBAL
        ),
        neutral={},
        late=RuntimeLateWorkDisposition.NOT_APPLICABLE,
        gate="G02-N11",
    ),
    _row("ngui.owner.input_register_bank", neutral={"state": "neutral"}, gate="G02-N01"),
    _row("ngui.owner.interaction_register", neutral={"state": "neutral"}, gate="G02-N14"),
    _row("ngui.owner.profile_authority", neutral={"state": "neutral"}, gate="G02-N11"),
    _row("ngui.owner.presentation_authority", neutral={"state": "neutral"}, gate="G02-N02"),
    _row("ngui.owner.edit_register", neutral={"state": "neutral"}, gate="G02-N12"),
    _row("ngui.owner.manual_action_lifecycle", neutral={"state": "idle"}, gate="G02-N08"),
    _row("ngui.owner.operator_event_feed", neutral={"state": "neutral"}, gate="G02-N14"),
    _row("ngui.owner.voice_lifecycle", neutral={"state": "inactive"}, gate="G02-N07"),
    _row("ngui.owner.chart_processor", neutral={"state": "neutral"}, gate="G02-N09"),
    _row("ngui.owner.utility_processors", neutral={"state": "neutral"}, gate="G02-N16"),
    _row("ngui.owner.orchestrator_runtime_state", neutral={"state": "neutral"}, gate="G02-N14"),
    _row("ngui.owner.page_truth_generation", neutral={"state": "fresh_generation"}, gate="G02-N10"),
    _row("ngui.owner.parent_async_fence", neutral={"state": "active_successor"}, gate="G02-N16"),
)


RUNTIME_RESET_MANIFEST: Mapping[str, RuntimeResetManifestRow] = MappingProxyType(
    {row.state_id: row for row in _ROWS}
)

if len(RUNTIME_RESET_MANIFEST) != len(_ROWS):
    raise RuntimeError("runtime_reset_manifest_state_id_duplicate")


def runtime_reset_manifest_payload() -> Mapping[str, Any]:
    return _deep_freeze(
        {
            "schema": RESET_MANIFEST_SCHEMA,
            "owner": RESET_MANIFEST_OWNER,
            "rows": tuple(
                _deep_freeze({
                    "state_id": row.state_id,
                    "scope_class": row.scope_class.value,
                    "cutover_disposition": row.cutover_disposition.value,
                    "neutral_seed": row.neutral_seed,
                    "late_work_disposition": row.late_work_disposition.value,
                    "verification_gate": row.verification_gate,
                })
                for row in RUNTIME_RESET_MANIFEST.values()
            ),
        }
    )


def _canonical_payload_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            return {
                "__runtime_reset_type__": "float",
                "value": (
                    "nan"
                    if math.isnan(value)
                    else "positive_infinity"
                    if value > 0.0
                    else "negative_infinity"
                ),
            }
        return value
    if isinstance(value, Enum):
        return {
            "__runtime_reset_type__": "enum",
            "class": (
                f"{type(value).__module__}.{type(value).__qualname__}"
            ),
            "value": _canonical_payload_value(value.value),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__runtime_reset_type__": "dataclass",
            "class": (
                f"{type(value).__module__}.{type(value).__qualname__}"
            ),
            "fields": {
                field.name: _canonical_payload_value(
                    getattr(value, field.name)
                )
                for field in fields(value)
            },
        }
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "runtime_reset_canonical_mapping_string_key_required"
                )
            output[key] = _canonical_payload_value(item)
        return {
            key: output[key]
            for key in sorted(output)
        }
    if isinstance(value, (list, tuple, deque)):
        return [_canonical_payload_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        rows = [_canonical_payload_value(item) for item in value]
        return sorted(
            rows,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    raise TypeError(
        "runtime_reset_canonical_value_required:"
        + type(value).__name__
    )


def _payload_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical_payload_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_reset_manifest_digest() -> str:
    return _payload_digest(runtime_reset_manifest_payload())


def runtime_reset_state_digest(*, state_id: str, value: Any) -> str:
    if type(state_id) is not str:
        raise TypeError("runtime_reset_state_id_string_required")
    if state_id not in RUNTIME_RESET_MANIFEST:
        raise ValueError("runtime_reset_state_unknown")
    return _payload_digest(
        {
            "state_id": state_id,
            "value": _canonical_payload_value(value),
        }
    )


@dataclass(frozen=True, slots=True)
class RuntimeResetEvidenceMapping(Mapping[str, Any]):
    """Pickle-safe immutable evidence stored in reset receipts."""

    payload_bytes: bytes
    payload_digest: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "RuntimeResetEvidenceMapping":
        if not isinstance(value, Mapping):
            raise TypeError("runtime_reset_evidence_mapping_required")
        payload_bytes = json.dumps(
            _canonical_payload_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            payload_bytes=payload_bytes,
            payload_digest=hashlib.sha256(payload_bytes).hexdigest(),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.payload_bytes, bytes):
            raise TypeError("runtime_reset_evidence_bytes_required")
        if (
            self.payload_digest
            != hashlib.sha256(self.payload_bytes).hexdigest()
        ):
            raise ValueError("runtime_reset_evidence_digest_mismatch")
        value = json.loads(self.payload_bytes.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("runtime_reset_evidence_dict_required")

    def to_mapping(self) -> dict[str, Any]:
        value = json.loads(self.payload_bytes.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("runtime_reset_evidence_dict_required")
        return value

    def __getitem__(self, key: str) -> Any:
        return self.to_mapping()[key]

    def __iter__(self):
        return iter(self.to_mapping())

    def __len__(self) -> int:
        return len(self.to_mapping())


class RuntimeResetCause(StrEnum):
    INITIAL_WORKER = "initial_worker"
    WORKER_REPLACEMENT = "worker_replacement"


def runtime_reset_transaction_digest(
    *,
    application_lease: ApplicationLeaseIdentity,
    worker_generation: int,
    previous_worker_generation: int,
    cause: RuntimeResetCause,
    manifest_digest: str,
) -> str:
    """Bind all pre-effect participants without including the final receipt."""

    if not isinstance(application_lease, ApplicationLeaseIdentity):
        raise TypeError("runtime_reset_transaction_application_lease_required")
    if type(worker_generation) is not int or worker_generation <= 0:
        raise ValueError("runtime_reset_transaction_worker_generation_invalid")
    if (
        type(previous_worker_generation) is not int
        or previous_worker_generation < 0
    ):
        raise ValueError(
            "runtime_reset_transaction_previous_generation_invalid"
        )
    if not isinstance(cause, RuntimeResetCause):
        raise TypeError("runtime_reset_transaction_cause_required")
    if type(manifest_digest) is not str:
        raise TypeError(
            "runtime_reset_transaction_manifest_digest_string_required"
        )
    if manifest_digest != runtime_reset_manifest_digest():
        raise ValueError("runtime_reset_transaction_manifest_mismatch")
    return _payload_digest(
        {
            "schema": "NGUIRuntimeResetTransaction.v1",
            "owner": RESET_MANIFEST_OWNER,
            "application_lease": application_lease.to_payload(),
            "worker_generation": worker_generation,
            "previous_worker_generation": previous_worker_generation,
            "cause": cause.value,
            "manifest_digest": manifest_digest,
        }
    )


class RuntimeResetRowResultState(StrEnum):
    RESET_TO_NEUTRAL = "reset_to_neutral"
    RETAINED = "retained"
    TERMINAL_QUARANTINED = "terminal_quarantined"


@dataclass(frozen=True, slots=True)
class RuntimeResetRowApplicationReceipt:
    state_id: str
    result_state: RuntimeResetRowResultState
    prior_digest: str
    result_digest: str

    def __post_init__(self) -> None:
        if type(self.state_id) is not str:
            raise TypeError(
                "runtime_reset_application_state_id_string_required"
            )
        row = RUNTIME_RESET_MANIFEST.get(self.state_id)
        if row is None:
            raise ValueError("runtime_reset_application_state_unknown")
        if not isinstance(
            self.result_state,
            RuntimeResetRowResultState,
        ):
            raise TypeError("runtime_reset_application_result_state_required")
        expected = (
            RuntimeResetRowResultState.RETAINED
            if row.cutover_disposition
            is RuntimeCutoverDisposition.RETAIN_APPLICATION_GLOBAL
            else (
                RuntimeResetRowResultState.TERMINAL_QUARANTINED
                if row.cutover_disposition
                is RuntimeCutoverDisposition.TERMINAL_QUARANTINE
                else RuntimeResetRowResultState.RESET_TO_NEUTRAL
            )
        )
        if self.result_state is not expected:
            raise ValueError(
                "runtime_reset_application_result_disposition_mismatch"
        )
        digests = {}
        for name in ("prior_digest", "result_digest"):
            digest = getattr(self, name)
            if type(digest) is not str:
                raise TypeError(
                    f"runtime_reset_application_{name}_string_required"
                )
            if (
                digest != digest.strip().lower()
                or len(digest) != 64
                or any(
                item not in "0123456789abcdef" for item in digest
                )
            ):
                raise ValueError(
                    f"runtime_reset_application_{name}_invalid"
                )
            digests[name] = digest
        if (
            self.result_state is RuntimeResetRowResultState.RETAINED
            and digests["prior_digest"] != digests["result_digest"]
        ):
            raise ValueError(
                "runtime_reset_application_retained_value_changed"
            )
        if self.result_state is not RuntimeResetRowResultState.RETAINED:
            expected_result = runtime_reset_state_digest(
                state_id=self.state_id,
                value=row.neutral_seed,
            )
            if digests["result_digest"] != expected_result:
                raise ValueError(
                    "runtime_reset_application_neutral_result_mismatch"
                )
    @classmethod
    def from_observed(
        cls,
        *,
        state_id: str,
        prior_value: Any,
        result_value: Any,
    ) -> "RuntimeResetRowApplicationReceipt":
        if type(state_id) is not str:
            raise TypeError(
                "runtime_reset_application_state_id_string_required"
            )
        row = RUNTIME_RESET_MANIFEST.get(state_id)
        if row is None:
            raise ValueError("runtime_reset_application_state_unknown")
        result_state = (
            RuntimeResetRowResultState.RETAINED
            if row.cutover_disposition
            is RuntimeCutoverDisposition.RETAIN_APPLICATION_GLOBAL
            else (
                RuntimeResetRowResultState.TERMINAL_QUARANTINED
                if row.cutover_disposition
                is RuntimeCutoverDisposition.TERMINAL_QUARANTINE
                else RuntimeResetRowResultState.RESET_TO_NEUTRAL
            )
        )
        return cls(
            state_id=row.state_id,
            result_state=result_state,
            prior_digest=runtime_reset_state_digest(
                state_id=row.state_id,
                value=prior_value,
            ),
            result_digest=runtime_reset_state_digest(
                state_id=row.state_id,
                value=result_value,
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "state_id": self.state_id,
            "result_state": self.result_state.value,
            "prior_digest": self.prior_digest,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeResetApplicationReceipt:
    """Proof that the complete NGUI parent reset manifest was applied."""

    application_lease: ApplicationLeaseIdentity
    worker_generation: int
    previous_worker_generation: int
    cause: RuntimeResetCause
    manifest_digest: str
    transaction_digest: str
    page_truth_rotation: PageTruthGenerationRotationReceipt
    row_results: tuple[RuntimeResetRowApplicationReceipt, ...]
    applied_at: float
    receipt_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.application_lease, ApplicationLeaseIdentity):
            raise TypeError("runtime_reset_application_lease_required")
        if type(self.worker_generation) is not int or self.worker_generation <= 0:
            raise ValueError("runtime_reset_worker_generation_invalid")
        if (
            type(self.previous_worker_generation) is not int
            or self.previous_worker_generation < 0
        ):
            raise ValueError(
                "runtime_reset_previous_worker_generation_invalid"
            )
        if not isinstance(self.cause, RuntimeResetCause):
            raise TypeError("runtime_reset_cause_required")
        if self.cause is RuntimeResetCause.INITIAL_WORKER:
            if self.previous_worker_generation != 0:
                raise ValueError(
                    "runtime_reset_initial_previous_generation_forbidden"
                )
        elif (
            self.previous_worker_generation <= 0
            or self.worker_generation
            != self.previous_worker_generation + 1
        ):
            raise ValueError(
                "runtime_reset_replacement_generation_not_contiguous"
            )
        if type(self.manifest_digest) is not str:
            raise TypeError("runtime_reset_manifest_digest_string_required")
        if self.manifest_digest != runtime_reset_manifest_digest():
            raise ValueError("runtime_reset_manifest_digest_mismatch")
        if not isinstance(self.row_results, tuple) or any(
            not isinstance(item, RuntimeResetRowApplicationReceipt)
            for item in self.row_results
        ):
            raise TypeError("runtime_reset_row_results_tuple_required")
        state_ids = tuple(item.state_id for item in self.row_results)
        if state_ids != tuple(RUNTIME_RESET_MANIFEST):
            raise ValueError("runtime_reset_row_result_denominator_mismatch")
        if type(self.transaction_digest) is not str:
            raise TypeError(
                "runtime_reset_transaction_digest_string_required"
            )
        expected_transaction_digest = runtime_reset_transaction_digest(
            application_lease=self.application_lease,
            worker_generation=self.worker_generation,
            previous_worker_generation=self.previous_worker_generation,
            cause=self.cause,
            manifest_digest=self.manifest_digest,
        )
        if self.transaction_digest != expected_transaction_digest:
            raise ValueError("runtime_reset_transaction_digest_mismatch")
        if not isinstance(
            self.page_truth_rotation,
            PageTruthGenerationRotationReceipt,
        ):
            raise TypeError(
                "runtime_reset_page_truth_rotation_receipt_required"
            )
        if (
            self.page_truth_rotation.application_lease
            != self.application_lease
            or self.page_truth_rotation.previous_generation
            != self.previous_worker_generation
            or self.page_truth_rotation.generation
            != self.worker_generation
            or self.page_truth_rotation.reset_transaction_digest
            != self.transaction_digest
        ):
            raise ValueError(
                "runtime_reset_page_truth_rotation_identity_mismatch"
            )
        if (
            self.page_truth_rotation.prior_termination.admission_closed
            is not True
            or self.page_truth_rotation.prior_termination.terminated
            is not True
            or self.page_truth_rotation.prior_termination.status
            != "complete"
        ):
            raise ValueError(
                "runtime_reset_page_truth_prior_writer_not_drained"
            )
        if type(self.applied_at) is not float:
            raise TypeError("runtime_reset_applied_at_float_required")
        if not math.isfinite(self.applied_at) or self.applied_at <= 0.0:
            raise ValueError("runtime_reset_applied_at_invalid")
        if type(self.receipt_digest) is not str:
            raise TypeError("runtime_reset_receipt_digest_string_required")
        expected = _payload_digest(self._digest_material())
        if self.receipt_digest and self.receipt_digest != expected:
            raise ValueError("runtime_reset_receipt_digest_mismatch")
        object.__setattr__(self, "receipt_digest", expected)

    def _digest_material(self) -> dict[str, Any]:
        return {
            "application_lease": self.application_lease.to_payload(),
            "worker_generation": self.worker_generation,
            "previous_worker_generation": self.previous_worker_generation,
            "cause": self.cause.value,
            "manifest_digest": self.manifest_digest,
            "transaction_digest": self.transaction_digest,
            "page_truth_rotation": self.page_truth_rotation.to_payload(),
            "row_results": [
                item.as_dict() for item in self.row_results
            ],
            "applied_at": self.applied_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._digest_material(),
            "receipt_digest": self.receipt_digest,
        }


class RuntimeResetApplicationPort(Protocol):
    """Apply the reset manifest atomically in the parent process."""

    def apply_runtime_reset(
        self,
        *,
        application_lease: ApplicationLeaseIdentity,
        worker_generation: int,
        previous_worker_generation: int,
        cause: RuntimeResetCause,
        expected_manifest_digest: str,
    ) -> RuntimeResetApplicationReceipt: ...


__all__ = (
    "RESET_MANIFEST_OWNER",
    "RESET_MANIFEST_SCHEMA",
    "RUNTIME_RESET_MANIFEST",
    "RuntimeCutoverDisposition",
    "RuntimeLateWorkDisposition",
    "RuntimeResetApplicationReceipt",
    "RuntimeResetApplicationPort",
    "RuntimeResetCause",
    "RuntimeResetEvidenceMapping",
    "RuntimeResetManifestRow",
    "RuntimeResetRowApplicationReceipt",
    "RuntimeResetRowResultState",
    "RuntimeStateScope",
    "runtime_reset_manifest_digest",
    "runtime_reset_manifest_payload",
    "runtime_reset_state_digest",
    "runtime_reset_transaction_digest",
)
