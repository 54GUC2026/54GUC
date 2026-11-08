"""Store the latest Modem Live RTT observation and probe result."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.rtt_observation import (
    PROBE_FAILED,
    PROBE_NEVER,
    PROBE_OK,
    PROBE_REACHABLE_ONLY,
    PROBE_UNKNOWN,
    RttObservation,
)


OWNER = "modem_live_rtt_observation_authority"


@dataclass(frozen=True, slots=True)
class ModemLiveRttNamespaceBindingPlan:
    requested_namespace: RuntimeGenerationNamespace
    prior_namespace: RuntimeGenerationNamespace | None
    prior_value_ms: float | None
    prior_observed_at: float
    prior_probe_status: str
    prior_target: str
    prior_last_probe_at: float
    prior_failure_reason: str
    prior_sequence: int
    prior_success_count: int
    prior_failure_count: int
    already_bound: bool

    def __post_init__(self) -> None:
        if not isinstance(
            self.requested_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("rtt_binding_plan_namespace_required")
        if self.prior_namespace is not None and not isinstance(
            self.prior_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("rtt_binding_plan_prior_namespace_invalid")
        if type(self.already_bound) is not bool:
            raise TypeError("rtt_binding_plan_disposition_invalid")
        if self.already_bound != (
            self.prior_namespace == self.requested_namespace
        ):
            raise ValueError("rtt_binding_plan_disposition_inconsistent")


@dataclass(frozen=True, slots=True)
class ModemLiveRttNamespaceBindingReceipt:
    namespace: RuntimeGenerationNamespace
    prior_namespace: RuntimeGenerationNamespace | None
    committed: bool
    already_bound: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("rtt_binding_receipt_namespace_required")
        if self.prior_namespace is not None and not isinstance(
            self.prior_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("rtt_binding_receipt_prior_namespace_invalid")
        if self.committed is not True or type(self.already_bound) is not bool:
            raise ValueError("rtt_binding_receipt_disposition_invalid")
        if self.already_bound != (
            self.prior_namespace == self.namespace
        ):
            raise ValueError("rtt_binding_receipt_disposition_inconsistent")


class ModemLiveRttObservationAuthority:
    """Store the last good RTT and the exact result of the latest probe.

    Modem Live acquisition still schedules and runs the probe. This class keeps
    its mutable evidence and creates immutable ``RttObservation`` snapshots.
    """

    owner = OWNER

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._value_ms: float | None = None
        self._observed_at = 0.0
        self._probe_status = PROBE_NEVER
        self._target = ""
        self._last_probe_at = 0.0
        self._failure_reason = ""
        self._sequence = 0
        self._success_count = 0
        self._failure_count = 0
        self._namespace: RuntimeGenerationNamespace | None = None

    def prepare_runtime_namespace_binding(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> ModemLiveRttNamespaceBindingPlan:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("rtt_authority_runtime_namespace_required")
        with self._lock:
            if self._namespace is not None and self._namespace != namespace:
                raise RuntimeError(
                    "rtt_authority_runtime_namespace_rebind_forbidden"
                )
            if self._namespace is None and any(
                (
                    self._value_ms is not None,
                    self._observed_at != 0.0,
                    self._probe_status != PROBE_NEVER,
                    bool(self._target),
                    self._last_probe_at != 0.0,
                    bool(self._failure_reason),
                    self._sequence != 0,
                    self._success_count != 0,
                    self._failure_count != 0,
                )
            ):
                raise RuntimeError("rtt_authority_unbound_state_not_pristine")
            return ModemLiveRttNamespaceBindingPlan(
                requested_namespace=namespace,
                prior_namespace=self._namespace,
                prior_value_ms=self._value_ms,
                prior_observed_at=self._observed_at,
                prior_probe_status=self._probe_status,
                prior_target=self._target,
                prior_last_probe_at=self._last_probe_at,
                prior_failure_reason=self._failure_reason,
                prior_sequence=self._sequence,
                prior_success_count=self._success_count,
                prior_failure_count=self._failure_count,
                already_bound=self._namespace == namespace,
            )

    def commit_runtime_namespace_binding(
        self,
        plan: ModemLiveRttNamespaceBindingPlan,
    ) -> ModemLiveRttNamespaceBindingReceipt:
        if not isinstance(plan, ModemLiveRttNamespaceBindingPlan):
            raise TypeError("rtt_binding_plan_required")
        with self._lock:
            if self._namespace != plan.prior_namespace:
                raise RuntimeError("rtt_binding_prior_namespace_changed")
            if (
                self._value_ms != plan.prior_value_ms
                or self._observed_at != plan.prior_observed_at
                or self._probe_status != plan.prior_probe_status
                or self._target != plan.prior_target
                or self._last_probe_at != plan.prior_last_probe_at
                or self._failure_reason != plan.prior_failure_reason
                or self._sequence != plan.prior_sequence
                or self._success_count != plan.prior_success_count
                or self._failure_count != plan.prior_failure_count
            ):
                raise RuntimeError("rtt_binding_state_changed_after_prepare")
            self._namespace = plan.requested_namespace
            return ModemLiveRttNamespaceBindingReceipt(
                namespace=plan.requested_namespace,
                prior_namespace=plan.prior_namespace,
                committed=True,
                already_bound=plan.already_bound,
            )

    def rollback_runtime_namespace_binding(
        self,
        plan: ModemLiveRttNamespaceBindingPlan,
        receipt: ModemLiveRttNamespaceBindingReceipt,
    ) -> None:
        if not isinstance(plan, ModemLiveRttNamespaceBindingPlan) or not (
            isinstance(receipt, ModemLiveRttNamespaceBindingReceipt)
        ):
            raise TypeError("rtt_binding_rollback_receipt_required")
        if receipt.namespace != plan.requested_namespace:
            raise ValueError("rtt_binding_rollback_identity_mismatch")
        with self._lock:
            if self._namespace != receipt.namespace:
                raise RuntimeError("rtt_binding_rollback_namespace_changed")
            if (
                self._value_ms != plan.prior_value_ms
                or self._observed_at != plan.prior_observed_at
                or self._probe_status != plan.prior_probe_status
                or self._target != plan.prior_target
                or self._last_probe_at != plan.prior_last_probe_at
                or self._failure_reason != plan.prior_failure_reason
                or self._sequence != plan.prior_sequence
                or self._success_count != plan.prior_success_count
                or self._failure_count != plan.prior_failure_count
            ):
                raise RuntimeError(
                    "rtt_binding_rollback_after_observation_forbidden"
                )
            self._namespace = plan.prior_namespace

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeGenerationNamespace:
        del namespace
        raise RuntimeError(
            "rtt_direct_namespace_bind_forbidden_use_transaction"
        )

    def _require_namespace(self) -> RuntimeGenerationNamespace:
        namespace = self._namespace
        if namespace is None:
            raise RuntimeError("rtt_authority_runtime_namespace_unbound")
        return namespace

    def observe_success(
        self,
        value: Any,
        *,
        target: str,
        observed_at: float | None = None,
    ) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed <= 0.0:
            return None
        timestamp = float(time.time() if observed_at is None else observed_at)
        probe_target = str(target or "").strip()
        if not probe_target:
            raise ValueError("rtt_observation_target_required")
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            raise ValueError("rtt_observation_timestamp_invalid")
        with self._lock:
            self._require_namespace()
            if timestamp < self._last_probe_at:
                raise ValueError("rtt_observation_probe_time_regressed")
            self._value_ms = parsed
            self._observed_at = timestamp
            self._probe_status = PROBE_OK
            self._target = probe_target
            self._last_probe_at = timestamp
            self._failure_reason = ""
            self._sequence += 1
            self._success_count += 1
        return parsed

    def observe_reachable_only(
        self,
        *,
        target: str,
        observed_at: float | None = None,
    ) -> None:
        """Publish reachability without changing the primary RTT value."""

        timestamp = float(time.time() if observed_at is None else observed_at)
        probe_target = str(target or "").strip()
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            raise ValueError("rtt_observation_timestamp_invalid")
        if not probe_target:
            raise ValueError("rtt_observation_target_required")
        with self._lock:
            self._require_namespace()
            if timestamp < self._last_probe_at:
                raise ValueError("rtt_observation_probe_time_regressed")
            self._probe_status = PROBE_REACHABLE_ONLY
            self._target = probe_target
            self._last_probe_at = timestamp
            self._failure_reason = ""
            self._sequence += 1
            self._success_count += 1

    def observe_failure(
        self,
        reason: Any,
        *,
        target: str,
        failed_at: float | None = None,
    ) -> None:
        self._observe_unsuccessful(
            reason, target=target, failed_at=failed_at, status=PROBE_FAILED
        )

    def observe_unknown(
        self,
        reason: Any,
        *,
        target: str,
        failed_at: float | None = None,
    ) -> None:
        """Retain an unmeasurable attempt without voting Internet failure."""
        self._observe_unsuccessful(
            reason, target=target, failed_at=failed_at, status=PROBE_UNKNOWN
        )

    def _observe_unsuccessful(
        self,
        reason: Any,
        *,
        target: str,
        failed_at: float | None,
        status: str,
    ) -> None:
        timestamp = float(time.time() if failed_at is None else failed_at)
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            raise ValueError("rtt_probe_failure_timestamp_invalid")
        message = str(reason or "rtt_probe_returned_no_value").strip()
        probe_target = str(target or "").strip()
        if not probe_target:
            raise ValueError("rtt_observation_target_required")
        if not message:
            message = "rtt_probe_returned_no_value"
        with self._lock:
            self._require_namespace()
            if timestamp < self._last_probe_at:
                raise ValueError("rtt_observation_probe_time_regressed")
            self._probe_status = status
            self._target = probe_target
            self._last_probe_at = timestamp
            self._failure_reason = message
            self._sequence += 1
            if status == PROBE_FAILED:
                self._failure_count += 1

    def snapshot(
        self,
        *,
        now: float | None = None,
        max_age_s: float,
    ) -> RttObservation:
        return self.read_register(now=now, max_age_s=max_age_s)[1]

    def read_register(
        self,
        *,
        now: float | None = None,
        max_age_s: float,
    ) -> tuple[int, RttObservation]:
        """Read the revision and immutable RTT observation atomically."""

        now_ts = float(time.time() if now is None else now)
        with self._lock:
            namespace = self._require_namespace()
            snapshot_at = max(now_ts, self._observed_at, self._last_probe_at)
            observation = RttObservation.from_owner_state(
                namespace=namespace,
                probe_sequence=self._sequence,
                value_ms=self._value_ms,
                observed_at=self._observed_at,
                snapshot_at=snapshot_at,
                max_age_s=float(max_age_s or 0.0),
                probe_status=self._probe_status,
                target=self._target,
                last_probe_at=self._last_probe_at,
                failure_reason=self._failure_reason,
            )
            return int(self._sequence), observation

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": self.owner,
                "namespace": (
                    self._namespace.as_dict()
                    if self._namespace is not None
                    else {}
                ),
                "sequence": int(self._sequence),
                "value_ms": self._value_ms,
                "observed_at": float(self._observed_at),
                "probe_status": str(self._probe_status),
                "target": str(self._target),
                "last_probe_at": float(self._last_probe_at),
                "failure_reason": str(self._failure_reason),
                "success_count": int(self._success_count),
                "failure_count": int(self._failure_count),
            }


__all__ = [
    "ModemLiveRttNamespaceBindingPlan",
    "ModemLiveRttNamespaceBindingReceipt",
    "ModemLiveRttObservationAuthority",
    "OWNER",
]
