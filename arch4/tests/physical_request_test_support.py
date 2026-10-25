from __future__ import annotations

from dataclasses import replace
from typing import Any

from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)


class AdvancingPhysicalClock:
    """Keep pacing semantics without spending wall time in unit tests."""

    def __init__(self) -> None:
        self.value = 100.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, float(seconds))


def install_physical_request_test_authority(
    modem: Any,
    *,
    backend: str,
    backend_family: str,
    gap_s: float = 0.5,
) -> ModemLivePhysicalRequestAuthority:
    clock = AdvancingPhysicalClock()
    profile = replace(
        modem_live_access_profile_for(
            backend=backend,
            backend_family=backend_family,
        ),
        min_inter_request_gap_s=float(gap_s),
    )
    authority = ModemLivePhysicalRequestAuthority(
        profile,
        monotonic=clock.now,
        wall_clock=clock.now,
        sleeper=clock.sleep,
    )
    modem._access_profile = profile
    modem._physical_request_authority = authority
    return authority


__all__ = [
    "AdvancingPhysicalClock",
    "install_physical_request_test_authority",
]
