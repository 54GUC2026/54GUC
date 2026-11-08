from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.policy.a1_provider_sequences import (
    decide_a1_provider_morning_sequence,
    decide_a1_provider_overnight_recovery,
    note_a1_overnight_action,
)


def decide_hilink_overnight_recovery(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
    return decide_a1_provider_overnight_recovery(engine, modem, now)


def decide_hilink_morning_sequence(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> Optional[LeverAction]:
    return decide_a1_provider_morning_sequence(engine, modem, now)


def note_hilink_overnight_action(engine: Any, action: LeverAction, now: float) -> None:
    note_a1_overnight_action(engine, action, now)
