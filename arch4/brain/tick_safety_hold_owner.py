"""Explicit hold inputs for the per-tick safety verdict."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.brain.decision_support import DecisionSupportMixin


OWNER = "arch4.brain.tick_safety_hold_owner"


class TickSafetyHoldOwnerError(RuntimeError):
    """Raised when tick-safety hold input cannot be evaluated."""


def known_outage_hold_for_tick(
    engine: Any,
    modem: Optional[CellularIntelligenceGeneration],
) -> bool:
    """Return the known-outage hold verdict for the tick-safety lane."""

    if modem is None:
        return False
    if not isinstance(engine, DecisionSupportMixin):
        return False
    try:
        return bool(DecisionSupportMixin._should_hold_known_outage_window(engine, modem, lane="tick_safety"))
    except Exception as exc:
        raise TickSafetyHoldOwnerError(f"known_outage_hold_unavailable:{type(exc).__name__}") from exc


def throughput_hold_for_tick(
    engine: Any,
    modem: Optional[CellularIntelligenceGeneration],
    computed: Optional[DerivedSignalFrames],
) -> bool:
    """Return the throughput hold verdict for the tick-safety lane."""

    if modem is None:
        return False
    if not isinstance(engine, DecisionSupportMixin):
        return False
    try:
        return bool(DecisionSupportMixin._throughput_hold_active(engine, modem, computed))
    except Exception as exc:
        raise TickSafetyHoldOwnerError(f"throughput_hold_unavailable:{type(exc).__name__}") from exc


__all__ = [
    "OWNER",
    "TickSafetyHoldOwnerError",
    "known_outage_hold_for_tick",
    "throughput_hold_for_tick",
]
