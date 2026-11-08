"""Stateful presentation rules independent of the GUI toolkit and runtime code."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..front_register import PresentationState
from ..input_registers import InputValue
from ..types import FreshnessState


@dataclass(frozen=True, slots=True)
class StabilizedValue:
    value: Any
    state: PresentationState
    source_sequence: int
    sampled_at: float
    freshness: FreshnessState
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return a transport-safe copy of the policy value."""

        return {
            "value": self.value,
            "state": self.state.value,
            "source_sequence": self.source_sequence,
            "sampled_at": self.sampled_at,
            "freshness": self.freshness.value,
            "reason": self.reason,
        }


@dataclass(slots=True)
class HeldValueStore:
    _values: dict[str, StabilizedValue] = field(default_factory=dict)

    def clone(self) -> "HeldValueStore":
        """Copy the mutable container; its values are immutable."""

        return HeldValueStore(_values=dict(self._values))

    def to_payload(self) -> dict[str, Any]:
        """Export held values without exposing the live store."""

        return {
            field_id: self._values[field_id].to_payload()
            for field_id in sorted(self._values)
        }

    def remember(self, field_id: str, source: InputValue) -> StabilizedValue:
        return self.remember_value(field_id, source.value, source)

    def remember_value(self, field_id: str, value: Any, source: InputValue) -> StabilizedValue:
        value = StabilizedValue(
            value=value,
            state=PresentationState.VALID,
            source_sequence=source.sample_sequence,
            sampled_at=source.sampled_at,
            freshness=source.freshness,
            reason=source.reason,
        )
        self._values[field_id] = value
        return value

    def forget(self, field_id: str) -> None:
        self._values.pop(field_id, None)

    def clear(self) -> None:
        self._values.clear()

    def held(
        self,
        field_id: str,
        *,
        now: float,
        hold_ms: int,
        indefinite: bool = False,
        reason: str = "short_miss_hold",
    ) -> StabilizedValue | None:
        value = self._values.get(field_id)
        if value is None:
            return None
        if not indefinite and now - value.sampled_at > hold_ms / 1000.0:
            return None
        return StabilizedValue(
            value=value.value,
            state=PresentationState.HELD,
            source_sequence=value.source_sequence,
            sampled_at=value.sampled_at,
            freshness=FreshnessState.HELD,
            reason=reason,
        )


@dataclass(slots=True)
class CaLdPolicy:
    _asserted: StabilizedValue | None = None
    _negative_sequences: deque[int] = field(default_factory=lambda: deque(maxlen=3))
    _last_observed_at: float = 0.0

    def clone(self) -> "CaLdPolicy":
        """Return an independent candidate for one transaction."""

        return CaLdPolicy(
            _asserted=self._asserted,
            _negative_sequences=deque(
                self._negative_sequences,
                maxlen=self._negative_sequences.maxlen,
            ),
            _last_observed_at=self._last_observed_at,
        )

    def to_payload(self) -> dict[str, Any]:
        """Export the CA-load hysteresis state."""

        return {
            "asserted": (
                self._asserted.to_payload()
                if self._asserted is not None
                else None
            ),
            "negative_sequences": list(self._negative_sequences),
            "last_observed_at": self._last_observed_at,
        }

    def evaluate(
        self,
        source: InputValue | None,
        *,
        valid_fresh_sample: bool,
        now: float,
        hold_ms: int,
    ) -> StabilizedValue | None:
        if not valid_fresh_sample or source is None:
            return self._held(now=now, hold_ms=hold_ms)
        self._last_observed_at = source.sampled_at
        value = float(source.value)
        if value > 0.0:
            self._negative_sequences.clear()
            self._asserted = StabilizedValue(
                value=value,
                state=PresentationState.VALID,
                source_sequence=source.sample_sequence,
                sampled_at=source.sampled_at,
                freshness=source.freshness,
                reason=source.reason,
            )
            return self._asserted
        if self._asserted is None:
            return StabilizedValue(
                value=0.0,
                state=PresentationState.VALID,
                source_sequence=source.sample_sequence,
                sampled_at=source.sampled_at,
                freshness=source.freshness,
                reason=source.reason,
            )
        if source.sample_sequence not in self._negative_sequences:
            self._negative_sequences.append(source.sample_sequence)
        if len(self._negative_sequences) < 3:
            return self._held(now=now, hold_ms=hold_ms)
        self._negative_sequences.clear()
        self._asserted = StabilizedValue(
            value=0.0,
            state=PresentationState.VALID,
            source_sequence=source.sample_sequence,
            sampled_at=source.sampled_at,
            freshness=source.freshness,
            reason=source.reason,
        )
        return self._asserted

    def _held(self, *, now: float, hold_ms: int) -> StabilizedValue | None:
        if self._asserted is None:
            return None
        if now - self._last_observed_at > hold_ms / 1000.0:
            return None
        return StabilizedValue(
            value=self._asserted.value,
            state=PresentationState.HELD,
            source_sequence=self._asserted.source_sequence,
            sampled_at=self._asserted.sampled_at,
            freshness=FreshnessState.HELD,
            reason="ca_ld_confirmation_hold",
        )


