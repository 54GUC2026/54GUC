"""Typed termination evidence for the NGUI-owned Product runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Any


class RuntimeTerminationParticipant(StrEnum):
    MODEM_SAMPLE_PUBLICATION = "modem_sample_publication"
    RUNTIME_CLOCK = "runtime_clock"
    RUNTIME_FRAME = "runtime_frame"


class RuntimeParticipantAbsenceDisposition(StrEnum):
    NOT_CREATED = "not_created"
    BLOCKED_BY_PREDECESSOR = "blocked_by_predecessor"


class RuntimeTerminationBlocker(StrEnum):
    NONE = "none"
    REQUIRED_WORKERS = "required_workers"
    RUNTIME_FRAME = "runtime_frame"
    MODEM_SAMPLE_PUBLICATION = "modem_sample_publication"
    RUNTIME_CLOCK = "runtime_clock"


@dataclass(frozen=True, slots=True)
class RuntimeParticipantStartReceipt:
    """Exact Product-owned runtime child admission evidence."""

    participant: RuntimeTerminationParticipant
    lifecycle_generation: int
    started_at: float

    def __post_init__(self) -> None:
        if not isinstance(
            self.participant,
            RuntimeTerminationParticipant,
        ):
            raise ValueError(
                "runtime_participant_start_product_owner_invalid"
            )
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError(
                "runtime_participant_start_generation_invalid"
            )
        if (
            type(self.started_at) is not float
            or not math.isfinite(self.started_at)
            or self.started_at <= 0.0
        ):
            raise ValueError(
                "runtime_participant_start_timestamp_invalid"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeParticipantStartReceipt.v1",
            "owner": "54guc.product_supervisor",
            "participant": self.participant.value,
            "lifecycle_generation": self.lifecycle_generation,
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class RuntimeParticipantAbsenceReceipt:
    """Product-issued proof that one runtime participant has no owner receipt."""

    participant: RuntimeTerminationParticipant
    disposition: RuntimeParticipantAbsenceDisposition
    blocker: RuntimeTerminationBlocker
    lifecycle_generation: int
    observed_at: float
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(
            self.participant,
            RuntimeTerminationParticipant,
        ):
            raise TypeError(
                "runtime_termination_absence_participant_required"
            )
        if not isinstance(
            self.disposition,
            RuntimeParticipantAbsenceDisposition,
        ):
            raise TypeError(
                "runtime_termination_absence_disposition_required"
            )
        if not isinstance(self.blocker, RuntimeTerminationBlocker):
            raise TypeError(
                "runtime_termination_absence_blocker_required"
            )
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError(
                "runtime_termination_absence_generation_invalid"
            )
        if (
            self.disposition
            is RuntimeParticipantAbsenceDisposition.NOT_CREATED
            and self.blocker is not RuntimeTerminationBlocker.NONE
        ):
            raise ValueError(
                "runtime_termination_not_created_blocker_forbidden"
            )
        if (
            self.disposition
            is RuntimeParticipantAbsenceDisposition.BLOCKED_BY_PREDECESSOR
            and self.blocker is RuntimeTerminationBlocker.NONE
        ):
            raise ValueError(
                "runtime_termination_blocked_predecessor_required"
            )
        allowed_blockers = {
            RuntimeTerminationParticipant.MODEM_SAMPLE_PUBLICATION: {
                RuntimeTerminationBlocker.REQUIRED_WORKERS,
                RuntimeTerminationBlocker.RUNTIME_FRAME,
            },
            RuntimeTerminationParticipant.RUNTIME_CLOCK: {
                RuntimeTerminationBlocker.REQUIRED_WORKERS,
                RuntimeTerminationBlocker.RUNTIME_FRAME,
                RuntimeTerminationBlocker.MODEM_SAMPLE_PUBLICATION,
            },
            RuntimeTerminationParticipant.RUNTIME_FRAME: {
                RuntimeTerminationBlocker.REQUIRED_WORKERS,
            },
        }
        if (
            self.disposition
            is RuntimeParticipantAbsenceDisposition.BLOCKED_BY_PREDECESSOR
            and self.blocker not in allowed_blockers[self.participant]
        ):
            raise ValueError(
                "runtime_termination_blocker_not_predecessor"
            )
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError(
                "runtime_termination_absence_timestamp_invalid"
            )

    @property
    def terminated(self) -> bool:
        return self.disposition is (
            RuntimeParticipantAbsenceDisposition.NOT_CREATED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeParticipantAbsenceReceipt.v2",
            "owner": "54guc.product_supervisor",
            "issuer": "ProductNGUIRuntimePort",
            "participant": self.participant.value,
            "disposition": self.disposition.value,
            "blocker": self.blocker.value,
            "lifecycle_generation": self.lifecycle_generation,
            "observed_at": self.observed_at,
            "terminated": self.terminated,
        }


@dataclass(frozen=True, slots=True)
class RequiredRuntimeWorkerTerminationReceipt:
    lifecycle_generation: int
    expected_workers: tuple[str, ...]
    observed_workers: tuple[str, ...]
    survivors: tuple[str, ...]
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError(
                "required_runtime_worker_lifecycle_generation_invalid"
            )
        for field_name in (
            "expected_workers",
            "observed_workers",
            "survivors",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, tuple)
                or any(
                    type(item) is not str
                    or not item
                    or item != item.strip()
                    for item in value
                )
            ):
                raise TypeError(
                    f"required_runtime_worker_{field_name}_tuple_required"
                )
            if len(value) != len(set(value)):
                raise ValueError(
                    f"required_runtime_worker_{field_name}_duplicate"
                )
        if (
            not self.expected_workers
            or set(self.observed_workers) - set(self.expected_workers)
            or set(self.survivors) - set(self.observed_workers)
        ):
            raise ValueError(
                "required_runtime_worker_denominator_invalid"
            )

    @property
    def terminated(self) -> bool:
        return bool(
            set(self.observed_workers) == set(self.expected_workers)
            and not self.survivors
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "RequiredRuntimeWorkerTerminationReceipt.v1",
            "owner": "54guc.ngui.runtime_worker",
            "lifecycle_generation": self.lifecycle_generation,
            "expected_workers": list(self.expected_workers),
            "observed_workers": list(self.observed_workers),
            "terminated": self.terminated,
            "survivors": list(self.survivors),
        }


@dataclass(frozen=True, slots=True)
class RuntimeFramePublisherTerminationReceipt:
    thread_alive: bool
    pending_cancelled: bool
    inflight_cancelled: bool

    def __post_init__(self) -> None:
        for field_name in (
            "thread_alive",
            "pending_cancelled",
            "inflight_cancelled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"runtime_frame_publisher_{field_name}_bool_required"
                )

    @property
    def terminated(self) -> bool:
        return not self.thread_alive

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeFramePublisherTerminationReceipt.v1",
            "owner": "ngui_runtime_frame_publisher",
            "terminated": self.terminated,
            "thread_alive": self.thread_alive,
            "pending_cancelled": self.pending_cancelled,
            "inflight_cancelled": self.inflight_cancelled,
        }


@dataclass(frozen=True, slots=True)
class RuntimeFrameBoundaryTerminationReceipt:
    lifecycle_generation: int
    projection_thread_alive: bool
    pending_projection_cancelled: bool
    inflight_projection_cancelled: bool
    listener_detachment_errors: tuple[str, ...]
    publisher: RuntimeFramePublisherTerminationReceipt
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation <= 0
        ):
            raise ValueError(
                "runtime_frame_boundary_lifecycle_generation_invalid"
            )
        for field_name in (
            "projection_thread_alive",
            "pending_projection_cancelled",
            "inflight_projection_cancelled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"runtime_frame_boundary_{field_name}_bool_required"
                )
        if not isinstance(
            self.publisher,
            RuntimeFramePublisherTerminationReceipt,
        ):
            raise TypeError(
                "runtime_frame_boundary_publisher_receipt_required"
            )
        if (
            not isinstance(self.listener_detachment_errors, tuple)
            or any(
                type(item) is not str
                or not item
                or item != item.strip()
                for item in self.listener_detachment_errors
            )
        ):
            raise TypeError(
                "runtime_frame_boundary_listener_errors_tuple_required"
            )
        if len(self.listener_detachment_errors) != len(
            set(self.listener_detachment_errors)
        ):
            raise ValueError(
                "runtime_frame_boundary_listener_errors_duplicate"
            )

    @property
    def terminated(self) -> bool:
        return bool(
            not self.projection_thread_alive
            and self.publisher.terminated
            and not self.listener_detachment_errors
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeFrameBoundaryTerminationReceipt.v1",
            "owner": "54guc.ngui.runtime_frame_boundary",
            "lifecycle_generation": self.lifecycle_generation,
            "terminated": self.terminated,
            "projection_thread_alive": self.projection_thread_alive,
            "pending_projection_cancelled": (
                self.pending_projection_cancelled
            ),
            "inflight_projection_cancelled": (
                self.inflight_projection_cancelled
            ),
            "listener_detachment_errors": list(
                self.listener_detachment_errors
            ),
            "publisher": self.publisher.as_dict(),
        }


__all__ = (
    "RequiredRuntimeWorkerTerminationReceipt",
    "RuntimeFrameBoundaryTerminationReceipt",
    "RuntimeFramePublisherTerminationReceipt",
    "RuntimeParticipantStartReceipt",
    "RuntimeParticipantAbsenceDisposition",
    "RuntimeParticipantAbsenceReceipt",
    "RuntimeTerminationBlocker",
    "RuntimeTerminationParticipant",
)
