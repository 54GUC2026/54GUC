"""Immutable notification sent after a Modem Live register commit."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.rtt_observation import MatrixBoundRttObservation

from .truth_matrix import ValidatedTruthMatrixReader


class ImmutableModemLiveMatrix:
    """Validated generation record with copy-out access."""

    __slots__ = ("__value",)

    def __init__(
        self,
        value: ValidatedTruthMatrixReader | Mapping[str, Any],
    ) -> None:
        reader = (
            value
            if isinstance(value, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(value)
        )
        object.__setattr__(self, "_ImmutableModemLiveMatrix__value", reader)

    @classmethod
    def capture(
        cls,
        value: "ImmutableModemLiveMatrix | Mapping[str, Any]",
    ) -> "ImmutableModemLiveMatrix":
        if isinstance(value, cls):
            return value
        if not isinstance(value, (ValidatedTruthMatrixReader, Mapping)):
            raise TypeError("Modem Live publication requires an exact Matrix mapping")
        return cls(value)

    def copy(self) -> dict[str, Any]:
        return self.__value.to_mapping()

    def reader(self) -> ValidatedTruthMatrixReader:
        return self.__value

    def __copy__(self) -> "ImmutableModemLiveMatrix":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "ImmutableModemLiveMatrix":
        del memo
        return self


@dataclass(frozen=True, slots=True)
class ModemLivePublicationNotice:
    matrix_identity: MatrixGenerationIdentity
    matrix_rtt: MatrixBoundRttObservation
    sampled_at: float
    published_at: float
    source: str
    truth_matrix: ImmutableModemLiveMatrix | Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "Modem Live publication Matrix identity is required"
            )
        if not isinstance(self.matrix_rtt, MatrixBoundRttObservation):
            raise TypeError(
                "Modem Live publication Matrix-bound RTT is required"
            )
        if self.matrix_rtt.matrix_identity != self.matrix_identity:
            raise ValueError(
                "Modem Live publication RTT identity mismatch"
            )
        if (
            type(self.sampled_at) is not float
            or type(self.published_at) is not float
            or not math.isfinite(self.sampled_at)
            or not math.isfinite(self.published_at)
            or self.sampled_at <= 0.0
            or self.published_at <= 0.0
        ):
            raise ValueError("Modem Live publication timestamps must be positive")
        if self.published_at < self.sampled_at:
            raise ValueError("Modem Live publication cannot precede its sample")
        if (
            type(self.source) is not str
            or not self.source
            or self.source != self.source.strip()
        ):
            raise ValueError("Modem Live publication source is required")
        payload = ImmutableModemLiveMatrix.capture(self.truth_matrix)
        matrix = payload.reader()
        if matrix.identity != self.matrix_identity:
            raise ValueError("Modem Live publication Matrix identity mismatch")
        if matrix.metadata("sampled_at") != self.sampled_at:
            raise ValueError("Modem Live publication Matrix timestamp mismatch")
        if matrix.metadata("source") != self.source:
            raise ValueError("Modem Live publication Matrix source mismatch")
        object.__setattr__(self, "truth_matrix", payload)

    @property
    def changed_lines(self) -> tuple[str, ...]:
        matrix = self.truth_matrix_reader()
        return tuple(
            sorted(
                line
                for line, row in dict(matrix.to_mapping().get("lines") or {}).items()
                if dict(row).get("changed") is True
            )
        )

    @property
    def sequence(self) -> int:
        return self.matrix_identity.matrix_sequence

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def lifecycle_generation(self) -> int:
        return self.matrix_identity.lifecycle_generation

    @property
    def matrix_revision(self) -> str:
        return self.matrix_identity.matrix_revision

    def truth_matrix_copy(self) -> dict[str, Any]:
        payload = self.truth_matrix
        if not isinstance(payload, ImmutableModemLiveMatrix):
            raise TypeError("Modem Live publication Matrix payload is invalid")
        return payload.copy()

    def truth_matrix_reader(self) -> ValidatedTruthMatrixReader:
        payload = self.truth_matrix
        if not isinstance(payload, ImmutableModemLiveMatrix):
            raise TypeError("Modem Live publication Matrix payload is invalid")
        return payload.reader()


@dataclass(frozen=True, slots=True)
class ModemLivePublicationAdmissionReceipt:
    matrix_identity: MatrixGenerationIdentity
    disposition: str
    accepted_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "modem_live_publication_admission_identity_required"
            )
        if self.disposition not in {
            "retained",
            "already_retained",
            "already_consumed",
        }:
            raise ValueError(
                "modem_live_publication_admission_disposition_invalid"
            )
        if (
            type(self.accepted_at) is not float
            or not math.isfinite(self.accepted_at)
            or self.accepted_at <= 0.0
        ):
            raise ValueError(
                "modem_live_publication_admission_timestamp_invalid"
            )


@dataclass(frozen=True, slots=True)
class ModemLivePublicationSinkDetachReceipt:
    namespace: RuntimeGenerationNamespace | None
    inflight_callbacks: int
    retained_notice_identity: MatrixGenerationIdentity | None
    detached: bool
    finished_at: float

    def __post_init__(self) -> None:
        if self.namespace is not None and not isinstance(
            self.namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "modem_live_publication_detach_namespace_invalid"
            )
        if (
            type(self.inflight_callbacks) is not int
            or self.inflight_callbacks < 0
        ):
            raise ValueError(
                "modem_live_publication_detach_inflight_invalid"
            )
        if (
            self.retained_notice_identity is not None
            and not isinstance(
                self.retained_notice_identity,
                MatrixGenerationIdentity,
            )
        ):
            raise TypeError(
                "modem_live_publication_detach_retained_identity_invalid"
            )
        expected_detached = bool(
            self.inflight_callbacks == 0
            and self.retained_notice_identity is None
        )
        if type(self.detached) is not bool or self.detached != expected_detached:
            raise ValueError(
                "modem_live_publication_detach_disposition_invalid"
            )
        if (
            type(self.finished_at) is not float
            or not math.isfinite(self.finished_at)
            or self.finished_at <= 0.0
        ):
            raise ValueError(
                "modem_live_publication_detach_timestamp_invalid"
            )


ModemLivePublicationSink = Callable[
    [ModemLivePublicationNotice],
    ModemLivePublicationAdmissionReceipt,
]


@dataclass(frozen=True, slots=True)
class ModemLiveRuntimeSupervisionSchedule:
    enabled: bool
    cadence_cycles: int
    budget_ms: float

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("modem_live_supervision_enabled_bool_required")
        if type(self.cadence_cycles) is not int or self.cadence_cycles <= 0:
            raise ValueError("modem_live_supervision_cadence_invalid")
        if (
            type(self.budget_ms) is not float
            or not math.isfinite(self.budget_ms)
            or self.budget_ms <= 0.0
        ):
            raise ValueError("modem_live_supervision_budget_invalid")


@dataclass(frozen=True, slots=True)
class ModemLiveRuntimeSupervisionReceipt:
    namespace: RuntimeGenerationNamespace
    observed_at: float
    accepted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("modem_live_supervision_namespace_required")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("modem_live_supervision_timestamp_invalid")
        if self.accepted is not True:
            raise ValueError("modem_live_supervision_acceptance_required")


class ModemLiveRuntimeSupervisionPort:
    """Typed Core boundary implemented by Modem Live."""

    __slots__ = ("_schedule_reader", "_service")

    def __init__(
        self,
        schedule_reader: Callable[[], ModemLiveRuntimeSupervisionSchedule],
        service: Callable[[], ModemLiveRuntimeSupervisionReceipt],
    ) -> None:
        if not callable(schedule_reader) or not callable(service):
            raise TypeError("modem_live_supervision_port_callable_required")
        self._schedule_reader = schedule_reader
        self._service = service

    def schedule(self) -> ModemLiveRuntimeSupervisionSchedule:
        schedule = self._schedule_reader()
        if not isinstance(schedule, ModemLiveRuntimeSupervisionSchedule):
            raise TypeError("modem_live_supervision_schedule_receipt_required")
        return schedule

    def service_once(self) -> ModemLiveRuntimeSupervisionReceipt:
        receipt = self._service()
        if not isinstance(receipt, ModemLiveRuntimeSupervisionReceipt):
            raise TypeError("modem_live_supervision_service_receipt_required")
        return receipt


def require_modem_live_runtime_supervision_port(
    owner: Any,
) -> ModemLiveRuntimeSupervisionPort:
    port = getattr(owner, "runtime_supervision_port", None)
    if not isinstance(port, ModemLiveRuntimeSupervisionPort):
        raise RuntimeError("modem_live_runtime_supervision_port_unavailable")
    return port


__all__ = [
    "ImmutableModemLiveMatrix",
    "ModemLivePublicationAdmissionReceipt",
    "ModemLivePublicationNotice",
    "ModemLivePublicationSink",
    "ModemLivePublicationSinkDetachReceipt",
    "ModemLiveRuntimeSupervisionPort",
    "ModemLiveRuntimeSupervisionReceipt",
    "ModemLiveRuntimeSupervisionSchedule",
    "require_modem_live_runtime_supervision_port",
]
