"""Atomic front register owned by NGUI presentation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

from .types import FreshnessState, PageId, SourceOwner
from .display_text import display_value_text
from .input_registers import (
    freeze_ngui_input_value,
    normalize_ngui_input_value,
)


class PresentationState(str, Enum):
    VALID = "valid"
    HELD = "held"
    TRANSITION = "transition"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    INVALID = "invalid"
    UNATTACHED = "unattached"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class PresentationValue:
    field_id: str
    value: Any
    text: str
    state: PresentationState
    source_sequence: int
    sampled_at: float
    published_at: float
    owner: SourceOwner
    source_path: str
    freshness: FreshnessState
    reason: str

    def __post_init__(self) -> None:
        field_id = str(self.field_id or "").strip()
        source_path = str(self.source_path or "").strip()
        if not field_id or not source_path:
            raise ValueError("presentation_value_identity_required")
        if not isinstance(self.state, PresentationState):
            raise TypeError("presentation_value_state_required")
        if not isinstance(self.owner, SourceOwner):
            raise TypeError("presentation_value_owner_required")
        if not isinstance(self.freshness, FreshnessState):
            raise TypeError("presentation_value_freshness_required")
        if type(self.source_sequence) is not int or self.source_sequence < 0:
            raise ValueError("presentation_value_source_sequence_invalid")
        for field_name in ("sampled_at", "published_at"):
            value = getattr(self, field_name)
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError(
                    f"presentation_value_{field_name}_invalid"
                )
            if float(value) < 0.0:
                raise ValueError(
                    f"presentation_value_{field_name}_invalid"
                )
            object.__setattr__(self, field_name, float(value))
        object.__setattr__(self, "field_id", field_id)
        object.__setattr__(self, "source_path", source_path)
        object.__setattr__(self, "text", display_value_text(self.text))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(
            self,
            "value",
            freeze_ngui_input_value(
                normalize_ngui_input_value(
                    self.value,
                    path=f"$.front.{field_id}",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class PageTruthSnapshot:
    page: PageId
    fields: Mapping[str, PresentationValue]
    matrix_identity: MatrixGenerationIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.page, PageId):
            raise TypeError("page_truth_page_required")
        current = dict(self.fields)
        if any(
            not isinstance(value, PresentationValue)
            or key != value.field_id
            for key, value in current.items()
        ):
            raise TypeError("page_truth_field_invalid")
        if self.matrix_identity is not None and not isinstance(
            self.matrix_identity,
            MatrixGenerationIdentity,
        ):
            raise TypeError("page_truth_matrix_identity_required")
        object.__setattr__(self, "fields", MappingProxyType(current))


@dataclass(frozen=True, slots=True)
class FrontRegisterSnapshot:
    sequence: int
    input_generation: int
    runtime_session_scope_key: str
    runtime_source_epoch: str
    accepted_modem_source_sequence: int
    accepted_modem_sampled_at: float
    published_at: float
    fields: Mapping[str, PresentationValue]
    pages: Mapping[PageId, PageTruthSnapshot]
    chart_snapshot: Any | None
    chart_status: Mapping[str, Any]
    matrix_identity: MatrixGenerationIdentity | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "sequence",
            "input_generation",
            "accepted_modem_source_sequence",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"front_snapshot_{field_name}_invalid")
        for field_name in ("accepted_modem_sampled_at", "published_at"):
            value = getattr(self, field_name)
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError(f"front_snapshot_{field_name}_invalid")
            if float(value) < 0.0:
                raise ValueError(f"front_snapshot_{field_name}_invalid")
            object.__setattr__(self, field_name, float(value))
        fields = dict(self.fields)
        if any(
            not isinstance(value, PresentationValue)
            or key != value.field_id
            for key, value in fields.items()
        ):
            raise TypeError("front_snapshot_field_invalid")
        pages = dict(self.pages)
        if any(
            not isinstance(page, PageId)
            or not isinstance(snapshot, PageTruthSnapshot)
            or snapshot.page is not page
            for page, snapshot in pages.items()
        ):
            raise TypeError("front_snapshot_page_invalid")
        if self.matrix_identity is not None and not isinstance(
            self.matrix_identity,
            MatrixGenerationIdentity,
        ):
            raise TypeError("front_snapshot_matrix_identity_required")
        if any(
            snapshot.matrix_identity != self.matrix_identity
            for snapshot in pages.values()
        ):
            raise ValueError("front_snapshot_page_matrix_identity_mismatch")
        object.__setattr__(self, "fields", MappingProxyType(fields))
        object.__setattr__(self, "pages", MappingProxyType(pages))
        chart_snapshot = self.chart_snapshot
        if chart_snapshot is not None and (
            type(chart_snapshot).__module__ != "arch4.ngui.chart_model"
            or type(chart_snapshot).__name__ != "ChartRenderSnapshot"
        ):
            raise TypeError("front_snapshot_chart_contract_invalid")
        # ChartRenderSnapshot is local to NGUI. Plot gaps may use NaN, but NaN
        # never crosses the JSON PageTruth boundary. This payload exports only
        # bounded chart identity and count metadata. Other page values are
        # normalized above.
        object.__setattr__(self, "chart_snapshot", chart_snapshot)
        object.__setattr__(
            self,
            "chart_status",
            freeze_ngui_input_value(
                normalize_ngui_input_value(
                    dict(self.chart_status),
                    path="$.front.chart_status",
                )
            ),
        )

class NGUIFrontRegister:
    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = FrontRegisterSnapshot(
            sequence=0,
            input_generation=0,
            runtime_session_scope_key="",
            runtime_source_epoch="",
            accepted_modem_source_sequence=0,
            accepted_modem_sampled_at=0.0,
            published_at=0.0,
            fields=MappingProxyType({}),
            pages=MappingProxyType({}),
            chart_snapshot=None,
            chart_status=MappingProxyType({}),
        )

    def publish(
        self,
        *,
        input_generation: int,
        runtime_session_scope_key: str,
        runtime_source_epoch: str,
        accepted_modem_source_sequence: int,
        accepted_modem_sampled_at: float,
        matrix_identity: MatrixGenerationIdentity | None = None,
        published_at: float,
        fields: Mapping[str, PresentationValue],
        pages: Mapping[PageId, PageTruthSnapshot],
        chart_snapshot: Any | None = None,
        chart_status: Mapping[str, Any] | None = None,
    ) -> FrontRegisterSnapshot:
        with self._lock:
            if input_generation < self._snapshot.input_generation:
                raise RuntimeError("front register cannot publish an older input generation")
            source_sequence = int(accepted_modem_source_sequence)
            sampled_at = float(accepted_modem_sampled_at)
            session_scope_key = str(runtime_session_scope_key or "").strip()
            source_epoch = str(runtime_source_epoch or "").strip()
            if bool(session_scope_key) != bool(source_epoch):
                raise ValueError(
                    "front register runtime namespace requires scope and epoch"
                )
            if source_sequence > 0 and not session_scope_key:
                raise ValueError(
                    "accepted modem source requires a runtime namespace"
                )
            if source_sequence < 0 or sampled_at < 0.0:
                raise ValueError("accepted modem source envelope cannot be negative")
            if bool(source_sequence) != bool(sampled_at):
                raise ValueError(
                    "accepted modem source envelope requires sequence and timestamp"
                )
            if matrix_identity is not None:
                if not isinstance(matrix_identity, MatrixGenerationIdentity):
                    raise TypeError("front register Matrix identity required")
                if (
                    matrix_identity.session_scope_key != session_scope_key
                    or matrix_identity.source_epoch != source_epoch
                    or matrix_identity.matrix_sequence != source_sequence
                ):
                    raise ValueError(
                        "front register Matrix identity mismatch"
                    )
            self._snapshot = FrontRegisterSnapshot(
                sequence=self._snapshot.sequence + 1,
                input_generation=input_generation,
                runtime_session_scope_key=session_scope_key,
                runtime_source_epoch=source_epoch,
                accepted_modem_source_sequence=source_sequence,
                accepted_modem_sampled_at=sampled_at,
                published_at=published_at,
                fields=MappingProxyType(dict(fields)),
                pages=MappingProxyType(dict(pages)),
                chart_snapshot=chart_snapshot,
                chart_status=MappingProxyType(dict(chart_status or {})),
                matrix_identity=matrix_identity,
            )
            return self._snapshot

    @staticmethod
    def prepare_snapshot(
        previous: FrontRegisterSnapshot,
        *,
        input_generation: int,
        runtime_session_scope_key: str,
        runtime_source_epoch: str,
        accepted_modem_source_sequence: int,
        accepted_modem_sampled_at: float,
        matrix_identity: MatrixGenerationIdentity | None,
        published_at: float,
        fields: Mapping[str, PresentationValue],
        pages: Mapping[PageId, PageTruthSnapshot],
        chart_snapshot: Any | None = None,
        chart_status: Mapping[str, Any] | None = None,
    ) -> FrontRegisterSnapshot:
        """Build the next complete Front without changing live state."""

        if not isinstance(previous, FrontRegisterSnapshot):
            raise TypeError("front_prepare_previous_snapshot_required")
        candidate = FrontRegisterSnapshot(
            sequence=previous.sequence + 1,
            input_generation=input_generation,
            runtime_session_scope_key=runtime_session_scope_key,
            runtime_source_epoch=runtime_source_epoch,
            accepted_modem_source_sequence=accepted_modem_source_sequence,
            accepted_modem_sampled_at=accepted_modem_sampled_at,
            published_at=published_at,
            fields=fields,
            pages=pages,
            chart_snapshot=chart_snapshot,
            chart_status=chart_status or {},
            matrix_identity=matrix_identity,
        )
        if candidate.input_generation < previous.input_generation:
            raise RuntimeError(
                "front_prepare_input_generation_regressed"
            )
        if bool(candidate.runtime_session_scope_key) != bool(
            candidate.runtime_source_epoch
        ):
            raise ValueError(
                "front_prepare_runtime_namespace_incomplete"
            )
        if (
            candidate.accepted_modem_source_sequence > 0
            and not candidate.runtime_session_scope_key
        ):
            raise ValueError(
                "front_prepare_accepted_source_namespace_required"
            )
        if bool(candidate.accepted_modem_source_sequence) != bool(
            candidate.accepted_modem_sampled_at
        ):
            raise ValueError(
                "front_prepare_accepted_source_envelope_incomplete"
            )
        if matrix_identity is not None and (
            matrix_identity.session_scope_key
            != candidate.runtime_session_scope_key
            or matrix_identity.source_epoch
            != candidate.runtime_source_epoch
            or matrix_identity.matrix_sequence
            != candidate.accepted_modem_source_sequence
        ):
            raise ValueError("front_prepare_matrix_identity_mismatch")
        return candidate

    def install_prepared(
        self,
        previous: FrontRegisterSnapshot,
        candidate: FrontRegisterSnapshot,
    ) -> FrontRegisterSnapshot:
        """Install the prepared Front with one assignment."""

        if not isinstance(previous, FrontRegisterSnapshot) or not isinstance(
            candidate,
            FrontRegisterSnapshot,
        ):
            raise TypeError("front_install_prepared_snapshot_required")
        with self._lock:
            if self._snapshot is not previous:
                raise RuntimeError("front_install_prepared_base_changed")
            if candidate.sequence != previous.sequence + 1:
                raise RuntimeError(
                    "front_install_prepared_sequence_invalid"
                )
            self._snapshot = candidate
            return candidate

    def snapshot(self) -> FrontRegisterSnapshot:
        with self._lock:
            return self._snapshot

    def reset_runtime_state(
        self,
    ) -> tuple[FrontRegisterSnapshot, FrontRegisterSnapshot]:
        with self._lock:
            prior = self._snapshot
            self._snapshot = FrontRegisterSnapshot(
                sequence=0,
                input_generation=0,
                runtime_session_scope_key="",
                runtime_source_epoch="",
                accepted_modem_source_sequence=0,
                accepted_modem_sampled_at=0.0,
                published_at=0.0,
                fields=MappingProxyType({}),
                pages=MappingProxyType({}),
                chart_snapshot=None,
                chart_status=MappingProxyType({}),
            )
            return prior, self._snapshot


def accepted_modem_source_generation(snapshot: FrontRegisterSnapshot) -> int:
    if not isinstance(snapshot, FrontRegisterSnapshot):
        raise TypeError("accepted source generation requires front register")
    value = int(snapshot.accepted_modem_source_sequence)
    return value if value > 0 else 0


def accepted_modem_source_sampled_at(snapshot: FrontRegisterSnapshot) -> float:
    if not isinstance(snapshot, FrontRegisterSnapshot):
        raise TypeError("accepted source timestamp requires front register")
    value = float(snapshot.accepted_modem_sampled_at)
    return value if value > 0.0 else 0.0
