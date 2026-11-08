"""Foundation-owned NGUI operational-service presentation extension."""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import RLock
from time import time
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime_snapshot import (
    FOUNDATION_ACTIVE_ANTENNA_GUIDE_SOURCE_IDS,
    FOUNDATION_ANTENNA_GUIDE_SOURCE_IDS,
    FOUNDATION_NGUI_EXTENSION_HOOK_ID,
    FOUNDATION_NGUI_EXTENSION_SCHEMA,
    FOUNDATION_NGUI_SOURCE_IDS,
    NGUIExtensionFrame,
)
from arch4.foundation.data_budget import FoundationDataBudgetProjection
from arch4.operational_service_contracts import (
    OperationalKind,
)
from arch4.foundation.schedule_service import (
    OWNER as FOUNDATION_SCHEDULE_OWNER,
    STATE_FILENAME as FOUNDATION_SCHEDULE_STATE_FILENAME,
    schedule_presentation_status,
)


FOUNDATION_NGUI_PROVIDER_ID = "54guc.foundation.ngui_extension"

FOUNDATION_RUNTIME_HEALTH_PRESENTATION_OWNER = "54guc.foundation.runtime_health"
FOUNDATION_PERSISTENCE_PRESENTATION_OWNER = "54guc.foundation.persistence"


@dataclass(frozen=True, slots=True)
class FoundationNamespaceState:
    """One immutable Foundation presentation catalog for one runtime."""

    namespace: RuntimeGenerationNamespace
    revision: int
    provider_sequence: int
    values: Mapping[str, Any]
    observed_at: Mapping[str, float]
    sample_sequences: Mapping[str, int]
    latest_frame: NGUIExtensionFrame

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_ngui_namespace_required")
        if (
            type(self.revision) is not int
            or type(self.provider_sequence) is not int
            or self.revision <= 0
            or self.provider_sequence <= 0
        ):
            raise ValueError("foundation_ngui_state_sequence_invalid")
        if not isinstance(self.latest_frame, NGUIExtensionFrame):
            raise TypeError("foundation_ngui_latest_frame_required")
        expected = set(FOUNDATION_NGUI_SOURCE_IDS)
        current_values = dict(self.values)
        current_observed_at = dict(self.observed_at)
        current_sample_sequences = dict(self.sample_sequences)
        for field_name, current in (
            ("values", current_values),
            ("observed_at", current_observed_at),
            ("sample_sequences", current_sample_sequences),
        ):
            if set(current) != expected:
                raise ValueError(
                    f"foundation_ngui_state_{field_name}_catalog_mismatch"
                )
        frozen_values = MappingProxyType({
            source_id: _freeze_presentation_value(value)
            for source_id, value in current_values.items()
        })
        exact_observed_at = MappingProxyType({
            source_id: _required_nonnegative_timestamp(
                value,
                "foundation_ngui_state_timestamp_invalid",
            )
            for source_id, value in current_observed_at.items()
        })
        exact_sample_sequences = MappingProxyType({
            source_id: _required_nonnegative_sequence(
                value,
                "foundation_ngui_state_sample_sequence_invalid",
            )
            for source_id, value in current_sample_sequences.items()
        })
        if (
            self.latest_frame.namespace != self.namespace
            or self.latest_frame.sequence != self.provider_sequence
            or self.latest_frame.provider_id != FOUNDATION_NGUI_PROVIDER_ID
            or self.latest_frame.values.to_mapping()
            != {
                source_id: _copy_presentation_value(value)
                for source_id, value in frozen_values.items()
            }
            or self.latest_frame.sampled_at.to_mapping()
            != dict(exact_observed_at)
            or self.latest_frame.sample_sequences.to_mapping()
            != dict(exact_sample_sequences)
        ):
            raise ValueError("foundation_ngui_state_frame_mismatch")
        object.__setattr__(self, "values", frozen_values)
        object.__setattr__(self, "observed_at", exact_observed_at)
        object.__setattr__(
            self,
            "sample_sequences",
            exact_sample_sequences,
        )