@dataclass(slots=True)
class JitterPolicy:
    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=3))
    _last_sequence: int = 0
    _pending_positive_since: float = 0.0
    _last_positive_at: float = 0.0
    _visible: float = 0.0
    _observed: bool = False
    _has_visible_measurement: bool = False
    _last_observed_at: float = 0.0

    def clone(self) -> "JitterPolicy":
        """Return an independent candidate for one transaction."""

        return JitterPolicy(
            _samples=deque(
                self._samples,
                maxlen=self._samples.maxlen,
            ),
            _last_sequence=self._last_sequence,
            _pending_positive_since=self._pending_positive_since,
            _last_positive_at=self._last_positive_at,
            _visible=self._visible,
            _observed=self._observed,
            _has_visible_measurement=self._has_visible_measurement,
            _last_observed_at=self._last_observed_at,
        )

    def to_payload(self) -> dict[str, Any]:
        """Export the jitter hold state."""

        return {
            "samples": list(self._samples),
            "last_sequence": self._last_sequence,
            "pending_positive_since": self._pending_positive_since,
            "last_positive_at": self._last_positive_at,
            "visible": self._visible,
            "observed": self._observed,
            "has_visible_measurement": self._has_visible_measurement,
            "last_observed_at": self._last_observed_at,
        }

    def clear_visible(self, _field_id: str = "") -> None:
        """Clear expired operator-visible jitter history."""

        self._samples.clear()
        self._last_sequence = 0
        self._pending_positive_since = 0.0
        self._last_positive_at = 0.0
        self._visible = 0.0
        self._observed = False
        self._has_visible_measurement = False
        self._last_observed_at = 0.0

    def evaluate(
        self,
        source: InputValue | None,
        *,
        valid_fresh_sample: bool,
        now: float,
        hold_ms: int,
    ) -> StabilizedValue | None:
        if not valid_fresh_sample or source is None:
            if source is not None and not source.valid:
                return self._held_or_missing(
                    now=now,
                    hold_ms=hold_ms,
                    reason=source.reason or "jitter_input_invalid",
                )
            return self._held_or_missing(now=now, hold_ms=hold_ms)
        value = max(0.0, float(source.value))
        new_sample = source.sample_sequence != self._last_sequence
        if new_sample:
            self._last_sequence = source.sample_sequence
            self._samples.append(value)
            self._observed = True
            # The hold starts when the value becomes visible. Transport delay
            # must not consume the three-second display time. Keep the source
            # sample time only as evidence.
            self._last_observed_at = now
        average = sum(self._samples) / max(1, len(self._samples))
        positive = value > 0.5 and average > 0.5
        if positive:
            if self._visible <= 0.5:
                if self._pending_positive_since <= 0.0:
                    self._pending_positive_since = now
                if now - self._pending_positive_since < 3.0:
                    if not self._has_visible_measurement:
                        return StabilizedValue(
                            value=None,
                            state=PresentationState.NOT_APPLICABLE,
                            source_sequence=source.sample_sequence,
                            sampled_at=source.sampled_at,
                            freshness=source.freshness,
                            reason="jitter_positive_confirming",
                        )
                    return self._decision(
                        source,
                        value=self._visible,
                        state=PresentationState.HELD,
                        freshness=FreshnessState.HELD,
                        reason="jitter_positive_confirming",
                    )
            first_visible = not self._has_visible_measurement
            self._visible = average
            self._has_visible_measurement = True
            self._last_positive_at = source.sampled_at
            if first_visible:
                self._last_observed_at = now
            self._pending_positive_since = 0.0
            return self._decision(source, value=self._visible)

        self._pending_positive_since = 0.0
        self._visible = 0.0
        self._has_visible_measurement = True
        return self._decision(source, value=0.0)

    def _held_or_missing(
        self,
        *,
        now: float,
        hold_ms: int,
        reason: str = "jitter_input_hold",
    ) -> StabilizedValue | None:
        if not self._has_visible_measurement:
            if (
                not self._observed
                or now - self._last_observed_at > hold_ms / 1000.0
            ):
                return None
            return StabilizedValue(
                value=None,
                state=PresentationState.NOT_APPLICABLE,
                source_sequence=self._last_sequence,
                sampled_at=self._last_observed_at,
                freshness=FreshnessState.HELD,
                reason=reason,
            )
        if now - self._last_observed_at > hold_ms / 1000.0:
            return None
        return StabilizedValue(
            value=self._visible,
            state=PresentationState.HELD,
            source_sequence=self._last_sequence,
            sampled_at=self._last_positive_at or self._last_observed_at,
            freshness=FreshnessState.HELD,
            reason=reason,
        )

    @staticmethod
    def _decision(
        source: InputValue,
        *,
        value: float,
        state: PresentationState = PresentationState.VALID,
        freshness: FreshnessState | None = None,
        reason: str = "",
    ) -> StabilizedValue:
        return StabilizedValue(
            value=value,
            state=state,
            source_sequence=source.sample_sequence,
            sampled_at=source.sampled_at,
            freshness=freshness or source.freshness,
            reason=reason or source.reason,
        )
