"""Exact action-scoped user-reachability evidence.

Modem management reads, radio-target readback, and user Internet reachability
are different facts.  This contract carries only the last one.  It is bound to
one command transaction and can never turn missing measurement into a numeric
zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left
from enum import Enum
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

ACTION_REACHABILITY_SCHEMA = "CoreActionReachabilityReceipt.v2"
ACTION_REACHABILITY_OWNER = "54guc.core.action_reachability"
ACTION_REACHABILITY_UNAVAILABLE_SCHEMA = (
    "CommandActionReachabilityUnavailable.v2"
)
ACTION_REACHABILITY_UNAVAILABLE_OWNER = (
    "arch4.command_transactions.CommandAuthority"
)
ACTION_REACHABILITY_SYSTEM_FAULT_SCHEMA = (
    "CommandActionReachabilitySystemFault.v1"
)
ACTION_REACHABILITY_PENDING_SCHEMA = (
    "CommandActionReachabilityPending.v1"
)
CURRENT_REACHABILITY_SCHEMA = "CoreCurrentReachabilityView.v2"
CURRENT_REACHABILITY_REGISTER_SCHEMA = "CoreInternetReachabilityRegister.v3"
REACHABILITY_EVIDENCE_SCHEMA = "CoreReachabilityEvidenceView.v5"
REACHABILITY_INTERVAL_SCHEMA = "CoreReachabilityIntervalReceipt.v5"


class ActionReachabilityDisposition(str, Enum):
    MEASURED = "measured"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    SYSTEM_FAULT = "system_fault"


class CurrentReachabilityDisposition(str, Enum):
    REACHABLE = "reachable"
    OUTAGE = "outage"
    UNAVAILABLE = "unavailable"


class ReachabilityIntervalCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ReachabilityPlannedActionInterval:
    """One intentional radio-action interval excluded from service grading."""

    action_id: str
    started_at: float
    ended_at: float

    def __post_init__(self) -> None:
        action_id = _required_text(
            self.action_id,
            "reachability_planned_action_id_required",
        )
        started_at = _finite_positive(
            self.started_at,
            "reachability_planned_action_start_invalid",
        )
        ended_at = _finite_positive(
            self.ended_at,
            "reachability_planned_action_end_invalid",
        )
        if ended_at < started_at:
            raise ValueError("reachability_planned_action_order_invalid")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "ended_at", ended_at)

    def contains(self, probed_at: float) -> bool:
        stamp = _finite_positive(
            probed_at,
            "reachability_planned_action_probe_time_invalid",
        )
        return self.started_at <= stamp <= self.ended_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ReachabilityPlannedActionInterval":
        row = dict(raw or {})
        if set(row) != {"action_id", "started_at", "ended_at"}:
            raise ValueError("reachability_planned_action_fields_invalid")
        return cls(
            action_id=row["action_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )


@dataclass(frozen=True, slots=True)
class ReachabilityCoverageGap:
    """Unmeasured time between probes, not a measured Internet outage."""

    last_probe_at: float
    resumed_at: float

    def __post_init__(self) -> None:
        start = _finite_positive(self.last_probe_at, "reachability_gap_start_invalid")
        end = _finite_positive(self.resumed_at, "reachability_gap_end_invalid")
        if end <= start:
            raise ValueError("reachability_gap_order_invalid")
        object.__setattr__(self, "last_probe_at", start)
        object.__setattr__(self, "resumed_at", end)

    def as_dict(self) -> dict[str, float]:
        return {"last_probe_at": self.last_probe_at, "resumed_at": self.resumed_at}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReachabilityCoverageGap":
        if set(value) != {"last_probe_at", "resumed_at"}:
            raise ValueError("reachability_gap_fields_invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class CoreCurrentReachabilityView:
    """Current continuous reachability derived by Core's sole RTT timeline."""

    session_scope_key: str
    identity_code: str
    observer_id: str
    register_sequence: int
    observed_at: float
    latest_probe_at: float
    disposition: CurrentReachabilityDisposition
    outage_started_at: float = 0.0
    outage_s: float = 0.0
    latest_completed_outage: "ReachabilityOutageInterval | None" = None
    latest_coverage_gap: ReachabilityCoverageGap | None = None
    reason: str = ""
    max_probe_gap_s: float = 0.0
    schema: str = CURRENT_REACHABILITY_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != CURRENT_REACHABILITY_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError("current_reachability_owner_schema_invalid")
        if not isinstance(self.disposition, CurrentReachabilityDisposition):
            raise TypeError("current_reachability_disposition_required")
        for field_name in ("session_scope_key", "identity_code", "observer_id"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"current_reachability_{field_name}_string_required")
            if value != value.strip():
                raise ValueError(f"current_reachability_{field_name}_invalid")
        if type(self.register_sequence) is not int or self.register_sequence < 0:
            raise TypeError("current_reachability_register_sequence_invalid")
        observed = _finite_positive(
            self.observed_at,
            "current_reachability_observed_at_invalid",
        )
        latest = _finite_nonnegative(
            self.latest_probe_at,
            "current_reachability_latest_probe_at_invalid",
        )
        outage_started = _finite_nonnegative(
            self.outage_started_at,
            "current_reachability_outage_started_at_invalid",
        )
        outage = _finite_nonnegative(
            self.outage_s,
            "current_reachability_outage_s_invalid",
        )
        latest_completed_outage = self.latest_completed_outage
        if self.latest_coverage_gap is not None:
            if not isinstance(self.latest_coverage_gap, ReachabilityCoverageGap):
                raise TypeError("current_reachability_gap_typed_required")
            if self.latest_coverage_gap.resumed_at > observed:
                raise ValueError("current_reachability_gap_after_observation")
        if latest_completed_outage is not None and not isinstance(
            latest_completed_outage,
            ReachabilityOutageInterval,
        ):
            raise TypeError(
                "current_reachability_latest_completed_outage_typed_required"
            )
        max_gap = _finite_positive(
            self.max_probe_gap_s,
            "current_reachability_max_probe_gap_invalid",
        )
        if latest > observed:
            raise ValueError("current_reachability_probe_after_observation")
        if self.disposition is CurrentReachabilityDisposition.OUTAGE:
            if not self.session_scope_key or not self.identity_code or not self.observer_id:
                raise ValueError("current_reachability_outage_scope_required")
            if outage_started <= 0.0 or observed < outage_started:
                raise ValueError("current_reachability_outage_start_required")
            if abs(outage - max(0.0, observed - outage_started)) > 0.000_001:
                raise ValueError("current_reachability_outage_duration_mismatch")
            if self.reason:
                raise ValueError("current_reachability_outage_reason_forbidden")
        else:
            if outage_started != 0.0 or outage != 0.0:
                raise ValueError("current_reachability_non_outage_has_duration")
            if (
                self.disposition is CurrentReachabilityDisposition.UNAVAILABLE
                and (not self.reason or self.reason != self.reason.strip())
            ):
                raise ValueError("current_reachability_unavailable_reason_required")
            if (
                self.disposition is CurrentReachabilityDisposition.REACHABLE
                and self.reason
            ):
                raise ValueError("current_reachability_reachable_reason_forbidden")
        if (
            latest_completed_outage is not None
            and latest_completed_outage.restored_at > observed
        ):
            raise ValueError(
                "current_reachability_completed_outage_after_observation"
            )
        if (
            self.disposition is CurrentReachabilityDisposition.OUTAGE
            and latest_completed_outage is not None
            and latest_completed_outage.restored_at > outage_started
        ):
            raise ValueError(
                "current_reachability_completed_outage_overlaps_current"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "latest_probe_at", latest)
        object.__setattr__(self, "outage_started_at", outage_started)
        object.__setattr__(self, "outage_s", outage)
        object.__setattr__(self, "max_probe_gap_s", max_gap)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "session_scope_key": self.session_scope_key,
            "identity_code": self.identity_code,
            "observer_id": self.observer_id,
            "register_sequence": self.register_sequence,
            "observed_at": self.observed_at,
            "latest_probe_at": self.latest_probe_at,
            "disposition": self.disposition.value,
            "outage_started_at": self.outage_started_at,
            "outage_s": self.outage_s,
            "latest_completed_outage": (
                self.latest_completed_outage.as_dict()
                if self.latest_completed_outage is not None
                else {}
            ),
            "latest_coverage_gap": self.latest_coverage_gap.as_dict() if self.latest_coverage_gap else {},
            "reason": self.reason,
            "max_probe_gap_s": self.max_probe_gap_s,
        }

    def as_register_dict(self) -> dict[str, Any]:
        """Return current state without pretending it is an interval percentage."""

        return {
            **self.as_dict(),
            "schema": CURRENT_REACHABILITY_REGISTER_SCHEMA,
        }


