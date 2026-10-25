"""Typed read-only boundary for Core-owned current runtime health."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.payload import CoreFrozenPayload


CORE_CURRENT_HEALTH_SCHEMA = "CoreCurrentHealthView.v1"
CORE_CURRENT_HEALTH_OWNER = "54guc.core.runtime_register_health"
MODEM_LIVE_CURRENT_HEALTH_SCHEMA = "ModemLiveCurrentHealthPublication.v1"
MODEM_LIVE_CURRENT_HEALTH_OWNER = "54guc.modem_live.current_health"


class ModemLiveHealthState(StrEnum):
    OK = "ok"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"


class ModemLiveOutageClass(StrEnum):
    NONE = "none"
    INTENTIONAL_SWITCH = "intentional_switch"
    BRIEF_UNEXPECTED = "brief_unexpected"
    PROLONGED_UNEXPECTED = "prolonged_unexpected"


@dataclass(frozen=True, slots=True)
class ModemLiveConnectivityHealthFacts:
    state: ModemLiveHealthState
    outage_class: ModemLiveOutageClass
    degraded: bool
    intentional_switch: bool
    consecutive_failures: int
    last_failure_ts: float
    last_success_ts: float
    last_reconnect_attempt_ts: float
    reconnect_backoff_s: float
    modem_read_missed_duration_s: float
    modem_read_missed_operator_min_s: float
    modem_read_missed_operator_visible: bool
    status_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, ModemLiveHealthState):
            raise TypeError("modem_live_health_state_typed_required")
        if not isinstance(self.outage_class, ModemLiveOutageClass):
            raise TypeError("modem_live_health_outage_class_typed_required")
        for field_name in (
            "degraded",
            "intentional_switch",
            "modem_read_missed_operator_visible",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"modem_live_health_{field_name}_bool_required"
                )
        if (
            type(self.consecutive_failures) is not int
            or self.consecutive_failures < 0
        ):
            raise ValueError(
                "modem_live_health_consecutive_failures_invalid"
            )
        for field_name in (
            "last_failure_ts",
            "last_success_ts",
            "last_reconnect_attempt_ts",
            "reconnect_backoff_s",
            "modem_read_missed_duration_s",
            "modem_read_missed_operator_min_s",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value < 0.0
            ):
                raise ValueError(
                    f"modem_live_health_{field_name}_invalid"
                )
        if (
            type(self.status_text) is not str
            or not self.status_text
            or self.status_text != self.status_text.strip()
        ):
            raise ValueError("modem_live_health_status_text_invalid")
        if self.state is ModemLiveHealthState.OK:
            if (
                self.degraded
                or self.intentional_switch
                or self.consecutive_failures != 0
                or self.outage_class is not ModemLiveOutageClass.NONE
            ):
                raise ValueError("modem_live_health_ok_facts_inconsistent")
        elif self.consecutive_failures <= 0:
            raise ValueError(
                "modem_live_health_failure_state_without_failures"
            )
        if self.state is ModemLiveHealthState.DEGRADED and not self.degraded:
            raise ValueError(
                "modem_live_health_degraded_state_inconsistent"
            )
        if self.degraded != (
            self.state is ModemLiveHealthState.DEGRADED
        ):
            raise ValueError("modem_live_health_degraded_flag_inconsistent")
        if self.intentional_switch != (
            self.outage_class is ModemLiveOutageClass.INTENTIONAL_SWITCH
        ):
            raise ValueError(
                "modem_live_health_intentional_outage_inconsistent"
            )
        if (
            self.state is ModemLiveHealthState.RECONNECTING
            and self.outage_class
            not in {
                ModemLiveOutageClass.INTENTIONAL_SWITCH,
                ModemLiveOutageClass.BRIEF_UNEXPECTED,
            }
        ):
            raise ValueError(
                "modem_live_health_reconnecting_outage_inconsistent"
            )
        if (
            self.state is ModemLiveHealthState.DEGRADED
            and self.outage_class
            is not ModemLiveOutageClass.PROLONGED_UNEXPECTED
        ):
            raise ValueError(
                "modem_live_health_degraded_outage_inconsistent"
            )

    @classmethod
    def from_owner_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ModemLiveConnectivityHealthFacts":
        if not isinstance(value, Mapping):
            raise TypeError("modem_live_health_owner_mapping_required")
        required = {
            "state",
            "outage_class",
            "degraded",
            "intentional_switch",
            "consecutive_failures",
            "last_failure_ts",
            "last_success_ts",
            "last_reconnect_attempt_ts",
            "reconnect_backoff_s",
            "modem_read_missed_duration_s",
            "modem_read_missed_operator_min_s",
            "modem_read_missed_operator_visible",
            "status_text",
        }
        if not required.issubset(value):
            raise ValueError("modem_live_health_decision_facts_incomplete")
        return cls(
            state=ModemLiveHealthState(value["state"]),
            outage_class=ModemLiveOutageClass(value["outage_class"]),
            degraded=value["degraded"],
            intentional_switch=value["intentional_switch"],
            consecutive_failures=value["consecutive_failures"],
            last_failure_ts=value["last_failure_ts"],
            last_success_ts=value["last_success_ts"],
            last_reconnect_attempt_ts=value[
                "last_reconnect_attempt_ts"
            ],
            reconnect_backoff_s=value["reconnect_backoff_s"],
            modem_read_missed_duration_s=value[
                "modem_read_missed_duration_s"
            ],
            modem_read_missed_operator_min_s=value[
                "modem_read_missed_operator_min_s"
            ],
            modem_read_missed_operator_visible=value[
                "modem_read_missed_operator_visible"
            ],
            status_text=value["status_text"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "outage_class": self.outage_class.value,
            "degraded": self.degraded,
            "intentional_switch": self.intentional_switch,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_ts": self.last_failure_ts,
            "last_success_ts": self.last_success_ts,
            "last_reconnect_attempt_ts": self.last_reconnect_attempt_ts,
            "reconnect_backoff_s": self.reconnect_backoff_s,
            "modem_read_missed_duration_s": (
                self.modem_read_missed_duration_s
            ),
            "modem_read_missed_operator_min_s": (
                self.modem_read_missed_operator_min_s
            ),
            "modem_read_missed_operator_visible": (
                self.modem_read_missed_operator_visible
            ),
            "status_text": self.status_text,
        }


@dataclass(frozen=True, slots=True)
class ModemLiveCurrentHealthAcceptanceReceipt:
    namespace: RuntimeGenerationNamespace
    observed_at: float
    register_sequence: int
    accepted: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "modem_live_health_acceptance_namespace_required"
            )
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError(
                "modem_live_health_acceptance_timestamp_invalid"
            )
        if (
            type(self.register_sequence) is not int
            or self.register_sequence <= 0
        ):
            raise ValueError(
                "modem_live_health_acceptance_sequence_invalid"
            )
        if self.accepted is not True:
            raise ValueError(
                "modem_live_health_acceptance_disposition_invalid"
            )


@dataclass(frozen=True, slots=True)
class ModemLiveCurrentHealthSinkDetachReceipt:
    namespace: RuntimeGenerationNamespace | None
    inflight_callbacks: int
    detached: bool
    finished_at: float

    def __post_init__(self) -> None:
        if self.namespace is not None and not isinstance(
            self.namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "modem_live_health_detach_namespace_invalid"
            )
        if (
            type(self.inflight_callbacks) is not int
            or self.inflight_callbacks < 0
        ):
            raise ValueError(
                "modem_live_health_detach_inflight_invalid"
            )
        if type(self.detached) is not bool or self.detached != (
            self.inflight_callbacks == 0
        ):
            raise ValueError(
                "modem_live_health_detach_disposition_invalid"
            )
        if (
            type(self.finished_at) is not float
            or not math.isfinite(self.finished_at)
            or self.finished_at <= 0.0
        ):
            raise ValueError(
                "modem_live_health_detach_timestamp_invalid"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "ModemLiveCurrentHealthSinkDetachReceipt.v1",
            "owner": MODEM_LIVE_CURRENT_HEALTH_OWNER,
            "runtime_namespace": (
                self.namespace.as_dict()
                if self.namespace is not None
                else None
            ),
            "inflight_callbacks": self.inflight_callbacks,
            "detached": self.detached,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class RuntimeHealthNamespaceBindingReceipt:
    namespace: RuntimeGenerationNamespace
    changed: bool
    cleared_previous_health: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "runtime_health_binding_receipt_namespace_required"
            )
        for field_name in ("changed", "cleared_previous_health"):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"runtime_health_binding_receipt_{field_name}_invalid"
                )
        if not self.changed and self.cleared_previous_health:
            raise ValueError(
                "runtime_health_binding_receipt_clear_without_change"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "RuntimeHealthNamespaceBindingReceipt.v1",
            "owner": MODEM_LIVE_CURRENT_HEALTH_OWNER,
            "runtime_namespace": self.namespace.as_dict(),
            "changed": self.changed,
            "cleared_previous_health": self.cleared_previous_health,
        }


@dataclass(frozen=True, slots=True)
class ModemLiveCurrentHealthPublication:
    """Typed Modem Live push payload for the Core current-health register."""

    namespace: RuntimeGenerationNamespace
    observed_at: float
    facts: ModemLiveConnectivityHealthFacts
    modem_health: CoreFrozenPayload
    modem_live_fast_lane: CoreFrozenPayload
    health_checks: CoreFrozenPayload
    runtime_recovery: CoreFrozenPayload
    schema: str = MODEM_LIVE_CURRENT_HEALTH_SCHEMA
    owner: str = MODEM_LIVE_CURRENT_HEALTH_OWNER

    def __post_init__(self) -> None:
        if self.schema != MODEM_LIVE_CURRENT_HEALTH_SCHEMA:
            raise ValueError("modem_live_current_health_schema_invalid")
        if self.owner != MODEM_LIVE_CURRENT_HEALTH_OWNER:
            raise ValueError("modem_live_current_health_owner_invalid")
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("modem_live_current_health_namespace_required")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("modem_live_current_health_timestamp_invalid")
        if not isinstance(
            self.facts,
            ModemLiveConnectivityHealthFacts,
        ):
            raise TypeError("modem_live_current_health_facts_required")
        for field_name in (
            "modem_health",
            "modem_live_fast_lane",
            "health_checks",
            "runtime_recovery",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"modem_live_current_health_{field_name}_payload_required"
                )

    @classmethod
    def capture(
        cls,
        *,
        namespace: RuntimeGenerationNamespace,
        observed_at: float,
        modem_health: Mapping[str, Any],
        modem_live_fast_lane: Mapping[str, Any],
        health_checks: Mapping[str, Any] | None = None,
        runtime_recovery: Mapping[str, Any] | None = None,
    ) -> "ModemLiveCurrentHealthPublication":
        if not isinstance(modem_health, Mapping):
            raise TypeError("modem_live_current_health_mapping_required")
        if not isinstance(modem_live_fast_lane, Mapping):
            raise TypeError("modem_live_fast_lane_health_mapping_required")
        facts = ModemLiveConnectivityHealthFacts.from_owner_mapping(
            modem_health
        )
        diagnostic_health = {
            key: item
            for key, item in modem_health.items()
            if key not in facts.to_payload()
        }
        return cls(
            namespace=namespace,
            observed_at=observed_at,
            facts=facts,
            modem_health=CoreFrozenPayload.from_mapping(
                diagnostic_health
            ),
            modem_live_fast_lane=CoreFrozenPayload.from_mapping(
                modem_live_fast_lane
            ),
            health_checks=CoreFrozenPayload.from_mapping(
                health_checks or {}
            ),
            runtime_recovery=CoreFrozenPayload.from_mapping(
                runtime_recovery or {}
            ),
        )

    def to_register_value(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "runtime_namespace": self.namespace.as_dict(),
            "observed_at": self.observed_at,
            "facts": self.facts.to_payload(),
            "modem_health": self.modem_health.to_payload(),
            "modem_live_fast_lane": (
                self.modem_live_fast_lane.to_payload()
            ),
            "health_checks": self.health_checks.to_payload(),
            "runtime_recovery": self.runtime_recovery.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class CoreCurrentHealthView:
    """One exact health-register read bound to the accepted Matrix namespace."""

    namespace: RuntimeGenerationNamespace
    register_sequence: int
    observed_at: float
    facts: ModemLiveConnectivityHealthFacts | None
    modem_health: CoreFrozenPayload
    modem_live_fast_lane: CoreFrozenPayload
    health_checks: CoreFrozenPayload
    runtime_recovery: CoreFrozenPayload
    schema: str = CORE_CURRENT_HEALTH_SCHEMA
    owner: str = CORE_CURRENT_HEALTH_OWNER

    def allows_measurement(self, acquired_at: float) -> bool:
        """A contact failure invalidates live/held data until a new acquisition.

        This is measurement validity, not another Internet decision. Identity,
        saved history and recovery status are not measurements.
        """
        facts = self.facts
        if facts is None:
            return True  # No contact-failure evidence at initial publication.
        return (facts.state is ModemLiveHealthState.OK and
                (facts.last_failure_ts == 0.0 or acquired_at > facts.last_failure_ts))

    def __post_init__(self) -> None:
        if self.schema != CORE_CURRENT_HEALTH_SCHEMA:
            raise ValueError("core_current_health_schema_invalid")
        if self.owner != CORE_CURRENT_HEALTH_OWNER:
            raise ValueError("core_current_health_owner_invalid")
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("core_current_health_namespace_required")
        if type(self.register_sequence) is not int or self.register_sequence < 0:
            raise ValueError("core_current_health_register_sequence_invalid")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at < 0.0
        ):
            raise ValueError("core_current_health_timestamp_invalid")
        if self.facts is None:
            if self.register_sequence != 0 or self.observed_at != 0.0:
                raise ValueError(
                    "core_current_health_pending_evidence_invalid"
                )
        elif (
            not isinstance(
                self.facts,
                ModemLiveConnectivityHealthFacts,
            )
            or self.register_sequence <= 0
            or self.observed_at <= 0.0
        ):
            raise ValueError("core_current_health_live_evidence_required")
        for field_name in (
            "modem_health",
            "modem_live_fast_lane",
            "health_checks",
            "runtime_recovery",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"core_current_health_{field_name}_payload_required"
                )

    @classmethod
    def pending(
        cls,
        *,
        namespace: RuntimeGenerationNamespace,
    ) -> "CoreCurrentHealthView":
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("core_current_health_pending_namespace_required")
        empty = CoreFrozenPayload.from_mapping({})
        return cls(
            namespace=namespace,
            register_sequence=0,
            observed_at=0.0,
            facts=None,
            modem_health=empty,
            modem_live_fast_lane=empty,
            health_checks=empty,
            runtime_recovery=empty,
        )

    @classmethod
    def from_register_value(
        cls,
        value: Mapping[str, Any],
        *,
        register_sequence: int,
        register_updated_at: float,
        expected_namespace: RuntimeGenerationNamespace,
    ) -> "CoreCurrentHealthView":
        if not isinstance(value, Mapping):
            raise TypeError("core_current_health_register_mapping_required")
        if not isinstance(expected_namespace, RuntimeGenerationNamespace):
            raise TypeError("core_current_health_expected_namespace_required")
        if set(value) != {
            "schema",
            "owner",
            "runtime_namespace",
            "observed_at",
            "facts",
            "modem_health",
            "modem_live_fast_lane",
            "health_checks",
            "runtime_recovery",
        }:
            raise ValueError("core_current_health_register_contract_invalid")
        if value["schema"] != MODEM_LIVE_CURRENT_HEALTH_SCHEMA:
            raise ValueError("core_current_health_register_schema_invalid")
        if value["owner"] != MODEM_LIVE_CURRENT_HEALTH_OWNER:
            raise ValueError("core_current_health_register_owner_invalid")
        observed_at = value["observed_at"]
        if (
            type(register_updated_at) is not float
            or not math.isfinite(register_updated_at)
            or register_updated_at <= 0.0
        ):
            raise ValueError(
                "core_current_health_register_updated_at_invalid"
            )
        if (
            type(observed_at) is not float
            or not math.isfinite(observed_at)
            or observed_at <= 0.0
        ):
            raise ValueError(
                "core_current_health_owner_observed_at_invalid"
            )
        if observed_at > register_updated_at:
            raise ValueError(
                "core_current_health_observed_after_register_update"
            )
        namespace = RuntimeGenerationNamespace.from_mapping(
            value["runtime_namespace"]
        )
        if namespace != expected_namespace:
            raise RuntimeError("core_current_health_namespace_mismatch")
        return cls(
            namespace=namespace,
            register_sequence=register_sequence,
            observed_at=observed_at,
            facts=ModemLiveConnectivityHealthFacts.from_owner_mapping(
                value["facts"]
            ),
            modem_health=CoreFrozenPayload.from_mapping(
                value["modem_health"]
            ),
            modem_live_fast_lane=CoreFrozenPayload.from_mapping(
                value["modem_live_fast_lane"]
            ),
            health_checks=CoreFrozenPayload.from_mapping(
                value["health_checks"]
            ),
            runtime_recovery=CoreFrozenPayload.from_mapping(
                value["runtime_recovery"]
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "runtime_namespace": self.namespace.as_dict(),
            "register_sequence": self.register_sequence,
            "observed_at": self.observed_at,
            "facts": (
                None if self.facts is None else self.facts.to_payload()
            ),
            "modem_health": self.modem_health.to_payload(),
            "modem_live_fast_lane": self.modem_live_fast_lane.to_payload(),
            "health_checks": self.health_checks.to_payload(),
            "runtime_recovery": self.runtime_recovery.to_payload(),
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "CoreCurrentHealthView":
        if not isinstance(payload, Mapping):
            raise TypeError("core_current_health_payload_required")
        if set(payload) != {
            "schema",
            "owner",
            "runtime_namespace",
            "register_sequence",
            "observed_at",
            "facts",
            "modem_health",
            "modem_live_fast_lane",
            "health_checks",
            "runtime_recovery",
        }:
            raise ValueError("core_current_health_payload_contract_invalid")
        return cls(
            namespace=RuntimeGenerationNamespace.from_mapping(
                payload["runtime_namespace"]
            ),
            register_sequence=payload["register_sequence"],
            observed_at=payload["observed_at"],
            facts=(
                None
                if payload["facts"] is None
                else ModemLiveConnectivityHealthFacts.from_owner_mapping(
                    payload["facts"]
                )
            ),
            modem_health=CoreFrozenPayload.from_mapping(
                payload["modem_health"]
            ),
            modem_live_fast_lane=CoreFrozenPayload.from_mapping(
                payload["modem_live_fast_lane"]
            ),
            health_checks=CoreFrozenPayload.from_mapping(
                payload["health_checks"]
            ),
            runtime_recovery=CoreFrozenPayload.from_mapping(
                payload["runtime_recovery"]
            ),
            schema=payload["schema"],
            owner=payload["owner"],
        )


__all__ = (
    "CORE_CURRENT_HEALTH_OWNER",
    "CORE_CURRENT_HEALTH_SCHEMA",
    "CoreCurrentHealthView",
    "ModemLiveCurrentHealthAcceptanceReceipt",
    "ModemLiveConnectivityHealthFacts",
    "ModemLiveHealthState",
    "ModemLiveCurrentHealthSinkDetachReceipt",
    "ModemLiveOutageClass",
    "RuntimeHealthNamespaceBindingReceipt",
    "MODEM_LIVE_CURRENT_HEALTH_OWNER",
    "MODEM_LIVE_CURRENT_HEALTH_SCHEMA",
    "ModemLiveCurrentHealthPublication",
)
