"""Store the current Modem Live Matrix generation atomically.

The translator registry prepares vendor data. This store builds one immutable
snapshot with its lines and diagnostics, validates it, and installs it in one
assignment. It does not own history or customer presentation.
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)

from .raw_observation import RawModemObservation
from .translator_registry import CanonicalModemTranslatorRegistry
from .truth_matrix import (
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
    RETENTION_CURRENT_NAMESPACE,
    build_truth_line,
    build_truth_matrix,
    build_truth_cell_retention_receipt,
    truth_value_fingerprint,
    ValidatedTruthMatrixReader,
)
from .snapshots import LiveModemRegisterSnapshot
from arch4.core_contracts.command_targets import CoreBandAutoTarget, CoreBandSetTarget, CorePBandTarget
from arch4.core_contracts.physical_commands import PhysicalCommandDispatchEvidence
from arch4.modem.lte_frequency import lte_earfcn_bounds


_MAX_UNKNOWN_TOKEN_RECEIPTS = 64
_CONFIGURED_SELECTION_CELLS = frozenset("radio." + field for field in (
    "selected_band_set", "selected_band_set_signature", "band_set", "band_set_signature",
    "configured_band_pool", "configured_band_pool_source", "configured_band_pool_signature",
    "configured_band_pool_status", "configured_band_pool_age_s", "configured_band_pool_last_sample_ts",
    "configured_ca_pool_display", "ca_signature", "ca_band_signature"))

# These identities describe one serving cell. Retention is legal only while
# that cell remains unchanged; LTE anchor and NR serving cell are independent.
_LTE_IDENTITY_CELLS = frozenset({"radio.serving_band", "rf.cell_id", "rf.pci", "rf.earfcn_dl"})
_NR_IDENTITY_CELLS = frozenset({"radio.nr_band", "radio.nr_pci", "radio.nr_arfcn"})
_LTE_CELL_FACTS = _LTE_IDENTITY_CELLS | frozenset({
    "rf.enb_id", "rf.earfcn_ul", "radio.serving_band_signature",
    "radio.serving_band_available", "radio.serving_band_unavailable_reason",
})
_NR_CELL_FACTS = _NR_IDENTITY_CELLS | frozenset({"radio.nr_cell_id"})
_SERVING_IDENTITY_GROUPS = ((_LTE_IDENTITY_CELLS, _LTE_CELL_FACTS), (_NR_IDENTITY_CELLS, _NR_CELL_FACTS))
_SERVING_IDENTITY_FACTS = _LTE_CELL_FACTS | _NR_CELL_FACTS


def _lte_identity_consistent(values: Mapping[str, Any]) -> bool:
    """Qualify supplied identity only; partial matching retention stays separate."""
    for path, limit in (("rf.pci", 504), ("rf.cell_id", 1 << 28)):
        value = values.get(path)
        if value is not None and (type(value) is not int or not 0 <= value < limit):
            return False
    band, channel = values.get("radio.serving_band"), values.get("rf.earfcn_dl")
    bounds = lte_earfcn_bounds(band) if type(band) is int else None
    return not (bounds is not None and type(channel) is int
                and not bounds[0] <= channel <= bounds[1])


@dataclass(frozen=True)
class LiveRegisterLine:
    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    sequence: int = 0
    source_sequence: int = 0
    source: str = ""
    changed: bool = True
    fingerprint: str = ""
    cell_evidence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cell_expectations: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(copy.deepcopy(dict(self.payload))),
        )
        object.__setattr__(
            self,
            "cell_evidence",
            MappingProxyType(copy.deepcopy(dict(self.cell_evidence))),
        )
        object.__setattr__(
            self,
            "cell_expectations",
            MappingProxyType(copy.deepcopy(dict(self.cell_expectations))),
        )

    def as_dict(
        self,
        *,
        current_namespace: RuntimeGenerationNamespace,
    ) -> dict[str, Any]:
        return build_truth_line(
            name=self.name,
            payload=self.payload,
            sampled_at=self.updated_at,
            line_sequence=self.sequence,
            source_sequence=self.source_sequence,
            source=self.source,
            changed=self.changed,
            cell_evidence=self.cell_evidence,
            current_namespace=current_namespace,
            cell_expectations=self.cell_expectations,
        )


@dataclass(frozen=True, slots=True)
class LiveRegisterCommittedGeneration:
    identity: MatrixGenerationIdentity
    matrix: ValidatedTruthMatrixReader | Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, MatrixGenerationIdentity):
            raise TypeError("live_register_committed_identity_required")
        reader = (
            self.matrix
            if isinstance(self.matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(self.matrix)
        )
        if reader.identity != self.identity:
            raise ValueError("live_register_committed_identity_mismatch")
        object.__setattr__(self, "matrix", reader)

    def matrix_copy(self) -> dict[str, Any]:
        matrix = self.matrix
        if not isinstance(matrix, ValidatedTruthMatrixReader):
            raise TypeError("live_register_committed_matrix_invalid")
        return matrix.to_mapping()

    def matrix_reader(self) -> ValidatedTruthMatrixReader:
        matrix = self.matrix
        if not isinstance(matrix, ValidatedTruthMatrixReader):
            raise TypeError("live_register_committed_matrix_invalid")
        return matrix


@dataclass(frozen=True, slots=True)
class LiveRegisterCurrentStatus:
    identity: MatrixGenerationIdentity | None
    sampled_at: float
    source: str
    available: bool

    def __post_init__(self) -> None:
        if self.identity is not None and not isinstance(
            self.identity,
            MatrixGenerationIdentity,
        ):
            raise TypeError("live_register_status_identity_invalid")
        if type(self.sampled_at) is not float or self.sampled_at < 0.0:
            raise TypeError("live_register_status_timestamp_invalid")
        if type(self.source) is not str:
            raise TypeError("live_register_status_source_invalid")
        if type(self.available) is not bool:
            raise TypeError("live_register_status_availability_invalid")
        if self.available != bool(
            self.identity is not None
            and self.sampled_at > 0.0
            and self.source
        ):
            raise ValueError("live_register_status_evidence_inconsistent")

    @property
    def sequence(self) -> int:
        return (
            self.identity.matrix_sequence
            if self.identity is not None
            else 0
        )


@dataclass(frozen=True, slots=True)
class LiveRegisterNamespaceBindingPlan:
    requested_namespace: RuntimeGenerationNamespace
    prior_namespace: RuntimeGenerationNamespace | None
    expected_sequence: int
    already_bound: bool

    def __post_init__(self) -> None:
        if not isinstance(
            self.requested_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("live_register_binding_plan_namespace_required")
        if self.prior_namespace is not None and not isinstance(
            self.prior_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "live_register_binding_plan_prior_namespace_invalid"
            )
        if type(self.expected_sequence) is not int or self.expected_sequence < 0:
            raise TypeError(
                "live_register_binding_plan_expected_sequence_invalid"
            )
        if type(self.already_bound) is not bool:
            raise TypeError(
                "live_register_binding_plan_already_bound_invalid"
            )
        if self.already_bound != (
            self.prior_namespace == self.requested_namespace
        ):
            raise ValueError(
                "live_register_binding_plan_disposition_inconsistent"
            )


@dataclass(frozen=True, slots=True)
class LiveRegisterNamespaceBindingReceipt:
    namespace: RuntimeGenerationNamespace
    prior_namespace: RuntimeGenerationNamespace | None
    committed: bool
    already_bound: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "live_register_binding_receipt_namespace_required"
            )
        if self.prior_namespace is not None and not isinstance(
            self.prior_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "live_register_binding_receipt_prior_namespace_invalid"
            )
        if type(self.committed) is not bool or type(self.already_bound) is not bool:
            raise TypeError(
                "live_register_binding_receipt_disposition_invalid"
            )
        if not self.committed:
            raise ValueError(
                "live_register_binding_receipt_commit_required"
            )
        if self.already_bound != (
            self.prior_namespace == self.namespace
        ):
            raise ValueError(
                "live_register_binding_receipt_disposition_inconsistent"
            )


@dataclass(frozen=True, slots=True)
class _LiveRegisterCommittedState:
    snapshot: LiveModemRegisterSnapshot
    lines: Mapping[str, LiveRegisterLine]
    unknown_alias_diagnostics: tuple[tuple[str, dict[str, Any]], ...]
    unknown_alias_sequence: int
    generation: LiveRegisterCommittedGeneration | None = None

    @classmethod
    def prepare(
        cls,
        *,
        snapshot: LiveModemRegisterSnapshot,
        lines: Mapping[str, LiveRegisterLine],
        unknown_alias_diagnostics: Mapping[str, Mapping[str, Any]],
        unknown_alias_sequence: int,
        generation: LiveRegisterCommittedGeneration | None = None,
    ) -> "_LiveRegisterCommittedState":
        if type(unknown_alias_sequence) is not int or unknown_alias_sequence < 0:
            raise TypeError("live_register_unknown_alias_sequence_invalid")
        exact_lines: dict[str, LiveRegisterLine] = {}
        for name, line in lines.items():
            if (
                type(name) is not str
                or not isinstance(line, LiveRegisterLine)
                or line.name != name
            ):
                raise TypeError("live_register_typed_line_required")
            exact_lines[name] = line
        return cls(
            snapshot=copy.deepcopy(snapshot),
            lines=MappingProxyType(exact_lines),
            unknown_alias_diagnostics=tuple(
                (key, copy.deepcopy(dict(value)))
                for key, value in unknown_alias_diagnostics.items()
            ),
            unknown_alias_sequence=unknown_alias_sequence,
            generation=generation,
        )

    def diagnostics_copy(self) -> OrderedDict[str, dict[str, Any]]:
        return OrderedDict(
            (key, copy.deepcopy(value))
            for key, value in self.unknown_alias_diagnostics
        )


@dataclass(slots=True)
class _LiveRegisterWorkingState:
    snapshot: LiveModemRegisterSnapshot
    lines: dict[str, LiveRegisterLine]
    unknown_alias_diagnostics: OrderedDict[str, dict[str, Any]]
    unknown_alias_sequence: int


class LiveRegisterStore:
    def __init__(
        self,
    ) -> None:
        self._translator = CanonicalModemTranslatorRegistry()
        self._lock = RLock()
        self._runtime_namespace: RuntimeGenerationNamespace | None = None
        self._configuration_write_cutoff: dict[str, float] = {}
        self._state = _LiveRegisterCommittedState.prepare(
            snapshot=LiveModemRegisterSnapshot(base_status={}),
            lines={},
            unknown_alias_diagnostics={},
            unknown_alias_sequence=0,
        )

    def note_configuration_write(self, stage: PhysicalCommandDispatchEvidence,
                                 *, namespace: RuntimeGenerationNamespace) -> None:
        """Invalidate pre-write configured selection, without inventing a read.

        This is a freshness fence inside the Matrix owner, not a second value
        store. Serving identity and every RF/traffic/scheduler cell are untouched.
        """
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError("configuration_write_physical_evidence_required")
        if not stage.permits_target_verification:
            return
        target = stage.target
        if not (isinstance(target, (CorePBandTarget, CoreBandSetTarget))
                or isinstance(target, CoreBandAutoTarget) and target.domain == "lte_allocated_auto"):
            return
        with self._lock:
            if namespace != self._runtime_namespace:
                raise RuntimeError("configuration_write_namespace_mismatch")
            for path in _CONFIGURED_SELECTION_CELLS:
                self._configuration_write_cutoff[path] = max(
                    self._configuration_write_cutoff.get(path, 0.), stage.request_dispatched_at)

    def replace_snapshot(self, snapshot: LiveModemRegisterSnapshot) -> None:
        del snapshot
        raise RuntimeError(
            "live_register_snapshot_replacement_forbidden_use_commit_observation"
        )

    def sequence(self) -> int:
        with self._lock:
            sequence = self._state.snapshot.sequence
            if type(sequence) is not int or sequence < 0:
                raise RuntimeError("live_register_snapshot_sequence_invalid")
            return sequence

    def snapshot_copy(self) -> LiveModemRegisterSnapshot:
        with self._lock:
            return copy.deepcopy(self._state.snapshot)

    def history(self) -> list[dict[str, Any]]:
        raise RuntimeError(
            "live_register_history_forbidden_use_accepted_root_transition"
        )

    def sample_history(self) -> list[dict[str, Any]]:
        raise RuntimeError(
            "live_register_sample_history_forbidden_use_accepted_root_outbox"
        )

    def sample_history_after(self, sequence: int) -> list[dict[str, Any]]:
        del sequence
        raise RuntimeError(
            "live_register_sample_history_forbidden_use_accepted_root_outbox"
        )

    def copy_parts(self) -> tuple[Any, dict[str, Any], list[dict[str, Any]], float, bool, int, str]:
        raise RuntimeError(
            "live_register_parts_forbidden_use_committed_matrix"
        )

    def copy_lines(self) -> dict[str, dict[str, Any]]:
        raise RuntimeError(
            "live_register_lines_forbidden_use_committed_matrix"
        )

    def _copy_lines_unlocked(self) -> dict[str, dict[str, Any]]:
        namespace = self._runtime_namespace
        if namespace is None:
            raise RuntimeError("live_register_runtime_namespace_unbound")
        return {
            name: line.as_dict(current_namespace=namespace)
            for name, line in sorted(self._state.lines.items())
        }

    def prepare_runtime_namespace_binding(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> LiveRegisterNamespaceBindingPlan:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("live_register_runtime_namespace_required")
        with self._lock:
            current = self._runtime_namespace
            sequence = self._state.snapshot.sequence
            if type(sequence) is not int or sequence < 0:
                raise RuntimeError(
                    "live_register_snapshot_sequence_invalid"
                )
            if current is not None and current != namespace:
                raise RuntimeError(
                    "live_register_runtime_namespace_rebind_forbidden"
                )
            if current is None and sequence != 0:
                raise RuntimeError(
                    "live_register_unbound_store_not_pristine"
                )
            return LiveRegisterNamespaceBindingPlan(
                requested_namespace=namespace,
                prior_namespace=current,
                expected_sequence=sequence,
                already_bound=current == namespace,
            )

    def commit_runtime_namespace_binding(
        self,
        plan: LiveRegisterNamespaceBindingPlan,
    ) -> LiveRegisterNamespaceBindingReceipt:
        if not isinstance(plan, LiveRegisterNamespaceBindingPlan):
            raise TypeError("live_register_binding_plan_required")
        with self._lock:
            if self._runtime_namespace != plan.prior_namespace:
                raise RuntimeError(
                    "live_register_binding_prior_namespace_changed"
                )
            if self._state.snapshot.sequence != plan.expected_sequence:
                raise RuntimeError(
                    "live_register_binding_sequence_changed"
                )
            self._runtime_namespace = plan.requested_namespace
            return LiveRegisterNamespaceBindingReceipt(
                namespace=plan.requested_namespace,
                prior_namespace=plan.prior_namespace,
                committed=True,
                already_bound=plan.already_bound,
            )

    def rollback_runtime_namespace_binding(
        self,
        plan: LiveRegisterNamespaceBindingPlan,
        receipt: LiveRegisterNamespaceBindingReceipt,
    ) -> None:
        if not isinstance(plan, LiveRegisterNamespaceBindingPlan) or not (
            isinstance(receipt, LiveRegisterNamespaceBindingReceipt)
        ):
            raise TypeError("live_register_binding_rollback_receipt_required")
        if receipt.namespace != plan.requested_namespace:
            raise ValueError(
                "live_register_binding_rollback_identity_mismatch"
            )
        with self._lock:
            if self._runtime_namespace != receipt.namespace:
                raise RuntimeError(
                    "live_register_binding_rollback_namespace_changed"
                )
            if self._state.snapshot.sequence != plan.expected_sequence:
                raise RuntimeError(
                    "live_register_binding_rollback_after_publication_forbidden"
                )
            self._runtime_namespace = plan.prior_namespace

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        del namespace
        raise RuntimeError(
            "live_register_direct_namespace_bind_forbidden_use_transaction"
        )

    @property
    def runtime_namespace(self) -> RuntimeGenerationNamespace:
        with self._lock:
            namespace = self._runtime_namespace
            if namespace is None:
                raise RuntimeError(
                    "live_register_runtime_namespace_unbound"
                )
            return namespace

    def truth_matrix(self) -> dict[str, Any]:
        raise RuntimeError(
            "live_register_matrix_copy_forbidden_use_committed_generation"
        )

    def _truth_matrix_unlocked(self) -> dict[str, Any]:
        return self._truth_matrix_for_state_unlocked(self._state)

    def _truth_matrix_for_state_unlocked(
        self,
        state: _LiveRegisterCommittedState,
    ) -> dict[str, Any]:
        namespace = self._runtime_namespace
        if namespace is None:
            raise RuntimeError("live_register_runtime_namespace_unbound")
        snap = state.snapshot
        return build_truth_matrix(
            source=snap.source,
            sampled_at=snap.updated_at,
            sequence=snap.sequence,
            source_epoch=namespace.source_epoch,
            session_scope_key=namespace.session_scope_key,
            lifecycle_generation=namespace.lifecycle_generation,
            lines={
                name: line.as_dict(current_namespace=namespace)
                for name, line in sorted(state.lines.items())
            },
        )

    def committed_generation(self) -> LiveRegisterCommittedGeneration:
        with self._lock:
            generation = self._state.generation
            if generation is None:
                raise RuntimeError("live_register_committed_generation_unavailable")
            return generation

    def current_status(self) -> LiveRegisterCurrentStatus:
        with self._lock:
            snapshot = self._state.snapshot
            if snapshot.sequence <= 0:
                return LiveRegisterCurrentStatus(
                    identity=None,
                    sampled_at=0.0,
                    source="",
                    available=False,
                )
            generation = self._state.generation
            if generation is None:
                raise RuntimeError("live_register_committed_generation_missing")
            reader = generation.matrix_reader()
            return LiveRegisterCurrentStatus(
                identity=generation.identity,
                sampled_at=float(reader.metadata("sampled_at")),
                source=str(reader.metadata("source")),
                available=True,
            )

    def has_fresh_sample(self, *, now: float, stale_s: float) -> bool:
        del now, stale_s
        raise RuntimeError(
            "live_register_freshness_forbidden_use_committed_matrix"
        )

    def publish(
        self,
        *,
        observation: RawModemObservation,
    ) -> LiveModemRegisterSnapshot:
        del observation
        raise RuntimeError(
            "live_register_publish_forbidden_use_commit_observation"
        )

    def publish_transaction(
        self,
        *,
        observation: RawModemObservation,
        commit: Callable[[LiveModemRegisterSnapshot], Any],
    ) -> Any:
        del observation, commit
        raise RuntimeError(
            "live_register_external_commit_callback_forbidden_use_commit_observation"
        )

    def commit_observation(
        self,
        *,
        observation: RawModemObservation,
    ) -> LiveRegisterCommittedGeneration:
        """Commit one observation atomically without caller side effects."""

        with self._lock:
            if not isinstance(observation, RawModemObservation):
                raise TypeError("raw_modem_observation_required")
            namespace = self._runtime_namespace
            if namespace is None:
                raise RuntimeError(
                    "live_register_runtime_namespace_unbound"
                )
            if observation.acquisition_namespace != namespace:
                raise RuntimeError(
                    "live_register_observation_namespace_mismatch"
                )
            current = self._state
            working = _LiveRegisterWorkingState(
                snapshot=current.snapshot,
                lines=dict(current.lines),
                unknown_alias_diagnostics=current.diagnostics_copy(),
                unknown_alias_sequence=current.unknown_alias_sequence,
            )
            self._publish_transaction_body(
                observation=observation,
                working=working,
            )
            candidate = _LiveRegisterCommittedState.prepare(
                snapshot=working.snapshot,
                lines=working.lines,
                unknown_alias_diagnostics=working.unknown_alias_diagnostics,
                unknown_alias_sequence=working.unknown_alias_sequence,
            )
            matrix = self._truth_matrix_for_state_unlocked(candidate)
            reader = ValidatedTruthMatrixReader.from_owner_built_matrix(matrix)
            committed = LiveRegisterCommittedGeneration(
                identity=reader.identity,
                matrix=reader,
            )
            candidate = replace(candidate, generation=committed)
            self._state = candidate
            return committed

    def _publish_transaction_body(
        self,
        *,
        observation: RawModemObservation,
        working: _LiveRegisterWorkingState,
    ) -> LiveModemRegisterSnapshot:
        """Commit one translated observation.

        The removed ``modem + base_status`` input could interpret data twice.
        Callers must provide one :class:`RawModemObservation` instead.
        """

        if not isinstance(observation, RawModemObservation):
            raise TypeError("raw_modem_observation_required")
        modem = observation.modem_copy()
        status = observation.status_copy()
        updated_at = observation.sampled_at
        source = observation.source
        previous = working.snapshot
        if type(previous.sequence) is not int or previous.sequence < 0:
            raise RuntimeError("live_register_snapshot_sequence_invalid")
        sequence = previous.sequence + 1
        working.snapshot = LiveModemRegisterSnapshot(
            modem=copy.deepcopy(modem),
            base_status=status,
            updated_at=updated_at,
            sequence=sequence,
            valid=True,
            last_error="",
            source=source,
            history=[],
        )
        touched_lines = frozenset(
            str(cell).partition(".")[0]
            for cell in observation.acquired_cells
            if str(cell).partition(".")[0]
            in MODEM_LIVE_TRUTH_FIELD_CATALOG
        )
        if self._configuration_write_cutoff:
            touched_lines = touched_lines | {"radio"}
        if previous.sequence == 0:
            touched_lines = frozenset(MODEM_LIVE_TRUTH_FIELD_CATALOG)
        if not touched_lines:
            raise RuntimeError(
                "live_register_observation_has_no_acquired_matrix_lines"
            )
        self._publish_register_lines(
            observation=observation,
            source_sequence=sequence,
            working=working,
            line_names=tuple(sorted(touched_lines)),
        )
        return working.snapshot

    def history_entry(self, snap: LiveModemRegisterSnapshot) -> Dict[str, Any]:
        del snap
        raise RuntimeError(
            "live_register_raw_history_interpretation_forbidden_use_matrix"
        )

    def canonical_history_entry(self) -> Dict[str, Any]:
        raise RuntimeError(
            "live_register_canonical_history_forbidden_use_radio_transition"
        )

    def sample_history_entry(self, snap: LiveModemRegisterSnapshot) -> Dict[str, Any]:
        del snap
        raise RuntimeError(
            "live_register_sample_history_forbidden_use_accepted_root_outbox"
        )

    def _publish_register_lines(
        self,
        *,
        observation: RawModemObservation,
        source_sequence: int,
        working: _LiveRegisterWorkingState,
        line_names: tuple[str, ...],
    ) -> None:
        updated_at = observation.sampled_at
        source = observation.source
        allowed = {str(name) for name in line_names}
        if not allowed:
            raise ValueError("live_register_publish_line_required")
        unknown_lines = allowed.difference(MODEM_LIVE_TRUTH_FIELD_CATALOG)
        if unknown_lines:
            raise ValueError(
                "live_register_publish_line_unknown:"
                + ",".join(sorted(unknown_lines))
            )
        payloads = {name: {} for name in allowed}
        if "identity" in allowed:
            payloads["identity"] = self._identity_line_payload(observation)
        if "capability" in allowed:
            payloads["capability"] = self._capability_line_payload(observation)
        translated = self._translator.translate(
            observation=observation,
            payloads=payloads,
            line_names=frozenset(allowed),
        )
        translated_payloads = {
            name: dict(payload)
            for name, payload in translated.lines.items()
        }
        translated_expectations: dict[str, dict[str, str]] = {
            name: {}
            for name in MODEM_LIVE_TRUTH_FIELD_CATALOG
        }
        for path, disposition in translated.cell_dispositions.items():
            line, separator, field = str(path).partition(".")
            if (
                not separator
                or field not in MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line, ())
            ):
                raise RuntimeError(
                    f"live_register_profile_matrix_address_invalid:{path}"
                )
            translated_expectations[line][field] = str(disposition)
        evidence = dict(translated.unknown_token_receipt)
        if evidence and "slow" in translated_payloads:
            slow = translated_payloads["slow"]
            slow["backend_diagnostic"] = (
                self._backend_diagnostic_with_unknown_alias_evidence(
                    existing=slow.get("backend_diagnostic"),
                    evidence=evidence,
                    observed_at=updated_at,
                    working=working,
                )
            )
        acquired_cells = set(observation.acquired_cells)
        # Optional lanes can finish before a newer read yet reach publication
        # afterwards. Only this Matrix owner decides whether their cells are
        # eligible to replace committed evidence; publication order is not read
        # order. Keep the existing value/age through the normal retention path.
        for cell_path in tuple(acquired_cells):
            line, _, field = cell_path.partition(".")
            previous_line = working.lines.get(line)
            prior = previous_line.cell_evidence.get(field) if previous_line else None
            if (cell_path in _SERVING_IDENTITY_FACTS
                    and prior and prior.get("acquisition_namespace") is None
                    and observation.acquisition_receipt_for_cell(cell_path).finished_at < previous_line.updated_at):
                # A cleared cell context is also a boundary for delayed reads.
                acquired_cells.remove(cell_path)
                continue
            if not prior or prior.get("acquisition_namespace") != observation.acquisition_namespace.as_dict():
                continue
            incoming = observation.acquisition_receipt_for_cell(cell_path)
            if (incoming.finished_at < float(prior["acquired_at"])
                    or incoming.request.attempt_id == prior["acquisition_attempt_id"]):
                acquired_cells.remove(cell_path)
        if "identity.imsi" in acquired_cells:
            acquired_cells.add("identity.sim_network")
        if ("radio.tx_power" in acquired_cells
                and "tx_power_support" in translated_payloads.get("radio", {})):
            acquired_cells.add("radio.tx_power_support")
        invalidated_identity_cells: set[str] = set()
        for path, cutoff in self._configuration_write_cutoff.items():
            incoming = (observation.acquisition_receipt_for_cell(path)
                        if path in acquired_cells else None)
            if incoming is not None and incoming.request.started_at >= cutoff:
                continue
            acquired_cells.discard(path)
            prior = working.lines.get("radio")
            evidence = prior.cell_evidence.get(path.partition(".")[2], {}) if prior else {}
            if float(evidence.get("acquired_at", 0.)) < cutoff:
                invalidated_identity_cells.add(path)
                translated_payloads.setdefault("radio", {})
        for identity_cells, group in _SERVING_IDENTITY_GROUPS:
            changed_receipts = []
            # NCI remains optional, but a native NCI change still identifies
            # a different NR cell even when PCI/channel happen to be reused.
            identity_scope = (identity_cells | {"radio.nr_cell_id"}
                              if identity_cells is _NR_IDENTITY_CELLS else identity_cells)
            observed_identity = identity_scope.intersection(acquired_cells)
            if not observed_identity:
                # Live measurements keep their independent acquisition cadence.
                # Only identity publication waits for the complete tuple.
                continue
            identity_attempts = {
                observation.acquisition_receipt_for_cell(path).request.attempt_id
                for path in observed_identity
            }
            identity_values = {
                path: translated_payloads.get(path.partition(".")[0], {}).get(path.partition(".")[2])
                for path in identity_scope
            }
            complete_values = all(
                type(value) is int and value >= 0
                and (value > 0 if path in {"radio.serving_band", "rf.cell_id", "radio.nr_band"} else True)
                for path, value in identity_values.items() if path in identity_cells
            )
            coherent_values = (identity_cells is not _LTE_IDENTITY_CELLS
                               or _lte_identity_consistent(identity_values))
            if (not identity_cells.issubset(acquired_cells)
                    or len(identity_attempts) != 1 or not complete_values or not coherent_values):
                # Raw partial/contradictory identity stays in its diagnostic.
                # Only a coherent tuple can become modem-neutral serving truth.
                acquired_cells.difference_update(group)
                previous_values = {
                    path: (working.lines[path.partition(".")[0]].payload.get(path.partition(".")[2])
                           if path.partition(".")[0] in working.lines else None)
                    for path in identity_scope
                }
                same_confirmed_cell = coherent_values and all(previous_values[path] is not None for path in identity_cells) and all(
                    identity_values[path] == previous_values[path] for path in observed_identity
                )
                if not same_confirmed_cell:
                    invalidated_identity_cells.update(group)
                for path in group:
                    translated_payloads.setdefault(path.partition(".")[0], {})
                continue
            for path in observed_identity:
                line, _, field = path.partition(".")
                prior = working.lines.get(line)
                incoming = translated_payloads.get(line, {}).get(field)
                if (prior is not None and field in prior.payload
                        and incoming is not None and incoming != ""
                        and incoming != prior.payload[field]):
                    changed_receipts.append(observation.acquisition_receipt_for_cell(path))
            if not changed_receipts:
                continue
            changed_at = max(receipt.finished_at for receipt in changed_receipts)
            for path in group:
                # A changed identity cannot inherit a missing field or a field
                # from a completed read preceding the new cell observation.
                if (path not in acquired_cells
                        or observation.acquisition_receipt_for_cell(path).finished_at < changed_at):
                    acquired_cells.discard(path)
                    invalidated_identity_cells.add(path)
                    line, _, _field = path.partition(".")
                    translated_payloads.setdefault(line, {})
        for name, payload in translated_payloads.items():
            payload = dict(payload or {})
            previous = working.lines.get(name)
            acquired_fields = frozenset(
                field
                for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[name]
                if f"{name}.{field}" in acquired_cells
            )
            invalidated_fields = frozenset(
                field for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[name]
                if f"{name}.{field}" in invalidated_identity_cells
            )
            if previous is not None and not acquired_fields and not invalidated_fields:
                # This line has no physical read in this generation. Keep its
                # previous metadata and cell evidence. Replacing them would
                # falsely report fresh data.
                continue
            if name == "traffic":
                payload = self._normalize_traffic_payload(payload)
            previous_payload = (
                dict(previous.payload)
                if previous is not None
                else {}
            )
            for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[name]:
                cell_path = f"{name}.{field}"
                if cell_path in acquired_cells:
                    continue
                if field in invalidated_fields:
                    payload.pop(field, None)
                elif field in previous_payload:
                    payload[field] = copy.deepcopy(
                        previous_payload[field]
                    )
                else:
                    payload.pop(field, None)
            fingerprint = truth_value_fingerprint(payload)
            changed = previous is None or str(previous.fingerprint or "") != fingerprint
            sequence = int(getattr(previous, "sequence", 0) or 0) + 1
            cell_evidence: dict[str, dict[str, Any]] = {}
            previous_evidence = (
                dict(previous.cell_evidence)
                if previous is not None
                else {}
            )
            cell_expectations = (
                dict(previous.cell_expectations)
                if previous is not None
                else {}
            )
            cell_expectations.update(
                translated_expectations.get(name, {})
            )
            for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[name]:
                cell_path = f"{name}.{field}"
                physically_acquired = (
                    cell_path in acquired_cells
                )
                if not physically_acquired and field in previous_evidence and field not in invalidated_fields:
                    retained = copy.deepcopy(previous_evidence[field])
                    origin_raw = retained.get("acquisition_namespace")
                    if origin_raw is None:
                        cell_evidence[field] = retained
                        continue
                    origin = RuntimeGenerationNamespace.from_mapping(
                        origin_raw
                    )
                    if origin == observation.acquisition_namespace:
                        disposition = RETENTION_CURRENT_NAMESPACE
                        device_key = ""
                    else:
                        raise RuntimeError(
                            "live_register_cross_scope_retention_unproven"
                        )
                    retained["retention"] = build_truth_cell_retention_receipt(
                        disposition=disposition,
                        origin_namespace=origin,
                        destination_namespace=(
                            observation.acquisition_namespace
                        ),
                        retained_at=updated_at,
                        device_identity_key=device_key,
                    )
                    cell_evidence[field] = retained
                elif not physically_acquired:
                    cell_evidence[field] = {
                        "acquired_at": 0.0,
                        "acquisition_attempt_id": "",
                        "source_lane": "",
                        "source_sequence": 0,
                        "acquisition_namespace": None,
                        "retention": None,
                    }
                else:
                    acquisition_receipt = (
                        observation.acquisition_receipt_for_cell(
                            "identity.imsi" if cell_path == "identity.sim_network"
                            else "radio.tx_power" if cell_path == "radio.tx_power_support"
                            else cell_path
                        )
                    )
                    cell_evidence[field] = {
                        "acquired_at": (
                            acquisition_receipt.finished_at
                        ),
                        "acquisition_attempt_id": (
                            acquisition_receipt.request.attempt_id
                        ),
                        "source_lane": (
                            acquisition_receipt.request.lane
                        ),
                        "source_sequence": source_sequence,
                        "acquisition_namespace": (
                            observation.acquisition_namespace.as_dict()
                        ),
                        "retention": None,
                    }
            working.lines[name] = LiveRegisterLine(
                name=name,
                payload=payload,
                updated_at=updated_at,
                sequence=sequence,
                source_sequence=source_sequence,
                source=source,
                changed=bool(changed),
                fingerprint=fingerprint,
                cell_evidence=cell_evidence,
                cell_expectations=cell_expectations,
            )

    def _radio_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        payload = self._translator.observed_fields(
            observation,
            (
                "network_type",
                "network_type_ex",
                "cell_locked",
                "carrier_count",
                "carrier_details",
                "carrier_rows",
                "configured_ca_pool_display",
                "ca_active",
                "ca_signature",
                "ca_band_signature",
                "ca_state_text",
                "ca_source",
                "ca_component_band_set",
                "ca_component_band_signature",
                "ca_component_count",
                "ca_composition_signature",
                "tx_power",
                "tx_power_dbm",
                "nsa_active",
                "sa_active",
                "nr5g_active",
                "nr_band",
                "nr_pci",
                "nr_arfcn",
                "nr_cell_id",
                "nr_sinr_db",
                "nr_rsrp_dbm",
                "nr_rsrq_db",
                "nr_mode",
                "nr_state_text",
            ),
        )
        return payload

    def _rf_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        payload = self._translator.observed_fields(
            observation,
            (
                "sinr_db",
                "rsrp_dbm",
                "rsrq_db",
                "rssi_dbm",
                "pci",
                "earfcn_dl",
                "cell_id",
                "enb_id",
                "cqi",
                "cqi0",
                "cqi1",
                "mcs_dl",
                "mcs_dl0",
                "mcs_dl1",
                "mcs_ul",
                "ri",
                "ri_valid",
                "ri_source",
                "qam_dl0",
                "qam_dl1",
                "qam_ul",
                "scheduler_zero_authoritative",
                "scheduler_stream1_available",
                "scheduler_source",
                "cqi_source",
            ),
        )
        return payload

    def _slow_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        payload = self._translator.observed_fields(
            observation,
            (
                "rrc",
                "trans_mode",
                "tac",
                "dl_freq_mhz",
                "ul_freq_mhz",
                "dl_bandwidth_mhz",
                "ul_bandwidth_mhz",
                "neighbors",
                "neighbor_count",
                "nr_bandwidth_mhz",
                "nr_scs_khz",
                "nr_cells",
                "temperature_modem_5g_c",
                "antenna",
                "wan_ip",
                "wifi",
                "wifi_enabled",
                "wifi_24ghz_enabled",
                "wifi_5ghz_enabled",
                "wifi_6ghz_enabled",
                "backend_diagnostic",
                "network_settings",
                "mobile_data_enabled",
                "radio_mode",
            ),
        )
        if "neighbors" in payload and "neighbor_count" not in payload:
            neighbors = payload["neighbors"]
            if isinstance(neighbors, (list, tuple)):
                payload["neighbor_count"] = len(neighbors)
        return payload

    def _backend_diagnostic_with_unknown_alias_evidence(
        self,
        *,
        existing: Any,
        evidence: Mapping[str, Any],
        observed_at: float,
        working: _LiveRegisterWorkingState,
    ) -> dict[str, Any]:
        """Return one backend diagnostic payload with alias evidence.

        Foundation reads ``backend_diagnostic`` as ``ModemBackendDiagnostic.v1``.
        Unknown aliases are diagnostic data. They must not replace the typed
        diagnostic root with another schema.
        """

        evidence_payload = dict(evidence or {})
        if not evidence_payload:
            return dict(existing) if isinstance(existing, Mapping) else {}

        if (
            isinstance(existing, Mapping)
            and str(existing.get("schema") or "") == "ModemBackendDiagnostic.v1"
        ):
            diagnostic = dict(existing)
            data = (
                dict(diagnostic.get("data"))
                if isinstance(diagnostic.get("data"), Mapping)
                else {}
            )
            data["unknown_alias_evidence"] = evidence_payload
            diagnostic["data"] = data
            return diagnostic

        fingerprint = truth_value_fingerprint(evidence_payload)
        cached = working.unknown_alias_diagnostics.get(fingerprint)
        if cached:
            return copy.deepcopy(cached)

        working.unknown_alias_sequence += 1
        sequence = working.unknown_alias_sequence
        owner = "54guc.modem_live.canonical_aliases"
        diagnostic = {
            "schema": "ModemBackendDiagnostic.v1",
            "owner": owner,
            "sequence": sequence,
            "observed_at": float(observed_at or 0.0),
            "kind": "modem_live_unknown_alias_evidence",
            "backend": str(evidence_payload.get("backend") or "unknown"),
            "severity": "INFO",
            "diagnostic_id": f"{owner}:{sequence}",
            "data": {
                "unknown_alias_evidence": evidence_payload,
            },
        }
        working.unknown_alias_diagnostics[fingerprint] = copy.deepcopy(
            diagnostic
        )
        working.unknown_alias_diagnostics.move_to_end(fingerprint)
        while (
            len(working.unknown_alias_diagnostics)
            > _MAX_UNKNOWN_TOKEN_RECEIPTS
        ):
            working.unknown_alias_diagnostics.popitem(last=False)
        return diagnostic

    def _timing_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        return self._translator.observed_fields(
            observation,
            (
                "modem_comm_alive",
                "modem_sample_interval_s",
                "modem_sample_expected_interval_s",
                "modem_sample_slo_exceeded",
                "modem_read_ms",
            ),
        )

    def _traffic_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        return self._translator.observed_fields(
            observation,
            (
                "throughput_dl_mbps",
                "throughput_ul_mbps",
                "dl_mbps",
                "ul_mbps",
                "rx_bytes_per_sec",
                "tx_bytes_per_sec",
                "has_traffic",
                "traffic_active",
                "traffic_valid",
                "traffic_state_detail",
                "traffic_invalid_reason",
                "packet_session_connected",
                "connection_status_code",
                "session_uptime_s",
                "session_uptime_source",
                "traffic_presence_active",
                "traffic_presence_interval_s",
                "traffic_current_download_rate_bps",
                "traffic_current_upload_rate_bps",
                "traffic_rate_source",
                "traffic_day_download_bytes",
                "traffic_day_upload_bytes",
                "traffic_day_used_bytes",
                "traffic_month_download_bytes",
                "traffic_month_upload_bytes",
                "traffic_total_download_bytes",
                "traffic_total_upload_bytes",
                "traffic_session_download_bytes",
                "traffic_session_upload_bytes",
                "traffic_device_download_counter_bytes",
                "traffic_device_upload_counter_bytes",
                "traffic_counter_period_started_at",
                "traffic_counter_interface_started_at",
                "traffic_accounting_source",
                "traffic_accounting_status",
                "traffic_accounting_month_key",
                "traffic_accounting_day_key",
                "traffic_session_duration_s",
                "traffic_day_duration_s",
                "traffic_month_duration_s",
                "traffic_total_duration_s",
                "traffic_volume_poll_interval_s",
                "traffic_volume_last_sample_ts",
                "traffic_volume_next_sample_ts",
                "traffic_volume_age_s",
                "traffic_quota_start_day",
                "traffic_quota_limit_label",
                "traffic_quota_left_bytes",
                "traffic_quota_limit_bytes",
                "traffic_quota_month_threshold_pct",
                "traffic_quota_day_threshold_pct",
            ),
        )

    @staticmethod
    def _normalize_traffic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(payload or {})
        if not _present(out.get("dl_mbps")) and _present(out.get("throughput_dl_mbps")):
            out["dl_mbps"] = copy.deepcopy(out.get("throughput_dl_mbps"))
        if not _present(out.get("ul_mbps")) and _present(out.get("throughput_ul_mbps")):
            out["ul_mbps"] = copy.deepcopy(out.get("throughput_ul_mbps"))
        return out

    def _health_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        return self._translator.observed_fields(
            observation,
            (
                "radio_connected",
                "radio_state_text",
                "stale",
                "modem_read_missed",
                "modem_live_io_quiet_active",
                "modem_live_io_quiet_remaining_s",
                "live_register_quiet_hold",
                "live_register_hold_reason",
                "live_register_stale",
                "target_guard_ok",
                "target_guard_reason",
                "backend_session",
                "lte_interface_disabled",
                "device_recovery_state",
                "session_transition",
                "connectivity_receipt",
                "startup_live_baseline",
                "acquisition_lane_state",
            ),
        )

    def _identity_line_payload(self, observation: RawModemObservation) -> Dict[str, Any]:
        # Identity translation already published catalog, Access Type, and
        # identity together in ``base_status``. This line copies that result.
        # Do not mix raw ``ModemInput`` fields back into it. Keep meaningful
        # empty values such as a provisional Access Type fingerprint.
        source = observation.status
        fields = (
                "decode_catalog_snapshot",
                "access_type_contract",
                "modem_identity_key",
                "identity_publication_order",
                "schema",
                "owner",
                "code_namespace",
                "identity_code",
                "identity_status",
                "catalog_version",
                "catalog_checksum",
                "combination_guard",
                "access_type_id",
                "access_type_fingerprint",
                "modem_live_access_profile",
                "manufacturer",
                "hardware_sku",
                "firmware_version",
                "backend",
                "backend_family",
                "vendor",
                "model",
                "router_model",
                "firmware",
                "serial",
                "serial_number",
                "imei",
                "imsi",
                "profile_id",
                "hardware_version",
                "hw_version",
                "software_version",
                "software",
                "webui",
                "access_type",
                "lan_mac",
                "provider_name",
                "provider_plmn",
                "runtime_identity",
                "resolved_identity_card",
                "capability_profile",
                "static_modem_truth",
            )
        return {
            key: copy.deepcopy(source[key])
            for key in fields
            if key in source
        }

    @staticmethod
    def _capability_line_payload(
        observation: RawModemObservation,
    ) -> Dict[str, Any]:
        """Store capability data already normalized by Modem Live."""

        source = observation.status
        return {
            field: copy.deepcopy(source[field])
            for field in MODEM_LIVE_TRUTH_FIELD_CATALOG["capability"]
            if field in source
        }

def _present(value: Any) -> bool:
    return value is not None and value != "" and value != []
