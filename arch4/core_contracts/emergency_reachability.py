"""Typed Modem Live evidence and Core emergency-reachability truth."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)


MODEM_LIVE_PROBE_ROUND_SCHEMA = "ModemLiveInternetProbeRound.v1"
MODEM_LIVE_PROBE_ROUND_OWNER = "54guc.modem_live.internet_probe"
CORE_EMERGENCY_REACHABILITY_SCHEMA = "CoreEmergencyReachabilityView.v2"
CORE_EMERGENCY_REACHABILITY_OWNER = "54guc.core.emergency_reachability"
CORE_EMERGENCY_RESTART_PERMIT_SCHEMA = "CoreEmergencyRestartPermit.v1"
CORE_EMERGENCY_RESTART_PERMIT_OWNER = "54guc.core.emergency_reachability"
BACKGROUND_CHECK_AFTER_S = 30.0
PREPARE_RECOVERY_AFTER_S = 60.0
TAKEOVER_AFTER_S = 90.0


class EmergencyReachabilityDisposition(StrEnum):
    NORMAL = "normal"
    VERIFYING = "verifying"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class RecoveryTimingProfile:
    """Selected-profile time allowances, never a second recovery controller."""

    rescue_window_s: float = 120.0
    restart_verification_s: float = 180.0

    def __post_init__(self) -> None:
        for name in ("rescue_window_s", "restart_verification_s"):
            _positive(getattr(self, name), "recovery_timing_positive_seconds_required")

    def as_dict(self) -> dict[str, float]:
        return {"rescue_window_s": float(self.rescue_window_s),
                "restart_verification_s": float(self.restart_verification_s)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecoveryTimingProfile":
        if not isinstance(value, Mapping) or set(value) != {"rescue_window_s", "restart_verification_s"}:
            raise ValueError("selected_recovery_timing_required")
        return cls(**value)


def _positive(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(reason)
    return result


def _nonnegative(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(reason)
    return result


def _text(value: Any, reason: str, *, empty: bool = False) -> str:
    if type(value) is not str or value != value.strip():
        raise TypeError(reason)
    if not empty and not value:
        raise ValueError(reason)
    return value


@dataclass(frozen=True, slots=True)
class ModemLiveInternetProbe:
    target: str
    probed_at: float
    reachable: bool | None
    rtt_ms: float | None = None
    failure_reason: str = ""

    def __post_init__(self) -> None:
        _text(self.target, "modem_live_internet_probe_target_required")
        _positive(self.probed_at, "modem_live_internet_probe_time_invalid")
        if self.reachable is not None and type(self.reachable) is not bool:
            raise TypeError("modem_live_internet_probe_result_bool_required")
        if self.rtt_ms is not None:
            _nonnegative(self.rtt_ms, "modem_live_internet_probe_rtt_invalid")
        _text(
            self.failure_reason,
            "modem_live_internet_probe_failure_reason_invalid",
            empty=True,
        )
        if self.reachable and (self.rtt_ms is None or self.failure_reason):
            raise ValueError("modem_live_internet_probe_success_invalid")
        if not self.reachable and (
            self.rtt_ms is not None or not self.failure_reason
        ):
            raise ValueError("modem_live_internet_probe_failure_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "probed_at": self.probed_at,
            "reachable": self.reachable,
            "rtt_ms": self.rtt_ms,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class ModemLiveInternetProbeRound:
    round_id: str
    started_at: float
    completed_at: float
    probes: tuple[ModemLiveInternetProbe, ...]
    schema: str = MODEM_LIVE_PROBE_ROUND_SCHEMA
    owner: str = MODEM_LIVE_PROBE_ROUND_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != MODEM_LIVE_PROBE_ROUND_SCHEMA
            or self.owner != MODEM_LIVE_PROBE_ROUND_OWNER
        ):
            raise ValueError("modem_live_internet_probe_round_owner_invalid")
        _text(self.round_id, "modem_live_internet_probe_round_id_required")
        started = _positive(
            self.started_at,
            "modem_live_internet_probe_round_start_invalid",
        )
        completed = _positive(
            self.completed_at,
            "modem_live_internet_probe_round_completion_invalid",
        )
        probes = tuple(self.probes)
        if completed < started or len(probes) < 2:
            raise ValueError("modem_live_internet_probe_round_invalid")
        if not all(isinstance(item, ModemLiveInternetProbe) for item in probes):
            raise TypeError("modem_live_internet_probe_round_typed_probes_required")
        targets = tuple(item.target for item in probes)
        if len(set(targets)) != len(targets):
            raise ValueError("modem_live_internet_probe_round_target_duplicate")
        object.__setattr__(self, "probes", probes)

    @property
    def reachable(self) -> bool | None:
        """One responding independent target proves the round is reachable."""

        if any(item.reachable is True for item in self.probes):
            return True
        if any(item.reachable is None for item in self.probes):
            return None
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "round_id": self.round_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "reachable": self.reachable,
            "probes": [item.as_dict() for item in self.probes],
        }


@dataclass(frozen=True, slots=True)
class CoreEmergencyReachabilityView:
    session_scope_key: str
    identity_code: str
    incident_id: str
    disposition: EmergencyReachabilityDisposition
    observed_at: float
    triggered_at: float
    primary_failure_streak: int
    primary_success_streak: int
    verification_round_count: int
    failed_round_count: int
    successful_round_streak: int
    latest_round_at: float
    schema: str = CORE_EMERGENCY_REACHABILITY_SCHEMA
    owner: str = CORE_EMERGENCY_REACHABILITY_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != CORE_EMERGENCY_REACHABILITY_SCHEMA
            or self.owner != CORE_EMERGENCY_REACHABILITY_OWNER
        ):
            raise ValueError("core_emergency_reachability_owner_invalid")
        for name in ("session_scope_key", "identity_code", "incident_id"):
            _text(
                getattr(self, name),
                f"core_emergency_reachability_{name}_invalid",
                empty=True,
            )
        if not isinstance(self.disposition, EmergencyReachabilityDisposition):
            raise TypeError("core_emergency_reachability_disposition_required")
        _positive(self.observed_at, "core_emergency_reachability_time_invalid")
        _nonnegative(
            self.triggered_at,
            "core_emergency_reachability_trigger_time_invalid",
        )
        _nonnegative(
            self.latest_round_at,
            "core_emergency_reachability_round_time_invalid",
        )
        for name in (
            "primary_failure_streak",
            "primary_success_streak",
            "verification_round_count",
            "failed_round_count",
            "successful_round_streak",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"core_emergency_reachability_{name}_invalid")
        if self.failed_round_count > self.verification_round_count:
            raise ValueError("core_emergency_reachability_round_counts_invalid")
        active = self.disposition is not EmergencyReachabilityDisposition.NORMAL
        if active and (
            not self.session_scope_key
            or not self.identity_code
            or not self.incident_id
            or self.triggered_at <= 0.0
        ):
            raise ValueError("core_emergency_reachability_active_state_invalid")
        if not active and any(
            (
                self.incident_id,
                self.triggered_at,
                self.verification_round_count,
                self.failed_round_count,
                self.successful_round_streak,
                self.latest_round_at,
            )
        ):
            raise ValueError("core_emergency_reachability_normal_state_invalid")

    @property
    def confirmed(self) -> bool:
        return self.disposition is EmergencyReachabilityDisposition.CONFIRMED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "session_scope_key": self.session_scope_key,
            "identity_code": self.identity_code,
            "incident_id": self.incident_id,
            "disposition": self.disposition.value,
            "confirmed": self.confirmed,
            "observed_at": self.observed_at,
            "triggered_at": self.triggered_at,
            "primary_failure_streak": self.primary_failure_streak,
            "primary_success_streak": self.primary_success_streak,
            "verification_round_count": self.verification_round_count,
            "failed_round_count": self.failed_round_count,
            "successful_round_streak": self.successful_round_streak,
            "latest_round_at": self.latest_round_at,
        }


@dataclass(frozen=True, slots=True)
class CoreEmergencyRestartPermit:
    """Short-lived Core proof allowing one exact outage restart."""

    request_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_code: str
    incident_id: str
    issued_at: float
    expires_at: float
    schema: str = CORE_EMERGENCY_RESTART_PERMIT_SCHEMA
    owner: str = CORE_EMERGENCY_RESTART_PERMIT_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != CORE_EMERGENCY_RESTART_PERMIT_SCHEMA
            or self.owner != CORE_EMERGENCY_RESTART_PERMIT_OWNER
        ):
            raise ValueError("core_emergency_restart_permit_owner_invalid")
        _text(self.request_id, "core_emergency_restart_permit_request_required")
        _text(self.identity_code, "core_emergency_restart_permit_identity_required")
        _text(self.incident_id, "core_emergency_restart_permit_incident_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_emergency_restart_permit_matrix_required")
        issued = _positive(
            self.issued_at,
            "core_emergency_restart_permit_issue_time_invalid",
        )
        expires = _positive(
            self.expires_at,
            "core_emergency_restart_permit_expiry_invalid",
        )
        if expires <= issued or expires - issued > 30.0:
            raise ValueError("core_emergency_restart_permit_window_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "identity_code": self.identity_code,
            "incident_id": self.incident_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
    ) -> "CoreEmergencyRestartPermit":
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "owner",
            "request_id",
            "matrix_identity",
            "identity_code",
            "incident_id",
            "issued_at",
            "expires_at",
        }:
            raise ValueError("core_emergency_restart_permit_shape_invalid")
        return cls(
            request_id=value["request_id"],
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                value["matrix_identity"]
            ),
            identity_code=value["identity_code"],
            incident_id=value["incident_id"],
            issued_at=value["issued_at"],
            expires_at=value["expires_at"],
            schema=value["schema"],
            owner=value["owner"],
        )


__all__ = (
    "CORE_EMERGENCY_REACHABILITY_OWNER",
    "CORE_EMERGENCY_REACHABILITY_SCHEMA",
    "CORE_EMERGENCY_RESTART_PERMIT_OWNER",
    "CORE_EMERGENCY_RESTART_PERMIT_SCHEMA",
    "MODEM_LIVE_PROBE_ROUND_OWNER",
    "MODEM_LIVE_PROBE_ROUND_SCHEMA",
    "CoreEmergencyReachabilityView",
    "CoreEmergencyRestartPermit",
    "EmergencyReachabilityDisposition",
    "ModemLiveInternetProbe",
    "ModemLiveInternetProbeRound",
)
