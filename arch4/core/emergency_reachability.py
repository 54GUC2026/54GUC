"""Core owner for statistically confirmed Internet emergencies."""

from __future__ import annotations

import hashlib
from threading import RLock

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyReachabilityView,
    CoreEmergencyRestartPermit,
    EmergencyReachabilityDisposition,
    ModemLiveInternetProbeRound,
)


PRIMARY_FAILURES_TO_VERIFY = 5
PRIMARY_SUCCESSES_TO_RECOVER = 5
VERIFICATION_WINDOW_ROUNDS = 10
VERIFICATION_FAILURES_TO_CONFIRM = 8
VERIFICATION_SUCCESSES_TO_RECOVER = 5
VERIFICATION_MAX_AGE_S = 30.0


class CoreEmergencyReachabilityAuthority:
    """Turn Modem Live probes into one modem-neutral emergency state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._session_scope_key = ""
        self._identity_code = ""
        self._incident_id = ""
        self._disposition = EmergencyReachabilityDisposition.NORMAL
        self._triggered_at = 0.0
        self._primary_failure_streak = 0
        self._primary_success_streak = 0
        self._rounds: tuple[ModemLiveInternetProbeRound, ...] = ()
        self._last_round: ModemLiveInternetProbeRound | None = None
        self._restart_permit: CoreEmergencyRestartPermit | None = None
        self._primary_was_uncertain = False

    def observe_primary(
        self,
        *,
        session_scope_key: str,
        identity_code: str,
        failure_streak: int,
        success_streak: int,
        first_failure_at: float,
        observed_at: float,
        activity_success_streak: int = 0,
        automatic_cellular_actions_allowed: bool = True,
    ) -> CoreEmergencyReachabilityView:
        scope = str(session_scope_key or "").strip()
        if type(automatic_cellular_actions_allowed) is not bool:
            raise TypeError("core_emergency_mobile_data_bool_required")
        identity = str(identity_code or "").strip()
        failures = max(0, int(failure_streak))
        successes = max(0, int(success_streak))
        activity_successes = max(0, int(activity_success_streak))
        if failures and (successes or activity_successes):
            raise ValueError("core_emergency_primary_streaks_contradictory")
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_emergency_primary_time_required")
        with self._lock:
            if (scope, identity) != (
                self._session_scope_key,
                self._identity_code,
            ):
                self._reset_locked(scope=scope, identity=identity)
            self._primary_failure_streak = failures
            self._primary_success_streak = successes
            if not automatic_cellular_actions_allowed:
                # A deliberate native OFF setting is not an Internet emergency.
                # The reachability timeline retains the actual outage unchanged.
                self._clear_incident_locked()
                return self._view_locked(now=now)
            if (
                self._disposition is not EmergencyReachabilityDisposition.NORMAL
                and max(successes, activity_successes) >= PRIMARY_SUCCESSES_TO_RECOVER
            ):
                self._clear_incident_locked()
                return self._view_locked(now=now)
            if (
                self._disposition is not EmergencyReachabilityDisposition.NORMAL
                and failures == 0
            ):
                # Unknown, stale or contradictory evidence withdraws permission;
                # it is not proof that an open incident has recovered.
                self._disposition = EmergencyReachabilityDisposition.VERIFYING
                if not self._primary_was_uncertain:
                    self._rounds = ()
                self._restart_permit = None
            self._primary_was_uncertain = failures == 0
            if (
                self._disposition is EmergencyReachabilityDisposition.NORMAL
                and scope
                and identity
                and failures >= PRIMARY_FAILURES_TO_VERIFY
            ):
                started = float(first_failure_at)
                if started <= 0.0:
                    raise ValueError("core_emergency_primary_failure_start_required")
                self._triggered_at = started
                self._incident_id = self._new_incident_id(
                    scope=scope,
                    identity=identity,
                    started_at=started,
                )
                self._disposition = EmergencyReachabilityDisposition.VERIFYING
                self._rounds = ()
            return self._view_locked(now=now)

    def accept_verification_round(
        self,
        value: ModemLiveInternetProbeRound,
        *,
        observed_at: float,
    ) -> CoreEmergencyReachabilityView:
        if not isinstance(value, ModemLiveInternetProbeRound):
            raise TypeError("core_emergency_modem_live_round_required")
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_emergency_verification_time_required")
        with self._lock:
            if self._disposition is EmergencyReachabilityDisposition.NORMAL:
                raise RuntimeError("core_emergency_verification_not_active")
            if self._last_round and value.completed_at <= self._last_round.completed_at:
                if value == self._last_round:
                    return self._view_locked(now=now)
                raise RuntimeError("core_emergency_verification_round_regression")
            self._last_round = value
            if (
                value.reachable is None
                or now - value.started_at > VERIFICATION_MAX_AGE_S
                or (self._rounds and value.started_at - self._rounds[-1].completed_at > VERIFICATION_MAX_AGE_S)
            ):
                self._rounds = ()
                self._disposition = EmergencyReachabilityDisposition.VERIFYING
                self._restart_permit = None
                return self._view_locked(now=now)
            self._rounds = (*self._rounds, value)[-VERIFICATION_WINDOW_ROUNDS:]
            success_streak = self._success_streak_locked()
            if success_streak >= VERIFICATION_SUCCESSES_TO_RECOVER:
                self._clear_incident_locked()
                return self._view_locked(now=now)
            failures = sum(item.reachable is False for item in self._rounds)
            if (
                len(self._rounds) >= VERIFICATION_WINDOW_ROUNDS
                and failures >= VERIFICATION_FAILURES_TO_CONFIRM
                and self._rounds[-1].reachable is False
                and not self._primary_was_uncertain
            ):
                self._disposition = EmergencyReachabilityDisposition.CONFIRMED
            else:
                self._disposition = EmergencyReachabilityDisposition.VERIFYING
                self._restart_permit = None
            return self._view_locked(now=now)

    def restart_verification(
        self,
        *,
        incident_id: str,
        observed_at: float,
    ) -> CoreEmergencyReachabilityView:
        key = str(incident_id or "").strip()
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_emergency_reverification_time_required")
        with self._lock:
            if not key or key != self._incident_id:
                raise RuntimeError("core_emergency_reverification_incident_mismatch")
            self._triggered_at = now
            self._incident_id = self._new_incident_id(
                scope=self._session_scope_key,
                identity=self._identity_code,
                started_at=now,
            )
            self._rounds = ()
            self._last_round = None
            self._restart_permit = None
            self._disposition = EmergencyReachabilityDisposition.VERIFYING
            return self._view_locked(now=now)

    def current_view(self, *, now: float) -> CoreEmergencyReachabilityView:
        with self._lock:
            return self._view_locked(now=float(now))

    def issue_restart_permit(
        self,
        *,
        request_id: str,
        matrix_identity: MatrixGenerationIdentity,
        identity_code: str,
        incident_id: str,
        now: float,
    ) -> CoreEmergencyRestartPermit:
        """Issue one short permit for the exact confirmed incident."""

        current = float(now)
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_emergency_restart_matrix_required")
        with self._lock:
            self._expire_confirmation_locked(now=current)
            if self._disposition is not EmergencyReachabilityDisposition.CONFIRMED:
                raise RuntimeError("core_emergency_restart_incident_not_confirmed")
            if (
                str(request_id or "").strip() == ""
                or str(incident_id or "").strip() != self._incident_id
                or matrix_identity.session_scope_key != self._session_scope_key
                or str(identity_code or "").strip() != self._identity_code
            ):
                raise RuntimeError("core_emergency_restart_incident_scope_mismatch")
            permit = CoreEmergencyRestartPermit(
                request_id=str(request_id).strip(),
                matrix_identity=matrix_identity,
                identity_code=self._identity_code,
                incident_id=self._incident_id,
                issued_at=current,
                expires_at=current + 30.0,
            )
            retained = self._restart_permit
            if (
                retained is not None
                and current <= retained.expires_at
                and retained != permit
            ):
                raise RuntimeError(
                    "core_emergency_restart_permit_already_issued"
                )
            self._restart_permit = permit
            return permit

    def validate_restart_permit(
        self,
        permit: CoreEmergencyRestartPermit,
        *,
        now: float,
    ) -> bool:
        """Revalidate the same incident immediately before modem effect."""

        if not isinstance(permit, CoreEmergencyRestartPermit):
            return False
        current = float(now)
        with self._lock:
            self._expire_confirmation_locked(now=current)
            return bool(
                current <= permit.expires_at
                and permit == self._restart_permit
                and self._disposition
                is EmergencyReachabilityDisposition.CONFIRMED
                and permit.incident_id == self._incident_id
                and permit.matrix_identity.session_scope_key
                == self._session_scope_key
                and permit.identity_code == self._identity_code
            )

    def _expire_confirmation_locked(self, *, now: float) -> None:
        if self._rounds and now - self._rounds[-1].completed_at > VERIFICATION_MAX_AGE_S:
            self._rounds = ()
            self._restart_permit = None
            if self._disposition is not EmergencyReachabilityDisposition.NORMAL:
                self._disposition = EmergencyReachabilityDisposition.VERIFYING

    def _view_locked(self, *, now: float) -> CoreEmergencyReachabilityView:
        self._expire_confirmation_locked(now=now)
        failures = sum(item.reachable is False for item in self._rounds)
        return CoreEmergencyReachabilityView(
            session_scope_key=self._session_scope_key,
            identity_code=self._identity_code,
            incident_id=self._incident_id,
            disposition=self._disposition,
            observed_at=now,
            triggered_at=self._triggered_at,
            primary_failure_streak=self._primary_failure_streak,
            primary_success_streak=self._primary_success_streak,
            verification_round_count=len(self._rounds),
            failed_round_count=failures,
            successful_round_streak=self._success_streak_locked(),
            latest_round_at=(
                self._rounds[-1].completed_at if self._rounds else 0.0
            ),
        )

    def _success_streak_locked(self) -> int:
        count = 0
        for item in reversed(self._rounds):
            if item.reachable is not True:
                break
            count += 1
        return count

    def _reset_locked(self, *, scope: str, identity: str) -> None:
        self._session_scope_key = scope
        self._identity_code = identity
        self._primary_failure_streak = 0
        self._primary_success_streak = 0
        self._primary_was_uncertain = False
        self._clear_incident_locked()

    def _clear_incident_locked(self) -> None:
        self._incident_id = ""
        self._disposition = EmergencyReachabilityDisposition.NORMAL
        self._triggered_at = 0.0
        self._rounds = ()
        self._last_round = None
        self._restart_permit = None

    @staticmethod
    def _new_incident_id(*, scope: str, identity: str, started_at: float) -> str:
        digest = hashlib.sha256(
            f"{scope}|{identity}|{started_at:.6f}".encode("utf-8")
        ).hexdigest()[:20]
        return f"internet-emergency:{digest}"


__all__ = (
    "PRIMARY_FAILURES_TO_VERIFY",
    "PRIMARY_SUCCESSES_TO_RECOVER",
    "VERIFICATION_FAILURES_TO_CONFIRM",
    "VERIFICATION_SUCCESSES_TO_RECOVER",
    "VERIFICATION_WINDOW_ROUNDS",
    "CoreEmergencyReachabilityAuthority",
)
