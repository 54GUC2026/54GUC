"""Foundation view of Modem Live connectivity changes."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from threading import RLock
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.current_health import (
    CoreCurrentHealthView,
    ModemLiveConnectivityHealthFacts,
    ModemLiveHealthState,
)
from arch4.foundation.ngui_extension import FoundationNGUIExtensionPublisher
from arch4.foundation.runtime_artifacts import CgfEvidenceStore


FOUNDATION_CONNECTIVITY_CLIENT_ID = "foundation.connectivity_transition"
FOUNDATION_CONNECTIVITY_TRANSITION_OWNER = (
    "54guc.foundation.connectivity_transition"
)
FOUNDATION_CONNECTIVITY_TRANSITION_SCHEMA = (
    "FoundationConnectivityTransition.v2"
)
FOUNDATION_CONNECTIVITY_STATES = frozenset(
    state.value for state in ModemLiveHealthState
)


@dataclass(frozen=True, slots=True)
class FoundationConnectivityTransition:
    sequence: int
    observed_at: float
    outage_started_at: float
    old_state: str
    new_state: str
    reason: str
    outage_duration_s: float
    consecutive_failures: int
    intentional_switch: bool
    physical_attempt_id: str
    read_failure_reason: str
    session_repair_attempted: bool
    session_repair_result: str
    session_repair_kind: str
    session_repair_error: str
    schema: str = FOUNDATION_CONNECTIVITY_TRANSITION_SCHEMA
    owner: str = FOUNDATION_CONNECTIVITY_TRANSITION_OWNER

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("foundation_connectivity_positive_sequence_required")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("foundation_connectivity_positive_timestamp_required")
        if (
            type(self.outage_started_at) is not float
            or not math.isfinite(self.outage_started_at)
            or self.outage_started_at <= 0.0
        ):
            raise ValueError(
                "foundation_connectivity_positive_outage_start_required"
            )
        if self.outage_started_at > self.observed_at:
            raise ValueError(
                "foundation_connectivity_outage_start_after_observation"
            )
        for field_name in ("old_state", "new_state"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or value not in FOUNDATION_CONNECTIVITY_STATES
            ):
                raise ValueError(
                    f"foundation_connectivity_{field_name}_invalid"
                )
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or self.reason != self.reason.strip()
        ):
            raise ValueError("foundation_connectivity_reason_required")
        if (
            type(self.outage_duration_s) is not float
            or not math.isfinite(self.outage_duration_s)
            or self.outage_duration_s < 0.0
        ):
            raise ValueError(
                "foundation_connectivity_outage_duration_invalid"
            )
        if (
            type(self.consecutive_failures) is not int
            or self.consecutive_failures < 0
        ):
            raise ValueError(
                "foundation_connectivity_failure_count_invalid"
            )
        if type(self.intentional_switch) is not bool:
            raise TypeError(
                "foundation_connectivity_intentional_switch_bool_required"
            )
        for field_name in (
            "physical_attempt_id",
            "read_failure_reason",
            "session_repair_result",
            "session_repair_kind",
            "session_repair_error",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(
                    f"foundation_connectivity_{field_name}_text_required"
                )
        if type(self.session_repair_attempted) is not bool:
            raise TypeError(
                "foundation_connectivity_session_repair_attempted_bool_required"
            )
        if self.session_repair_result not in {
            "",
            "not_attempted",
            "restored",
            "failed",
        }:
            raise ValueError(
                "foundation_connectivity_session_repair_result_invalid"
            )
        if self.schema != FOUNDATION_CONNECTIVITY_TRANSITION_SCHEMA:
            raise ValueError("foundation_connectivity_transition_schema_invalid")
        if self.owner != FOUNDATION_CONNECTIVITY_TRANSITION_OWNER:
            raise ValueError("foundation_connectivity_transition_owner_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "outage_started_at": self.outage_started_at,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "reason": self.reason,
            "outage_duration_s": self.outage_duration_s,
            "consecutive_failures": self.consecutive_failures,
            "intentional_switch": self.intentional_switch,
            "physical_attempt_id": self.physical_attempt_id,
            "read_failure_reason": self.read_failure_reason,
            "session_repair_attempted": self.session_repair_attempted,
            "session_repair_result": self.session_repair_result,
            "session_repair_kind": self.session_repair_kind,
            "session_repair_error": self.session_repair_error,
        }


@dataclass(frozen=True, slots=True)
class FoundationConnectivityDeliveryObligation:
    namespace: RuntimeGenerationNamespace
    transition: FoundationConnectivityTransition
    next_raw_state: str
    next_operator_state: str
    next_outage_started_at: float
    next_outage_intentional: bool
    ngui_delivered: bool = False
    diagnostic_delivered: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "foundation_connectivity_delivery_namespace_required"
            )
        if not isinstance(
            self.transition,
            FoundationConnectivityTransition,
        ):
            raise TypeError(
                "foundation_connectivity_delivery_transition_required"
            )
        if self.next_raw_state not in FOUNDATION_CONNECTIVITY_STATES:
            raise ValueError(
                "foundation_connectivity_delivery_raw_state_invalid"
            )
        if self.next_operator_state not in FOUNDATION_CONNECTIVITY_STATES:
            raise ValueError(
                "foundation_connectivity_delivery_operator_state_invalid"
            )
        if (
            type(self.next_outage_started_at) is not float
            or not math.isfinite(self.next_outage_started_at)
            or self.next_outage_started_at < 0.0
        ):
            raise ValueError(
                "foundation_connectivity_delivery_outage_start_invalid"
            )
        for field_name in (
            "next_outage_intentional",
            "ngui_delivered",
            "diagnostic_delivered",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"foundation_connectivity_delivery_{field_name}_bool_required"
                )

    @property
    def delivered(self) -> bool:
        return self.ngui_delivered and self.diagnostic_delivered


class FoundationConnectivityTransitionProjector:
    """Detect a connectivity change once and send the same record to both consumers."""

    def __init__(
        self,
        *,
        diagnostics: CgfEvidenceStore,
        ngui_extension: FoundationNGUIExtensionPublisher,
    ) -> None:
        if not isinstance(diagnostics, CgfEvidenceStore):
            raise TypeError("foundation_connectivity_diagnostics_required")
        if not isinstance(ngui_extension, FoundationNGUIExtensionPublisher):
            raise TypeError("foundation_connectivity_ngui_extension_required")
        self._diagnostics = diagnostics
        self._ngui_extension = ngui_extension
        self._lock = RLock()
        self._namespace: RuntimeGenerationNamespace | None = None
        self._raw_state = ""
        self._operator_state = ""
        self._sequence = 0
        self._outage_started_at = 0.0
        self._outage_intentional = False
        self._pending_delivery: (
            FoundationConnectivityDeliveryObligation | None
        ) = None

    def observe(
        self,
        current_health: CoreCurrentHealthView,
    ) -> FoundationConnectivityTransition | None:
        with self._lock:
            return self._observe_locked(current_health)

    def _observe_locked(
        self,
        current_health: CoreCurrentHealthView,
    ) -> FoundationConnectivityTransition | None:
        if not isinstance(current_health, CoreCurrentHealthView):
            raise TypeError(
                "foundation_connectivity_current_health_required"
            )
        namespace = current_health.namespace
        pending = self._pending_delivery
        if pending is not None:
            if pending.namespace != namespace:
                raise RuntimeError(
                    "foundation_connectivity_pending_delivery_blocks_cutover"
                )
            return self._deliver_pending_locked()
        if self._namespace != namespace:
            self._namespace = namespace
            self._raw_state = ""
            self._operator_state = ""
            self._sequence = 0
            self._outage_started_at = 0.0
            self._outage_intentional = False
        facts = current_health.facts
        if facts is None:
            return None
        state = facts.state.value
        operator_visible = facts.modem_read_missed_operator_visible
        now = current_health.observed_at
        if type(now) is not float or not math.isfinite(now) or now <= 0.0:
            raise ValueError("foundation_connectivity_positive_timestamp_required")
        intentional_value = facts.intentional_switch
        failure_value = facts.consecutive_failures
        if not self._raw_state:
            self._raw_state = state
            self._operator_state = (
                state if state == "ok" or operator_visible else "ok"
            )
            if state != "ok":
                self._outage_started_at = now
                self._outage_intentional = intentional_value
            return None

        raw_old_state = self._raw_state
        next_outage_started_at = self._outage_started_at
        next_outage_intentional = self._outage_intentional
        if raw_old_state == "ok" and state != "ok":
            next_outage_started_at = now
            next_outage_intentional = intentional_value
        elif state != "ok":
            next_outage_intentional = (
                next_outage_intentional or intentional_value
            )

        if state != "ok" and not operator_visible:
            self._raw_state = state
            self._outage_started_at = next_outage_started_at
            self._outage_intentional = next_outage_intentional
            return None
        if state == "ok" and self._operator_state == "ok":
            self._raw_state = state
            self._outage_started_at = 0.0
            self._outage_intentional = False
            return None
        if state == self._operator_state:
            self._raw_state = state
            self._outage_started_at = next_outage_started_at
            self._outage_intentional = next_outage_intentional
            return None

        old_state = self._operator_state
        outage_duration_s = 0.0
        if state == "ok" and next_outage_started_at > 0.0:
            outage_duration_s = max(
                0.0,
                now - next_outage_started_at,
            )

        transition = FoundationConnectivityTransition(
            sequence=self._sequence + 1,
            observed_at=now,
            outage_started_at=(
                next_outage_started_at
                if next_outage_started_at > 0.0
                else now
            ),
            old_state=old_state,
            new_state=state,
            reason=self._transition_reason(
                facts,
                old_state=old_state,
                new_state=state,
            ),
            outage_duration_s=outage_duration_s,
            consecutive_failures=failure_value,
            intentional_switch=(
                intentional_value or next_outage_intentional
            ),
            **self._connectivity_diagnostic(current_health),
        )
        self._pending_delivery = FoundationConnectivityDeliveryObligation(
            namespace=namespace,
            transition=transition,
            next_raw_state=state,
            next_operator_state=state,
            next_outage_started_at=(
                0.0 if state == "ok" else next_outage_started_at
            ),
            next_outage_intentional=(
                False if state == "ok" else next_outage_intentional
            ),
        )
        return self._deliver_pending_locked()

    @staticmethod
    def _connectivity_diagnostic(
        current_health: CoreCurrentHealthView,
    ) -> dict[str, Any]:
        fast_lane = current_health.modem_live_fast_lane.to_mapping()
        connectivity = fast_lane.get("connectivity")
        connectivity = connectivity if isinstance(connectivity, dict) else {}
        observation = connectivity.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        return {
            "physical_attempt_id": str(
                observation.get("physical_attempt_id") or ""
            ),
            "read_failure_reason": str(
                observation.get("read_failure_reason") or ""
            ),
            "session_repair_attempted": bool(
                observation.get("session_repair_attempted", False)
            ),
            "session_repair_result": str(
                observation.get("session_repair_result") or ""
            ),
            "session_repair_kind": str(
                observation.get("session_repair_kind") or ""
            ),
            "session_repair_error": str(
                observation.get("session_repair_error") or ""
            ),
        }

    @staticmethod
    def _transition_reason(
        facts: ModemLiveConnectivityHealthFacts,
        *,
        old_state: str,
        new_state: str,
    ) -> str:
        if facts.status_text:
            return facts.status_text
        if facts.outage_class.value:
            return facts.outage_class.value
        return f"{old_state}->{new_state}"

    def _deliver_pending_locked(
        self,
    ) -> FoundationConnectivityTransition:
        obligation = self._pending_delivery
        if obligation is None:
            raise RuntimeError(
                "foundation_connectivity_delivery_obligation_missing"
            )
        transition = obligation.transition
        payload = transition.to_payload()
        if not obligation.ngui_delivered:
            frame = self._ngui_extension.record_connectivity_transition(
                payload,
                namespace=obligation.namespace,
                observed_at=transition.observed_at,
                sample_sequence=transition.sequence,
            )
            if (
                frame.namespace != obligation.namespace
                or frame.values.to_mapping().get(
                    "event.connectivity_transition"
                )
                != payload
                or frame.sample_sequences.to_mapping().get(
                    "event.connectivity_transition"
                )
                != transition.sequence
            ):
                raise RuntimeError(
                    "foundation_connectivity_ngui_delivery_mismatch"
                )
            obligation = replace(obligation, ngui_delivered=True)
            self._pending_delivery = obligation
        if not obligation.diagnostic_delivered:
            delivered = self._diagnostics.log_event(
                "modem_connectivity_transition",
                event_owner=transition.owner,
                severity=(
                    "INFO"
                    if transition.new_state == "ok"
                    else "WARNING"
                ),
                detail=transition.reason,
                **payload,
            )
            if delivered is not True:
                raise RuntimeError(
                    "foundation_connectivity_diagnostic_delivery_unacknowledged"
                )
            obligation = replace(
                obligation,
                diagnostic_delivered=True,
            )
            self._pending_delivery = obligation
        if not obligation.delivered:
            raise RuntimeError(
                "foundation_connectivity_delivery_incomplete"
            )
        self._sequence = transition.sequence
        self._raw_state = obligation.next_raw_state
        self._operator_state = obligation.next_operator_state
        self._outage_started_at = obligation.next_outage_started_at
        self._outage_intentional = obligation.next_outage_intentional
        self._pending_delivery = None
        return transition


__all__ = (
    "FOUNDATION_CONNECTIVITY_CLIENT_ID",
    "FOUNDATION_CONNECTIVITY_TRANSITION_OWNER",
    "FOUNDATION_CONNECTIVITY_TRANSITION_SCHEMA",
    "FOUNDATION_CONNECTIVITY_STATES",
    "FoundationConnectivityDeliveryObligation",
    "FoundationConnectivityTransition",
    "FoundationConnectivityTransitionProjector",
)