@dataclass(frozen=True, slots=True)
class FoundationRuntimeHealthPresentationReceipt:
    status: str
    observed_at: float
    owner: str = FOUNDATION_RUNTIME_HEALTH_PRESENTATION_OWNER
    schema: str = "FoundationRuntimeHealthPresentationReceipt.v1"

    def __post_init__(self) -> None:
        if self.owner != FOUNDATION_RUNTIME_HEALTH_PRESENTATION_OWNER:
            raise ValueError("foundation_runtime_health_presentation_owner_invalid")
        if self.status not in {"OK", "DEGRADED"}:
            raise ValueError("foundation_runtime_health_presentation_status_invalid")
        _required_positive_timestamp(
            self.observed_at,
            "foundation_runtime_health_presentation_timestamp_invalid",
        )


@dataclass(frozen=True, slots=True)
class FoundationPersistencePresentationReceipt:
    status: str
    observed_at: float
    owner: str = FOUNDATION_PERSISTENCE_PRESENTATION_OWNER
    schema: str = "FoundationPersistencePresentationReceipt.v1"

    def __post_init__(self) -> None:
        if self.owner != FOUNDATION_PERSISTENCE_PRESENTATION_OWNER:
            raise ValueError("foundation_persistence_presentation_owner_invalid")
        if self.status not in {"OK", "DEGRADED"}:
            raise ValueError("foundation_persistence_presentation_status_invalid")
        _required_positive_timestamp(
            self.observed_at,
            "foundation_persistence_presentation_timestamp_invalid",
        )


