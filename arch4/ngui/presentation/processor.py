"""Produce all operator-visible NGUI field values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from collections.abc import Mapping
import re
from types import MappingProxyType
from typing import Any

from arch4.core_contracts.current_state import CoreCurrentFieldState
from arch4.canonical_scheduler_display import modulation_efficiency_from_qam
from arch4.radio_band_projection import (
    RadioBandProjectionError,
    frequency_set_from_ca_pool,
)

from ..front_register import PresentationState, PresentationValue
from ..input_registers import InputRegisterSnapshot, InputValue
from ..millisecond_text import format_milliseconds
from ..login_lifecycle import login_lifecycle_state
from ..registry import NGUI_FIELD_REGISTRY
from ..types import (
    FieldKind,
    FieldSpec,
    FreshnessState,
    PresentationPolicy,
    SourceOwner,
)
from .policies import CaLdPolicy, HeldValueStore, JitterPolicy, StabilizedValue
from .dashboard_decision import connectivity_transition_conclusion


_HOLD_POLICIES = frozenset(
    {
        PresentationPolicy.SHORT_MISS_HOLD,
        PresentationPolicy.DURATION_HMS,
        PresentationPolicy.RADIO_IDENTITY_PACKAGE,
        PresentationPolicy.DEVICE_IDENTITY_PACKAGE,
        PresentationPolicy.TX_POWER_COMPONENT,
        PresentationPolicy.QAM_MODULATION_EFFICIENCY,
    }
)

_TRANSIENT_STATE_OWNING_POLICIES = frozenset(
    {
        PresentationPolicy.CA_LD_MODEM_SAMPLES,
        PresentationPolicy.JITTER_STABLE,
        PresentationPolicy.NEIGHBOR_SLIDING_HOLD,
        PresentationPolicy.CONNECTION_LED,
        PresentationPolicy.CONNECTION_STATUS,
        PresentationPolicy.NETWORK_BADGE,
    }
)

_TX_POWER_COMPONENTS = {
    "rf.tx_ppusch": "ppusch",
    "rf.tx_ppucch": "ppucch",
    "rf.tx_psrs": "psrs",
    "rf.tx_pprach": "pprach",
}

_NR_OBSERVED_FIELD_IDS = frozenset(
    {
        "nr.mode",
        "nr.band",
        "nr.pci",
        "nr.arfcn",
        "nr.cell_id",
        "nr.bandwidth_mhz",
        "nr.sinr_db",
        "nr.rsrp_dbm",
        "nr.rsrq_db",
        "nr.cells",
        "nr.ri", "nr.cqi", "nr.mcs_dl", "nr.mcs_ul", "nr.qam_dl", "nr.qam_ul",
        "nr.me_dl", "nr.me_ul",
        "nr.num_rbs", "nr.tx_power_dbm", "nr.ul_bandwidth_mhz",
        "nr.dl_frequency_mhz", "nr.ul_frequency_mhz",
    }
)

_DEPENDENCY_CACHEABLE_POLICIES = frozenset(
    {
        PresentationPolicy.DIRECT,
        PresentationPolicy.USER_INPUT,
        PresentationPolicy.COMMAND_READBACK,
        PresentationPolicy.ANTENNA_GUIDE,
    }
)

@dataclass(frozen=True, slots=True)
class PresentationBuildCandidate:
    """Prepared fields and the policy state used to build them."""

    fields: Mapping[str, PresentationValue]
    held: HeldValueStore
    ca_ld: CaLdPolicy
    jitter: JitterPolicy
    dependency_keys: Mapping[str, object]

    def policy_state_payload(self) -> dict[str, Any]:
        """Return policy evidence without exposing mutable state."""

        return {
            "held": self.held.to_payload(),
            "ca_ld": self.ca_ld.to_payload(),
            "jitter": self.jitter.to_payload(),
        }


class NGUIPresentationProcessor:
    def __init__(self) -> None:
        self._held = HeldValueStore()
        self._ca_ld = CaLdPolicy()
        self._jitter = JitterPolicy()
        self._dependency_keys: dict[str, object] = {}

    def reset_for_changed_device(self) -> None:
        """Clear presentation memory from the previous modem."""

        self._held.clear()
        self._ca_ld = CaLdPolicy()
        self._jitter = JitterPolicy()
        self._dependency_keys.clear()

    def runtime_reset_observation(self) -> Mapping[str, Any]:
        return {
            "held": self._held.to_payload(),
            "ca_ld": self._ca_ld.to_payload(),
            "jitter": self._jitter.to_payload(),
        }

    def prepare_fields(
        self,
        inputs: InputRegisterSnapshot,
        *,
        now: float,
        previous_fields: Mapping[str, PresentationValue] | None = None,
    ) -> PresentationBuildCandidate:
        """Prepare one input generation without changing accepted policy state."""

        candidate = NGUIPresentationProcessor()
        candidate._held = self._held.clone()
        candidate._ca_ld = self._ca_ld.clone()
        candidate._jitter = self._jitter.clone()
        candidate._dependency_keys = dict(self._dependency_keys)
        fields = candidate._build_fields(
            inputs,
            now=now,
            previous_fields=previous_fields or {},
        )
        return PresentationBuildCandidate(
            fields=fields,
            held=candidate._held,
            ca_ld=candidate._ca_ld,
            jitter=candidate._jitter,
            dependency_keys=MappingProxyType(dict(candidate._dependency_keys)),
        )

    def commit_fields(self, candidate: PresentationBuildCandidate) -> None:
        """Accept policy state only after publishing the complete Front."""

        if not isinstance(candidate, PresentationBuildCandidate):
            raise TypeError("presentation policy commit requires typed candidate")
        self._held = candidate.held
        self._ca_ld = candidate.ca_ld
        self._jitter = candidate.jitter
        self._dependency_keys = dict(candidate.dependency_keys)

    def _build_fields(
        self,
        inputs: InputRegisterSnapshot,
        *,
        now: float,
        previous_fields: Mapping[str, PresentationValue],
    ) -> dict[str, PresentationValue]:
        if now <= 0:
            raise ValueError("presentation time must be positive")
        fields: dict[str, PresentationValue] = {}
        nr_presence = self._nr_physical_presence(
            inputs,
            now=now,
        )
        for field_id, spec in NGUI_FIELD_REGISTRY.items():
            if field_id in {"connection.user_link", "device.modem_status"}:
                lifecycle = self._source_value(inputs, "profile.login_lifecycle")
                phase = login_lifecycle_state(lifecycle.value) if lifecycle is not None and lifecycle.valid else ""
                if phase and phase != "connected":
                    self._held.forget(field_id)
                    self._dependency_keys.pop(field_id, None)
                    text = "Not monitored" if field_id == "device.modem_status" else {
                        "draft": "Not connected", "stopped": "Disconnected",
                        "failed": "Connection failed", "stopping": "Disconnecting",
                        "identity_required": "Waiting for serial number",
                        "reconnecting": "Reconnecting",
                    }.get(phase, "Connecting")
                    fields[field_id] = self._output(
                        spec, text, text, PresentationState.TRANSITION,
                        lifecycle, now, freshness=lifecycle.freshness,
                        reason="login_lifecycle_" + phase,
                    )
                    continue
            source = self._source_value(inputs, spec.source_path)
            if source is not None and source.reason == "modem_measurement_interrupted":
                # Hard invalidation precedes cache reuse and every hold policy.
                self._held.forget(field_id)
                self._dependency_keys.pop(field_id, None)
                if spec.policy is PresentationPolicy.CA_LD_MODEM_SAMPLES:
                    self._ca_ld = CaLdPolicy()
                if spec.policy is PresentationPolicy.JITTER_STABLE:
                    self._jitter = JitterPolicy()
                fields[field_id] = self._unavailable(
                    spec, source, now, state=CoreCurrentFieldState.MISSING,
                    reason=source.reason,
                )
                continue
            dependency_key = self._dependency_key(inputs, spec)
            previous = previous_fields.get(field_id)
            if (
                dependency_key is not None
                and self._dependency_keys.get(field_id) == dependency_key
                and isinstance(previous, PresentationValue)
                and previous.field_id == field_id
                and previous.owner is spec.owner
                and previous.source_path == spec.source_path
            ):
                fields[field_id] = previous
                continue
            source = self._source_value(
                inputs,
                spec.source_path,
            )
            if (field_id in _NR_OBSERVED_FIELD_IDS and nr_presence is False
                    and not (source is not None and source.fact_state is CoreCurrentFieldState.UNSUPPORTED)):
                self._held.forget(field_id)
                fields[field_id] = self._unavailable(
                    spec,
                    source,
                    now,
                    state=CoreCurrentFieldState.NOT_APPLICABLE,
                    reason="nr_inactive",
                )
                self._dependency_keys.pop(field_id, None)
                continue
            source_current = self._source_current(
                inputs,
                spec.source_path,
                spec.owner,
                source,
            )
            fields[field_id] = self._present(
                spec,
                source,
                source_current=source_current,
                now=now,
                inputs=inputs,
            )
            if dependency_key is None:
                self._dependency_keys.pop(field_id, None)
            else:
                self._dependency_keys[field_id] = dependency_key
        self._apply_connectivity_transition_summary(fields, now=now)
        return fields

    @staticmethod
    def _dependency_key(
        inputs: InputRegisterSnapshot,
        spec: FieldSpec,
    ) -> object | None:
        """Describe every fact that permits safe reuse of one projection."""

        if (
            spec.policy not in _DEPENDENCY_CACHEABLE_POLICIES
            or spec.freshness_ms != 0
            or spec.field_id in _NR_OBSERVED_FIELD_IDS
        ):
            return None
        return (
            inputs.runtime_session_scope_key,
            inputs.runtime_source_epoch,
            inputs.owner_sequences.get(spec.owner, 0),
            inputs.source_owner_sequences.get(spec.source_path, 0),
            inputs.values.get(spec.source_path),
            tuple(inputs.values.get(f"rf.{field}_support") for field in ("cqi1", "mcs_dl1", "qam_dl1")) if spec.field_id.startswith("scheduler.") else None,
        )

    @staticmethod
    def _apply_connectivity_transition_summary(
        fields: dict[str, PresentationValue],
        *,
        now: float,
    ) -> None:
        conclusion = fields["event.connectivity_conclusion"]
        context = conclusion.value if isinstance(conclusion.value, Mapping) else {}
        if context:
            conclusion_text = connectivity_transition_conclusion(context)
            reason = "foundation_connectivity_transition_summary"
        else:
            conclusion_text = "---"
            reason = "foundation_connectivity_transition_unavailable"
        fields["event.connectivity_conclusion"] = replace(
            conclusion,
            value=conclusion_text,
            text=conclusion_text,
            state=(PresentationState.VALID if context else conclusion.state),
            published_at=now,
            reason=reason,
        )

    @staticmethod
    def _nr_physical_presence(
        inputs: InputRegisterSnapshot,
        *,
        now: float,
    ) -> bool | None:
        """Physical presence is separate from complete-identity grading."""
        observed: list[bool] = []
        for field_id in ("nr.nsa_active", "nr.sa_active"):
            spec = NGUI_FIELD_REGISTRY[field_id]
            source = NGUIPresentationProcessor._source_value(inputs, spec.source_path)
            if (source is not None and source.valid
                    and type(source.value) is bool
                    and source.freshness is FreshnessState.FRESH
                    and NGUIPresentationProcessor._source_current(inputs, spec.source_path, spec.owner, source)
                    and (not spec.freshness_ms or now - source.sampled_at <= spec.freshness_ms / 1000.0)):
                observed.append(source.value)
        if any(observed):
            return True
        return False if len(observed) == 2 else None

    @staticmethod
    def _source_value(
        inputs: InputRegisterSnapshot,
        source_id: str,
    ) -> InputValue | None:
        return inputs.values.get(source_id)

    @staticmethod
    def _source_current(
        inputs: InputRegisterSnapshot,
        source_id: str,
        owner: Any,
        source: InputValue | None,
    ) -> bool:
        if source is None:
            return False
        return bool(
            inputs.source_owner_sequences.get(source_id, 0)
            == inputs.owner_sequences.get(owner, 0)
            and inputs.owner_sequences.get(owner, 0) > 0
        )

    def _present(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        source_current: bool,
        now: float,
        inputs: InputRegisterSnapshot,
    ) -> PresentationValue:
        source_contract_valid = self._source_value_is_valid(
            spec,
            source,
            kind=spec.source_kind,
        )
        if spec.policy is PresentationPolicy.QAM_MODULATION_EFFICIENCY and source is not None:
            # Explicit display adapter: derive from qualified QAM, retaining
            # its support, namespace and freshness rather than a native ME read.
            modulation = modulation_efficiency_from_qam(source.value)
            if source.value is not None and modulation is None:
                self._held.forget(spec.field_id)
            source = replace(source, value=modulation)
            source_contract_valid = source_contract_valid and source.value is not None
        if spec.field_id.startswith("scheduler.") and not source_contract_valid:
            field = spec.field_id.partition(".")[2]
            field = {"me0": "qam_dl0", "me1": "qam_dl1", "me_ul": "qam_ul"}.get(field, field)
            support_id = f"rf.{field}_support"
            support = self._source_value(inputs, support_id)
            if (support is not None
                    and self._source_current(inputs, support_id, SourceOwner.MODEM_MATRIX, support)
                    and support.fact_state is CoreCurrentFieldState.UNSUPPORTED):
                source = replace(support, value=None, valid=False,
                    fact_state=CoreCurrentFieldState.UNSUPPORTED,
                    reason="modem_live_profile_unsupported")
        if spec.policy is PresentationPolicy.TX_POWER_COMPONENT:
            source = self._tx_power_component_source(spec, source)
            support = self._source_value(inputs, "rf.tx_power_support")
            if (support is not None and support.valid
                    and self._source_current(inputs, "rf.tx_power_support", spec.owner, support)
                    and isinstance(support.value, Mapping)
                    and support.value.get(_TX_POWER_COMPONENTS[spec.field_id]) == "unsupported"
                    and (source is None or source.value is None)):
                source = replace(support, value=None, valid=False,
                    fact_state=CoreCurrentFieldState.UNSUPPORTED,
                    reason="modem_live_profile_unsupported")
        if spec.field_id == "radio.frequency_set":
            source = self._frequency_set_source(source)
        if spec.policy is PresentationPolicy.NEIGHBOR_SLIDING_HOLD:
            return self._present_neighbor_sliding_hold(
                spec,
                source,
                source_current=source_current,
                now=now,
            )
        if (
            source is not None
            and source.fact_state is not CoreCurrentFieldState.AVAILABLE
            and spec.policy not in _TRANSIENT_STATE_OWNING_POLICIES
        ):
            return self._unavailable(
                spec,
                source,
                now,
                state=source.fact_state,
                reason=source.reason or source.fact_state.value,
            )

        source_valid = source_contract_valid
        source_age_valid = self._source_age_valid(spec, source, now=now)
        valid_fresh = bool(
            source_valid
            and source_current
            and source_age_valid
            and source is not None
            and source.freshness is FreshnessState.FRESH
        )

        if spec.policy is PresentationPolicy.CA_LD_MODEM_SAMPLES:
            decision = self._ca_ld.evaluate(
                source,
                valid_fresh_sample=valid_fresh,
                now=now,
                hold_ms=spec.hold_ms,
            )
            return self._decision_or_missing(spec, decision, source, now)
        if spec.policy is PresentationPolicy.JITTER_STABLE:
            decision = self._jitter.evaluate(
                source,
                valid_fresh_sample=valid_fresh,
                now=now,
                hold_ms=spec.hold_ms,
            )
            if decision is not None:
                return self._decision_or_missing(spec, decision, source, now)
            if source is not None and not source.valid:
                # Signal Processing decides whether the current sample is
                # qualified. JitterPolicy holds the last qualified value for a
                # short time. Show NA only when neither source has a value.
                self._jitter.clear_visible(spec.field_id)
                return self._output(
                    spec,
                    None,
                    "NA",
                    PresentationState.NOT_APPLICABLE,
                    source,
                    now,
                    freshness=(
                        source.freshness
                        if source is not None
                        else FreshnessState.UNKNOWN
                    ),
                    reason=(
                        source.reason
                        if source is not None and source.reason
                        else "jitter_not_qualified"
                    ),
                )
            return self._decision_or_missing(spec, decision, source, now)
        if spec.policy is PresentationPolicy.CONNECTION_LED:
            return self._present_connection_led(
                spec,
                source,
                valid_fresh=valid_fresh,
                now=now,
            )
        if spec.policy is PresentationPolicy.CONNECTION_STATUS:
            return self._present_connection_status(
                spec,
                source,
                source_valid=source_valid,
                source_current=source_current,
                source_age_valid=source_age_valid,
                now=now,
            )
        if spec.policy is PresentationPolicy.NETWORK_BADGE:
            ca_active = self._source_value(inputs, "radio.ca_active")
            ca_active_current = self._source_current(
                inputs,
                "radio.ca_active",
                SourceOwner.MODEM_MATRIX,
                ca_active,
            )
            nr_transition_spec = NGUI_FIELD_REGISTRY["quality.nr_transition_state"]
            nr_transition = self._source_value(
                inputs,
                nr_transition_spec.source_path,
            )
            nr_transition_current = self._source_current(
                inputs,
                nr_transition_spec.source_path,
                nr_transition_spec.owner,
                nr_transition,
            )
            return self._present_network_badge(
                spec,
                source,
                valid_fresh=valid_fresh,
                ca_active=bool(
                    ca_active is not None
                    and ca_active_current
                    and ca_active.valid
                    and ca_active.fact_state is CoreCurrentFieldState.AVAILABLE
                    and ca_active.value is True
                ),
                nr_transition=(
                    str(nr_transition.value or "").strip().lower()
                    if nr_transition is not None
                    and nr_transition_current
                    and nr_transition.valid
                    and nr_transition.freshness is FreshnessState.FRESH
                    and nr_transition.fact_state is CoreCurrentFieldState.AVAILABLE
                    else ""
                ),
                now=now,
            )
        if spec.policy is PresentationPolicy.RTT_OBSERVATION:
            return self._present_rtt_observation(
                spec,
                source,
                source_valid=source_valid,
                source_current=source_current,
                now=now,
            )

        if valid_fresh:
            assert source is not None
            decision = self._held.remember(spec.field_id, source)
            return self._from_decision(spec, decision, source, now)

        if (
            source_valid
            and source_current
            and source_age_valid
            and source is not None
            and source.freshness is FreshnessState.TRANSITION
            and spec.policy not in _HOLD_POLICIES
        ):
            return self._output(
                spec,
                source.value,
                self._format(spec, source.value),
                PresentationState.TRANSITION,
                source,
                now,
                freshness=FreshnessState.TRANSITION,
                reason=source.reason or "source_transition",
            )

        if spec.policy in _HOLD_POLICIES:
            decision = self._held.held(
                spec.field_id,
                now=now,
                hold_ms=spec.hold_ms,
                indefinite=spec.policy is PresentationPolicy.DEVICE_IDENTITY_PACKAGE,
                reason=self._hold_reason(source, source_current),
            )
            if decision is not None:
                return self._from_decision(spec, decision, source, now)

        return self._missing(spec, source, now)

    def _present_rtt_observation(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        source_valid: bool,
        source_current: bool,
        now: float,
    ) -> PresentationValue:
        """Show the last raw RTT with its real evidence state."""

        if not source_valid or not source_current or source is None:
            return self._missing(spec, source, now)
        fresh = source.freshness is FreshnessState.FRESH
        return self._output(
            spec,
            source.value,
            self._format(spec, source.value),
            PresentationState.VALID if fresh else PresentationState.HELD,
            source,
            now,
            freshness=source.freshness,
            reason=(
                source.reason
                or ("raw_rtt_observation" if fresh else "raw_rtt_observation_stale")
            ),
        )

    @staticmethod
    def _tx_power_component_source(
        spec: FieldSpec,
        source: InputValue | None,
    ) -> InputValue | None:
        if source is None:
            return None
        if source.fact_state is not CoreCurrentFieldState.AVAILABLE:
            return source
        component = _TX_POWER_COMPONENTS.get(spec.field_id)
        if component is None:
            raise RuntimeError(f"undeclared TX power component field: {spec.field_id}")
        value = _extract_tx_power_component(source.value, component)
        return replace(
            source,
            value=value,
            valid=source.valid and value is not None,
            reason=source.reason if value is not None else f"{component}_missing",
            fact_state=(
                CoreCurrentFieldState.MISSING
                if source.valid and value is None
                else source.fact_state
            ),
        )

    @staticmethod
    def _frequency_set_source(source: InputValue | None) -> InputValue | None:
        if source is None or not source.valid:
            return source
        try:
            value = frequency_set_from_ca_pool(source.value)
        except RadioBandProjectionError as exc:
            return replace(
                source,
                value=None,
                valid=False,
                reason=str(exc),
                fact_state=CoreCurrentFieldState.INVALID,
            )
        return replace(source, value=value)

    def _present_network_badge(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        valid_fresh: bool,
        ca_active: bool,
        nr_transition: str,
        now: float,
    ) -> PresentationValue:
        if valid_fresh and source is not None:
            raw = str(source.value).strip().upper()
            if nr_transition == "sa_unsupported" or (
                "5G SA" in raw and "NSA" not in raw
            ):
                badge = "5G SA UNSUP."
            elif nr_transition == "attaching":
                badge = "NR ATTACH"
            elif nr_transition == "detaching":
                badge = "NR DETACH"
            elif nr_transition == "stable" or "NSA" in raw:
                badge = "5G NSA+" if ca_active else "5G NSA"
            elif "5G" in raw:
                badge = "5G"
            elif "LTE" in raw or "4G" in raw:
                badge = "4G+" if ca_active else "4G"
            else:
                badge = str(source.value).strip() or "---"
            decision = self._held.remember_value(spec.field_id, badge, source)
            return self._from_decision(spec, decision, source, now)
        decision = self._held.held(
            spec.field_id,
            now=now,
            hold_ms=spec.hold_ms,
            reason="network_badge_hold",
        )
        return self._decision_or_missing(spec, decision, source, now)

    def _present_neighbor_sliding_hold(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        source_current: bool,
        now: float,
    ) -> PresentationValue:
        """Keep sparse neighbor scans visible for a short time.

        Modem Live remains the physical truth owner. A confirmed empty scan
        clears previous neighbours. Missing scans retain the bounded hold;
        explicit unsupported capability must never reuse a previous device.
        """

        if source is not None and source.fact_state is CoreCurrentFieldState.UNSUPPORTED:
            return self._unavailable(spec, source, now, state=source.fact_state,
                                     reason=source.reason or source.fact_state.value)
        source_valid = self._source_value_is_valid(
            spec,
            source,
            kind=spec.source_kind,
        )
        source_age_valid = self._source_age_valid(spec, source, now=now)
        valid_fresh = bool(
            source_valid
            and source_current
            and source_age_valid
            and source is not None
            and source.freshness is FreshnessState.FRESH
        )
        if valid_fresh and source is not None and _format_neighbors(source.value) != "---":
            decision = self._held.remember(spec.field_id, source)
            return self._from_decision(spec, decision, source, now)

        decision = self._held.held(
            spec.field_id,
            now=now,
            hold_ms=spec.hold_ms,
            reason="neighbor_sliding_hold",
        )
        if decision is not None:
            return self._from_decision(spec, decision, source, now)

        if source is not None and source.fact_state is not CoreCurrentFieldState.AVAILABLE:
            return self._unavailable(
                spec,
                source,
                now,
                state=source.fact_state,
                reason=source.reason or source.fact_state.value,
            )
        return self._missing(spec, source, now)

    def _present_connection_led(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        valid_fresh: bool,
        now: float,
    ) -> PresentationValue:
        if valid_fresh and source is not None:
            quality = max(0.0, min(100.0, float(source.value)))
            level = 0 if quality <= 0 else min(5, max(1, int((quality + 19.999) // 20)))
            decision = self._held.remember_value(spec.field_id, level, source)
            return self._from_decision(spec, decision, source, now)
        decision = self._held.held(
            spec.field_id,
            now=now,
            hold_ms=spec.hold_ms,
            reason="connection_quality_hold",
        )
        return self._decision_or_missing(spec, decision, source, now)

    def _present_connection_status(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        *,
        source_valid: bool,
        source_current: bool,
        source_age_valid: bool,
        now: float,
    ) -> PresentationValue:
        if source_valid and source_current and source_age_valid and source is not None:
            normalized = str(source.value).strip().lower()
            text = {
                "connected": "CONNECTED",
                "working": "CONNECTED",
                "live": "CONNECTED",
                "reconnecting": "RECONNECTING",
                "recovering": "RECONNECTING",
                "transition": "RECONNECTING",
                "no_link": "NO LINK",
                "disconnected": "NO LINK",
                "offline": "NO LINK",
            }.get(normalized, str(source.value).strip().upper())
            self._held.remember_value(spec.field_id, text, source)
            state = (
                PresentationState.TRANSITION
                if source.freshness is FreshnessState.TRANSITION
                else PresentationState.VALID
            )
            return self._output(
                spec,
                text,
                text,
                state,
                source,
                now,
                freshness=source.freshness,
                reason=source.reason,
            )
        decision = self._held.held(
            spec.field_id,
            now=now,
            hold_ms=spec.hold_ms,
            reason="connection_state_hold",
        )
        return self._decision_or_missing(spec, decision, source, now)

    def _decision_or_missing(
        self,
        spec: FieldSpec,
        decision: StabilizedValue | None,
        source: InputValue | None,
        now: float,
    ) -> PresentationValue:
        if decision is None:
            return self._missing(spec, source, now)
        return self._from_decision(spec, decision, source, now)

    def _from_decision(
        self,
        spec: FieldSpec,
        decision: StabilizedValue,
        source: InputValue | None,
        now: float,
    ) -> PresentationValue:
        return PresentationValue(
            field_id=spec.field_id,
            value=decision.value,
            text=(
                "NA"
                if decision.value is None
                and decision.state is PresentationState.NOT_APPLICABLE
                else self._format(spec, decision.value)
            ),
            state=decision.state,
            source_sequence=decision.source_sequence,
            sampled_at=decision.sampled_at,
            published_at=now,
            owner=spec.owner,
            source_path=spec.source_path,
            freshness=decision.freshness,
            reason=decision.reason,
        )

    def _missing(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        now: float,
        *,
        reason: str = "",
    ) -> PresentationValue:
        return self._output(
            spec,
            None,
            spec.missing_text,
            PresentationState.MISSING,
            source,
            now,
            freshness=source.freshness if source is not None else FreshnessState.UNKNOWN,
            reason=reason or (source.reason if source is not None and source.reason else "source_missing"),
        )

    def _unavailable(
        self,
        spec: FieldSpec,
        source: InputValue | None,
        now: float,
        *,
        state: CoreCurrentFieldState,
        reason: str,
    ) -> PresentationValue:
        presentation_state, text = {
            CoreCurrentFieldState.UNSUPPORTED: (
                PresentationState.UNSUPPORTED,
                spec.unsupported_text,
            ),
            CoreCurrentFieldState.NOT_APPLICABLE: (
                PresentationState.NOT_APPLICABLE,
                "----",
            ),
            CoreCurrentFieldState.PENDING: (
                PresentationState.PENDING,
                "----",
            ),
            CoreCurrentFieldState.INVALID: (
                PresentationState.INVALID,
                "----",
            ),
            CoreCurrentFieldState.UNATTACHED: (
                PresentationState.UNATTACHED,
                "----",
            ),
            CoreCurrentFieldState.MISSING: (
                PresentationState.MISSING,
                spec.missing_text,
            ),
        }.get(state, (PresentationState.MISSING, spec.missing_text))
        value: Any = None
        self._held.forget(spec.field_id)
        return self._output(
            spec,
            value,
            text,
            presentation_state,
            source,
            now,
            freshness=(
                source.freshness
                if source is not None
                else FreshnessState.UNKNOWN
            ),
            reason=reason,
        )

    @staticmethod
    def _hold_reason(source: InputValue | None, source_current: bool) -> str:
        if source is None:
            return "source_missing_hold"
        if not source_current:
            return "sparse_owner_batch_hold"
        if source.freshness is FreshnessState.TRANSITION:
            return "controlled_transition_hold"
        if source.reason:
            return source.reason
        return "short_miss_hold"

    @staticmethod
    def _source_value_is_valid(
        spec: FieldSpec,
        source: InputValue | None,
        *,
        kind: FieldKind,
    ) -> bool:
        if source is None or not source.valid or source.value is None:
            return False
        allowed_freshness = {FreshnessState.FRESH, FreshnessState.TRANSITION}
        if spec.policy is PresentationPolicy.RTT_OBSERVATION:
            allowed_freshness.add(FreshnessState.STALE)
        if source.freshness not in allowed_freshness:
            return False
        if kind is FieldKind.FLOAT:
            try:
                value = float(source.value)
            except (TypeError, ValueError):
                return False
            if not isfinite(value):
                return False
            if not spec.allow_zero and value == 0:
                return False
        if kind is FieldKind.INTEGER:
            if isinstance(source.value, bool):
                return False
            if isinstance(source.value, int):
                value = source.value
            elif isinstance(source.value, float):
                if not isfinite(source.value) or not source.value.is_integer():
                    return False
                value = int(source.value)
            elif isinstance(source.value, str):
                try:
                    value = int(source.value.strip())
                except (TypeError, ValueError):
                    return False
            else:
                return False
            if not spec.allow_zero and value == 0:
                return False
        if (
            kind is FieldKind.TEXT
            and not str(source.value).strip()
            and not spec.allow_empty_text
            and spec.field_id != "event.recent"
        ):
            return False
        return True

    @staticmethod
    def _source_age_valid(
        spec: FieldSpec,
        source: InputValue | None,
        *,
        now: float,
    ) -> bool:
        if source is None:
            return False
        return bool(
            spec.freshness_ms == 0
            or now - source.sampled_at <= spec.freshness_ms / 1000.0
        )

    @staticmethod
    def _format(spec: FieldSpec, value: Any) -> str:
        if spec.field_id == "device.modem_status":
            return "Connected" if value is True else "Disconnected"
        if spec.policy is PresentationPolicy.ANTENNA_GUIDE:
            return str(value)
        if spec.policy is PresentationPolicy.DURATION_HMS:
            elapsed_s = max(0, int(float(value)))
            hours, remainder = divmod(elapsed_s, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if spec.field_id == "radio.pband":
            text = str(value or "").strip()
            if text and text.isdigit() and int(text) > 0:
                return f"B{int(text)}"
        if spec.field_id == "radio.ca_pool" and isinstance(value, (list, tuple, set)):
            bands: list[str] = []
            for item in value:
                text = str(item or "").strip().upper()
                if not text:
                    continue
                bands.append(text if text.startswith("B") else f"B{text}")
            return "+".join(bands)
        if spec.field_id == "radio.neighbors":
            return _format_neighbors(value)
        if spec.field_id == "radio.carrier_details":
            return _format_carrier_details(value)
        if spec.field_id == "radio.frequency_set" and isinstance(value, (list, tuple)):
            return "/".join(str(item) for item in value)
        if spec.field_id == "radio.ca_active":
            return "ACTIVE" if bool(value) else "INACTIVE"
        if spec.kind is FieldKind.BOOL:
            return "YES" if bool(value) else "NO"
        if spec.kind is FieldKind.FLOAT:
            if spec.unit == "ms":
                return format_milliseconds(value)
            text = f"{float(value):.1f}"
            return f"{text} {spec.unit}".strip()
        if spec.kind is FieldKind.INTEGER:
            if spec.field_id.startswith("traffic.") and spec.field_id.endswith("_bytes"):
                return _format_bytes(int(value))
            return f"{int(value)} {spec.unit}".strip()
        if spec.kind is FieldKind.LIST:
            return ", ".join(str(item) for item in value)
        if spec.kind is FieldKind.MAPPING and isinstance(value, Mapping):
            return " | ".join(f"{key}: {value[key]}" for key in sorted(value))
        return str(value)

    @staticmethod
    def _output(
        spec: FieldSpec,
        value: Any,
        text: str,
        state: PresentationState,
        source: InputValue | None,
        now: float,
        *,
        freshness: FreshnessState | None = None,
        reason: str | None = None,
    ) -> PresentationValue:
        return PresentationValue(
            field_id=spec.field_id,
            value=value,
            text=text,
            state=state,
            source_sequence=source.sample_sequence if source is not None else 0,
            sampled_at=source.sampled_at if source is not None else 0.0,
            published_at=now,
            owner=spec.owner,
            source_path=spec.source_path,
            freshness=freshness or (source.freshness if source is not None else FreshnessState.UNKNOWN),
            reason=str(reason if reason is not None else (source.reason if source is not None else "")),
        )


def _format_bytes(value: int) -> str:
    absolute = abs(value)
    gib = 1024 ** 3
    tib = 1024 ** 4
    if absolute >= tib:
        return f"{value / tib:.2f} TB"
    if absolute >= gib:
        shown = value / gib
        if shown >= 10:
            return f"{shown:.0f} GB"
        return f"{shown:.2f} GB"
    mib = 1024 ** 2
    if absolute >= mib:
        return f"{value / mib:.0f} MB"
    return f"{value} B"


def _format_neighbors(value: Any) -> str:
    if isinstance(value, (list, tuple)) and not value:
        return "None"
    if isinstance(value, Mapping):
        rows = (
            [value]
            if any(key in value for key in ("band", "pci"))
            else list(value.values())
        )
    elif isinstance(value, (list, tuple, set)):
        rows = list(value)
    else:
        rows = [value]

    rendered: list[str] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        band = _neighbor_int(row, "band", positive=True)
        pci = _neighbor_int(row, "pci", positive=False)
        if band is None or pci is None:
            continue
        identity = (band, pci)
        if identity in seen:
            continue
        seen.add(identity)
        rendered.append(f"B{band}/{pci}")
    return ", ".join(rendered) if rendered else "---"


def _format_carrier_details(value: Any) -> str:
    if not isinstance(value, (list, tuple)):
        return "---"
    primary: str | None = None
    secondary: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            band = int(item.get("band", 0) or 0)
        except (TypeError, ValueError):
            continue
        if band <= 0:
            continue
        if bool(item.get("is_pcell", False)) and primary is None:
            primary = f"B{band}"
        elif not bool(item.get("is_pcell", False)):
            secondary.append(f"C{len(secondary) + 1}B{band}")
    members = ([primary] if primary else []) + secondary
    return "+".join(members) if members else "---"


def _neighbor_int(row: Any, field: str, *, positive: bool) -> int | None:
    if not isinstance(row, Mapping):
        return None
    candidate = row.get(field)
    if isinstance(candidate, bool):
        return None
    try:
        number = int(candidate)
    except (TypeError, ValueError):
        return None
    if positive and number <= 0:
        return None
    if not positive and number < 0:
        return None
    return number


def _extract_tx_power_component(value: Any, component: str) -> str | None:
    normalized_component = re.sub(r"[^a-z0-9]", "", component.lower())
    candidate: Any = None
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key == normalized_component:
                candidate = item
                break
    else:
        text = str(value or "")
        match = re.search(
            rf"\b{re.escape(component)}\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:dBm)?",
            text,
            flags=re.IGNORECASE,
        )
        if match is not None:
            candidate = match.group(1)
    if candidate is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(candidate))
    if match is None:
        text = str(candidate).strip()
        return text or None
    number = float(match.group(0))
    shown = str(int(number)) if number.is_integer() else f"{number:g}"
    return f"{shown} dBm"
