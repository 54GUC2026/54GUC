"""Typed RTT evidence contract owned by Modem Live.

The value, its observation time, and the latest probe outcome are one fact.
Consumers may project the last good value, but they must not turn expiry or a
failed refresh into invented absence and must not treat an old value as a new
sample.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)

SCHEMA = "RttObservation.v3"
OWNER = "modem_live"

FRESH = "fresh"
STALE = "stale"
UNAVAILABLE = "unavailable"

PROBE_NEVER = "never"
PROBE_OK = "ok"
PROBE_REACHABLE_ONLY = "reachable_only"
PROBE_FAILED = "failed"
PROBE_UNKNOWN = "unknown"

_FRESHNESS = frozenset({FRESH, STALE, UNAVAILABLE})
_PROBE_STATES = frozenset(
    {PROBE_NEVER, PROBE_OK, PROBE_REACHABLE_ONLY, PROBE_FAILED, PROBE_UNKNOWN}
)


def _finite_nonnegative(value: Any, *, field: str) -> float:
    if type(value) is not float:
        raise TypeError(f"rtt_observation_{field}_type_invalid")
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"rtt_observation_{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class RttObservation:
    """One immutable read of Modem Live's sole RTT evidence owner."""

    namespace: RuntimeGenerationNamespace
    probe_sequence: int
    value_ms: float | None
    observed_at: float
    snapshot_at: float
    freshness: str
    probe_status: str
    target: str
    last_probe_at: float
    failure_reason: str = ""
    source: str = "modem_live_rtt"
    schema: str = SCHEMA
    owner: str = OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("rtt_observation_runtime_namespace_required")
        if type(self.probe_sequence) is not int:
            raise TypeError("rtt_observation_probe_sequence_type_invalid")
        probe_sequence = self.probe_sequence
        if probe_sequence < 0:
            raise ValueError("rtt_observation_probe_sequence_invalid")
        snapshot_at = _finite_nonnegative(self.snapshot_at, field="snapshot_at")
        if snapshot_at <= 0.0:
            raise ValueError("rtt_observation_snapshot_at_required")
        observed_at = _finite_nonnegative(self.observed_at, field="observed_at")
        last_probe_at = _finite_nonnegative(
            self.last_probe_at, field="last_probe_at"
        )
        value_ms = self.value_ms
        if value_ms is not None:
            if type(value_ms) is not float:
                raise TypeError("rtt_observation_value_ms_type_invalid")
            if not math.isfinite(value_ms) or value_ms <= 0.0:
                raise ValueError("rtt_observation_value_ms_invalid")
        for name in (
            "freshness",
            "probe_status",
            "target",
            "failure_reason",
            "source",
            "schema",
            "owner",
        ):
            value = getattr(self, name)
            if type(value) is not str or value != value.strip():
                raise TypeError(f"rtt_observation_{name}_text_invalid")
        freshness = self.freshness
        probe_status = self.probe_status
        failure_reason = self.failure_reason
        source = self.source
        target = self.target
        if freshness not in _FRESHNESS:
            raise ValueError("rtt_observation_freshness_invalid")
        if probe_status not in _PROBE_STATES:
            raise ValueError("rtt_observation_probe_status_invalid")
        if not source:
            raise ValueError("rtt_observation_source_required")
        if probe_status == PROBE_NEVER:
            if target:
                raise ValueError("rtt_observation_never_probe_target_forbidden")
        elif not target:
            raise ValueError("rtt_observation_probe_target_required")
        if value_ms is None:
            if observed_at != 0.0 or freshness != UNAVAILABLE:
                raise ValueError("rtt_observation_unavailable_value_contract")
        elif observed_at <= 0.0 or freshness == UNAVAILABLE:
            raise ValueError("rtt_observation_observed_value_contract")
        if observed_at > snapshot_at or last_probe_at > snapshot_at:
            raise ValueError("rtt_observation_future_timestamp")
        if probe_status == PROBE_NEVER and last_probe_at != 0.0:
            raise ValueError("rtt_observation_never_probe_timestamp")
        if probe_status == PROBE_NEVER and probe_sequence != 0:
            raise ValueError("rtt_observation_never_probe_sequence")
        if probe_status != PROBE_NEVER and probe_sequence <= 0:
            raise ValueError("rtt_observation_probe_sequence_required")
        if probe_status == PROBE_OK:
            if value_ms is None or last_probe_at <= 0.0 or failure_reason:
                raise ValueError("rtt_observation_success_contract")
        if probe_status == PROBE_REACHABLE_ONLY:
            if last_probe_at <= 0.0 or failure_reason:
                raise ValueError("rtt_observation_reachable_only_contract")
        if probe_status in {PROBE_FAILED, PROBE_UNKNOWN}:
            if last_probe_at <= 0.0 or not failure_reason:
                raise ValueError("rtt_observation_failure_contract")
        object.__setattr__(self, "value_ms", value_ms)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "snapshot_at", snapshot_at)
        object.__setattr__(self, "last_probe_at", last_probe_at)
        object.__setattr__(self, "freshness", freshness)
        object.__setattr__(self, "probe_status", probe_status)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "failure_reason", failure_reason)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "probe_sequence", probe_sequence)
        if self.schema != SCHEMA or self.owner != OWNER:
            raise ValueError("rtt_observation_owner_schema_invalid")

    @classmethod
    def unavailable(
        cls,
        *,
        namespace: RuntimeGenerationNamespace,
        snapshot_at: float,
        source: str = "modem_live_rtt",
    ) -> "RttObservation":
        return cls(
            namespace=namespace,
            probe_sequence=0,
            value_ms=None,
            observed_at=0.0,
            snapshot_at=snapshot_at,
            freshness=UNAVAILABLE,
            probe_status=PROBE_NEVER,
            target="",
            last_probe_at=0.0,
            source=source,
        )

    @classmethod
    def from_owner_state(
        cls,
        *,
        namespace: RuntimeGenerationNamespace,
        probe_sequence: int,
        value_ms: float | None,
        observed_at: float,
        snapshot_at: float,
        max_age_s: float,
        probe_status: str,
        target: str,
        last_probe_at: float,
        failure_reason: str,
        source: str = "modem_live_rtt",
    ) -> "RttObservation":
        if value_ms is not None and type(value_ms) is not float:
            raise TypeError("rtt_observation_value_ms_type_invalid")
        value = value_ms
        observed = _finite_nonnegative(
            observed_at,
            field="observed_at",
        )
        snap = _finite_nonnegative(snapshot_at, field="snapshot_at")
        maximum_age = _finite_nonnegative(max_age_s, field="max_age_s")
        if value is None:
            freshness = UNAVAILABLE
        else:
            age_s = max(0.0, snap - observed)
            freshness = (
                FRESH
                if maximum_age <= 0.0
                or age_s <= maximum_age
                else STALE
            )
        return cls(
            namespace=namespace,
            probe_sequence=probe_sequence,
            value_ms=value,
            observed_at=observed,
            snapshot_at=snap,
            freshness=freshness,
            probe_status=probe_status,
            target=target,
            last_probe_at=last_probe_at,
            failure_reason=failure_reason,
            source=source,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RttObservation":
        if not isinstance(raw, Mapping):
            raise TypeError("rtt_observation_mapping_required")
        row = dict(raw)
        if set(row) != {
            "schema",
            "owner",
            "namespace",
            "probe_sequence",
            "value_ms",
            "observed_at",
            "snapshot_at",
            "freshness",
            "probe_status",
            "target",
            "last_probe_at",
            "failure_reason",
            "source",
        }:
            raise ValueError("rtt_observation_fields_invalid")
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("rtt_observation_owner_schema_invalid")
        return cls(
            namespace=RuntimeGenerationNamespace.from_mapping(
                _mapping(row.get("namespace"))
            ),
            probe_sequence=row["probe_sequence"],
            value_ms=row["value_ms"],
            observed_at=row["observed_at"],
            snapshot_at=row["snapshot_at"],
            freshness=row["freshness"],
            probe_status=row["probe_status"],
            target=row["target"],
            last_probe_at=row["last_probe_at"],
            failure_reason=row["failure_reason"],
            source=row["source"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "namespace": self.namespace.as_dict(),
            "probe_sequence": self.probe_sequence,
            "value_ms": self.value_ms,
            "observed_at": self.observed_at,
            "snapshot_at": self.snapshot_at,
            "freshness": self.freshness,
            "probe_status": self.probe_status,
            "target": self.target,
            "last_probe_at": self.last_probe_at,
            "failure_reason": self.failure_reason,
            "source": self.source,
        }

    @property
    def has_fresh_sample(self) -> bool:
        return (
            self.value_ms is not None
            and self.freshness == FRESH
            and self.probe_status == PROBE_OK
        )

    def is_new_sample_after(self, previous: RttObservation | None) -> bool:
        """Use the existing probe identity, without discarding its validity.

        Consumers keep their last consumed observation. A fresh repeated probe
        remains usable, but cannot advance a measurement or learning window.
        """
        if previous is not None and not isinstance(previous, RttObservation):
            raise TypeError("rtt_previous_observation_required")
        if not self.has_fresh_sample:
            return False
        if previous is None or self.namespace != previous.namespace:
            return True
        return self.probe_sequence > previous.probe_sequence


@dataclass(frozen=True, slots=True)
class MatrixBoundRttObservation:
    """One RTT owner snapshot bound to one exact committed Matrix."""

    matrix_identity: MatrixGenerationIdentity
    observation: RttObservation
    bound_at: float
    schema: str = "MatrixBoundRttObservation.v1"
    owner: str = OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("matrix_bound_rtt_matrix_identity_required")
        if not isinstance(self.observation, RttObservation):
            raise TypeError("matrix_bound_rtt_observation_required")
        if self.observation.namespace != self.matrix_identity.namespace:
            raise ValueError("matrix_bound_rtt_namespace_mismatch")
        bound_at = _finite_nonnegative(self.bound_at, field="bound_at")
        if bound_at <= 0.0:
            raise ValueError("matrix_bound_rtt_bound_at_required")
        if self.observation.snapshot_at > bound_at:
            raise ValueError("matrix_bound_rtt_future_snapshot")
        if self.schema != "MatrixBoundRttObservation.v1" or self.owner != OWNER:
            raise ValueError("matrix_bound_rtt_owner_schema_invalid")
        object.__setattr__(self, "bound_at", bound_at)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "MatrixBoundRttObservation":
        if not isinstance(raw, Mapping):
            raise TypeError("matrix_bound_rtt_mapping_required")
        row = dict(raw)
        if set(row) != {
            "schema",
            "owner",
            "matrix_identity",
            "observation",
            "bound_at",
        }:
            raise ValueError("matrix_bound_rtt_fields_invalid")
        return cls(
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                _mapping(row.get("matrix_identity"))
            ),
            observation=RttObservation.from_mapping(
                _mapping(row.get("observation"))
            ),
            bound_at=row["bound_at"],
            schema=row["schema"],
            owner=row["owner"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "observation": self.observation.as_dict(),
            "bound_at": self.bound_at,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("rtt_observation_namespace_mapping_required")
    return value


__all__ = [
    "FRESH",
    "MatrixBoundRttObservation",
    "OWNER",
    "PROBE_FAILED",
    "PROBE_NEVER",
    "PROBE_OK",
    "PROBE_REACHABLE_ONLY",
    "PROBE_UNKNOWN",
    "RttObservation",
    "SCHEMA",
    "STALE",
    "UNAVAILABLE",
]
