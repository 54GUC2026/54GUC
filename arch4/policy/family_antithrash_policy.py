from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.policy.a1_b818_antithrash import (
    a1_pick_freeze_bands,
    apply_a1_restricted_band_filter,
    guard_a1_antithrash,
    is_strict_a1_band_action,
)


def is_hilink_strict_band_action(action: LeverAction) -> bool:
    return is_strict_a1_band_action(action)


def apply_hilink_restricted_band_filter(engine: Any, action: Optional[LeverAction], modem: CellularIntelligenceGeneration, now: float) -> Optional[LeverAction]:
    return apply_a1_restricted_band_filter(engine, action, modem, now)


def pick_hilink_freeze_bands(engine: Any, modem: CellularIntelligenceGeneration, now: float):
    return a1_pick_freeze_bands(engine, modem, now)


def guard_hilink_antithrash(engine: Any, action: Optional[LeverAction], modem: CellularIntelligenceGeneration, now: float) -> Optional[LeverAction]:
    return guard_a1_antithrash(engine, action, modem, now)
