"""
A1+B818 window/restriction helpers.

Pure provider/backend gating logic extracted from the decision mixin so the
brain-owned file carries less inline provider-specific policy code.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, List, Optional

def is_a1_overnight_profile(engine: Any) -> bool:
    """True if the A1 overnight schedule applies.

    Gates: morning sequence, restricted night band pool, big-dip B20 fallback.
    Historically required modem_backend == 'b818'. Relaxed to A1 SIM only —
    any modem with A1 SIM near overnight-power-saving towers sees the same
    B3 collapse and benefits from these recovery paths.
    """
    p = engine.policy_profile
    return bool(
        p
        and getattr(p, "overnight_recovery_enabled", getattr(p, "a1_overnight_recovery", False))
    )


def policy_provider_plmn(engine: Any) -> str:
    p = engine.policy_profile
    if not p:
        return ""
    return str(p.provider_plmn or "")


def hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def is_a1_restricted_night_window(
    engine: Any,
    now: Optional[float] = None,
    modem: Optional[CellularIntelligenceGeneration] = None,
) -> bool:
    if not is_a1_overnight_profile(engine):
        return False
    ts = time.time() if now is None else now
    hour = time.localtime(ts).tm_hour
    in_window = hour_in_window(
        hour,
        int(engine.cfg.a1_night_restrict_start_hour),
        int(engine.cfg.a1_night_restrict_end_hour),
    )
    if not in_window:
        return False
    # A1+B818 restricted-night policy is unconditional inside the configured
    # window. The point is to keep the backend inside the B3/B20 pool and stop
    # generic B1 exploration from running overnight.
    return True


def is_a1_hard_night_freeze_window(
    engine: Any,
    now: Optional[float] = None,
) -> bool:
    """
    Retired A1+B818 hard freeze window.

    Kept only as a compatibility helper so older status/reporting code does not
    break while the runtime path remains restricted-night + morning-sequence
    only. The live brain no longer uses a global hard-freeze gate.
    """
    return False


def a1_restricted_band_pool(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: Optional[float] = None,
) -> List[int]:
    available = set(engine.deployed_bands)
    if modem.cellular.primary_band > 0:
        available.add(int(modem.cellular.primary_band))
    if not is_a1_restricted_night_window(engine, now, modem):
        return sorted(available)
    allowed = {3, 20}
    out = sorted(available & allowed)
    if out:
        return out
    return [20] if 20 in available else ([3] if 3 in available else sorted(available))


def filter_a1_restricted_bands(
    engine: Any,
    bands: List[int],
    modem: CellularIntelligenceGeneration,
    now: Optional[float] = None,
) -> List[int]:
    norm: List[int] = []
    for b in bands:
        try:
            bi = int(b)
        except (TypeError, ValueError):
            continue
        if bi > 0:
            norm.append(bi)
    if not is_a1_restricted_night_window(engine, now, modem):
        return sorted(set(norm))
    allowed = set(a1_restricted_band_pool(engine, modem, now))
    out = sorted(set(b for b in norm if b in allowed))
    if out:
        return out
    return sorted(allowed)