class FoundationNGUIExtensionPublisher:
    """The only Foundation presentation owner for light-product services."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._state: FoundationNamespaceState | None = None

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
        *,
        published_at: float | None = None,
    ) -> bool:
        """Install unavailable presentation values for this namespace."""

        candidate = self.prepare_runtime_namespace(
            namespace,
            published_at=published_at,
        )
        if candidate is None:
            return False
        self.commit_runtime_namespace(candidate)
        return True

    def prepare_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
        *,
        published_at: float | None = None,
    ) -> FoundationNamespaceState | None:
        """Build a complete unavailable catalog without changing live state."""

        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_ngui_namespace_required")
        now = _required_positive_timestamp(
            time() if published_at is None else published_at,
            "foundation_ngui_positive_publication_timestamp_required",
        )
        with self._lock:
            current = self._state
            if current is not None and current.namespace == namespace:
                return None
            revision = 1 if current is None else current.revision + 1
            return self._build_state(
                namespace=namespace,
                revision=revision,
                provider_sequence=1,
                values={
                    source_id: None
                    for source_id in FOUNDATION_NGUI_SOURCE_IDS
                },
                observed_at={
                    source_id: 0.0
                    for source_id in FOUNDATION_NGUI_SOURCE_IDS
                },
                sample_sequences={
                    source_id: 0
                    for source_id in FOUNDATION_NGUI_SOURCE_IDS
                },
                published_at=now,
            )

    def commit_runtime_namespace(
        self,
        candidate: FoundationNamespaceState,
    ) -> None:
        """Commit one already validated state through one root assignment."""

        if not isinstance(candidate, FoundationNamespaceState):
            raise TypeError("foundation_ngui_namespace_candidate_required")
        with self._lock:
            current = self._state
            expected_revision = 1 if current is None else current.revision + 1
            if candidate.revision != expected_revision:
                raise RuntimeError(
                    "foundation_ngui_namespace_candidate_stale"
                )
            self._state = candidate

    def record_sms_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float | None = None,
        sample_sequence: int | None = None,
    ) -> NGUIExtensionFrame:
        if not isinstance(snapshot, Mapping):
            raise TypeError("foundation_sms_snapshot_mapping_required")
        command = snapshot.get("command_control")
        inbox = snapshot.get("inbox")
        if not isinstance(command, Mapping):
            raise RuntimeError("foundation_sms_command_control_missing")
        if not isinstance(inbox, Mapping):
            raise RuntimeError("foundation_sms_inbox_missing")
        updates = {
            "command.sms_enabled": command.get("enabled"),
            "command.sms_keyword": command.get("code_word"),
            "command.sms_every_s": inbox.get("poll_interval_s"),
            "command.sms_message": inbox.get("message"),
        }
        return self._record(
            updates,
            namespace=namespace,
            observed_at=_required_positive_timestamp(
                observed_at if observed_at is not None else inbox.get("ts"),
                "foundation_sms_positive_timestamp_required",
            ),
            sample_sequence=sample_sequence,
            skip_if_values_unchanged=True,
        )

    def record_connectivity_transition(
        self,
        transition: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float,
        sample_sequence: int,
    ) -> NGUIExtensionFrame:
        if not isinstance(transition, Mapping):
            raise TypeError("foundation_connectivity_transition_mapping_required")
        if transition.get("schema") != "FoundationConnectivityTransition.v2":
            raise ValueError("foundation_connectivity_transition_schema_invalid")
        if transition.get("owner") != "54guc.foundation.connectivity_transition":
            raise ValueError("foundation_connectivity_transition_owner_invalid")
        transition_sequence = transition.get("sequence")
        if (
            type(transition_sequence) is not int
            or type(sample_sequence) is not int
            or transition_sequence != sample_sequence
        ):
            raise ValueError("foundation_connectivity_transition_sequence_mismatch")
        return self._record(
            {"event.connectivity_transition": dict(transition)},
            namespace=namespace,
            observed_at=_required_positive_timestamp(
                observed_at,
                "foundation_connectivity_positive_timestamp_required",
            ),
            sample_sequence=sample_sequence,
        )

    def record_antenna_guide_surface(
        self,
        surface: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float | None = None,
        sample_sequence: int | None = None,
    ) -> NGUIExtensionFrame:
        if not isinstance(surface, Mapping):
            raise TypeError("foundation_antenna_guide_surface_mapping_required")
        actual = set(surface)
        expected = set(FOUNDATION_ANTENNA_GUIDE_SOURCE_IDS)
        if actual != expected:
            raise ValueError(
                "foundation_antenna_guide_surface_catalog_mismatch:"
                f"missing={sorted(expected - actual)};extra={sorted(actual - expected)}"
            )
        return self._record(
            {
                source_id: surface[source_id]
                for source_id in FOUNDATION_ACTIVE_ANTENNA_GUIDE_SOURCE_IDS
            },
            namespace=namespace,
            observed_at=_required_positive_timestamp(
                observed_at if observed_at is not None else surface.get("ts"),
                "foundation_antenna_guide_positive_timestamp_required",
            ),
            sample_sequence=sample_sequence,
            skip_if_values_unchanged=True,
        )

    def record_data_budget_projection(
        self,
        projection: FoundationDataBudgetProjection,
        *,
        namespace: RuntimeGenerationNamespace,
    ) -> NGUIExtensionFrame:
        if type(projection) is not FoundationDataBudgetProjection:
            raise TypeError("foundation_typed_data_budget_projection_required")
        updates = projection.ngui_values()
        return self._record(
            updates,
            namespace=namespace,
            observed_at=projection.observed_at,
            sample_sequence=None,
            skip_if_values_unchanged=True,
        )

    def record_runtime_status(
        self,
        *,
        namespace: RuntimeGenerationNamespace,
        health: FoundationRuntimeHealthPresentationReceipt,
        persistence: FoundationPersistencePresentationReceipt,
    ) -> NGUIExtensionFrame:
        """Publish Foundation health and storage status without GUI inference."""

        if not isinstance(health, FoundationRuntimeHealthPresentationReceipt):
            raise TypeError("foundation_typed_runtime_health_presentation_required")
        if not isinstance(persistence, FoundationPersistencePresentationReceipt):
            raise TypeError("foundation_typed_persistence_presentation_required")
        return self._record(
            {
                "device.health_status": health.status,
                "device.persistence_status": persistence.status,
            },
            namespace=namespace,
            observed_at={
                "device.health_status": health.observed_at,
                "device.persistence_status": persistence.observed_at,
            },
            sample_sequence=None,
            skip_if_values_unchanged=True,
        )

    def record_schedule_state(
        self,
        state: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float | None = None,
    ) -> NGUIExtensionFrame:
        if not isinstance(state, Mapping):
            raise TypeError("foundation_schedule_state_mapping_required")
        if state.get("artifact") != FOUNDATION_SCHEDULE_STATE_FILENAME:
            raise ValueError("foundation_schedule_state_artifact_invalid")
        if "schema_version" in state:
            raise ValueError(
                "foundation_schedule_state_versioned_contract_forbidden"
            )
        if state.get("owner") != FOUNDATION_SCHEDULE_OWNER:
            raise ValueError("foundation_schedule_state_owner_invalid")
        lifecycle = str(state.get("lifecycle_state") or "").strip()
        operation = str(state.get("operation") or "").strip()
        if not lifecycle:
            raise ValueError("foundation_schedule_lifecycle_missing")
        try:
            kind = OperationalKind(operation)
        except ValueError as exc:
            raise ValueError("foundation_schedule_operation_invalid") from exc
        source_id = _schedule_source_id(kind)
        if not source_id:
            raise ValueError("foundation_schedule_operation_not_presentable")
        status = (
            schedule_presentation_status(state)
            if kind is OperationalKind.PLAN_BAND_SCHEDULE
            else lifecycle
        )
        return self._record(
            {source_id: status},
            namespace=namespace,
            observed_at=_required_positive_timestamp(
                observed_at if observed_at is not None else state.get("ts"),
                "foundation_schedule_positive_timestamp_required",
            ),
            sample_sequence=None,
        )

    @staticmethod
    def _build_state(
        *,
        namespace: RuntimeGenerationNamespace,
        revision: int,
        provider_sequence: int,
        values: Mapping[str, Any],
        observed_at: Mapping[str, float],
        sample_sequences: Mapping[str, int],
        published_at: float,
    ) -> FoundationNamespaceState:
        requested_published_at = _required_positive_timestamp(
            published_at,
            "foundation_ngui_positive_publication_timestamp_required",
        )
        expected = set(FOUNDATION_NGUI_SOURCE_IDS)
        for field_name, current in (
            ("values", values),
            ("observed_at", observed_at),
            ("sample_sequences", sample_sequences),
        ):
            if not isinstance(current, Mapping) or set(current) != expected:
                raise ValueError(
                    f"foundation_ngui_state_{field_name}_catalog_mismatch"
                )
        current_values = {
            source_id: _copy_presentation_value(value)
            for source_id, value in values.items()
        }
        current_observed_at = {
            source_id: _required_nonnegative_timestamp(
                value,
                "foundation_ngui_state_timestamp_invalid",
            )
            for source_id, value in observed_at.items()
        }
        current_sample_sequences = {
            source_id: _required_nonnegative_sequence(
                value,
                "foundation_ngui_state_sample_sequence_invalid",
            )
            for source_id, value in sample_sequences.items()
        }
        latest_source_at = max(
            (
                observed
                for observed in current_observed_at.values()
                if observed > 0.0
            ),
            default=requested_published_at,
        )
        frame_published_at = max(requested_published_at, latest_source_at)
        validity = {
            source_id: current_observed_at[source_id] > 0.0
            for source_id in FOUNDATION_NGUI_SOURCE_IDS
        }
        frame = NGUIExtensionFrame(
            namespace=namespace,
            hook_id=FOUNDATION_NGUI_EXTENSION_HOOK_ID,
            payload_schema=FOUNDATION_NGUI_EXTENSION_SCHEMA,
            provider_id=FOUNDATION_NGUI_PROVIDER_ID,
            sequence=provider_sequence,
            published_at=frame_published_at,
            values=CoreFrozenPayload.from_mapping(current_values),
            validity=CoreFrozenPayload.from_mapping(validity),
            freshness=CoreFrozenPayload.from_mapping({
                source_id: "fresh" if validity[source_id] else "unknown"
                for source_id in FOUNDATION_NGUI_SOURCE_IDS
            }),
            reasons=CoreFrozenPayload.from_mapping({
                source_id: "" if validity[source_id] else "foundation_source_unavailable"
                for source_id in FOUNDATION_NGUI_SOURCE_IDS
            }),
            sampled_at=CoreFrozenPayload.from_mapping({
                source_id: current_observed_at[source_id]
                for source_id in FOUNDATION_NGUI_SOURCE_IDS
            }),
            sample_sequences=CoreFrozenPayload.from_mapping(
                current_sample_sequences
            ),
        )
        return FoundationNamespaceState(
            namespace=namespace,
            revision=revision,
            provider_sequence=provider_sequence,
            values=current_values,
            observed_at=current_observed_at,
            sample_sequences=current_sample_sequences,
            latest_frame=frame,
        )

    def latest_for(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> NGUIExtensionFrame:
        with self._lock:
            return self._require_state_locked(namespace).latest_frame

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            return {
                "schema": "FoundationNamespaceStateStatus.v1",
                "owner": FOUNDATION_NGUI_PROVIDER_ID,
                "bound": state is not None,
                "runtime_namespace": (
                    state.namespace.as_dict() if state is not None else {}
                ),
                "revision": int(state.revision if state is not None else 0),
                "sequence": int(
                    state.provider_sequence if state is not None else 0
                ),
                "published": state is not None,
                "source_count": len(FOUNDATION_NGUI_SOURCE_IDS),
            }

    def state(self) -> FoundationNamespaceState:
        with self._lock:
            state = self._state
            if state is None:
                raise RuntimeError("foundation_ngui_namespace_unbound")
            return state

    def _record(
        self,
        updates: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float | Mapping[str, float],
        sample_sequence: int | Mapping[str, int] | None,
        skip_if_values_unchanged: bool = False,
        skip_if_sample_sequences_match: bool = False,
    ) -> NGUIExtensionFrame:
        if not isinstance(updates, Mapping) or not updates:
            raise TypeError("foundation_ngui_nonempty_updates_mapping_required")
        unknown = sorted(set(updates) - FOUNDATION_NGUI_SOURCE_IDS)
        if unknown:
            raise ValueError(
                "foundation_ngui_unknown_source:" + ",".join(unknown)
            )
        normalized_observed_at = _normalize_source_timestamps(
            updates,
            observed_at,
        )
        normalized_sequences = _normalize_source_sequences(
            updates,
            sample_sequence,
        )
        normalized_updates = {
            source_id: _freeze_presentation_value(value)
            for source_id, value in updates.items()
        }
        with self._lock:
            state = self._require_state_locked(namespace)
            values_match = all(
                state.values[source_id] == normalized_updates[source_id]
                for source_id in normalized_updates
            )
            timestamps_match = all(
                state.observed_at[source_id] == normalized_observed_at[source_id]
                for source_id in normalized_updates
            )
            sequences_match = (
                normalized_sequences is not None
                and all(
                    state.sample_sequences[source_id]
                    == normalized_sequences[source_id]
                    for source_id in normalized_updates
                )
            )
            if normalized_sequences is not None:
                for source_id, incoming_sequence in normalized_sequences.items():
                    retained_sequence = state.sample_sequences[source_id]
                    if incoming_sequence < retained_sequence:
                        raise RuntimeError(
                            "foundation_ngui_source_sequence_stale:"
                            + source_id
                        )
                    if incoming_sequence == retained_sequence and (
                        state.values[source_id]
                        != normalized_updates[source_id]
                        or state.observed_at[source_id]
                        != normalized_observed_at[source_id]
                    ):
                        raise RuntimeError(
                            "foundation_ngui_same_sequence_different_evidence:"
                            + source_id
                        )
                if sequences_match and values_match and timestamps_match:
                    return state.latest_frame
            if (
                skip_if_values_unchanged
                and values_match
                and (
                    not skip_if_sample_sequences_match
                    or sequences_match
                )
            ):
                return state.latest_frame
            values = dict(state.values)
            observed_at_values = dict(state.observed_at)
            sample_sequences = dict(state.sample_sequences)
            for source_id, value in normalized_updates.items():
                values[source_id] = value
                observed_at_values[source_id] = normalized_observed_at[source_id]
                sample_sequences[source_id] = (
                    normalized_sequences[source_id]
                    if normalized_sequences is not None
                    else state.sample_sequences[source_id] + 1
                )
            candidate = self._build_state(
                namespace=namespace,
                revision=state.revision + 1,
                provider_sequence=state.provider_sequence + 1,
                values=values,
                observed_at=observed_at_values,
                sample_sequences=sample_sequences,
                published_at=max(normalized_observed_at.values()),
            )
            self._state = candidate
            return candidate.latest_frame

    def _require_state_locked(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> FoundationNamespaceState:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_ngui_namespace_required")
        state = self._state
        if state is None:
            raise RuntimeError("foundation_ngui_namespace_unbound")
        if state.namespace != namespace:
            raise RuntimeError("foundation_ngui_namespace_not_active")
        return state


def _schedule_source_id(kind: OperationalKind) -> str:
    if kind in {
        OperationalKind.PLAN_BAND_SEQUENCE,
        OperationalKind.CANCEL_BAND_SEQUENCE,
    }:
        return "command.band_sequence_status"
    if kind in {
        OperationalKind.PLAN_BAND_SCHEDULE,
        OperationalKind.CANCEL_BAND_SCHEDULE,
    }:
        return "command.band_schedule_status"
    return ""


def _freeze_presentation_value(value: Any) -> Any:
    """Validate JSON values and keep saved containers read-only."""

    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("foundation_ngui_value_string_keys_required")
        return MappingProxyType({
            key: _freeze_presentation_value(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_presentation_value(item) for item in value)
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("foundation_ngui_value_finite_float_required")
        return value
    raise TypeError("foundation_ngui_value_exact_json_type_required")


def _copy_presentation_value(value: Any) -> Any:
    """Return a detached JSON-safe copy for frame serialization."""

    if isinstance(value, Mapping):
        return {
            key: _copy_presentation_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_copy_presentation_value(item) for item in value]
    return value


def _normalize_source_timestamps(
    updates: Mapping[str, Any],
    observed_at: float | Mapping[str, float],
) -> dict[str, float]:
    if isinstance(observed_at, Mapping):
        if set(observed_at) != set(updates):
            raise ValueError("foundation_ngui_source_timestamp_catalog_mismatch")
        return {
            source_id: _required_positive_timestamp(
                observed_at[source_id],
                "foundation_ngui_positive_timestamp_required",
            )
            for source_id in updates
        }
    normalized = _required_positive_timestamp(
        observed_at,
        "foundation_ngui_positive_timestamp_required",
    )
    return {
        source_id: normalized
        for source_id in updates
    }


def _normalize_source_sequences(
    updates: Mapping[str, Any],
    sample_sequence: int | Mapping[str, int] | None,
) -> dict[str, int] | None:
    if sample_sequence is None:
        return None
    if isinstance(sample_sequence, Mapping):
        if set(sample_sequence) != set(updates):
            raise ValueError("foundation_ngui_source_sequence_catalog_mismatch")
        raw_sequences = {
            source_id: sample_sequence[source_id]
            for source_id in updates
        }
    else:
        raw_sequences = {
            source_id: sample_sequence
            for source_id in updates
        }
    normalized: dict[str, int] = {}
    for source_id, raw_sequence in raw_sequences.items():
        if type(raw_sequence) is not int:
            raise TypeError(
                "foundation_ngui_positive_sample_sequence_required"
            )
        if raw_sequence <= 0:
            raise ValueError(
                "foundation_ngui_positive_sample_sequence_required"
            )
        normalized[source_id] = raw_sequence
    return normalized


def _required_positive_timestamp(value: Any, error: str) -> float:
    if type(value) is not float:
        raise TypeError(error)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(error)
    return value


def _required_nonnegative_timestamp(value: Any, error: str) -> float:
    if type(value) is not float:
        raise TypeError(error)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(error)
    return value


def _required_nonnegative_sequence(value: Any, error: str) -> int:
    if type(value) is not int:
        raise TypeError(error)
    if value < 0:
        raise ValueError(error)
    return value


__all__ = (
    "FOUNDATION_NGUI_PROVIDER_ID",
    "FOUNDATION_PERSISTENCE_PRESENTATION_OWNER",
    "FOUNDATION_RUNTIME_HEALTH_PRESENTATION_OWNER",
    "FoundationPersistencePresentationReceipt",
    "FoundationNamespaceState",
    "FoundationNGUIExtensionPublisher",
    "FoundationRuntimeHealthPresentationReceipt",
)
