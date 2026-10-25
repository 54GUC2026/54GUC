"""Immutable physical-generation admission and signal-compute authority.

This module is intentionally independent of Modem Live, Controller Runtime,
Brain, NGUI, and modem profiles.  A future composition cutover can feed raw
physical generations into :class:`SignalProcessingMatrix` without
making this contract another acquisition or scheduling owner.

The authority has one deliberately narrow privilege: it is the only object in
this module that can call ``SignalComputer.update``.  Consumers receive a
restricted facade for configuration, checkpoint, and diagnostic operations;
the state-advancing ``update`` method is not exposed.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.modem_live.truth_matrix import (
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
    ValidatedTruthMatrixReader,
)
from arch4.rtt_observation import MatrixBoundRttObservation


_SIGNAL_RUNTIME_FACADE_METHODS = frozenset(
    {
        "refresh_thresholds",
        "get_congestion_weights_status",
        "set_runtime_capabilities",
        "get_dl_jitter_formula_status",
        "bind_config_generation",
        "admit_learned_signal_baselines",
        "baseline_persistence_status",
    }
)


class ImmutablePayload:
    """Defensively copied opaque payload with copy-out-only access.

    Modem and computed-signal carriers are dataclasses in production, while
    status/line surfaces are mappings.  Keeping the carrier opaque avoids an
    architecture dependency here.  No caller receives the retained object;
    each read returns a new deep copy.
    """

    __slots__ = ("__value",)

    def __init__(self, value: Any) -> None:
        try:
            retained = copy.deepcopy(value)
        except Exception as exc:
            raise TypeError(
                f"generation payload must be defensively copyable: {type(exc).__name__}: {exc}"
            ) from exc
        object.__setattr__(self, "_ImmutablePayload__value", retained)

    @classmethod
    def capture(cls, value: Any) -> "ImmutablePayload":
        if isinstance(value, cls):
            return value
        return cls(value)

    def copy(self) -> Any:
        """Return a detached copy; mutating it cannot change the contract."""

        return copy.deepcopy(self.__value)

    def mapping_copy(self) -> dict[str, Any]:
        value = self.copy()
        if not isinstance(value, Mapping):
            raise TypeError("generation payload is not a mapping")
        return dict(value)

    def readonly_mapping(self) -> Mapping[str, Any]:
        """Return a detached, top-level read-only mapping view."""

        return MappingProxyType(self.mapping_copy())

    def __copy__(self) -> "ImmutablePayload":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "ImmutablePayload":
        return self

    def __repr__(self) -> str:
        return f"ImmutablePayload(type={type(self.__value).__name__})"


@dataclass(frozen=True, slots=True)
class RawModemGeneration:
    """One immutable Matrix-only physical Modem Live observation.

    The historic detached ``ModemInput`` and ``base_status`` carriers are
    intentionally absent.  Modem Live's validated Matrix is the only raw
    current-state truth admitted by this authority.
    """

    matrix_identity: MatrixGenerationIdentity
    sampled_at: float
    published_at: float
    source: str
    truth_matrix: ValidatedTruthMatrixReader | Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("raw_modem_generation_matrix_identity_required")
        if type(self.sampled_at) is not float:
            raise TypeError("raw_modem_generation_sampled_at_invalid")
        if type(self.published_at) is not float:
            raise TypeError("raw_modem_generation_published_at_invalid")
        if (
            type(self.source) is not str
            or not self.source
            or self.source != self.source.strip()
        ):
            raise TypeError("raw_modem_generation_source_invalid")
        sampled_at = self.sampled_at
        published_at = self.published_at
        source = self.source
        if not math.isfinite(sampled_at) or sampled_at <= 0.0:
            raise ValueError(
                "raw modem generation sampled_at must be positive and finite"
            )
        if not math.isfinite(published_at) or published_at <= 0.0:
            raise ValueError(
                "raw modem generation published_at must be positive and finite"
            )
        if published_at < sampled_at:
            raise ValueError(
                "raw modem generation cannot be published before it was sampled"
            )
        if not source:
            raise ValueError("raw modem generation source is required")
        matrix = (
            self.truth_matrix
            if isinstance(self.truth_matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(self.truth_matrix)
        )
        matrix_sampled_at = matrix.metadata("sampled_at")
        matrix_source = matrix.metadata("source")
        matrix_identity = matrix.identity
        if matrix_identity != self.matrix_identity:
            raise ValueError("raw modem generation Matrix identity mismatch")
        if matrix_sampled_at != sampled_at:
            raise ValueError("raw modem generation Matrix timestamp mismatch")
        if matrix_source != source:
            raise ValueError("raw modem generation Matrix source mismatch")
        object.__setattr__(self, "sampled_at", sampled_at)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(
            self,
            "truth_matrix",
            matrix,
        )

    @classmethod
    def capture(
        cls,
        *,
        matrix_identity: MatrixGenerationIdentity,
        sampled_at: float,
        published_at: float,
        source: str,
        truth_matrix: Mapping[str, Any],
    ) -> "RawModemGeneration":
        return cls(
            matrix_identity=matrix_identity,
            sampled_at=sampled_at,
            published_at=published_at,
            source=source,
            truth_matrix=truth_matrix,
        )

    @classmethod
    def capture_owner_validated(
        cls,
        *,
        matrix: ValidatedTruthMatrixReader,
        published_at: float,
    ) -> "RawModemGeneration":
        if not isinstance(matrix, ValidatedTruthMatrixReader):
            raise TypeError("raw_modem_generation_validated_matrix_required")
        return cls(
            matrix_identity=matrix.identity,
            sampled_at=float(matrix.metadata("sampled_at")),
            published_at=published_at,
            source=str(matrix.metadata("source")),
            truth_matrix=matrix,
        )

    def truth_matrix_copy(self) -> dict[str, Any]:
        matrix = self.truth_matrix
        if not isinstance(matrix, ValidatedTruthMatrixReader):
            raise TypeError("raw_modem_generation_matrix_invalid")
        return matrix.to_mapping()

    def truth_matrix_reader(self) -> ValidatedTruthMatrixReader:
        matrix = self.truth_matrix
        if not isinstance(matrix, ValidatedTruthMatrixReader):
            raise TypeError("raw_modem_generation_matrix_invalid")
        return matrix

    @property
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def session_scope_key(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation


@dataclass(frozen=True, slots=True)
class AcceptedModemGeneration:
    """One raw generation after exactly one successful signal computation."""

    raw: RawModemGeneration
    accepted_at: float
    computed: ImmutablePayload
    matrix_rtt: MatrixBoundRttObservation

    def __post_init__(self) -> None:
        if not isinstance(self.raw, RawModemGeneration):
            raise TypeError("accepted generation requires a RawModemGeneration")
        if type(self.accepted_at) is not float:
            raise TypeError("accepted_generation_accepted_at_invalid")
        accepted_at = self.accepted_at
        if not math.isfinite(accepted_at) or accepted_at <= 0.0:
            raise ValueError(
                "accepted generation accepted_at must be positive and finite"
            )
        if accepted_at < self.raw.published_at:
            raise ValueError("accepted generation cannot precede raw publication")
        if not isinstance(self.matrix_rtt, MatrixBoundRttObservation):
            raise TypeError("accepted generation requires Matrix-bound RTT")
        if self.matrix_rtt.matrix_identity != self.raw.matrix_identity:
            raise ValueError("accepted_generation_rtt_identity_mismatch")
        observation = self.matrix_rtt.observation
        latest_rtt_evidence_at = max(
            float(observation.snapshot_at),
            float(observation.observed_at),
            float(observation.last_probe_at),
            float(self.matrix_rtt.bound_at),
        )
        if accepted_at < latest_rtt_evidence_at:
            raise ValueError(
                "accepted generation cannot precede attached RTT evidence"
            )
        computed = ImmutablePayload.capture(self.computed)
        if computed.copy() is None:
            raise ValueError("accepted generation requires computed signal truth")
        object.__setattr__(self, "accepted_at", accepted_at)
        object.__setattr__(self, "computed", computed)

    @property
    def source_sequence(self) -> int:
        return self.raw.source_sequence

    def computed_copy(self) -> Any:
        return self.computed.copy()

    @property
    def delivery_identity(self) -> "AcceptedGenerationIdentity":
        """Return the full identity of this accepted physical generation."""

        return AcceptedGenerationIdentity.from_accepted(self)


@dataclass(frozen=True, slots=True)
class AcceptedGenerationIdentity:
    """Typed fan-out wrapper around the sole Matrix identity."""

    matrix_identity: MatrixGenerationIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "accepted_generation_matrix_identity_required"
            )

    @classmethod
    def from_accepted(
        cls,
        accepted: AcceptedModemGeneration,
    ) -> "AcceptedGenerationIdentity":
        if not isinstance(accepted, AcceptedModemGeneration):
            raise TypeError("accepted_generation_required")
        return cls(matrix_identity=accepted.raw.matrix_identity)

    def same_namespace(self, other: "AcceptedGenerationIdentity") -> bool:
        return (
            isinstance(other, AcceptedGenerationIdentity)
            and self.matrix_identity.namespace
            == other.matrix_identity.namespace
        )

    def as_dict(self) -> dict[str, Any]:
        return {"matrix_identity": self.matrix_identity.as_dict()}

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


@dataclass(frozen=True, slots=True)
class QuarantinedModemGeneration:
    """Terminal record for a generation whose signal computation failed."""

    raw: RawModemGeneration
    quarantined_at: float
    reason: str
    error_type: str
    error_message: str
    state_rollback_succeeded: bool = False
    rollback_error_type: str = ""
    rollback_error_message: str = ""


@dataclass(frozen=True, slots=True)
class GenerationOfferResult:
    queued: bool
    source_sequence: int
    reason: str
    replaced_sequence: int = 0


@dataclass(frozen=True, slots=True)
class GenerationProcessResult:
    ran: bool
    status: str
    source_sequence: int
    sequence_gap: int = 0
    accepted: AcceptedModemGeneration | None = None
    quarantine: QuarantinedModemGeneration | None = None


@dataclass(frozen=True, slots=True)
class PendingGenerationDisposition:
    """Terminal evidence for an offered generation closed before compute."""

    identity: AcceptedGenerationIdentity
    disposed_at: float
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AcceptedGenerationIdentity):
            raise TypeError("pending_generation_disposition_identity_required")
        if (
            type(self.disposed_at) is not float
            or not math.isfinite(self.disposed_at)
            or self.disposed_at <= 0.0
        ):
            raise TypeError("pending_generation_disposition_timestamp_invalid")
        if (
            type(self.reason) is not str
            or not self.reason
            or self.reason != self.reason.strip()
        ):
            raise TypeError("pending_generation_disposition_reason_invalid")


@dataclass(frozen=True, slots=True)
class GenerationAuthorityCloseReceipt:
    """Exact shutdown receipt; pending accepted work is never silently erased."""

    closed_at: float
    namespace: RuntimeGenerationNamespace | None
    pending_disposition: PendingGenerationDisposition | None
    inflight_sequence: int

    def __post_init__(self) -> None:
        if (
            type(self.closed_at) is not float
            or not math.isfinite(self.closed_at)
            or self.closed_at <= 0.0
        ):
            raise TypeError("generation_authority_close_timestamp_invalid")
        if self.namespace is not None and not isinstance(
            self.namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("generation_authority_close_namespace_invalid")
        if self.pending_disposition is not None and not isinstance(
            self.pending_disposition,
            PendingGenerationDisposition,
        ):
            raise TypeError("generation_authority_close_disposition_invalid")
        if type(self.inflight_sequence) is not int or self.inflight_sequence < 0:
            raise TypeError("generation_authority_close_inflight_invalid")


@dataclass(frozen=True, slots=True)
class GenerationAuthorityTerminationReadiness:
    namespace: RuntimeGenerationNamespace | None
    pending_identity: AcceptedGenerationIdentity | None
    inflight_sequence: int
    closed: bool

    def __post_init__(self) -> None:
        if self.namespace is not None and not isinstance(
            self.namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "generation_authority_readiness_namespace_invalid"
            )
        if self.pending_identity is not None and not isinstance(
            self.pending_identity,
            AcceptedGenerationIdentity,
        ):
            raise TypeError(
                "generation_authority_readiness_pending_invalid"
            )
        if type(self.inflight_sequence) is not int or self.inflight_sequence < 0:
            raise TypeError(
                "generation_authority_readiness_inflight_invalid"
            )
        if type(self.closed) is not bool:
            raise TypeError(
                "generation_authority_readiness_closed_invalid"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "namespace": (
                self.namespace.as_dict()
                if self.namespace is not None
                else {}
            ),
            "pending_identity": (
                self.pending_identity.as_dict()
                if self.pending_identity is not None
                else {}
            ),
            "inflight_sequence": self.inflight_sequence,
            "closed": self.closed,
        }


@dataclass(frozen=True, slots=True)
class AcceptedGenerationNamespaceBindingPlan:
    """Prepared no-fail namespace commit for the composition transaction."""

    namespace: RuntimeGenerationNamespace
    expected_namespace_generation: int
    already_bound: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "accepted_generation_binding_plan_namespace_required"
            )
        if (
            type(self.expected_namespace_generation) is not int
            or self.expected_namespace_generation < 0
        ):
            raise TypeError(
                "accepted_generation_binding_plan_generation_invalid"
            )
        if type(self.already_bound) is not bool:
            raise TypeError(
                "accepted_generation_binding_plan_disposition_invalid"
            )


@dataclass(frozen=True, slots=True)
class _PendingGeneration:
    raw: RawModemGeneration
    matrix_rtt: MatrixBoundRttObservation


class _MatrixSignalInput:
    """Permanent Matrix-to-SignalComputer boundary adapter.

    It exposes only canonical Matrix facts plus the accepted-generation
    timing overlay.  Duplicate canonical field names with different values
    fail loudly instead of choosing an implicit line precedence.
    """

    __slots__ = (
        "__values",
        "__cell_states",
        "session_scope_key",
        "sample_sequence",
        "sample_fresh",
        "engine_tick_ts",
        "timestamp",
        "sample_source",
    )

    def __init__(
        self,
        matrix: ValidatedTruthMatrixReader | Mapping[str, Any],
        *,
        source_sequence: int,
        accepted_at: float,
    ) -> None:
        reader = (
            matrix
            if isinstance(matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(matrix)
        )
        values: dict[str, Any] = {}
        cell_states: dict[str, str] = {}
        for line in MODEM_LIVE_TRUTH_FIELD_CATALOG:
            for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[line]:
                cell_states[f"{line}.{field}"] = reader.cell(
                    line,
                    field,
                ).state
            for name, value in reader.line_values(line).items():
                if name in values and values[name] != value:
                    raise RuntimeError(
                        f"matrix_signal_input_field_collision:{name}"
                    )
                values[name] = value
        if "serving_band" in values:
            values["primary_band"] = values["serving_band"]
            values["pband"] = values["serving_band"]
        if "dl_bandwidth_mhz" in values:
            values["primary_bw_mhz"] = values["dl_bandwidth_mhz"]
        values["nsa_snapshot"] = reader.nsa_snapshot()
        from arch4.core_contracts.quality_model import quality_model_for_device_class
        values["quality_model"] = quality_model_for_device_class(values.get("device_radio_class", "unclassified"))
        def current_value(line: str, field: str):
            cell = reader.cell(line, field)
            return cell.value if cell.state == "available" and cell.retention is None else None
        def optional_rank(line: str, field: str) -> bool:
            cell = reader.cell(line, field)
            # A profile with no RI producer must not block RF scoring. A
            # failed, invalid or retained read is not evidence of optionality.
            return cell.state == "unsupported" or (
                cell.state == "missing"
                and cell.reason == "modem_live_profile_untested_not_observed"
            )
        values["quality_radio_inputs"] = {
            "sequence": source_sequence,
            "connected": current_value("health", "radio_connected"),
            "network_type": current_value("radio", "network_type"),
            "ca_active": current_value("radio", "ca_active"),
            "nr_active": current_value("radio", "nr5g_active"),
            "lte": {"bw": current_value("slow", "dl_bandwidth_mhz"),
                    "sinr": current_value("rf", "sinr_db"),
                    "rsrp": current_value("rf", "rsrp_dbm"),
                    "ri": current_value("rf", "ri"),
                    "ri_optional": optional_rank("rf", "ri")},
            "nr": {"bw": current_value("slow", "nr_bandwidth_mhz"),
                   "sinr": current_value("radio", "nr_sinr_db"),
                   "rsrp": current_value("radio", "nr_rsrp_dbm"),
                   "ri": current_value("radio", "nr_ri"),
                   "ri_optional": optional_rank("radio", "nr_ri")},
        }
        values["matrix_ca_metric_supported"] = (
            cell_states.get("radio.ca_active") != "unsupported"
        )
        values["matrix_bandwidth_supported"] = (
            cell_states.get("slow.dl_bandwidth_mhz") != "unsupported"
        )
        if type(source_sequence) is not int or source_sequence <= 0:
            raise TypeError("matrix_signal_input_sequence_invalid")
        if type(accepted_at) is not float or not math.isfinite(accepted_at):
            raise TypeError("matrix_signal_input_accepted_at_invalid")
        sampled_at = reader.metadata("sampled_at")
        source = reader.metadata("source")
        if type(sampled_at) is not float or type(source) is not str:
            raise TypeError("matrix_signal_input_metadata_invalid")
        object.__setattr__(self, "_MatrixSignalInput__values", values)
        object.__setattr__(self, "session_scope_key", reader.identity.session_scope_key)
        object.__setattr__(
            self,
            "_MatrixSignalInput__cell_states",
            cell_states,
        )
        object.__setattr__(self, "sample_sequence", source_sequence)
        object.__setattr__(self, "sample_fresh", True)
        object.__setattr__(self, "engine_tick_ts", accepted_at)
        object.__setattr__(
            self,
            "timestamp",
            sampled_at,
        )
        object.__setattr__(
            self,
            "sample_source",
            source,
        )

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_MatrixSignalInput__values")
        if name in values:
            return copy.deepcopy(values[name])
        raise AttributeError(name)

    def matrix_cell_state(self, line: str, field: str) -> str:
        states = object.__getattribute__(
            self,
            "_MatrixSignalInput__cell_states",
        )
        return str(states[f"{line}.{field}"])

    @property
    def radio_connected(self) -> bool | None:
        """Unknown mobile state does not prevent a local router session."""
        values = object.__getattribute__(self, "_MatrixSignalInput__values")
        return values.get("radio_connected")


class SignalRuntimeFacade:
    """Serialized non-advancing access to the retained SignalComputer.

    ``update`` is intentionally absent.  Every allowed method is explicit so a
    newly added SignalComputer method cannot silently become a second mutation
    path.
    """

    __slots__ = ("__authority",)

    def __init__(self, authority: "SignalProcessingMatrix") -> None:
        object.__setattr__(self, "_SignalRuntimeFacade__authority", authority)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in _SIGNAL_RUNTIME_FACADE_METHODS:
            raise AttributeError(
                f"SignalRuntimeFacade does not expose {method}"
            )
        authority = object.__getattribute__(self, "_SignalRuntimeFacade__authority")
        return authority._invoke_signal_method(method, *args, **kwargs)

    def refresh_thresholds(self) -> Any:
        return self._call("refresh_thresholds")

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> Any:
        return self._call(
            "bind_config_generation",
            config,
            generation_id=generation_id,
            config_fingerprint=config_fingerprint,
        )

    def get_congestion_weights_status(self) -> Any:
        return self._call("get_congestion_weights_status")

    def baseline_persistence_status(self) -> Any:
        return self._call("baseline_persistence_status")

    def set_runtime_capabilities(self, capabilities: Any) -> Any:
        return self._call("set_runtime_capabilities", capabilities)

    def get_dl_jitter_formula_status(self) -> Any:
        return self._call("get_dl_jitter_formula_status")

    def admit_learned_signal_baselines(self, **kwargs: Any) -> Any:
        return self._call("admit_learned_signal_baselines", **kwargs)

class SignalProcessingMatrix:
    """Sole modem-neutral owner of derived state from canonical Matrix input."""

    owner = "signal_processing_matrix"

    def __init__(self, signal_computer: Any) -> None:
        update = getattr(signal_computer, "update", None)
        if not callable(update):
            raise TypeError(
                "accepted generation authority requires SignalComputer.update"
            )
        capture_state = getattr(signal_computer, "capture_transaction_state", None)
        restore_state = getattr(signal_computer, "restore_transaction_state", None)
        if not callable(capture_state) or not callable(restore_state):
            raise TypeError(
                "accepted generation authority requires atomic signal transaction state"
            )
        self.__signal_computer = signal_computer
        # The admission fence is held for namespace bind/reset, every signal
        # transaction, every facade call, and close.  State may still accept a
        # newer pending offer while a compute is running, but no namespace can
        # become visible, disappear, or reuse old signal state mid-transaction.
        self.__admission_fence = threading.RLock()
        self.__state_lock = threading.RLock()
        self.__signal_lock = threading.RLock()
        self.__facade = SignalRuntimeFacade(self)
        # Capture/restore is required for each admitted signal transaction.
        # Namespace prepare itself must remain side-effect free: a newly
        # constructed authority cannot process or configure signals before its
        # namespace is bound, so there is no observation state to reset here.
        capture_state()
        self.__pending: _PendingGeneration | None = None
        self.__inflight_sequence = 0
        self.__last_terminal_sequence = 0
        self.__last_accepted: AcceptedModemGeneration | None = None
        self.__compute_quarantine: QuarantinedModemGeneration | None = None
        self.__last_quarantine: QuarantinedModemGeneration | None = None
        self.__coalesced_total = 0
        self.__duplicate_total = 0
        self.__retryable_compute_failure_total = 0
        self.__rollback_total = 0
        self.__rollback_failure_total = 0
        self.__session_rejection_total = 0
        self.__last_session_reason = ""
        self.__closed = False
        self.__active_namespace: RuntimeGenerationNamespace | None = None
        self.__namespace_generation = 0

    @property
    def signal_facade(self) -> SignalRuntimeFacade:
        return self.__facade

    def prepare_runtime_namespace_binding(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> AcceptedGenerationNamespaceBindingPlan:
        """Validate a no-fail, side-effect-free namespace commit."""

        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "accepted_generation_runtime_namespace_required"
            )
        with self.__admission_fence:
            with self.__state_lock:
                if self.__closed:
                    raise RuntimeError("signal_processing_matrix_closed")
                if self.__inflight_sequence > 0:
                    raise RuntimeError(
                        "accepted_generation_namespace_bind_inflight"
                    )
                if self.__active_namespace == namespace:
                    return AcceptedGenerationNamespaceBindingPlan(
                        namespace=namespace,
                        expected_namespace_generation=(
                            self.__namespace_generation
                        ),
                        already_bound=True,
                    )
                if self.__active_namespace is not None:
                    raise RuntimeError(
                        "accepted_generation_namespace_replacement_requires_new_worker"
                    )
                expected_generation = self.__namespace_generation
            with self.__state_lock:
                if (
                    self.__closed
                    or self.__active_namespace is not None
                    or self.__namespace_generation != expected_generation
                ):
                    raise RuntimeError(
                        "accepted_generation_namespace_prepare_state_changed"
                    )
        return AcceptedGenerationNamespaceBindingPlan(
            namespace=namespace,
            expected_namespace_generation=expected_generation,
            already_bound=False,
        )

    def commit_runtime_namespace_binding(
        self,
        plan: AcceptedGenerationNamespaceBindingPlan,
    ) -> RuntimeGenerationNamespace:
        if not isinstance(plan, AcceptedGenerationNamespaceBindingPlan):
            raise TypeError(
                "accepted_generation_namespace_binding_plan_required"
            )
        namespace = plan.namespace
        with self.__admission_fence:
            with self.__state_lock:
                if self.__closed:
                    raise RuntimeError("signal_processing_matrix_closed")
                if plan.already_bound:
                    if (
                        self.__active_namespace != namespace
                        or self.__namespace_generation
                        != plan.expected_namespace_generation
                    ):
                        raise RuntimeError(
                            "accepted_generation_namespace_binding_plan_stale"
                        )
                    return namespace
                if (
                    self.__active_namespace is not None
                    or self.__inflight_sequence > 0
                    or self.__namespace_generation
                    != plan.expected_namespace_generation
                ):
                    raise RuntimeError(
                        "accepted_generation_namespace_binding_plan_stale"
                    )
                self.__pending = None
                self.__last_terminal_sequence = 0
                self.__last_accepted = None
                self.__compute_quarantine = None
                self.__last_quarantine = None
                self.__active_namespace = namespace
                self.__namespace_generation += 1
        return namespace

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        del namespace
        raise RuntimeError(
            "direct_compute_namespace_binding_illegal:"
            "use_modem_sample_publication_atomic_bind"
        )

    def rollback_runtime_namespace_binding(
        self,
        plan: AcceptedGenerationNamespaceBindingPlan,
    ) -> None:
        if not isinstance(plan, AcceptedGenerationNamespaceBindingPlan):
            raise TypeError(
                "accepted_generation_namespace_binding_plan_required"
            )
        if plan.already_bound:
            return
        with self.__admission_fence:
            with self.__state_lock:
                if (
                    self.__active_namespace is None
                    and self.__namespace_generation
                    == plan.expected_namespace_generation
                ):
                    return
                if (
                    self.__active_namespace != plan.namespace
                    or self.__namespace_generation
                    != plan.expected_namespace_generation + 1
                    or self.__pending is not None
                    or self.__inflight_sequence > 0
                ):
                    raise RuntimeError(
                        "accepted_generation_namespace_rollback_state_mismatch"
                    )
            with self.__state_lock:
                self.__active_namespace = None
                self.__last_terminal_sequence = 0
                self.__last_accepted = None
                self.__compute_quarantine = None
                self.__last_quarantine = None
                self.__namespace_generation = (
                    plan.expected_namespace_generation
                )

    def offer(
        self,
        raw: RawModemGeneration,
        *,
        matrix_rtt: MatrixBoundRttObservation,
    ) -> GenerationOfferResult:
        if not isinstance(raw, RawModemGeneration):
            raise TypeError(
                "accepted generation authority only accepts RawModemGeneration"
            )
        if not isinstance(matrix_rtt, MatrixBoundRttObservation):
            raise TypeError("generation requires Matrix-bound RTT")
        if matrix_rtt.matrix_identity != raw.matrix_identity:
            raise ValueError("accepted_generation_offer_rtt_identity_mismatch")
        pending = _PendingGeneration(
            raw=raw,
            matrix_rtt=matrix_rtt,
        )
        sequence = raw.source_sequence
        with self.__state_lock:
            if self.__closed:
                return GenerationOfferResult(False, sequence, "authority_closed")
            active_namespace = self.__active_namespace
            if active_namespace is None:
                return GenerationOfferResult(
                    False, sequence, "runtime_namespace_unbound"
                )
            if raw.matrix_identity.namespace != active_namespace:
                self.__session_rejection_total += 1
                self.__last_session_reason = "foreign_namespace_rejected"
                return GenerationOfferResult(
                    False,
                    sequence,
                    "foreign_namespace_rejected",
                )
            highest_owned = max(
                self.__last_terminal_sequence,
                self.__inflight_sequence,
                self.__pending.raw.source_sequence if self.__pending is not None else 0,
            )
            if sequence <= highest_owned:
                self.__duplicate_total += 1
                return GenerationOfferResult(
                    False, sequence, "duplicate_or_out_of_order"
                )
            replaced = (
                self.__pending.raw.source_sequence if self.__pending is not None else 0
            )
            if replaced:
                self.__coalesced_total += 1
            self.__pending = pending
            reason = (
                "queued_while_compute_quarantined"
                if self.__compute_quarantine is not None
                else ("coalesced_latest" if replaced else "queued")
            )
            return GenerationOfferResult(
                True, sequence, reason, replaced_sequence=replaced
            )

    def adopt_source_epoch(
        self,
        *,
        session_scope_key: str,
        source_epoch: str,
        expected_current_source_epoch: str,
    ) -> None:
        """Forbidden: Product replacement owns namespace changes."""

        del (
            session_scope_key,
            source_epoch,
            expected_current_source_epoch,
        )
        raise RuntimeError(
            "accepted_generation_source_epoch_adoption_forbidden_use_runtime_namespace"
        )

    def adopt_session(
        self,
        session_scope_key: str,
        *,
        expected_current_session_scope_key: str,
    ) -> None:
        """Forbidden: Product replacement owns namespace changes."""

        del session_scope_key, expected_current_session_scope_key
        raise RuntimeError(
            "accepted_generation_session_adoption_forbidden_use_runtime_namespace"
        )

    def promote_session_scope_enrichment(
        self,
        session_scope_key: str,
        *,
        expected_current_session_scope_key: str,
        source_epoch: str,
    ) -> None:
        """Forbidden: Product replacement owns namespace changes."""

        del (
            session_scope_key,
            expected_current_session_scope_key,
            source_epoch,
        )
        raise RuntimeError(
            "accepted_generation_session_enrichment_forbidden_use_runtime_namespace"
        )

    def process_latest(
        self, *, accepted_at: Optional[float] = None
    ) -> GenerationProcessResult:
        with self.__admission_fence:
            return self._process_latest_under_fence(accepted_at=accepted_at)

    def _process_latest_under_fence(
        self,
        *,
        accepted_at: Optional[float],
    ) -> GenerationProcessResult:
        with self.__state_lock:
            if self.__closed:
                return GenerationProcessResult(False, "authority_closed", 0)
            if self.__compute_quarantine is not None:
                return GenerationProcessResult(
                    False,
                    "compute_authority_quarantined",
                    self.__compute_quarantine.raw.source_sequence,
                    quarantine=self.__compute_quarantine,
                )
            if self.__inflight_sequence > 0:
                return GenerationProcessResult(
                    False, "generation_inflight", self.__inflight_sequence
                )
            pending = self.__pending
            if pending is None:
                return GenerationProcessResult(
                    False,
                    "no_pending_generation",
                    self.__last_terminal_sequence,
                )
            self.__pending = None
            sequence = pending.raw.source_sequence
            active_namespace = self.__active_namespace
            if (
                active_namespace is None
                or pending.raw.matrix_identity.namespace != active_namespace
            ):
                return GenerationProcessResult(
                    False,
                    "foreign_namespace_rejected",
                    sequence,
                )
            namespace_generation = self.__namespace_generation
            previous_terminal = self.__last_terminal_sequence
            self.__inflight_sequence = sequence

        accepted_ts = float(time.time() if accepted_at is None else accepted_at)
        sequence_gap = (
            max(0, sequence - previous_terminal - 1) if previous_terminal else 0
        )
        transaction_state: Any = None
        update_started = False
        try:
            if accepted_ts < pending.raw.published_at:
                raise ValueError("accepted timestamp precedes raw publication")
            if accepted_ts < max(
                pending.matrix_rtt.observation.snapshot_at,
                pending.matrix_rtt.observation.observed_at,
                pending.matrix_rtt.observation.last_probe_at,
                pending.matrix_rtt.bound_at,
            ):
                raise ValueError("accepted timestamp precedes attached RTT evidence")
            admitted_input = _MatrixSignalInput(
                pending.raw.truth_matrix_reader(),
                source_sequence=sequence,
                accepted_at=accepted_ts,
            )
            with self.__signal_lock:
                transaction_state = self.__signal_computer.capture_transaction_state()
                update_started = True
                computed = self.__signal_computer.update(
                    admitted_input, pending.matrix_rtt.observation
                )
            if computed is None:
                raise RuntimeError("signal_compute_returned_none")
            accepted = AcceptedModemGeneration(
                raw=pending.raw,
                accepted_at=accepted_ts,
                computed=ImmutablePayload.capture(computed),
                matrix_rtt=pending.matrix_rtt,
            )
            persist_state = getattr(
                self.__signal_computer,
                "persist_durable_state",
                None,
            )
            if callable(persist_state):
                with self.__signal_lock:
                    persist_state()
        except Exception as exc:
            rollback_succeeded = not update_started
            rollback_error_type = ""
            rollback_error_message = ""
            if update_started:
                try:
                    with self.__signal_lock:
                        rollback_succeeded = bool(
                            self.__signal_computer.restore_transaction_state(
                                transaction_state
                            )
                        )
                    if not rollback_succeeded:
                        raise RuntimeError("signal_transaction_restore_returned_false")
                except Exception as rollback_exc:
                    rollback_succeeded = False
                    rollback_error_type = type(rollback_exc).__name__
                    rollback_error_message = str(rollback_exc)
            quarantined = QuarantinedModemGeneration(
                raw=pending.raw,
                quarantined_at=max(accepted_ts, pending.raw.published_at),
                reason=(
                    "signal_compute_failed_rolled_back"
                    if rollback_succeeded
                    else "signal_compute_rollback_failed"
                ),
                error_type=type(exc).__name__,
                error_message=str(exc),
                state_rollback_succeeded=rollback_succeeded,
                rollback_error_type=rollback_error_type,
                rollback_error_message=rollback_error_message,
            )
            with self.__state_lock:
                self.__last_terminal_sequence = sequence
                self.__inflight_sequence = 0
                self.__last_quarantine = quarantined
                if update_started and rollback_succeeded:
                    self.__rollback_total += 1
                    self.__retryable_compute_failure_total += 1
                elif not update_started:
                    self.__retryable_compute_failure_total += 1
                else:
                    self.__rollback_failure_total += 1
                    self.__compute_quarantine = quarantined
            return GenerationProcessResult(
                True,
                "quarantined",
                sequence,
                sequence_gap=sequence_gap,
                quarantine=quarantined,
            )

        with self.__state_lock:
            if (
                self.__closed
                or self.__active_namespace
                != pending.raw.matrix_identity.namespace
                or self.__namespace_generation != namespace_generation
            ):
                rollback_succeeded = False
                with self.__signal_lock:
                    rollback_succeeded = bool(
                        self.__signal_computer.restore_transaction_state(
                            transaction_state
                        )
                    )
                self.__inflight_sequence = 0
                if rollback_succeeded:
                    self.__rollback_total += 1
                else:
                    self.__rollback_failure_total += 1
                return GenerationProcessResult(
                    False,
                    (
                        "namespace_changed_during_compute_rolled_back"
                        if rollback_succeeded
                        else "namespace_changed_during_compute_rollback_failed"
                    ),
                    sequence,
                )
            self.__last_terminal_sequence = sequence
            self.__inflight_sequence = 0
            self.__last_accepted = accepted
        return GenerationProcessResult(
            True,
            "accepted",
            sequence,
            sequence_gap=sequence_gap,
            accepted=accepted,
        )

    def latest_accepted(self) -> AcceptedModemGeneration | None:
        with self.__state_lock:
            return self.__last_accepted

    def status(self) -> dict[str, Any]:
        with self.__state_lock:
            quarantine = self.__compute_quarantine
            return {
                "owner": self.owner,
                "closed": self.__closed,
                "pending_sequence": (
                    self.__pending.raw.source_sequence
                    if self.__pending is not None
                    else 0
                ),
                "inflight_sequence": self.__inflight_sequence,
                "last_terminal_sequence": self.__last_terminal_sequence,
                "last_accepted_sequence": (
                    self.__last_accepted.source_sequence
                    if self.__last_accepted is not None
                    else 0
                ),
                "compute_quarantined": quarantine is not None,
                "quarantined_sequence": quarantine.raw.source_sequence
                if quarantine is not None
                else 0,
                "last_quarantined_sequence": (
                    self.__last_quarantine.raw.source_sequence
                    if self.__last_quarantine is not None
                    else 0
                ),
                "last_quarantine_reason": (
                    self.__last_quarantine.reason
                    if self.__last_quarantine is not None
                    else ""
                ),
                "coalesced_total": self.__coalesced_total,
                "duplicate_total": self.__duplicate_total,
                "retryable_compute_failure_total": self.__retryable_compute_failure_total,
                "rollback_total": self.__rollback_total,
                "rollback_failure_total": self.__rollback_failure_total,
                "active_runtime_namespace": (
                    self.__active_namespace.as_dict()
                    if self.__active_namespace is not None
                    else {}
                ),
                "session_rejection_total": self.__session_rejection_total,
                "last_session_reason": self.__last_session_reason,
            }

    def close(self) -> GenerationAuthorityCloseReceipt:
        with self.__admission_fence:
            with self.__state_lock:
                closed_at = float(time.time())
                namespace = self.__active_namespace
                pending = self.__pending
                disposition = (
                    PendingGenerationDisposition(
                        identity=AcceptedGenerationIdentity(
                            pending.raw.matrix_identity
                        ),
                        disposed_at=max(
                            closed_at,
                            pending.raw.published_at,
                        ),
                        reason="authority_closed_before_compute",
                    )
                    if pending is not None
                    else None
                )
                self.__closed = True
                self.__pending = None
                self.__active_namespace = None
                self.__namespace_generation += 1
                return GenerationAuthorityCloseReceipt(
                    closed_at=closed_at,
                    namespace=namespace,
                    pending_disposition=disposition,
                    inflight_sequence=self.__inflight_sequence,
                )

    def termination_readiness(
        self,
    ) -> GenerationAuthorityTerminationReadiness:
        with self.__admission_fence:
            with self.__state_lock:
                pending = self.__pending
                return GenerationAuthorityTerminationReadiness(
                    namespace=self.__active_namespace,
                    pending_identity=(
                        AcceptedGenerationIdentity(
                            pending.raw.matrix_identity
                        )
                        if pending is not None
                        else None
                    ),
                    inflight_sequence=self.__inflight_sequence,
                    closed=self.__closed,
                )

    def _invoke_signal_method(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "update":
            raise AttributeError("SignalRuntimeFacade does not expose update")
        target = getattr(self.__signal_computer, method, None)
        if not callable(target):
            raise AttributeError(f"SignalComputer has no allowed method {method!r}")
        with self.__admission_fence:
            # Save health remains readable before namespace binding and after
            # close so Foundation can report startup/shutdown honestly.
            if method == "baseline_persistence_status":
                with self.__signal_lock:
                    return target()
            with self.__state_lock:
                if self.__closed:
                    raise RuntimeError("signal_processing_matrix_closed")
                if self.__active_namespace is None:
                    raise RuntimeError(
                        "accepted_generation_runtime_namespace_unbound"
                    )
            with self.__signal_lock:
                result = target(*args, **kwargs)
            return result


__all__ = [
    "AcceptedGenerationIdentity",
    "AcceptedModemGeneration",
    "SignalProcessingMatrix",
    "GenerationOfferResult",
    "GenerationProcessResult",
    "GenerationAuthorityCloseReceipt",
    "GenerationAuthorityTerminationReadiness",
    "ImmutablePayload",
    "QuarantinedModemGeneration",
    "PendingGenerationDisposition",
    "RawModemGeneration",
    "SignalRuntimeFacade",
]
