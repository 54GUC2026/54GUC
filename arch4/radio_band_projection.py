"""Pure configured-CA-pool to nominal-frequency presentation contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class RadioBandProjectionError(ValueError):
    """Raised when an exact ordered frequency projection cannot be produced."""


LTE_BAND_DISPLAY_FREQUENCY_MHZ: dict[int, int] = {
    1: 2100,
    2: 1900,
    3: 1800,
    4: 1700,
    5: 850,
    7: 2600,
    8: 900,
    12: 700,
    13: 700,
    17: 700,
    18: 850,
    19: 850,
    20: 800,
    26: 850,
    28: 700,
    32: 1500,
    38: 2600,
    40: 2300,
    41: 2500,
    42: 3500,
    43: 3700,
    66: 1700,
}


def frequency_set_from_ca_pool(pool: Sequence[Any] | None) -> list[int]:
    """Project one nominal MHz label per configured band, preserving order."""

    if pool is None:
        return []
    if isinstance(pool, (str, bytes, bytearray, set, frozenset)) or not isinstance(
        pool, Sequence
    ):
        raise RadioBandProjectionError("configured_ca_pool_ordered_sequence_required")
    frequencies: list[int] = []
    for raw in pool:
        try:
            band = int(raw)
        except (TypeError, ValueError) as exc:
            raise RadioBandProjectionError(
                f"configured_ca_pool_band_invalid:{raw!r}"
            ) from exc
        if band <= 0:
            raise RadioBandProjectionError(
                f"configured_ca_pool_band_invalid:{band}"
            )
        frequency = LTE_BAND_DISPLAY_FREQUENCY_MHZ.get(band)
        if frequency is None:
            raise RadioBandProjectionError(
                f"configured_ca_pool_frequency_unknown:B{band}"
            )
        frequencies.append(int(frequency))
    return frequencies


__all__ = (
    "LTE_BAND_DISPLAY_FREQUENCY_MHZ",
    "RadioBandProjectionError",
    "frequency_set_from_ca_pool",
)
