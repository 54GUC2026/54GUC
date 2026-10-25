"""Publish complete NGUI Front generations."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping, Any

from ..front_register import FrontRegisterSnapshot, NGUIFrontRegister
from ..input_registers import InputRegisterSnapshot
from ..page_owners import build_all_page_truth
from .processor import NGUIPresentationProcessor, PresentationBuildCandidate


@dataclass(frozen=True, slots=True)
class NGUIPresentationCandidate:
    """Prepared presentation policy and its next Front."""

    previous_front: FrontRegisterSnapshot
    policy: PresentationBuildCandidate
    front: FrontRegisterSnapshot


@dataclass(frozen=True, slots=True)
class NGUIChartPresentationCandidate:
    """Next Front with new chart evidence and unchanged field policy."""

    previous_front: FrontRegisterSnapshot
    front: FrontRegisterSnapshot


def _front_reset_evidence(
    snapshot: FrontRegisterSnapshot,
) -> Mapping[str, Any]:
    """Copy typed Front state into reset evidence."""

    if not isinstance(snapshot, FrontRegisterSnapshot):
        raise TypeError("presentation_reset_front_snapshot_required")
    return MappingProxyType(
        {
            "sequence": snapshot.sequence,
            "input_generation": snapshot.input_generation,
            "runtime_session_scope_key": snapshot.runtime_session_scope_key,
            "runtime_source_epoch": snapshot.runtime_source_epoch,
            "accepted_modem_source_sequence": (
                snapshot.accepted_modem_source_sequence
            ),
            "accepted_modem_sampled_at": snapshot.accepted_modem_sampled_at,
            "published_at": snapshot.published_at,
            "fields": dict(snapshot.fields),
            "pages": {
                page.value: {
                    "page": truth.page.value,
                    "fields": dict(truth.fields),
                    "matrix_identity": truth.matrix_identity,
                }
                for page, truth in snapshot.pages.items()
            },
            "chart_snapshot": snapshot.chart_snapshot,
            "chart_status": dict(snapshot.chart_status),
            "matrix_identity": snapshot.matrix_identity,
        }
    )


class NGUIPresentationAuthority:
    def __init__(
        self,
        *,
        chart_register: object | None = None,
        chart_status_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._processor = NGUIPresentationProcessor()
        self._front = NGUIFrontRegister()
        self._chart_register = chart_register
        self._chart_status_provider = chart_status_provider
        self._lock = RLock()

    @property
    def front_register(self) -> NGUIFrontRegister:
        return self._front

    def reset_for_changed_device(self) -> None:
        with self._lock:
            self._processor.reset_for_changed_device()

    def reset_runtime_state(
        self,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self._lock:
            prior_processor = self._processor.runtime_reset_observation()
            self._processor.reset_for_changed_device()
            prior_front, result_front = self._front.reset_runtime_state()
            prior = {
                "processor": prior_processor,
                "front": _front_reset_evidence(prior_front),
            }
            result = {
                "processor": self._processor.runtime_reset_observation(),
                "front": _front_reset_evidence(result_front),
            }
            return MappingProxyType(prior), MappingProxyType(result)

    def publish(self, inputs: InputRegisterSnapshot, *, now: float) -> FrontRegisterSnapshot:
        candidate = self.prepare_runtime_candidate(inputs, now=now)
        return self.commit_runtime_candidate(candidate)

    def prepare_runtime_candidate(
        self,
        inputs: InputRegisterSnapshot,
        *,
        now: float,
    ) -> NGUIPresentationCandidate:
        """Prepare policy and a complete Front without changing live owners."""

        if not isinstance(inputs, InputRegisterSnapshot):
            raise TypeError("presentation_prepare_input_snapshot_required")
        with self._lock:
            previous_front = self._front.snapshot()
            policy = self._processor.prepare_fields(
                inputs,
                now=now,
                previous_fields=previous_front.fields,
            )
            pages = build_all_page_truth(
                policy.fields,
                matrix_identity=inputs.matrix_identity,
                previous_pages=previous_front.pages,
            )
            chart_snapshot, chart_status = self._chart_evidence(inputs)
            front = self._front.prepare_snapshot(
                previous_front,
                input_generation=inputs.generation,
                runtime_session_scope_key=inputs.runtime_session_scope_key,
                runtime_source_epoch=inputs.runtime_source_epoch,
                accepted_modem_source_sequence=(
                    inputs.accepted_source_sequence
                ),
                accepted_modem_sampled_at=(
                    inputs.accepted_source_sampled_at
                ),
                matrix_identity=inputs.matrix_identity,
                published_at=now,
                fields=policy.fields,
                pages=pages,
                chart_snapshot=chart_snapshot,
                chart_status=chart_status,
            )
            return NGUIPresentationCandidate(
                previous_front=previous_front,
                policy=policy,
                front=front,
            )

    def commit_runtime_candidate(
        self,
        candidate: NGUIPresentationCandidate,
    ) -> FrontRegisterSnapshot:
        """Install the validated presentation state."""

        if not isinstance(candidate, NGUIPresentationCandidate):
            raise TypeError("presentation_runtime_candidate_required")
        with self._lock:
            front = self._front.install_prepared(
                candidate.previous_front,
                candidate.front,
            )
            self._processor.commit_fields(candidate.policy)
            return front

    def publish_chart_update(
        self,
        inputs: InputRegisterSnapshot,
        *,
        accepted_front: FrontRegisterSnapshot,
        now: float,
    ) -> FrontRegisterSnapshot:
        """Attach completed chart evidence without reinterpreting input values."""

        candidate = self.prepare_chart_candidate(
            inputs,
            accepted_front=accepted_front,
            now=now,
        )
        return self.commit_chart_candidate(candidate)

    def prepare_chart_candidate(
        self,
        inputs: InputRegisterSnapshot,
        *,
        accepted_front: FrontRegisterSnapshot,
        now: float,
    ) -> NGUIChartPresentationCandidate:
        """Prepare chart evidence without changing presentation or Front."""

        if not isinstance(inputs, InputRegisterSnapshot):
            raise TypeError("chart_presentation_input_snapshot_required")
        if not isinstance(accepted_front, FrontRegisterSnapshot):
            raise TypeError("chart_presentation_front_snapshot_required")
        with self._lock:
            previous_front = self._front.snapshot()
            if previous_front != accepted_front:
                raise RuntimeError(
                    "chart-only presentation requires the live accepted front"
                )
            if accepted_front.input_generation != inputs.generation:
                raise RuntimeError(
                    "chart-only presentation requires the accepted input generation"
                )
            if (
                accepted_front.runtime_session_scope_key
                != inputs.runtime_session_scope_key
                or accepted_front.runtime_source_epoch
                != inputs.runtime_source_epoch
                or accepted_front.matrix_identity != inputs.matrix_identity
            ):
                raise RuntimeError(
                    "chart-only presentation requires the accepted runtime namespace"
                )
            chart_snapshot, chart_status = self._chart_evidence(inputs)
            front = self._front.prepare_snapshot(
                previous_front,
                input_generation=inputs.generation,
                runtime_session_scope_key=inputs.runtime_session_scope_key,
                runtime_source_epoch=inputs.runtime_source_epoch,
                accepted_modem_source_sequence=(
                    inputs.accepted_source_sequence
                ),
                accepted_modem_sampled_at=(
                    inputs.accepted_source_sampled_at
                ),
                matrix_identity=inputs.matrix_identity,
                published_at=now,
                fields=accepted_front.fields,
                pages=accepted_front.pages,
                chart_snapshot=chart_snapshot,
                chart_status=chart_status,
            )
            return NGUIChartPresentationCandidate(
                previous_front=previous_front,
                front=front,
            )

    def commit_chart_candidate(
        self,
        candidate: NGUIChartPresentationCandidate,
    ) -> FrontRegisterSnapshot:
        """Install the chart Front accepted by the coordinator."""

        if not isinstance(candidate, NGUIChartPresentationCandidate):
            raise TypeError("chart_presentation_candidate_required")
        with self._lock:
            return self._front.install_prepared(
                candidate.previous_front,
                candidate.front,
            )

    def _publish(
        self,
        inputs: InputRegisterSnapshot,
        *,
        now: float,
        fields,
        pages,
    ) -> FrontRegisterSnapshot:
        chart_snapshot, chart_status = self._chart_evidence(inputs)
        return self._front.publish(
            input_generation=inputs.generation,
            runtime_session_scope_key=inputs.runtime_session_scope_key,
            runtime_source_epoch=inputs.runtime_source_epoch,
            accepted_modem_source_sequence=(
                inputs.accepted_source_sequence
            ),
            accepted_modem_sampled_at=(
                inputs.accepted_source_sampled_at
            ),
            matrix_identity=inputs.matrix_identity,
            published_at=now,
            fields=fields,
            pages=pages,
            chart_snapshot=chart_snapshot,
            chart_status=chart_status,
        )

    def _chart_evidence(
        self,
        inputs: InputRegisterSnapshot,
    ) -> tuple[Any | None, Mapping[str, Any]]:
        chart_snapshot = None
        chart_status: Mapping[str, Any] = {}
        if self._chart_register is not None:
            candidate = self._chart_register.snapshot()
            if (
                candidate is not None
                and str(getattr(candidate, "session_scope_key", "") or "")
                == inputs.runtime_session_scope_key
                and str(getattr(candidate, "source_epoch", "") or "")
                == inputs.runtime_source_epoch
            ):
                chart_snapshot = candidate
        if self._chart_status_provider is not None:
            chart_status = dict(self._chart_status_provider())
        return chart_snapshot, chart_status
