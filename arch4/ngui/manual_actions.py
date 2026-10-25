"""Build manual actions with the proven GUI behavior."""

from __future__ import annotations

import re
from typing import Any

from arch4.interfaces import LeverAction, LeverType


MANUAL_ACTION_SOURCE = "ngui_manual_actions"
MANUAL_BAND_SELECTION_REASON = "Manual band selection"


def parse_manual_band_pool(value: Any) -> list[int]:
    """Parse an LTE CA pool and preserve the entered order."""

    parts = re.findall(r"\d+", str(value or ""))
    bands: list[int] = []
    seen: set[int] = set()
    for part in parts:
        try:
            band = int(part)
        except ValueError:
            continue
        if band <= 0 or band in seen:
            continue
        seen.add(band)
        bands.append(band)
    return bands


def tag_manual_action_source(action: LeverAction, source: str) -> LeverAction:
    """Add GUI correlation data without changing the Engine contract."""

    setattr(action, "_gui_action_source", str(source or "").strip())
    return action


def build_manual_band_apply_action(
    *,
    pband: int = 0,
    bands: list[int],
    source: str = MANUAL_ACTION_SOURCE,
) -> LeverAction:
    """Build a PBAND and CA-pool command, or a CA-pool-only command."""

    pool = parse_manual_band_pool(bands)
    if pband > 0:
        if pband not in pool:
            pool = [pband] + pool
        params: dict[str, Any] = {
            "pband": int(pband),
            "ca_pool": pool,
        }
        reason = f"Manual PBAND B{int(pband)} + CA pool"
        return tag_manual_action_source(
            LeverAction(
                lever=LeverType.PBAND,
                params=params,
                reason=reason,
                intent="manual",
            ),
            source,
        )

    params = {"bands": pool, "sequential": False}
    reason = MANUAL_BAND_SELECTION_REASON
    return tag_manual_action_source(
        LeverAction(
            lever=LeverType.BAND_SELECTION,
            params=params,
            reason=reason,
            intent="manual",
        ),
        source,
    )


def build_manual_nr_apply_action(*, band: int, source: str = MANUAL_ACTION_SOURCE) -> LeverAction:
    """NR has its own declared command family; it is never an LTE parameter."""
    if type(band) is not int or band <= 0:
        raise ValueError("Enter one valid NR band number, for example n78.")
    return tag_manual_action_source(
        LeverAction(LeverType.NR_BAND_LOCK, {"nr_bands": [band]},
                    f"Manual NR band n{band}", intent="manual"), source,
    )


__all__ = [
    "MANUAL_ACTION_SOURCE",
    "MANUAL_BAND_SELECTION_REASON",
    "build_manual_band_apply_action",
    "build_manual_nr_apply_action",
    "parse_manual_band_pool",
    "tag_manual_action_source",
]
