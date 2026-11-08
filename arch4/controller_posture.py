"""Immutable controller posture publication for one accepted generation.

The controller posture authority owns transition, hysteresis, warning, fault,
and recovery-dwell state.  Consumers receive this immutable publication; they
must not inspect or mutate the authority's private working state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from typing import Any, Iterable, Mapping

from arch4.interfaces import SystemState


SCHEMA = "ControllerPosture.v1"
OWNER = "controller_posture_authority"

BASIS_ACCEPTED_OBSERVATIONS = "accepted_observations"
BASIS_ELAPSED_TIME = "elapsed_time"
_RULE_BASES = frozenset(
    {BASIS_ACCEPTED_OBSERVATIONS, BASIS_ELAPSED_TIME}
)

SESSION_STARTUP_UNBOUND = "startup_unbound"
SESSION_CONTINUED = "continued_session"
SESSION_NEW_RESET = "new_session_reset"
_SESSION_DISPOSITIONS = frozenset(
    {SESSION_STARTUP_UNBOUND, SESSION_CONTINUED, SESSION_NEW_RESET}
)

CONGESTION_AVAILABLE = "available"
CONGESTION_UNAVAILABLE = "unavailable"
_CONGESTION_DISPOSITIONS = frozenset(
    {CONGESTION_AVAILABLE, CONGESTION_UNAVAILABLE}
)


@dataclass(frozen=True, slots=True)
class PostureRuleProgress:
    rule_id: str
    basis: str
    threshold: float
    progress: float
    satisfied: bool
    status: str = "available"

    def __post_init__(self) -> None:
        if not str(self.rule_id or "").strip():
            raise ValueError("controller_posture_rule_id_missing")
        if self.basis not in _RULE_BASES:
            raise ValueError("controller_posture_rule_basis_invalid")
        if not isfinite(float(self.threshold)) or float(self.threshold) < 0.0:
            raise ValueError("controller_posture_rule_threshold_invalid")
        if not isfinite(float(self.progress)) or float(self.progress) < 0.0:
            raise ValueError("controller_posture_rule_progress_invalid")
        if self.status not in {"available", "unavailable", "inactive"}:
            raise ValueError("controller_posture_rule_status_invalid")

    @property
    def unit(self) -> str:
        if self.basis == BASIS_ACCEPTED_OBSERVATIONS:
            return "observations"
        return "seconds"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "basis": self.basis,
            "unit": self.unit,
            "threshold": float(self.threshold),
            "progress": float(self.progress),
            "satisfied": bool(self.satisfied),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ControllerPosture:
    source_generation: int
    source_epoch: str
    session_scope_key: str
    identity_key: str
    previous_state: SystemState
    state: SystemState
    state_changed: bool
    published_ts: float
    entered_at: float
    time_in_state_s: float
    session_disposition: str
    congestion_input_disposition: str
    congestion_state_bucket: str
    congestion_state_avg_pct: float | None
    congestion_kind: str
    warning_class: str
    warning_source: str
    warning_mechanism: str
    warning_reason: str
    fault_class: str
    emergency_class: str
    emergency_origin_class: str
    emergency_reason: str
    degraded_actions_tried: int
    degraded_timeout_at: float
    congested_last_check: float
    congested_action_taken: bool
    emergency_timeout_at: float
    soft_emergency_hold_until: float
    emergency_observe_until: float
    emergency_reentry_guard_until: float
    emergency_recovery_since: float
    emergency_recovery_step: int
    emergency_next_action_at: float
    bad_rf_recovery_since: float
    bad_rf_recovery_step: int
    bad_rf_next_action_at: float
    rf_cliff_event_at: float
    rf_cliff_event_reason: str
    post_fault_class: str
    post_fault_until: float
    recovered_lane_bands: tuple[int, ...]
    recovered_lane_ts: float
    recovered_lane_from_fault: str
    recovered_lane_until: float
    rule_progress: tuple[PostureRuleProgress, ...]
    posture_id: str

    @property
    def progress_scope_id(self) -> str:
        """Stable identity for one posture episode, not one generation."""

        content = {
            "source_epoch": self.source_epoch,
            "session_scope_key": self.session_scope_key,
            "identity_key": self.identity_key,
            "state": self.state.name,
            "entered_at": round(float(self.entered_at), 6),
        }
        return "posture-episode:" + _digest(content)

    def __post_init__(self) -> None:
        if self.state.name == "BLANKING" or self.previous_state.name == "BLANKING":
            raise ValueError("legacy_blanket_blanking_posture_rejected")
        if self.session_disposition not in _SESSION_DISPOSITIONS:
            raise ValueError("controller_posture_session_disposition_invalid")
        if self.congestion_input_disposition not in _CONGESTION_DISPOSITIONS:
            raise ValueError("controller_posture_congestion_disposition_invalid")
        if self.source_generation < 0:
            raise ValueError("controller_posture_generation_invalid")
        if self.session_disposition == SESSION_STARTUP_UNBOUND:
            if self.source_generation != 0:
                raise ValueError("controller_posture_startup_generation_invalid")
        else:
            if self.source_generation <= 0:
                raise ValueError("controller_posture_generation_missing")
            if not self.source_epoch or not self.session_scope_key:
                raise ValueError("controller_posture_generation_scope_missing")
            if not self.identity_key:
                raise ValueError("controller_posture_identity_scope_missing")
        for value in (
            self.published_ts,
            self.entered_at,
            self.time_in_state_s,
            self.degraded_timeout_at,
            self.congested_last_check,
            self.emergency_timeout_at,
            self.soft_emergency_hold_until,
            self.emergency_observe_until,
            self.emergency_reentry_guard_until,
            self.emergency_recovery_since,
            self.emergency_next_action_at,
            self.bad_rf_recovery_since,
            self.bad_rf_next_action_at,
            self.rf_cliff_event_at,
            self.post_fault_until,
            self.recovered_lane_ts,
            self.recovered_lane_until,
        ):
            if not isfinite(float(value)):
                raise ValueError("controller_posture_time_not_finite")
        if float(self.time_in_state_s) < 0.0:
            raise ValueError("controller_posture_time_in_state_invalid")
        if self.degraded_actions_tried < 0:
            raise ValueError("controller_posture_degraded_attempts_invalid")
        if self.emergency_recovery_step < 0 or self.bad_rf_recovery_step < 0:
            raise ValueError("controller_posture_recovery_step_invalid")
        canonical_bands = tuple(
            sorted({int(value) for value in self.recovered_lane_bands if int(value) > 0})
        )
        if canonical_bands != self.recovered_lane_bands:
            raise ValueError("controller_posture_recovered_bands_not_canonical")
        rule_ids = tuple(row.rule_id for row in self.rule_progress)
        if rule_ids != tuple(sorted(set(rule_ids))):
            raise ValueError("controller_posture_rules_not_canonical")
        if self.congestion_input_disposition == CONGESTION_AVAILABLE:
            if self.congestion_state_bucket not in {"normal", "mild", "high"}:
                raise ValueError("controller_posture_congestion_bucket_invalid")
            if self.congestion_state_avg_pct is None:
                raise ValueError("controller_posture_congestion_average_missing")
            value = float(self.congestion_state_avg_pct)
            if not isfinite(value) or not 0.0 <= value <= 100.0:
                raise ValueError("controller_posture_congestion_average_invalid")
        else:
            if self.congestion_state_bucket:
                raise ValueError(
                    "controller_posture_unavailable_congestion_bucket_present"
                )
            if self.congestion_state_avg_pct is not None:
                raise ValueError(
                    "controller_posture_unavailable_congestion_average_present"
                )
        expected = _digest(self._content())
        if not self.posture_id or self.posture_id != expected:
            raise ValueError("controller_posture_checksum_invalid")

    @property
    def generation_key(self) -> tuple[str, str, int]:
        return (
            self.source_epoch,
            self.session_scope_key,
            self.source_generation,
        )

    @property
    def rule_rows(self) -> Mapping[str, PostureRuleProgress]:
        return {row.rule_id: row for row in self.rule_progress}

    def _content(self) -> dict[str, Any]:
        return {
            "source_generation": int(self.source_generation),
            "source_epoch": self.source_epoch,
            "session_scope_key": self.session_scope_key,
            "identity_key": self.identity_key,
            "previous_state": self.previous_state.name,
            "state": self.state.name,
            "state_changed": bool(self.state_changed),
            "published_ts": float(self.published_ts),
            "entered_at": float(self.entered_at),
            "time_in_state_s": float(self.time_in_state_s),
            "session_disposition": self.session_disposition,
            "congestion_input_disposition": self.congestion_input_disposition,
            "congestion_state_bucket": self.congestion_state_bucket,
            "congestion_state_avg_pct": (
                None
                if self.congestion_state_avg_pct is None
                else float(self.congestion_state_avg_pct)
            ),
            "congestion_kind": self.congestion_kind,
            "warning_class": self.warning_class,
            "warning_source": self.warning_source,
            "warning_mechanism": self.warning_mechanism,
            "warning_reason": self.warning_reason,
            "fault_class": self.fault_class,
            "emergency_class": self.emergency_class,
            "emergency_origin_class": self.emergency_origin_class,
            "emergency_reason": self.emergency_reason,
            "degraded_actions_tried": int(self.degraded_actions_tried),
            "degraded_timeout_at": float(self.degraded_timeout_at),
            "congested_last_check": float(self.congested_last_check),
            "congested_action_taken": bool(self.congested_action_taken),
            "emergency_timeout_at": float(self.emergency_timeout_at),
            "soft_emergency_hold_until": float(self.soft_emergency_hold_until),
            "emergency_observe_until": float(self.emergency_observe_until),
            "emergency_reentry_guard_until": float(
                self.emergency_reentry_guard_until
            ),
            "emergency_recovery_since": float(self.emergency_recovery_since),
            "emergency_recovery_step": int(self.emergency_recovery_step),
            "emergency_next_action_at": float(self.emergency_next_action_at),
            "bad_rf_recovery_since": float(self.bad_rf_recovery_since),
            "bad_rf_recovery_step": int(self.bad_rf_recovery_step),
            "bad_rf_next_action_at": float(self.bad_rf_next_action_at),
            "rf_cliff_event_at": float(self.rf_cliff_event_at),
            "rf_cliff_event_reason": self.rf_cliff_event_reason,
            "post_fault_class": self.post_fault_class,
            "post_fault_until": float(self.post_fault_until),
            "recovered_lane_bands": list(self.recovered_lane_bands),
            "recovered_lane_ts": float(self.recovered_lane_ts),
            "recovered_lane_from_fault": self.recovered_lane_from_fault,
            "recovered_lane_until": float(self.recovered_lane_until),
            "rule_progress": [row.as_dict() for row in self.rule_progress],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            **self._content(),
            "posture_id": self.posture_id,
            "progress_scope_id": self.progress_scope_id,
        }


def _digest(content: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(content),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_controller_posture(
    *,
    source_generation: int,
    source_epoch: str,
    session_scope_key: str,
    identity_key: str,
    previous_state: SystemState,
    state: SystemState,
    state_changed: bool,
    published_ts: float,
    entered_at: float,
    session_disposition: str,
    congestion_input_disposition: str,
    congestion_state_bucket: str = "",
    congestion_state_avg_pct: float | None = None,
    congestion_kind: str = "",
    warning_class: str = "",
    warning_source: str = "",
    warning_mechanism: str = "",
    warning_reason: str = "",
    fault_class: str = "",
    emergency_class: str = "",
    emergency_origin_class: str = "",
    emergency_reason: str = "",
    degraded_actions_tried: int = 0,
    degraded_timeout_at: float = 0.0,
    congested_last_check: float = 0.0,
    congested_action_taken: bool = False,
    emergency_timeout_at: float = 0.0,
    soft_emergency_hold_until: float = 0.0,
    emergency_observe_until: float = 0.0,
    emergency_reentry_guard_until: float = 0.0,
    emergency_recovery_since: float = 0.0,
    emergency_recovery_step: int = 0,
    emergency_next_action_at: float = 0.0,
    bad_rf_recovery_since: float = 0.0,
    bad_rf_recovery_step: int = 0,
    bad_rf_next_action_at: float = 0.0,
    rf_cliff_event_at: float = 0.0,
    rf_cliff_event_reason: str = "",
    post_fault_class: str = "",
    post_fault_until: float = 0.0,
    recovered_lane_bands: Iterable[int] = (),
    recovered_lane_ts: float = 0.0,
    recovered_lane_from_fault: str = "",
    recovered_lane_until: float = 0.0,
    rule_progress: Iterable[PostureRuleProgress] = (),
) -> ControllerPosture:
    published = float(published_ts)
    entered = float(entered_at)
    rules = tuple(sorted(tuple(rule_progress), key=lambda row: row.rule_id))
    values: dict[str, Any] = {
        "source_generation": int(source_generation),
        "source_epoch": str(source_epoch or ""),
        "session_scope_key": str(session_scope_key or ""),
        "identity_key": str(identity_key or ""),
        "previous_state": previous_state,
        "state": state,
        "state_changed": bool(state_changed),
        "published_ts": published,
        "entered_at": entered,
        "time_in_state_s": max(0.0, published - entered),
        "session_disposition": str(session_disposition or ""),
        "congestion_input_disposition": str(
            congestion_input_disposition or ""
        ),
        "congestion_state_bucket": str(congestion_state_bucket or ""),
        "congestion_state_avg_pct": (
            None
            if congestion_state_avg_pct is None
            else float(congestion_state_avg_pct)
        ),
        "congestion_kind": str(congestion_kind or ""),
        "warning_class": str(warning_class or ""),
        "warning_source": str(warning_source or ""),
        "warning_mechanism": str(warning_mechanism or ""),
        "warning_reason": str(warning_reason or ""),
        "fault_class": str(fault_class or ""),
        "emergency_class": str(emergency_class or ""),
        "emergency_origin_class": str(emergency_origin_class or ""),
        "emergency_reason": str(emergency_reason or ""),
        "degraded_actions_tried": max(0, int(degraded_actions_tried)),
        "degraded_timeout_at": float(degraded_timeout_at),
        "congested_last_check": float(congested_last_check),
        "congested_action_taken": bool(congested_action_taken),
        "emergency_timeout_at": float(emergency_timeout_at),
        "soft_emergency_hold_until": float(soft_emergency_hold_until),
        "emergency_observe_until": float(emergency_observe_until),
        "emergency_reentry_guard_until": float(
            emergency_reentry_guard_until
        ),
        "emergency_recovery_since": float(emergency_recovery_since),
        "emergency_recovery_step": max(0, int(emergency_recovery_step)),
        "emergency_next_action_at": float(emergency_next_action_at),
        "bad_rf_recovery_since": float(bad_rf_recovery_since),
        "bad_rf_recovery_step": max(0, int(bad_rf_recovery_step)),
        "bad_rf_next_action_at": float(bad_rf_next_action_at),
        "rf_cliff_event_at": float(rf_cliff_event_at),
        "rf_cliff_event_reason": str(rf_cliff_event_reason or ""),
        "post_fault_class": str(post_fault_class or ""),
        "post_fault_until": float(post_fault_until),
        "recovered_lane_bands": tuple(
            sorted({int(value) for value in recovered_lane_bands if int(value) > 0})
        ),
        "recovered_lane_ts": float(recovered_lane_ts),
        "recovered_lane_from_fault": str(recovered_lane_from_fault or ""),
        "recovered_lane_until": float(recovered_lane_until),
        "rule_progress": rules,
    }
    probe = object.__new__(ControllerPosture)
    for key, value in values.items():
        object.__setattr__(probe, key, value)
    content = ControllerPosture._content(probe)
    return ControllerPosture(**values, posture_id=_digest(content))


def build_startup_posture(*, now: float) -> ControllerPosture:
    return build_controller_posture(
        source_generation=0,
        source_epoch="startup",
        session_scope_key="startup-unbound",
        identity_key="",
        previous_state=SystemState.WORKING,
        state=SystemState.WORKING,
        state_changed=False,
        published_ts=float(now),
        entered_at=float(now),
        session_disposition=SESSION_STARTUP_UNBOUND,
        congestion_input_disposition=CONGESTION_UNAVAILABLE,
    )


__all__ = [
    "BASIS_ACCEPTED_OBSERVATIONS",
    "BASIS_ELAPSED_TIME",
    "CONGESTION_AVAILABLE",
    "CONGESTION_UNAVAILABLE",
    "ControllerPosture",
    "OWNER",
    "PostureRuleProgress",
    "SCHEMA",
    "SESSION_CONTINUED",
    "SESSION_NEW_RESET",
    "SESSION_STARTUP_UNBOUND",
    "build_controller_posture",
    "build_startup_posture",
]
