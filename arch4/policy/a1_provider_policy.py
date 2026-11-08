"""Retired provider action aliases.

All imported public entrypoints fail loudly. The retained implementations are
non-public behavior oracles in ``a1_b818_policy``.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.policy.a1_b818_policy import (
    decide_a1_b818_big_dip,
    maybe_ca_restore_sequence,
    suppress_a1_provider_daytime_band_action,
    suppress_b818_daytime_band_action,
)


def decide_a1_provider_big_dip(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    return decide_a1_b818_big_dip(engine, modem, now)


__all__ = [
    "decide_a1_provider_big_dip",
    "maybe_ca_restore_sequence",
    "suppress_a1_provider_daytime_band_action",
    "suppress_b818_daytime_band_action",
]
