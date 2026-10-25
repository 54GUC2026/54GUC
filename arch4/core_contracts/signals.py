"""Structural Core signal-computation input contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class SignalSamplePort(Protocol):
    """Minimum accepted-generation view consumed by deterministic signals."""

    ca_active: bool
    radio_connected: bool | None
    nsa_snapshot: Mapping[str, Any]
    session_scope_key: str
    quality_model: str
    quality_radio_inputs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LatencyTrafficQualification:
    """One Core-owned interpretation of traffic for latency processors."""

    valid: bool
    invalid_reason: str
    rtt_baseline_idle: bool
    rtt_busy_now: bool
    rtt_release_safe: bool
    rtt_instant_busy: bool
    rtt_busy_mode: str
    low_load_latency_valid: bool
    low_load_latency_reason: str
    jitter_valid: bool
    jitter_reason: str
    jitter_load_mbps: float

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise TypeError("latency_traffic_valid_bool_required")
        reason = str(self.invalid_reason or "").strip().lower()
        if self.valid and reason:
            raise ValueError("valid_latency_traffic_cannot_have_invalid_reason")
        if not self.valid and not reason:
            raise ValueError("invalid_latency_traffic_reason_required")
        object.__setattr__(self, "invalid_reason", reason)
        for field_name in (
            "rtt_baseline_idle",
            "rtt_busy_now",
            "rtt_release_safe",
            "rtt_instant_busy",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"latency_traffic_{field_name}_bool_required")
        busy_mode = str(self.rtt_busy_mode or "").strip().lower()
        if busy_mode not in {"", "dl", "ul"}:
            raise ValueError("latency_traffic_rtt_busy_mode_invalid")
        object.__setattr__(self, "rtt_busy_mode", busy_mode)
        for field_name, reason_name in (
            ("low_load_latency_valid", "low_load_latency_reason"),
            ("jitter_valid", "jitter_reason"),
        ):
            qualified = getattr(self, field_name)
            if type(qualified) is not bool:
                raise TypeError(f"latency_traffic_{field_name}_bool_required")
            qualification_reason = str(getattr(self, reason_name) or "").strip().lower()
            if qualified and qualification_reason:
                raise ValueError(f"latency_traffic_{reason_name}_forbidden_when_valid")
            if not qualified and not qualification_reason:
                raise ValueError(f"latency_traffic_{reason_name}_required")
            object.__setattr__(self, reason_name, qualification_reason)
        load = float(self.jitter_load_mbps)
        if load < 0.0:
            raise ValueError("latency_traffic_jitter_load_nonnegative_required")
        object.__setattr__(self, "jitter_load_mbps", load)


__all__ = ("LatencyTrafficQualification", "SignalSamplePort")
