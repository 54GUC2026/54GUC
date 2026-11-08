from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.policy.a1_provider_policy import decide_a1_provider_big_dip
from arch4.policy.family_daytime_policy import filter_family_daytime_band_action
from arch4.policy.family_sequence_policy import (
    decide_hilink_morning_sequence as decide_hilink_morning_sequence_impl,
    decide_hilink_overnight_recovery as decide_hilink_overnight_recovery_impl,
)


def decide_hilink_overnight_recovery(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
    return decide_hilink_overnight_recovery_impl(engine, modem, now)


def decide_hilink_morning_sequence(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> Optional[LeverAction]:
    return decide_hilink_morning_sequence_impl(engine, modem, now)


def decide_hilink_radio_drop_recovery(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
    return decide_a1_provider_big_dip(engine, modem, now)


def filter_hilink_daytime_band_action(engine: Any, action: Optional[LeverAction]) -> Optional[LeverAction]:
    return filter_family_daytime_band_action(engine, action, family="huawei_hilink_like")
