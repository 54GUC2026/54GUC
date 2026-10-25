"""Mechanical modem-interface connectivity owned by the Modem Live translator.

This owner may account for physical read success/failure and retry the existing
backend session. It must not choose or execute a corrective radio/device
mutation, decide that user service recovered, or release upper action/proof
state. Callers outside the Modem Live boundary may read its published health
but must not advance its state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import logging
from math import isfinite
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from arch4.modem.read_miss_visibility import (
    DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S,
    annotate_modem_read_miss_visibility,
)

log = logging.getLogger("arch4.modem.connectivity")

CONNECTIVITY_OBSERVATION_SCHEMA = "ConnectivityObservation.v1"
MAX_EXPECTED_INTERRUPTION_S = 600.0


@dataclass(frozen=True, slots=True)
class PreparedConnectivityAttempt:
    owner_id: int
    before_state: Mapping[str, Any]
    after_state: Mapping[str, Any]
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModemLiveSessionRepairRequest:
    """Exact failed acquisition that asks Connectivity for one repair."""

    request_id: str
    cause: str
    source_epoch: str
    evidence_generation: int
    observed_at: float

    def __post_init__(self) -> None:
        for name in ("request_id", "cause", "source_epoch"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"session_repair_request_{name}_required")
        if (
            type(self.evidence_generation) is not int
            or self.evidence_generation < 0
        ):
            raise ValueError("session_repair_request_generation_invalid")
        if (
            type(self.observed_at) is not float
            or not isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("session_repair_request_time_invalid")


@dataclass(frozen=True, slots=True)
class ModemLiveSessionRepairAdmission:
    """One repair permission issued by the connectivity state owner."""

    owner_id: int
    token: str
    admitted: bool
    reason: str
    admitted_at: float
    next_eligible_at: float
    attempt_number: int
    request_id: str
    cause: str
    source_epoch: str
    evidence_generation: int

    def __post_init__(self) -> None:
        if type(self.owner_id) is not int or self.owner_id <= 0:
            raise ValueError("session_repair_admission_owner_invalid")
        if type(self.admitted) is not bool:
            raise TypeError("session_repair_admission_bool_required")
        if type(self.token) is not str or self.token != self.token.strip():
            raise TypeError("session_repair_admission_token_invalid")
        if self.admitted and not self.token:
            raise ValueError("session_repair_admission_token_required")
        if not self.admitted and self.token:
            raise ValueError("session_repair_denial_token_forbidden")
        if type(self.reason) is not str or not self.reason or self.reason != self.reason.strip():
            raise ValueError("session_repair_admission_reason_required")
        for name in ("admitted_at", "next_eligible_at"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value < 0.0:
                raise ValueError(f"session_repair_admission_{name}_invalid")
        if type(self.attempt_number) is not int or self.attempt_number < 0:
            raise ValueError("session_repair_admission_attempt_invalid")
        if self.admitted and self.attempt_number <= 0:
            raise ValueError("session_repair_admission_attempt_required")
        for name in ("request_id", "cause", "source_epoch"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"session_repair_admission_{name}_required")
        if (
            type(self.evidence_generation) is not int
            or self.evidence_generation < 0
        ):
            raise ValueError("session_repair_admission_generation_invalid")


@dataclass(frozen=True, slots=True)
class ModemLiveSessionRepairReceipt:
    """Completion evidence returned by the sole Modem Live executor."""

    admission_owner_id: int
    admission_token: str
    kind: str
    transport_succeeded: bool
    recovered: bool
    error: str
    started_at: float
    completed_at: float
    attempt_number: int
    request_id: str
    cause: str
    source_epoch: str
    evidence_generation: int

    def __post_init__(self) -> None:
        if type(self.admission_owner_id) is not int or self.admission_owner_id <= 0:
            raise ValueError("session_repair_receipt_owner_invalid")
        for name in ("admission_token", "kind"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"session_repair_receipt_{name}_required")
        if type(self.error) is not str or self.error != self.error.strip():
            raise TypeError("session_repair_receipt_error_invalid")
        if type(self.transport_succeeded) is not bool or type(self.recovered) is not bool:
            raise TypeError("session_repair_receipt_result_bool_required")
        if self.recovered and not self.transport_succeeded:
            raise ValueError("session_repair_receipt_recovery_without_transport")
        for name in ("started_at", "completed_at"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value <= 0.0:
                raise ValueError(f"session_repair_receipt_{name}_invalid")
        if self.completed_at < self.started_at:
            raise ValueError("session_repair_receipt_interval_invalid")
        if type(self.attempt_number) is not int or self.attempt_number <= 0:
            raise ValueError("session_repair_receipt_attempt_invalid")
        for name in ("request_id", "cause", "source_epoch"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"session_repair_receipt_{name}_required")
        if (
            type(self.evidence_generation) is not int
            or self.evidence_generation < 0
        ):
            raise ValueError("session_repair_receipt_generation_invalid")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "owner": "modem_live",
            "admission_owner": "modem_live_connectivity",
            "admission_owner_id": self.admission_owner_id,
            "admission_token": self.admission_token,
            "attempted": True,
            "succeeded": self.recovered,
            "transport_succeeded": self.transport_succeeded,
            "kind": self.kind,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempt_number": self.attempt_number,
            "request_id": self.request_id,
            "cause": self.cause,
            "source_epoch": self.source_epoch,
            "evidence_generation": self.evidence_generation,
        }


@dataclass(frozen=True, slots=True)
class ModemLiveRadioAvailabilityObservation:
    """One timestamped radio fact acquired by the Modem Live backend."""

    available: bool
    started_at: float
    observed_at: float

    def __post_init__(self) -> None:
        if type(self.available) is not bool:
            raise TypeError("modem_live_radio_availability_bool_required")
        for name in ("started_at", "observed_at"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"modem_live_radio_availability_{name}_invalid"
                )
        if self.observed_at < self.started_at:
            raise ValueError(
                "modem_live_radio_availability_interval_invalid"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "started_at": self.started_at,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ModemLiveExpectedInterruption:
    """One physical command lifetime owned inside Modem Live."""

    operation_id: str
    lever: str
    opened_at: float
    diagnostic_deadline_at: float
    reason: str
    miss_seen: bool = False
    radio_down_observed_at: float = 0.0
    command_completed_at: float = 0.0

    def __post_init__(self) -> None:
        for name in ("operation_id", "lever", "reason"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"modem_live_expected_interruption_{name}_required")
        for name in ("opened_at", "diagnostic_deadline_at"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value <= 0.0:
                raise ValueError(f"modem_live_expected_interruption_{name}_invalid")
        if type(self.miss_seen) is not bool:
            raise TypeError("modem_live_expected_interruption_miss_seen_bool_required")
        if (
            type(self.radio_down_observed_at) is not float
            or not isfinite(self.radio_down_observed_at)
            or self.radio_down_observed_at < 0.0
        ):
            raise ValueError(
                "modem_live_expected_interruption_radio_down_observed_at_invalid"
            )
        if self.radio_down_seen and not self.miss_seen:
            raise ValueError(
                "modem_live_expected_interruption_radio_down_requires_miss"
            )
        if (
            type(self.command_completed_at) is not float
            or not isfinite(self.command_completed_at)
            or self.command_completed_at < 0.0
        ):
            raise ValueError(
                "modem_live_expected_interruption_command_completed_at_invalid"
            )
        duration = self.diagnostic_deadline_at - self.opened_at
        if duration <= 0.0 or duration > MAX_EXPECTED_INTERRUPTION_S:
            raise ValueError("modem_live_expected_interruption_window_invalid")

    @property
    def radio_down_seen(self) -> bool:
        return self.radio_down_observed_at > 0.0

    def with_miss_seen(self) -> "ModemLiveExpectedInterruption":
        if self.miss_seen:
            return self
        return ModemLiveExpectedInterruption(
            operation_id=self.operation_id,
            lever=self.lever,
            opened_at=self.opened_at,
            diagnostic_deadline_at=self.diagnostic_deadline_at,
            reason=self.reason,
            miss_seen=True,
            radio_down_observed_at=self.radio_down_observed_at,
            command_completed_at=self.command_completed_at,
        )

    def with_radio_down_seen(
        self,
        *,
        observed_at: float,
    ) -> "ModemLiveExpectedInterruption":
        if self.radio_down_seen:
            return self
        return ModemLiveExpectedInterruption(
            operation_id=self.operation_id,
            lever=self.lever,
            opened_at=self.opened_at,
            diagnostic_deadline_at=self.diagnostic_deadline_at,
            reason=self.reason,
            miss_seen=True,
            radio_down_observed_at=float(observed_at),
            command_completed_at=self.command_completed_at,
        )

    def with_command_completed(self, *, completed_at: float) -> "ModemLiveExpectedInterruption":
        return ModemLiveExpectedInterruption(
            operation_id=self.operation_id,
            lever=self.lever,
            opened_at=self.opened_at,
            diagnostic_deadline_at=self.diagnostic_deadline_at,
            reason=self.reason,
            miss_seen=self.miss_seen,
            radio_down_observed_at=self.radio_down_observed_at,
            command_completed_at=float(completed_at),
        )

    def may_close_on_success(
        self,
        *,
        radio_observation: ModemLiveRadioAvailabilityObservation | None,
    ) -> bool:
        """Return whether one successful read proves this operation recovered."""

        if self.command_completed_at <= 0.0:
            return False
        # A restart request can return before the modem actually goes down.
        # Its contract is reconnect proof, so the pre-reboot success is not a
        # recovery.  Other radio writes may complete without missing a read.
        if self.lever == "RESTART":
            if not self.miss_seen:
                return False
            if self.radio_down_seen:
                return bool(
                    radio_observation is not None
                    and radio_observation.available
                    and radio_observation.started_at
                    >= self.radio_down_observed_at
                )
            return bool(
                radio_observation is None or radio_observation.available
            )
        return True

    def to_status_payload(self, *, observed_at: float) -> dict[str, Any]:
        remaining = max(0.0, self.diagnostic_deadline_at - observed_at)
        return {
            "active": True,
            "operation_id": self.operation_id,
            "diagnostic_deadline_ts": self.diagnostic_deadline_at,
            "remaining_s": remaining,
            "diagnostic_deadline_exceeded": remaining <= 0.0,
            "lever": self.lever,
            "reason": self.reason,
            "miss_seen": self.miss_seen,
            "radio_down_seen": self.radio_down_seen,
            "radio_down_observed_at": self.radio_down_observed_at,
            "command_completed_at": self.command_completed_at,
        }


def _inactive_expected_interruption_payload() -> dict[str, Any]:
    return {
        "active": False,
        "operation_id": "",
        "diagnostic_deadline_ts": 0.0,
        "remaining_s": 0.0,
        "diagnostic_deadline_exceeded": False,
        "lever": "",
        "reason": "",
        "miss_seen": False,
        "radio_down_seen": False,
        "radio_down_observed_at": 0.0,
        "command_completed_at": 0.0,
    }


class ConnectivityModule:
    def __init__(
        self,
        *,
        reconnect_interval_s: float = 15.0,
        read_miss_operator_min_s: float = DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S,
    ) -> None:
        self._lock = threading.RLock()
        self._consecutive_failures: int = 0
        self._last_failure_ts: float = 0.0
        self._last_success_ts: float = 0.0
        self._last_reconnect_attempt_ts: float = 0.0
        reconnect_interval = float(reconnect_interval_s)
        if not isfinite(reconnect_interval) or reconnect_interval < 5.0:
            raise ValueError("session_repair_reconnect_interval_invalid")
        self._reconnect_interval_s = reconnect_interval
        self._reconnect_backoff_s: float = self._reconnect_interval_s
        self._reconnect_backoff_max_s: float = 60.0
        self._session_repair_sequence: int = 0
        self._session_repair_attempt_count: int = 0
        self._session_repair_inflight_token: str = ""
        self._session_repair_last_finished_ts: float = 0.0
        self._session_repair_next_eligible_ts: float = 0.0
        self._session_repair_last_defer_reason: str = ""
        self._observed_session_repair_tokens: Dict[str, float] = {}
        read_miss_interval = float(read_miss_operator_min_s)
        if not isfinite(read_miss_interval) or read_miss_interval < 0.0:
            raise ValueError("session_repair_read_miss_interval_invalid")
        self._read_miss_operator_min_s = read_miss_interval
        self._degraded: bool = False
        self._intentional_switch: bool = False
        self._expected_interruption: ModemLiveExpectedInterruption | None = None
        self._physical_attempt_sequence: int = 0
        self._observation_count: int = 0
        self._observed_attempt_ids: Dict[str, float] = {}
        self._last_observation: Dict[str, Any] = {}

    def _transaction_state_unlocked(self) -> Dict[str, Any]:
        return {
            "consecutive_failures": int(self._consecutive_failures),
            "last_failure_ts": float(self._last_failure_ts),
            "last_success_ts": float(self._last_success_ts),
            "last_reconnect_attempt_ts": float(
                self._last_reconnect_attempt_ts
            ),
            "reconnect_backoff_s": float(self._reconnect_backoff_s),
            "session_repair_sequence": int(self._session_repair_sequence),
            "session_repair_attempt_count": int(
                self._session_repair_attempt_count
            ),
            "session_repair_inflight_token": str(
                self._session_repair_inflight_token
            ),
            "session_repair_last_finished_ts": float(
                self._session_repair_last_finished_ts
            ),
            "session_repair_next_eligible_ts": float(
                self._session_repair_next_eligible_ts
            ),
            "session_repair_last_defer_reason": str(
                self._session_repair_last_defer_reason
            ),
            "observed_session_repair_tokens": dict(
                self._observed_session_repair_tokens
            ),
            "degraded": bool(self._degraded),
            "intentional_switch": bool(self._intentional_switch),
            "expected_interruption": self._expected_interruption,
            "physical_attempt_sequence": int(
                self._physical_attempt_sequence
            ),
            "observation_count": int(self._observation_count),
            "observed_attempt_ids": dict(self._observed_attempt_ids),
            "last_observation": copy.deepcopy(self._last_observation),
        }

    def _install_transaction_state_unlocked(
        self,
        state: Mapping[str, Any],
    ) -> None:
        payload = dict(state or {})
        self._consecutive_failures = int(
            payload["consecutive_failures"]
        )
        self._last_failure_ts = float(payload["last_failure_ts"])
        self._last_success_ts = float(payload["last_success_ts"])
        self._last_reconnect_attempt_ts = float(
            payload["last_reconnect_attempt_ts"]
        )
        self._reconnect_backoff_s = float(payload["reconnect_backoff_s"])
        self._session_repair_sequence = int(payload["session_repair_sequence"])
        self._session_repair_attempt_count = int(
            payload["session_repair_attempt_count"]
        )
        self._session_repair_inflight_token = str(
            payload["session_repair_inflight_token"]
        )
        self._session_repair_last_finished_ts = float(
            payload["session_repair_last_finished_ts"]
        )
        self._session_repair_next_eligible_ts = float(
            payload["session_repair_next_eligible_ts"]
        )
        self._session_repair_last_defer_reason = str(
            payload["session_repair_last_defer_reason"]
        )
        self._observed_session_repair_tokens = dict(
            payload["observed_session_repair_tokens"]
        )
        self._degraded = bool(payload["degraded"])
        self._intentional_switch = bool(payload["intentional_switch"])
        interruption = payload["expected_interruption"]
        if interruption is not None and not isinstance(
            interruption,
            ModemLiveExpectedInterruption,
        ):
            raise TypeError("expected_interruption_transaction_state_invalid")
        self._expected_interruption = interruption
        self._physical_attempt_sequence = int(
            payload["physical_attempt_sequence"]
        )
        self._observation_count = int(payload["observation_count"])
        self._observed_attempt_ids = dict(payload["observed_attempt_ids"])
        self._last_observation = copy.deepcopy(
            dict(payload["last_observation"])
        )

    def prepare_physical_attempt(
        self,
        *,
        succeeded: bool,
        now: Optional[float] = None,
        attempt_context: Optional[Mapping[str, Any]] = None,
        backend_repair: ModemLiveSessionRepairReceipt | None = None,
        radio_availability_observation: (
            ModemLiveRadioAvailabilityObservation | None
        ) = None,
    ) -> PreparedConnectivityAttempt:
        """Prepare exact connectivity truth without making it visible."""

        with self._lock:
            before = self._transaction_state_unlocked()
            try:
                receipt = self.record_physical_attempt(
                    succeeded=succeeded,
                    now=now,
                    attempt_context=attempt_context,
                    backend_repair=backend_repair,
                    radio_availability_observation=(
                        radio_availability_observation
                    ),
                )
                after = self._transaction_state_unlocked()
            finally:
                self._install_transaction_state_unlocked(before)
            return PreparedConnectivityAttempt(
                owner_id=id(self),
                before_state=copy.deepcopy(before),
                after_state=copy.deepcopy(after),
                receipt=copy.deepcopy(receipt),
            )

    def commit_prepared_attempt(
        self,
        prepared: PreparedConnectivityAttempt,
        *,
        commit: Callable[[], Any],
    ) -> Any:
        """Commit connectivity and its same-generation Matrix atomically."""

        if (
            not isinstance(prepared, PreparedConnectivityAttempt)
            or prepared.owner_id != id(self)
        ):
            raise TypeError("prepared_connectivity_attempt_owner_mismatch")
        if not callable(commit):
            raise TypeError("prepared_connectivity_attempt_commit_required")
        with self._lock:
            current = self._transaction_state_unlocked()
            if current != dict(prepared.before_state):
                raise RuntimeError(
                    "prepared_connectivity_attempt_state_changed"
                )
            self._install_transaction_state_unlocked(prepared.after_state)
            try:
                return commit()
            except Exception:
                self._install_transaction_state_unlocked(
                    prepared.before_state
                )
                raise

    def open_expected_interruption(
        self,
        *,
        operation_id: str,
        lever: str,
        duration_s: float,
        reason: str,
        now: Optional[float] = None,
    ) -> ModemLiveExpectedInterruption:
        """Open one physical-owner window immediately before a device write."""

        ts = float(now if now is not None else time.time())
        duration = float(duration_s)
        candidate = ModemLiveExpectedInterruption(
            operation_id=str(operation_id or "").strip(),
            lever=str(lever or "").strip(),
            opened_at=ts,
            diagnostic_deadline_at=ts + duration,
            reason=reason,
        )
        with self._lock:
            current = self._active_expected_interruption(now=ts)
            if current is not None:
                if current.operation_id != candidate.operation_id:
                    raise RuntimeError("expected_interruption_operation_conflict")
                return current
            self._expected_interruption = candidate
            log.info(
                "Modem Live expected interruption opened: lever=%s duration_s=%.1f operation=%s",
                candidate.lever,
                duration,
                candidate.operation_id,
            )
            return candidate

    def _active_expected_interruption(
        self,
        *,
        now: float,
    ) -> ModemLiveExpectedInterruption | None:
        interruption = self._expected_interruption
        if interruption is None:
            return None
        return interruption

    def expected_interruption(
        self,
        *,
        now: Optional[float] = None,
    ) -> ModemLiveExpectedInterruption | None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            return self._active_expected_interruption(now=ts)

    def expected_interruption_status_payload(
        self,
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return the current physical-owner window for Matrix projection."""

        ts = float(now if now is not None else time.time())
        interruption = self.expected_interruption(now=ts)
        return (
            interruption.to_status_payload(observed_at=ts)
            if interruption is not None
            else _inactive_expected_interruption_payload()
        )

    def admit_session_repair(
        self,
        *,
        request: ModemLiveSessionRepairRequest,
        now: float,
    ) -> ModemLiveSessionRepairAdmission:
        """Admit at most one physical session repair at the owned cadence."""

        ts = float(now)
        if not isinstance(request, ModemLiveSessionRepairRequest):
            raise TypeError("session_repair_request_required")
        if ts < request.observed_at:
            raise ValueError("session_repair_admission_before_evidence")
        if not isfinite(ts) or ts <= 0.0:
            raise ValueError("session_repair_admission_time_invalid")
        with self._lock:
            reason = ""
            next_eligible = float(self._session_repair_next_eligible_ts)
            interruption = self._active_expected_interruption(now=ts)
            # A completed radio write may invalidate the login. Waiting for a
            # good sample before permitting login would prevent recovery when
            # fresh device identity is itself available only after login.
            # Keep the interruption until readback proves recovery; only the
            # unfinished write / not-yet-observed loss blocks session repair.
            if interruption is not None and (
                interruption.command_completed_at <= 0.0
                or not interruption.miss_seen
            ):
                reason = "expected_interruption_active"
            elif self._session_repair_inflight_token:
                reason = "session_repair_inflight"
            elif next_eligible > ts and request.cause != "confirmed_session_expired":
                reason = "session_repair_backoff_active"
            if reason:
                self._session_repair_last_defer_reason = reason
                return ModemLiveSessionRepairAdmission(
                    owner_id=id(self),
                    token="",
                    admitted=False,
                    reason=reason,
                    admitted_at=0.0,
                    next_eligible_at=next_eligible,
                    attempt_number=int(self._session_repair_attempt_count),
                    request_id=request.request_id,
                    cause=request.cause,
                    source_epoch=request.source_epoch,
                    evidence_generation=request.evidence_generation,
                )

            self._session_repair_sequence += 1
            self._session_repair_attempt_count += 1
            token = (
                f"session-repair:{self._session_repair_sequence}:"
                f"{int(ts * 1_000_000)}"
            )
            self._session_repair_inflight_token = token
            self._last_reconnect_attempt_ts = ts
            self._session_repair_last_defer_reason = ""
            return ModemLiveSessionRepairAdmission(
                owner_id=id(self),
                token=token,
                admitted=True,
                reason="session_repair_admitted",
                admitted_at=ts,
                next_eligible_at=0.0,
                attempt_number=int(self._session_repair_attempt_count),
                request_id=request.request_id,
                cause=request.cause,
                source_epoch=request.source_epoch,
                evidence_generation=request.evidence_generation,
            )

    def complete_session_repair(
        self,
        admission: ModemLiveSessionRepairAdmission,
        *,
        kind: str,
        transport_succeeded: bool,
        recovered: bool,
        error: str,
        now: float,
    ) -> ModemLiveSessionRepairReceipt:
        """Close the exact admitted repair and schedule any later attempt."""

        if (
            not isinstance(admission, ModemLiveSessionRepairAdmission)
            or admission.owner_id != id(self)
            or not admission.admitted
        ):
            raise RuntimeError("session_repair_completion_admission_invalid")
        repair_kind = str(kind or "").strip()
        if not repair_kind:
            raise ValueError("session_repair_completion_kind_required")
        if type(transport_succeeded) is not bool or type(recovered) is not bool:
            raise TypeError("session_repair_completion_result_bool_required")
        repair_error = str(error or "").strip()
        completed = float(now)
        if (
            not isfinite(completed)
            or completed <= 0.0
            or completed < admission.admitted_at
        ):
            raise ValueError("session_repair_completion_time_invalid")
        with self._lock:
            if self._session_repair_inflight_token != admission.token:
                raise RuntimeError("session_repair_completion_token_mismatch")
            self._session_repair_inflight_token = ""
            self._session_repair_last_finished_ts = completed
            if recovered:
                self._reconnect_backoff_s = self._reconnect_interval_s
                self._session_repair_next_eligible_ts = 0.0
                self._session_repair_last_defer_reason = ""
            else:
                wait_s = (
                    self._reconnect_interval_s
                    if admission.attempt_number <= 1
                    else min(
                        self._reconnect_backoff_max_s,
                        max(
                            self._reconnect_interval_s,
                            self._reconnect_backoff_s * 2.0,
                        ),
                    )
                )
                self._reconnect_backoff_s = wait_s
                self._session_repair_next_eligible_ts = completed + wait_s
                self._session_repair_last_defer_reason = (
                    "session_repair_backoff_active"
                )
        return ModemLiveSessionRepairReceipt(
            admission_owner_id=id(self),
            admission_token=admission.token,
            kind=repair_kind,
            transport_succeeded=transport_succeeded,
            recovered=recovered,
            error=repair_error,
            started_at=admission.admitted_at,
            completed_at=completed,
            attempt_number=admission.attempt_number,
            request_id=admission.request_id,
            cause=admission.cause,
            source_epoch=admission.source_epoch,
            evidence_generation=admission.evidence_generation,
        )

    def complete_expected_interruption_operation(
        self,
        *,
        operation_id: str,
        now: Optional[float] = None,
    ) -> None:
        ts = float(now or time.time())
        with self._lock:
            interruption = self._active_expected_interruption(now=ts)
            if interruption is None:
                return
            if interruption.operation_id != str(operation_id or ""):
                raise RuntimeError("expected_interruption_operation_mismatch")
            self._expected_interruption = interruption.with_command_completed(
                completed_at=ts,
            )

    def close_expected_interruption_from_command_terminal(
        self,
        *,
        operation_id: str,
        terminal_reason: str,
        now: Optional[float] = None,
    ) -> bool:
        """Close only when the matching Core command becomes terminal."""

        root_id = str(operation_id or "").strip()
        reason = str(terminal_reason or "").strip()
        if not root_id or not reason:
            raise ValueError("expected_interruption_terminal_identity_required")
        ts = float(now if now is not None else time.time())
        with self._lock:
            interruption = self._expected_interruption
            if interruption is None:
                return False
            if interruption.operation_id != root_id:
                raise RuntimeError("expected_interruption_operation_mismatch")
            self._expected_interruption = None
            log.info(
                "Modem Live expected interruption closed by command terminal: "
                "lever=%s duration_s=%.3f miss_seen=%s operation=%s reason=%s",
                interruption.lever,
                max(0.0, ts - interruption.opened_at),
                interruption.miss_seen,
                interruption.operation_id,
                reason,
            )
            return True

    def _expected_interruption_for_attempt(
        self,
        *,
        now: float,
    ) -> ModemLiveExpectedInterruption | None:
        return self.expected_interruption(now=now)

    def import_state(
        self,
        *,
        consecutive_failures: int,
        last_failure_ts: float,
        last_reconnect_attempt_ts: float,
        reconnect_backoff_s: float,
        reconnect_backoff_max_s: float,
        degraded: bool,
        last_success_ts: float,
        intentional_switch: bool,
    ) -> None:
        with self._lock:
            self._consecutive_failures = int(consecutive_failures or 0)
            self._last_failure_ts = float(last_failure_ts or 0.0)
            self._last_reconnect_attempt_ts = float(last_reconnect_attempt_ts or 0.0)
            self._reconnect_backoff_s = float(
                reconnect_backoff_s or self._reconnect_interval_s
            )
            self._reconnect_backoff_max_s = float(reconnect_backoff_max_s or 60.0)
            self._degraded = bool(degraded)
            self._last_success_ts = float(last_success_ts or 0.0)
            self._intentional_switch = bool(intentional_switch)

    def export_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "owner": "modem_live_connectivity",
                "source": "connectivity_module",
                "consecutive_failures": int(self._consecutive_failures),
                "last_failure_ts": float(self._last_failure_ts),
                "last_reconnect_attempt_ts": float(self._last_reconnect_attempt_ts),
                "reconnect_backoff_s": float(self._reconnect_backoff_s),
                "reconnect_backoff_max_s": float(self._reconnect_backoff_max_s),
                "reconnect_interval_s": float(self._reconnect_interval_s),
                "session_repair_attempt_count": int(
                    self._session_repair_attempt_count
                ),
                "session_repair_inflight": bool(
                    self._session_repair_inflight_token
                ),
                "session_repair_last_finished_ts": float(
                    self._session_repair_last_finished_ts
                ),
                "session_repair_next_eligible_ts": float(
                    self._session_repair_next_eligible_ts
                ),
                "session_repair_defer_reason": str(
                    self._session_repair_last_defer_reason
                ),
                "degraded": bool(self._degraded),
                "last_success_ts": float(self._last_success_ts),
                "intentional_switch": bool(self._intentional_switch),
                "expected_interruption": self.expected_interruption_status_payload(),
                "observation_count": int(self._observation_count),
                "last_observation": dict(self._last_observation),
            }

    def _health(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now or time.time())
        failures = int(self._consecutive_failures)
        degraded = bool(self._degraded)
        backoff_s = float(self._reconnect_backoff_s)
        intentional = bool(self._intentional_switch)

        if failures == 0:
            outage_class = "none"
        elif intentional:
            outage_class = "intentional_switch"
        elif degraded:
            outage_class = "prolonged_unexpected"
        else:
            outage_class = "brief_unexpected"

        if failures == 0:
            state = "ok"
            status_text = "OK"
        elif intentional:
            state = "reconnecting"
            status_text = f"RECONNECTING (expected after radio action, attempt {failures})"
        elif degraded:
            outage_s = int(round(ts - self._last_success_ts)) if self._last_success_ts > 0 else 0
            outage_text = f"{outage_s // 60}m{outage_s % 60}s" if outage_s >= 60 else f"{outage_s}s"
            state = "degraded"
            status_text = f"DEGRADED ({outage_text}, next retry {int(backoff_s)}s)"
        else:
            state = "reconnecting"
            status_text = f"RECONNECTING (live modem read missed, attempt {failures})"

        expected_interruption = self.expected_interruption_status_payload(now=ts)
        return annotate_modem_read_miss_visibility({
            "owner": "modem_live_connectivity",
            "source": "connectivity_module",
            "state": state,
            "status_text": status_text,
            "outage_class": outage_class,
            "degraded": degraded,
            "intentional_switch": intentional,
            "expected_interruption": expected_interruption,
            "consecutive_failures": failures,
            "last_failure_ts": float(self._last_failure_ts),
            "last_success_ts": float(self._last_success_ts),
            "last_reconnect_attempt_ts": float(self._last_reconnect_attempt_ts),
            "reconnect_backoff_s": backoff_s,
            "reconnect_backoff_max_s": float(self._reconnect_backoff_max_s),
            "reconnect_interval_s": float(self._reconnect_interval_s),
            "session_repair_attempt_count": int(
                self._session_repair_attempt_count
            ),
            "session_repair_inflight": bool(
                self._session_repair_inflight_token
            ),
            "session_repair_last_finished_ts": float(
                self._session_repair_last_finished_ts
            ),
            "session_repair_next_eligible_ts": float(
                self._session_repair_next_eligible_ts
            ),
            "session_repair_defer_reason": str(
                self._session_repair_last_defer_reason
            ),
            "time_since_last_success_s": round(ts - self._last_success_ts, 1) if self._last_success_ts > 0 else 0.0,
            "time_since_last_failure_s": round(ts - self._last_failure_ts, 1) if self._last_failure_ts > 0 else 0.0,
        }, now=ts, min_s=self._read_miss_operator_min_s)

    def _next_attempt_id(self) -> str:
        self._physical_attempt_sequence += 1
        return f"connectivity-attempt-{self._physical_attempt_sequence}"

    def _claim_attempt_context(
        self,
        attempt_context: Optional[Mapping[str, Any]],
        *,
        now: float,
    ) -> Dict[str, Any]:
        context = dict(attempt_context or {})
        attempt_id = str(context.get("physical_attempt_id") or self._next_attempt_id())
        if attempt_id in self._observed_attempt_ids:
            raise RuntimeError("duplicate_connectivity_observation_for_physical_attempt")
        context["physical_attempt_id"] = attempt_id
        self._observed_attempt_ids[attempt_id] = float(now)
        while len(self._observed_attempt_ids) > 512:
            oldest = next(iter(self._observed_attempt_ids))
            self._observed_attempt_ids.pop(oldest, None)
        return context

    def _observation(
        self,
        *,
        outcome: str,
        now: float,
        attempt_context: Optional[Mapping[str, Any]],
        repair_attempted: bool,
        repair_succeeded: bool,
        repair_kind: str,
        repair_error: str,
        health: Mapping[str, Any],
    ) -> Dict[str, Any]:
        context = dict(attempt_context or {})
        attempt_id = str(context.get("physical_attempt_id") or "")
        if not attempt_id:
            raise RuntimeError("connectivity_physical_attempt_identity_missing")
        observation = {
            "schema": CONNECTIVITY_OBSERVATION_SCHEMA,
            "owner": "modem_live_connectivity",
            "physical_attempt_id": attempt_id,
            "observed_ts": float(now),
            "outcome": str(outcome or "unknown"),
            "source_epoch": str(context.get("source_epoch") or ""),
            "source_generation": int(context.get("source_generation", 0) or 0),
            "session_scope_key": str(context.get("session_scope_key") or ""),
            "identity_key": str(context.get("identity_key") or ""),
            "identity_code": str(context.get("identity_code") or ""),
            "read_failure_reason": str(
                context.get("read_failure_reason") or ""
            ),
            "failure_streak": int(self._consecutive_failures),
            "session_repair_attempted": bool(repair_attempted),
            "session_repair_kind": str(repair_kind or ""),
            "session_repair_result": (
                "restored"
                if repair_succeeded
                else "failed"
                if repair_attempted
                else "not_attempted"
            ),
            "session_repair_error": str(repair_error or ""),
            "expected_interruption": self.expected_interruption_status_payload(now=now),
            "health": dict(health or {}),
        }
        self._observation_count += 1
        self._last_observation = dict(observation)
        return observation

    def record_physical_attempt(
        self,
        *,
        succeeded: bool,
        now: Optional[float] = None,
        attempt_context: Optional[Mapping[str, Any]] = None,
        backend_repair: ModemLiveSessionRepairReceipt | None = None,
        radio_availability_observation: (
            ModemLiveRadioAvailabilityObservation | None
        ) = None,
    ) -> Dict[str, Any]:
        """Publish exactly one receipt for one physical Modem Live attempt.

        Backend session repair is evidence inside this receipt. A restored
        backend session never erases the failed physical read; only a later
        successful physical attempt clears the failure streak.
        """
        ts = float(now or time.time())
        if radio_availability_observation is not None and not isinstance(
            radio_availability_observation,
            ModemLiveRadioAvailabilityObservation,
        ):
            raise TypeError(
                "radio_availability_observation_required"
            )
        with self._lock:
            if backend_repair is not None and not isinstance(
                backend_repair,
                ModemLiveSessionRepairReceipt,
            ):
                raise TypeError("modem_live_session_repair_receipt_required")
            if backend_repair is not None:
                if backend_repair.admission_owner_id != id(self):
                    raise RuntimeError(
                        "modem_live_session_repair_receipt_owner_mismatch"
                    )
                if backend_repair.completed_at > ts:
                    raise RuntimeError(
                        "modem_live_session_repair_receipt_from_future"
                    )
                if (
                    backend_repair.admission_token
                    in self._observed_session_repair_tokens
                ):
                    raise RuntimeError(
                        "modem_live_session_repair_receipt_already_observed"
                    )
            claimed_context = self._claim_attempt_context(
                attempt_context,
                now=ts,
            )
            if backend_repair is not None:
                self._observed_session_repair_tokens[
                    backend_repair.admission_token
                ] = backend_repair.completed_at
                while len(self._observed_session_repair_tokens) > 512:
                    oldest = next(iter(self._observed_session_repair_tokens))
                    self._observed_session_repair_tokens.pop(oldest, None)
            repair_attempted = backend_repair is not None
            repair_succeeded = bool(
                backend_repair.recovered
                if backend_repair is not None
                else False
            )
            repair_kind = str(
                backend_repair.kind if backend_repair is not None else ""
            )
            repair_error = str(
                backend_repair.error if backend_repair is not None else ""
            )
            expected_interruption = self._expected_interruption_for_attempt(now=ts)
            restart_radio_observation = (
                radio_availability_observation
                if expected_interruption is not None
                and expected_interruption.lever == "RESTART"
                and radio_availability_observation is not None
                and radio_availability_observation.started_at
                >= expected_interruption.opened_at
                else None
            )
            radio_down_observed = bool(
                expected_interruption is not None
                and expected_interruption.lever == "RESTART"
                and restart_radio_observation is not None
                and not restart_radio_observation.available
            )
            if radio_down_observed:
                assert expected_interruption is not None
                first_radio_down = not expected_interruption.radio_down_seen
                expected_interruption = (
                    expected_interruption.with_radio_down_seen(
                        observed_at=(
                            restart_radio_observation.observed_at
                        ),
                    )
                )
                self._expected_interruption = expected_interruption
                if first_radio_down:
                    log.info(
                        "Modem Live restart radio-down observed: operation=%s",
                        expected_interruption.operation_id,
                    )
            if succeeded:
                completed_expected_interruption = bool(
                    expected_interruption is not None
                    and expected_interruption.may_close_on_success(
                        radio_observation=restart_radio_observation
                    )
                )
                expected_interruption_miss_seen = bool(
                    completed_expected_interruption
                    and expected_interruption is not None
                    and expected_interruption.miss_seen
                )
                if completed_expected_interruption:
                    assert expected_interruption is not None
                    log.info(
                        "Modem Live expected interruption closed by valid sample: lever=%s duration_s=%.3f miss_seen=%s operation=%s",
                        expected_interruption.lever,
                        max(0.0, ts - expected_interruption.opened_at),
                        expected_interruption.miss_seen,
                        expected_interruption.operation_id,
                    )
                event = {
                    "had_failures": bool(self._consecutive_failures > 0),
                    "was_degraded": bool(self._degraded),
                    "was_intentional": bool(self._intentional_switch),
                    "prev_failures": int(self._consecutive_failures),
                    # Foundation connectivity transition is the sole outage
                    # duration owner.  A successful Modem Live read reports
                    # only whether it closed a failure streak.
                    "outage_duration_s": 0.0,
                    "reconnect_attempted": repair_attempted,
                    "reconnect_succeeded": repair_succeeded,
                    "reconnect_error": repair_error,
                    "completed_expected_interruption": completed_expected_interruption,
                    "expected_interruption_miss_seen": expected_interruption_miss_seen,
                    "expected_interruption_radio_observation": (
                        restart_radio_observation.to_payload()
                        if restart_radio_observation is not None
                        else None
                    ),
                }
                self._consecutive_failures = 0
                self._degraded = False
                self._reconnect_backoff_s = self._reconnect_interval_s
                self._session_repair_attempt_count = 0
                self._session_repair_next_eligible_ts = 0.0
                self._session_repair_last_defer_reason = ""
                self._last_success_ts = ts
                self._intentional_switch = False
                event["health"] = self._health(now=ts)
                event["health"]["reconnect_attempted"] = repair_attempted
                event["health"]["reconnect_succeeded"] = repair_succeeded
                event["observation"] = self._observation(
                    outcome=(
                        "expected_interruption_radio_down"
                        if radio_down_observed
                        else "success"
                    ),
                    now=ts,
                    attempt_context=claimed_context,
                    repair_attempted=repair_attempted,
                    repair_succeeded=repair_succeeded,
                    repair_kind=repair_kind,
                    repair_error=repair_error,
                    health=event["health"],
                )
                if completed_expected_interruption:
                    self._expected_interruption = None
                return event

            expected_interruption_active = expected_interruption is not None
            if expected_interruption_active:
                assert expected_interruption is not None
                first_expected_miss = not expected_interruption.miss_seen
                expected_interruption = expected_interruption.with_miss_seen()
                self._expected_interruption = expected_interruption
                if first_expected_miss:
                    log.info(
                        "Modem Live expected interruption observed: lever=%s operation=%s",
                        expected_interruption.lever,
                        expected_interruption.operation_id,
                    )
            self._consecutive_failures += 1
            self._last_failure_ts = ts
            if expected_interruption_active:
                self._intentional_switch = True
            elif self._intentional_switch:
                self._intentional_switch = False

            became_degraded = False
            if (
                not self._degraded
                and self._consecutive_failures >= 15
                and not expected_interruption_active
            ):
                self._degraded = True
                became_degraded = True

            health = self._health(now=ts)
            health["reconnect_attempted"] = repair_attempted
            health["reconnect_succeeded"] = repair_succeeded
            health = annotate_modem_read_miss_visibility(
                health,
                now=ts,
                min_s=self._read_miss_operator_min_s,
            )
            observation = self._observation(
                outcome=(
                    "expected_interruption_miss"
                    if expected_interruption_active
                    else "read_failed"
                ),
                now=ts,
                attempt_context=claimed_context,
                repair_attempted=repair_attempted,
                repair_succeeded=repair_succeeded,
                repair_kind=repair_kind,
                repair_error=repair_error,
                health=health,
            )
            return {
                "failure_count": int(self._consecutive_failures),
                "became_degraded": bool(became_degraded),
                "reconnect_attempted": repair_attempted,
                "reconnect_succeeded": repair_succeeded,
                "reconnect_interval_s": float(self._reconnect_interval_s),
                "reconnect_error": repair_error,
                "reconnect_info": {
                    "kind": repair_kind,
                    "result": (
                        "restored"
                        if repair_succeeded
                        else "failed"
                        if repair_attempted
                        else "not_attempted"
                    ),
                },
                "expected_interruption": (
                    expected_interruption.to_status_payload(observed_at=ts)
                    if expected_interruption is not None
                    else _inactive_expected_interruption_payload()
                ),
                "observation": observation,
                "health": health,
            }

    def get_modem_health_status(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        return self._health(now=now)


__all__ = [
    "ConnectivityModule",
    "ModemLiveExpectedInterruption",
    "ModemLiveRadioAvailabilityObservation",
    "ModemLiveSessionRepairAdmission",
    "ModemLiveSessionRepairReceipt",
    "PreparedConnectivityAttempt",
]