def validate_current_reachability_register(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Reject any register payload not produced by the typed Core view."""

    row = dict(value or {})
    expected = {
        "schema",
        "owner",
        "session_scope_key",
        "identity_code",
        "observer_id",
        "register_sequence",
        "observed_at",
        "latest_probe_at",
        "disposition",
        "outage_started_at",
        "outage_s",
        "latest_completed_outage",
        "latest_coverage_gap",
        "reason",
        "max_probe_gap_s",
    }
    if set(row) != expected or row.get("schema") != CURRENT_REACHABILITY_REGISTER_SCHEMA:
        raise ValueError("current_reachability_register_contract_invalid")
    completed_raw = row["latest_completed_outage"]
    if not isinstance(completed_raw, Mapping):
        raise TypeError(
            "current_reachability_latest_completed_outage_mapping_required"
        )
    view = CoreCurrentReachabilityView(
        session_scope_key=row["session_scope_key"],
        identity_code=row["identity_code"],
        observer_id=row["observer_id"],
        register_sequence=row["register_sequence"],
        observed_at=row["observed_at"],
        latest_probe_at=row["latest_probe_at"],
        disposition=CurrentReachabilityDisposition(row["disposition"]),
        outage_started_at=row["outage_started_at"],
        outage_s=row["outage_s"],
        latest_completed_outage=(
            ReachabilityOutageInterval.from_mapping(completed_raw)
            if completed_raw
            else None
        ),
        latest_coverage_gap=(ReachabilityCoverageGap.from_mapping(row["latest_coverage_gap"]) if row["latest_coverage_gap"] else None),
        reason=row["reason"],
        max_probe_gap_s=row["max_probe_gap_s"],
    )
    canonical = view.as_register_dict()
    if row != canonical:
        raise ValueError("current_reachability_register_value_invalid")
    return canonical


@dataclass(frozen=True, slots=True)
class ReachabilityActivityProof:
    """Positive Internet evidence. This is not a ping or a latency sample."""

    confirmed_at: float
    source: str
    evidence_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmed_at", _finite_positive(
            self.confirmed_at, "reachability_activity_time_invalid"))
        if self.source not in {"wan_rx", "external_response", "verification_reply"}:
            raise ValueError("reachability_activity_source_invalid")
        _required_text(self.evidence_id, "reachability_activity_identity_required")

    def as_dict(self) -> dict[str, Any]:
        return {"confirmed_at": self.confirmed_at, "source": self.source,
                "evidence_id": self.evidence_id}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReachabilityActivityProof":
        if set(raw) != {"confirmed_at", "source", "evidence_id"}:
            raise ValueError("reachability_activity_fields_invalid")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ReachabilityServiceState:
    """One Core NET decision, not a fabricated ping result."""

    observed_at: float
    reachable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _finite_positive(
            self.observed_at, "reachability_service_time_invalid"))
        if type(self.reachable) is not bool:
            raise TypeError("reachability_service_bool_required")

    def as_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "reachable": self.reachable}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReachabilityServiceState":
        if set(raw) != {"observed_at", "reachable"}:
            raise ValueError("reachability_service_fields_invalid")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CoreReachabilityEvidenceView:
    """Immutable bounded copy of Core's sole Internet-attempt timeline."""

    session_scope_key: str
    identity_code: str
    observer_id: str
    register_sequence: int
    observed_at: float
    max_probe_gap_s: float
    samples: tuple["ReachabilityProbeSample", ...]
    planned_action_intervals: tuple[ReachabilityPlannedActionInterval, ...] = ()
    activity_proofs: tuple[ReachabilityActivityProof, ...] = ()
    service_states: tuple[ReachabilityServiceState, ...] = ()
    schema: str = REACHABILITY_EVIDENCE_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != REACHABILITY_EVIDENCE_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError("reachability_evidence_owner_schema_invalid")
        for field_name in ("session_scope_key", "identity_code", "observer_id"):
            value = getattr(self, field_name)
            if type(value) is not str or value != value.strip():
                raise TypeError(f"reachability_evidence_{field_name}_invalid")
        if type(self.register_sequence) is not int or self.register_sequence < 0:
            raise TypeError("reachability_evidence_register_sequence_invalid")
        observed_at = _finite_positive(
            self.observed_at,
            "reachability_evidence_observed_at_invalid",
        )
        max_probe_gap_s = _finite_positive(
            self.max_probe_gap_s,
            "reachability_evidence_max_probe_gap_invalid",
        )
        samples = tuple(self.samples)
        if any(not isinstance(item, ReachabilityProbeSample) for item in samples):
            raise TypeError("reachability_evidence_typed_samples_required")
        if any(
            left.probed_at >= right.probed_at
            for left, right in zip(samples, samples[1:])
        ):
            raise ValueError("reachability_evidence_probe_order_invalid")
        if samples and samples[-1].probed_at > observed_at:
            raise ValueError("reachability_evidence_probe_after_observation")
        if samples and (
            not self.session_scope_key
            or not self.identity_code
            or not self.observer_id
            or any(item.source != self.observer_id for item in samples)
        ):
            raise ValueError("reachability_evidence_scope_invalid")
        planned_action_intervals = tuple(self.planned_action_intervals)
        if any(
            not isinstance(item, ReachabilityPlannedActionInterval)
            for item in planned_action_intervals
        ):
            raise TypeError("reachability_evidence_planned_actions_typed_required")
        if any(
            left.started_at > right.started_at
            for left, right in zip(
                planned_action_intervals,
                planned_action_intervals[1:],
            )
        ):
            raise ValueError("reachability_evidence_planned_actions_order_invalid")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "max_probe_gap_s", max_probe_gap_s)
        object.__setattr__(self, "samples", samples)
        proofs = tuple(self.activity_proofs)
        if any(not isinstance(p, ReachabilityActivityProof) for p in proofs):
            raise TypeError("reachability_activity_typed_required")
        if any(p.confirmed_at > observed_at for p in proofs) or any(
            a.confirmed_at > b.confirmed_at for a, b in zip(proofs, proofs[1:])
        ):
            raise ValueError("reachability_activity_order_invalid")
        if proofs and not (self.session_scope_key and self.identity_code and self.observer_id):
            raise ValueError("reachability_activity_scope_required")
        object.__setattr__(self, "activity_proofs", proofs)
        states = tuple(self.service_states)
        if (samples or proofs) and not states:
            raise ValueError("reachability_service_states_required")
        if any(not isinstance(s, ReachabilityServiceState) for s in states):
            raise TypeError("reachability_service_states_typed_required")
        if any(s.observed_at > observed_at for s in states) or any(
            a.observed_at >= b.observed_at or a.reachable == b.reachable
            for a, b in zip(states, states[1:])
        ):
            raise ValueError("reachability_service_states_order_invalid")
        if states and not (self.session_scope_key and self.identity_code and self.observer_id):
            raise ValueError("reachability_service_states_scope_required")
        object.__setattr__(self, "service_states", states)
        object.__setattr__(
            self,
            "planned_action_intervals",
            planned_action_intervals,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "session_scope_key": self.session_scope_key,
            "identity_code": self.identity_code,
            "observer_id": self.observer_id,
            "register_sequence": self.register_sequence,
            "observed_at": self.observed_at,
            "max_probe_gap_s": self.max_probe_gap_s,
            "samples": [sample.as_dict() for sample in self.samples],
            "activity_proofs": [proof.as_dict() for proof in self.activity_proofs],
            "service_states": [state.as_dict() for state in self.service_states],
            "planned_action_intervals": [
                interval.as_dict()
                for interval in self.planned_action_intervals
            ],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CoreReachabilityEvidenceView":
        row = dict(raw or {})
        if set(row) != {
            "schema",
            "owner",
            "session_scope_key",
            "identity_code",
            "observer_id",
            "register_sequence",
            "observed_at",
            "max_probe_gap_s",
            "samples",
            "activity_proofs",
            "service_states",
            "planned_action_intervals",
        }:
            raise ValueError("reachability_evidence_fields_invalid")
        samples = row["samples"]
        if not isinstance(samples, list):
            raise TypeError("reachability_evidence_samples_list_required")
        planned_action_intervals = row["planned_action_intervals"]
        if not isinstance(planned_action_intervals, list):
            raise TypeError(
                "reachability_evidence_planned_actions_list_required"
            )
        return cls(
            schema=row["schema"],
            owner=row["owner"],
            session_scope_key=row["session_scope_key"],
            identity_code=row["identity_code"],
            observer_id=row["observer_id"],
            register_sequence=row["register_sequence"],
            observed_at=row["observed_at"],
            max_probe_gap_s=row["max_probe_gap_s"],
            samples=tuple(ReachabilityProbeSample.from_mapping(item) for item in samples),
            activity_proofs=tuple(ReachabilityActivityProof.from_mapping(item)
                                  for item in row["activity_proofs"]),
            service_states=tuple(ReachabilityServiceState.from_mapping(item)
                                 for item in row["service_states"]),
            planned_action_intervals=tuple(
                ReachabilityPlannedActionInterval.from_mapping(item)
                for item in planned_action_intervals
            ),
        )


@dataclass(frozen=True, slots=True)
class CoreReachabilityIntervalReceipt:
    """Core-owned availability result for one requested wall-clock interval."""

    session_scope_key: str
    identity_code: str
    observer_id: str
    register_sequence: int
    interval_started_at: float
    interval_ended_at: float
    evaluated_at: float
    coverage: ReachabilityIntervalCoverage
    attempt_count: int
    excluded_attempt_count: int
    success_count: int
    failure_count: int
    availability_pct: float | None
    latest_reachable: bool | None
    measured_seconds: float
    reason: str = ""
    schema: str = REACHABILITY_INTERVAL_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != REACHABILITY_INTERVAL_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError("reachability_interval_owner_schema_invalid")
        if not isinstance(self.coverage, ReachabilityIntervalCoverage):
            raise TypeError("reachability_interval_coverage_required")
        for field_name in ("session_scope_key", "identity_code", "observer_id"):
            value = getattr(self, field_name)
            if type(value) is not str or value != value.strip():
                raise TypeError(f"reachability_interval_{field_name}_invalid")
        if type(self.register_sequence) is not int or self.register_sequence < 0:
            raise TypeError("reachability_interval_register_sequence_invalid")
        started_at = _finite_positive(
            self.interval_started_at,
            "reachability_interval_start_invalid",
        )
        ended_at = _finite_positive(
            self.interval_ended_at,
            "reachability_interval_end_invalid",
        )
        evaluated_at = _finite_positive(
            self.evaluated_at,
            "reachability_interval_evaluated_at_invalid",
        )
        if ended_at < started_at or evaluated_at < ended_at:
            raise ValueError("reachability_interval_time_order_invalid")
        if (type(self.measured_seconds) not in {int, float}
                or not math.isfinite(self.measured_seconds)
                or not 0 <= self.measured_seconds <= ended_at - started_at):
            raise ValueError("reachability_interval_measured_seconds_invalid")
        for field_name in (
            "attempt_count",
            "excluded_attempt_count",
            "success_count",
            "failure_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(f"reachability_interval_{field_name}_invalid")
        if self.success_count + self.failure_count != self.attempt_count:
            raise ValueError("reachability_interval_attempt_count_mismatch")
        if self.latest_reachable is not None and type(self.latest_reachable) is not bool:
            raise TypeError("reachability_interval_latest_state_invalid")
        if self.coverage is ReachabilityIntervalCoverage.UNAVAILABLE:
            if self.availability_pct is not None or not self.reason:
                raise ValueError("reachability_interval_unavailable_contract_invalid")
        else:
            if (
                type(self.availability_pct) not in {int, float}
                or isinstance(self.availability_pct, bool)
                or not 0.0 <= float(self.availability_pct) <= 100.0
                or self.latest_reachable is None
                or self.reason
            ):
                raise ValueError("reachability_interval_available_contract_invalid")
        object.__setattr__(self, "interval_started_at", started_at)
        object.__setattr__(self, "interval_ended_at", ended_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "session_scope_key": self.session_scope_key,
            "identity_code": self.identity_code,
            "observer_id": self.observer_id,
            "register_sequence": self.register_sequence,
            "interval_started_at": self.interval_started_at,
            "interval_ended_at": self.interval_ended_at,
            "evaluated_at": self.evaluated_at,
            "coverage": self.coverage.value,
            "attempt_count": self.attempt_count,
            "excluded_attempt_count": self.excluded_attempt_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "availability_pct": self.availability_pct,
            "latest_reachable": self.latest_reachable,
            "measured_seconds": self.measured_seconds,
            "reason": self.reason,
        }


def core_reachability_interval_receipt(
    evidence: CoreReachabilityEvidenceView,
    *,
    interval_started_at: float,
    interval_ended_at: float,
    complete: bool,
) -> CoreReachabilityIntervalReceipt:
    """Measure the same Core NET decisions shown live, excluding planned pauses."""

    if not isinstance(evidence, CoreReachabilityEvidenceView):
        raise TypeError("reachability_interval_evidence_required")
    if type(complete) is not bool:
        raise TypeError("reachability_interval_complete_bool_required")
    start = _finite_positive(interval_started_at, "reachability_interval_start_invalid")
    end = _finite_positive(interval_ended_at, "reachability_interval_end_invalid")
    if end < start:
        raise ValueError("reachability_interval_order_invalid")
    states = evidence.service_states
    stamps = tuple(s.observed_at for s in states)

    def state_at(stamp: float) -> bool | None:
        index = bisect_left(stamps, stamp)
        if index < len(stamps) and stamps[index] == stamp:
            return states[index].reachable
        return states[index - 1].reachable if index else None

    def planned(stamp: float) -> bool:
        return any(p.contains(stamp) for p in evidence.planned_action_intervals)

    # Sparse state transitions cover time, not the number of replies. A slow
    # check must not remove time from Referee or create another NET decision.
    boundaries = sorted({start, end} | {
        s.observed_at for s in states if start < s.observed_at < end
    } | {
        t for p in evidence.planned_action_intervals
        for t in (p.started_at, p.ended_at) if start < t < end
    })
    up_s = measured_s = 0.0
    uncovered = False
    for left, right in zip(boundaries, boundaries[1:]):
        middle = (left + right) / 2.0
        if planned(middle):
            continue
        state = state_at(middle)
        if state is None:
            uncovered = True
            continue
        measured_s += right - left
        if state:
            up_s += right - left
    selected = tuple(s for s in evidence.samples if start <= s.probed_at <= end)
    eligible = tuple(s for s in selected if not planned(s.probed_at))
    success_count = sum(state_at(s.probed_at) is True for s in eligible)
    failure_count = sum(state_at(s.probed_at) is False for s in eligible)
    latest = state_at(end)
    reason = ""
    if end > evidence.observed_at:
        reason = "reachability_evidence_not_observed_through_interval"
    elif not evidence.session_scope_key or not evidence.observer_id:
        reason = "reachability_evidence_scope_unavailable"
    elif uncovered or latest is None:
        reason = "reachability_interval_start_not_covered"
    elif (end > start and not measured_s) or (end == start and planned(end)):
        reason = "reachability_interval_only_planned_action_evidence"
    availability = (100.0 * up_s / measured_s if measured_s else
                    100.0 if latest is True else 0.0)
    return CoreReachabilityIntervalReceipt(
        session_scope_key=evidence.session_scope_key,
        identity_code=evidence.identity_code, observer_id=evidence.observer_id,
        register_sequence=evidence.register_sequence,
        interval_started_at=start, interval_ended_at=end,
        evaluated_at=max(evidence.observed_at, end),
        coverage=(ReachabilityIntervalCoverage.UNAVAILABLE if reason else
                  ReachabilityIntervalCoverage.COMPLETE if complete else
                  ReachabilityIntervalCoverage.PARTIAL),
        attempt_count=success_count + failure_count,
        excluded_attempt_count=len(selected) - len(eligible),
        success_count=success_count, failure_count=failure_count,
        availability_pct=None if reason else availability,
        latest_reachable=latest, reason=reason,
        measured_seconds=0.0 if reason else measured_s,
    )


def reachability_probe_gap_is_covered(
    older: ReachabilityProbeSample,
    newer: ReachabilityProbeSample,
    *,
    max_probe_gap_s: float,
) -> bool:
    """Accept one bounded failed attempt without accepting a missing probe lane.

    A successful probe normally completes inside one cadence allowance. A failed
    attempt can consume that cadence plus one bounded timeout. The failure is
    explicit evidence, not a missing sample. More than two normal gaps remains a
    continuity failure.
    """

    if not isinstance(older, ReachabilityProbeSample) or not isinstance(
        newer,
        ReachabilityProbeSample,
    ):
        raise TypeError("reachability_probe_gap_sample_required")
    max_gap = _finite_positive(
        max_probe_gap_s,
        "reachability_probe_gap_limit_invalid",
    )
    gap = newer.probed_at - older.probed_at
    if gap < 0.0:
        raise ValueError("reachability_probe_gap_order_invalid")
    allowed = max_gap * (
        2.0
        if older.service_reachable is not True
        or newer.service_reachable is not True
        else 1.0
    )
    return gap <= allowed


def _required_text(value: Any, reason: str) -> str:
    if type(value) is not str:
        raise TypeError(reason)
    if not value or value != value.strip():
        raise ValueError(reason)
    return value


def _finite_positive(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(reason)
    return number


def _finite_nonnegative(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(reason)
    return number


@dataclass(frozen=True, slots=True)
class ReachabilityProbeSample:
    probed_at: float
    reachable: bool | None
    source: str
    target: str
    service_reachable: bool | None
    failure_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "probed_at",
            _finite_positive(
                self.probed_at,
                "action_reachability_probe_timestamp_invalid",
            ),
        )
        if self.reachable is not None and type(self.reachable) is not bool:
            raise TypeError("action_reachability_probe_result_bool_required")
        if (
            self.service_reachable is not None
            and type(self.service_reachable) is not bool
        ):
            raise TypeError("action_reachability_service_result_bool_required")
        object.__setattr__(
            self,
            "source",
            _required_text(
                self.source,
                "action_reachability_probe_source_required",
            ),
        )
        object.__setattr__(
            self,
            "target",
            _required_text(
                self.target,
                "action_reachability_probe_target_required",
            ),
        )
        if type(self.failure_reason) is not str:
            raise TypeError(
                "action_reachability_probe_failure_reason_string_required"
            )
        reason = self.failure_reason
        if self.reachable and reason != "":
            raise ValueError("action_reachability_success_has_failure_reason")
        if not self.reachable and (
            not reason or reason != reason.strip()
        ):
            raise ValueError(
                "action_reachability_failure_reason_required"
            )
        # A PC reply may use another network while the selected modem is absent.
        # Core retains that raw success without claiming verified modem service.
        if self.service_reachable is False and self.reachable is not False:
            raise ValueError("action_reachability_outage_requires_failed_reply")

    def as_dict(self) -> dict[str, Any]:
        return {
            "probed_at": self.probed_at,
            "reachable": self.reachable,
            "source": self.source,
            "target": self.target,
            "service_reachable": self.service_reachable,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ReachabilityProbeSample":
        row = dict(raw or {})
        if set(row) != {
            "probed_at",
            "reachable",
            "source",
            "target",
            "service_reachable",
            "failure_reason",
        }:
            raise ValueError("action_reachability_probe_fields_invalid")
        return cls(
            probed_at=row["probed_at"],
            reachable=row["reachable"],
            source=row["source"],
            target=row["target"],
            service_reachable=row["service_reachable"],
            failure_reason=row["failure_reason"],
        )


@dataclass(frozen=True, slots=True)
class ReachabilityOutageInterval:
    started_at: float
    restored_at: float

    def __post_init__(self) -> None:
        started = _finite_positive(
            self.started_at,
            "action_reachability_outage_start_invalid",
        )
        restored = _finite_positive(
            self.restored_at,
            "action_reachability_outage_restore_invalid",
        )
        if restored < started:
            raise ValueError("action_reachability_outage_order_invalid")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "restored_at", restored)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.restored_at - self.started_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "restored_at": self.restored_at,
            "duration_s": self.duration_s,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ReachabilityOutageInterval":
        row = dict(raw or {})
        if set(row) != {"started_at", "restored_at", "duration_s"}:
            raise ValueError("action_reachability_outage_fields_invalid")
        value = cls(
            started_at=row["started_at"],
            restored_at=row["restored_at"],
        )
        supplied = _finite_nonnegative(
            row["duration_s"],
            "action_reachability_outage_duration_invalid",
        )
        if abs(supplied - value.duration_s) > 0.000_001:
            raise ValueError("action_reachability_outage_duration_mismatch")
        return value


@dataclass(frozen=True, slots=True)
class ActionReachabilityReceipt:
    request_id: str
    transaction_root_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_key: str
    identity_code: str
    observer_id: str
    measurement_started_at: float
    measurement_completed_at: float
    physical_completed_at: float
    max_probe_gap_s: float
    coverage_complete: bool
    samples: tuple[ReachabilityProbeSample, ...]
    outage_intervals: tuple[ReachabilityOutageInterval, ...]
    complete: bool
    disposition: ActionReachabilityDisposition = (
        ActionReachabilityDisposition.MEASURED
    )
    schema: str = ACTION_REACHABILITY_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != ACTION_REACHABILITY_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError("action_reachability_owner_schema_invalid")
        for field_name in (
            "request_id",
            "transaction_root_id",
            "identity_key",
            "identity_code",
            "observer_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name),
                    f"action_reachability_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("action_reachability_matrix_identity_required")
        if self.disposition is not ActionReachabilityDisposition.MEASURED:
            raise ValueError("action_reachability_disposition_must_be_measured")
        started = _finite_positive(
            self.measurement_started_at,
            "action_reachability_measurement_start_invalid",
        )
        completed = _finite_nonnegative(
            self.measurement_completed_at,
            "action_reachability_measurement_completion_invalid",
        )
        physical_completed = _finite_positive(
            self.physical_completed_at,
            "action_reachability_physical_completion_invalid",
        )
        max_gap = _finite_positive(
            self.max_probe_gap_s,
            "action_reachability_max_probe_gap_invalid",
        )
        if physical_completed < started:
            raise ValueError("action_reachability_physical_time_order_invalid")
        if type(self.coverage_complete) is not bool or type(self.complete) is not bool:
            raise TypeError("action_reachability_completion_bool_required")
        samples = tuple(self.samples)
        intervals = tuple(self.outage_intervals)
        if not all(isinstance(item, ReachabilityProbeSample) for item in samples):
            raise TypeError("action_reachability_typed_samples_required")
        if not all(
            isinstance(item, ReachabilityOutageInterval) for item in intervals
        ):
            raise TypeError("action_reachability_typed_intervals_required")
        if any(
            left.probed_at >= right.probed_at
            for left, right in zip(samples, samples[1:])
        ):
            raise ValueError("action_reachability_probe_order_invalid")
        if any(
            left.restored_at > right.started_at
            for left, right in zip(intervals, intervals[1:])
        ):
            raise ValueError("action_reachability_outage_overlap_invalid")
        if self.complete:
            if completed < physical_completed:
                raise ValueError(
                    "action_reachability_completed_before_physical_command"
                )
            if not self.coverage_complete:
                raise ValueError(
                    "action_reachability_complete_without_probe_coverage"
                )
            if not samples or samples[-1].service_reachable is not True:
                raise ValueError(
                    "action_reachability_complete_without_restored_probe"
                )
            if samples[-1].probed_at > completed:
                raise ValueError(
                    "action_reachability_completed_before_last_probe"
                )
        elif completed != 0.0:
            raise ValueError(
                "action_reachability_incomplete_has_completion_timestamp"
            )
        object.__setattr__(self, "measurement_started_at", started)
        object.__setattr__(self, "measurement_completed_at", completed)
        object.__setattr__(self, "physical_completed_at", physical_completed)
        object.__setattr__(self, "max_probe_gap_s", max_gap)
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "outage_intervals", intervals)

    @property
    def total_outage_s(self) -> float | None:
        if not self.complete or not self.coverage_complete:
            return None
        return sum(interval.duration_s for interval in self.outage_intervals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_key": self.identity_key,
            "identity_code": self.identity_code,
            "observer_id": self.observer_id,
            "measurement_started_at": self.measurement_started_at,
            "measurement_completed_at": self.measurement_completed_at,
            "physical_completed_at": self.physical_completed_at,
            "max_probe_gap_s": self.max_probe_gap_s,
            "coverage_complete": self.coverage_complete,
            "samples": [sample.as_dict() for sample in self.samples],
            "outage_intervals": [
                interval.as_dict() for interval in self.outage_intervals
            ],
            "complete": self.complete,
            "disposition": self.disposition.value,
            "total_outage_s": self.total_outage_s,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ActionReachabilityReceipt":
        row = dict(raw or {})
        expected = {
            "schema",
            "owner",
            "request_id",
            "transaction_root_id",
            "matrix_identity",
            "identity_key",
            "identity_code",
            "observer_id",
            "measurement_started_at",
            "measurement_completed_at",
            "physical_completed_at",
            "max_probe_gap_s",
            "coverage_complete",
            "samples",
            "outage_intervals",
            "complete",
            "disposition",
            "total_outage_s",
        }
        if set(row) != expected:
            raise ValueError("action_reachability_receipt_fields_invalid")
        samples_raw = row["samples"]
        intervals_raw = row["outage_intervals"]
        if not isinstance(samples_raw, list) or not isinstance(intervals_raw, list):
            raise TypeError("action_reachability_receipt_lists_required")
        value = cls(
            request_id=row["request_id"],
            transaction_root_id=row["transaction_root_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            identity_key=row["identity_key"],
            identity_code=row["identity_code"],
            observer_id=row["observer_id"],
            measurement_started_at=row["measurement_started_at"],
            measurement_completed_at=row["measurement_completed_at"],
            physical_completed_at=row["physical_completed_at"],
            max_probe_gap_s=row["max_probe_gap_s"],
            coverage_complete=row["coverage_complete"],
            samples=tuple(
                ReachabilityProbeSample.from_mapping(item)
                for item in samples_raw
            ),
            outage_intervals=tuple(
                ReachabilityOutageInterval.from_mapping(item)
                for item in intervals_raw
            ),
            complete=row["complete"],
            disposition=ActionReachabilityDisposition(row["disposition"]),
            schema=row["schema"],
            owner=row["owner"],
        )
        supplied_total = row["total_outage_s"]
        if value.total_outage_s is None:
            if supplied_total is not None:
                raise ValueError(
                    "action_reachability_unmeasured_total_must_be_null"
                )
        else:
            parsed_total = _finite_nonnegative(
                supplied_total,
                "action_reachability_total_outage_invalid",
            )
            if abs(parsed_total - value.total_outage_s) > 0.000_001:
                raise ValueError("action_reachability_total_outage_mismatch")
        return value


@dataclass(frozen=True, slots=True)
class ActionReachabilityUnavailable:
    """Command Authority's terminal proof that bounded evidence did not close."""

    request_id: str
    transaction_root_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_key: str
    identity_code: str
    coverage_required_at: float
    deadline_at: float
    resolved_at: float
    reason: str
    disposition: ActionReachabilityDisposition = (
        ActionReachabilityDisposition.UNAVAILABLE
    )
    schema: str = ACTION_REACHABILITY_UNAVAILABLE_SCHEMA
    owner: str = ACTION_REACHABILITY_UNAVAILABLE_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != ACTION_REACHABILITY_UNAVAILABLE_SCHEMA
            or self.owner != ACTION_REACHABILITY_UNAVAILABLE_OWNER
        ):
            raise ValueError(
                "action_reachability_unavailable_owner_schema_invalid"
            )
        for field_name in (
            "request_id",
            "transaction_root_id",
            "identity_key",
            "identity_code",
            "reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name),
                    f"action_reachability_unavailable_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "action_reachability_unavailable_matrix_identity_required"
            )
        if self.disposition is not ActionReachabilityDisposition.UNAVAILABLE:
            raise ValueError(
                "action_reachability_unavailable_disposition_invalid"
            )
        coverage = _finite_positive(
            self.coverage_required_at,
            "action_reachability_unavailable_coverage_invalid",
        )
        deadline = _finite_positive(
            self.deadline_at,
            "action_reachability_unavailable_deadline_invalid",
        )
        resolved = _finite_positive(
            self.resolved_at,
            "action_reachability_unavailable_resolved_invalid",
        )
        if deadline < coverage:
            raise ValueError(
                "action_reachability_unavailable_deadline_before_coverage"
            )
        if resolved < coverage:
            raise ValueError(
                "action_reachability_unavailable_resolved_before_coverage"
            )
        object.__setattr__(self, "coverage_required_at", coverage)
        object.__setattr__(self, "deadline_at", deadline)
        object.__setattr__(self, "resolved_at", resolved)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_key": self.identity_key,
            "identity_code": self.identity_code,
            "coverage_required_at": self.coverage_required_at,
            "deadline_at": self.deadline_at,
            "resolved_at": self.resolved_at,
            "reason": self.reason,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ActionReachabilityUnavailable":
        row = dict(raw or {})
        expected = {
            "schema",
            "owner",
            "request_id",
            "transaction_root_id",
            "matrix_identity",
            "identity_key",
            "identity_code",
            "coverage_required_at",
            "deadline_at",
            "resolved_at",
            "reason",
            "disposition",
        }
        if set(row) != expected:
            raise ValueError(
                "action_reachability_unavailable_fields_invalid"
            )
        return cls(
            request_id=row["request_id"],
            transaction_root_id=row["transaction_root_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            identity_key=row["identity_key"],
            identity_code=row["identity_code"],
            coverage_required_at=row["coverage_required_at"],
            deadline_at=row["deadline_at"],
            resolved_at=row["resolved_at"],
            reason=row["reason"],
            disposition=ActionReachabilityDisposition(row["disposition"]),
            schema=row["schema"],
            owner=row["owner"],
        )


@dataclass(frozen=True, slots=True)
class ActionReachabilityPending:
    request_id: str
    transaction_root_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_code: str
    coverage_required_at: float
    deadline_at: float
    observed_at: float
    reason: str
    disposition: ActionReachabilityDisposition = (
        ActionReachabilityDisposition.PENDING
    )
    schema: str = ACTION_REACHABILITY_PENDING_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "transaction_root_id",
            "identity_code",
            "reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name),
                    f"action_reachability_pending_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("action_reachability_pending_matrix_identity_required")
        coverage = _finite_positive(
            self.coverage_required_at,
            "action_reachability_pending_coverage_invalid",
        )
        deadline = _finite_positive(
            self.deadline_at,
            "action_reachability_pending_deadline_invalid",
        )
        observed = _finite_positive(
            self.observed_at,
            "action_reachability_pending_observed_invalid",
        )
        if deadline < coverage or observed >= deadline:
            raise ValueError("action_reachability_pending_time_order_invalid")
        object.__setattr__(self, "coverage_required_at", coverage)
        object.__setattr__(self, "deadline_at", deadline)
        object.__setattr__(self, "observed_at", observed)
        if self.disposition is not ActionReachabilityDisposition.PENDING:
            raise ValueError("action_reachability_pending_disposition_invalid")
        if (
            self.schema != ACTION_REACHABILITY_PENDING_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError(
                "action_reachability_pending_owner_schema_invalid"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_code": self.identity_code,
            "coverage_required_at": self.coverage_required_at,
            "deadline_at": self.deadline_at,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ActionReachabilityPending":
        row = dict(raw or {})
        if set(row) != {
            "schema",
            "owner",
            "request_id",
            "transaction_root_id",
            "matrix_identity",
            "identity_code",
            "coverage_required_at",
            "deadline_at",
            "observed_at",
            "reason",
            "disposition",
        }:
            raise ValueError("action_reachability_pending_fields_invalid")
        return cls(
            request_id=row["request_id"],
            transaction_root_id=row["transaction_root_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            identity_code=row["identity_code"],
            coverage_required_at=row["coverage_required_at"],
            deadline_at=row["deadline_at"],
            observed_at=row["observed_at"],
            reason=row["reason"],
            disposition=ActionReachabilityDisposition(
                row["disposition"]
            ),
            schema=row["schema"],
            owner=row["owner"],
        )


@dataclass(frozen=True, slots=True)
class ActionReachabilitySystemFault:
    request_id: str
    transaction_root_id: str
    matrix_identity: MatrixGenerationIdentity
    operation: str
    error_type: str
    detail: str
    occurred_at: float
    disposition: ActionReachabilityDisposition = (
        ActionReachabilityDisposition.SYSTEM_FAULT
    )
    schema: str = ACTION_REACHABILITY_SYSTEM_FAULT_SCHEMA
    owner: str = ACTION_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "transaction_root_id",
            "operation",
            "error_type",
            "detail",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name),
                    f"action_reachability_system_fault_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "action_reachability_system_fault_matrix_identity_required"
            )
        object.__setattr__(
            self,
            "occurred_at",
            _finite_positive(
                self.occurred_at,
                "action_reachability_system_fault_time_invalid",
            ),
        )
        if self.disposition is not ActionReachabilityDisposition.SYSTEM_FAULT:
            raise ValueError(
                "action_reachability_system_fault_disposition_invalid"
            )
        if (
            self.schema != ACTION_REACHABILITY_SYSTEM_FAULT_SCHEMA
            or self.owner != ACTION_REACHABILITY_OWNER
        ):
            raise ValueError(
                "action_reachability_system_fault_owner_schema_invalid"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "operation": self.operation,
            "error_type": self.error_type,
            "detail": self.detail,
            "occurred_at": self.occurred_at,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ActionReachabilitySystemFault":
        row = dict(raw or {})
        if set(row) != {
            "schema",
            "owner",
            "request_id",
            "transaction_root_id",
            "matrix_identity",
            "operation",
            "error_type",
            "detail",
            "occurred_at",
            "disposition",
        }:
            raise ValueError(
                "action_reachability_system_fault_fields_invalid"
            )
        return cls(
            request_id=row["request_id"],
            transaction_root_id=row["transaction_root_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                row["matrix_identity"]
            ),
            operation=row["operation"],
            error_type=row["error_type"],
            detail=row["detail"],
            occurred_at=row["occurred_at"],
            disposition=ActionReachabilityDisposition(
                row["disposition"]
            ),
            schema=row["schema"],
            owner=row["owner"],
        )


__all__ = (
    "CURRENT_REACHABILITY_SCHEMA",
    "CURRENT_REACHABILITY_REGISTER_SCHEMA",
    "REACHABILITY_EVIDENCE_SCHEMA",
    "REACHABILITY_INTERVAL_SCHEMA",
    "ACTION_REACHABILITY_OWNER",
    "ACTION_REACHABILITY_PENDING_SCHEMA",
    "ACTION_REACHABILITY_SCHEMA",
    "ACTION_REACHABILITY_UNAVAILABLE_OWNER",
    "ACTION_REACHABILITY_UNAVAILABLE_SCHEMA",
    "ACTION_REACHABILITY_SYSTEM_FAULT_SCHEMA",
    "ActionReachabilityDisposition",
    "ActionReachabilityPending",
    "ActionReachabilityUnavailable",
    "ActionReachabilityReceipt",
    "ActionReachabilitySystemFault",
    "CoreCurrentReachabilityView",
    "CoreReachabilityEvidenceView",
    "CoreReachabilityIntervalReceipt",
    "CurrentReachabilityDisposition",
    "ReachabilityIntervalCoverage",
    "ReachabilityOutageInterval",
    "ReachabilityProbeSample",
    "core_reachability_interval_receipt",
    "reachability_probe_gap_is_covered",
    "validate_current_reachability_register",
)
