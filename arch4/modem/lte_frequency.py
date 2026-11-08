"""Canonical LTE EARFCN to carrier-frequency conversion."""

from __future__ import annotations


# 3GPP TS 36.101: band, DL EARFCN low/high, DL base MHz, UL base MHz.
# ``None`` is used for supplemental downlink bands without an uplink carrier.
_LTE_EARFCN_BANDS = (
    (1, 0, 599, 2110.0, 1920.0),
    (3, 1200, 1949, 1805.0, 1710.0),
    (4, 1950, 2399, 2110.0, 1710.0),
    (5, 2400, 2649, 869.0, 824.0),
    (7, 2750, 3449, 2620.0, 2500.0),
    (8, 3450, 3799, 925.0, 880.0),
    (18, 5850, 5999, 860.0, 815.0),
    (19, 6000, 6149, 875.0, 830.0),
    (20, 6150, 6449, 791.0, 832.0),
    (26, 8690, 9039, 859.0, 814.0),
    (28, 9210, 9659, 758.0, 703.0),
    (32, 9920, 10359, 1452.0, None),
    (34, 36200, 36349, 2010.0, 2010.0),
    (38, 37750, 38249, 2570.0, 2570.0),
    (39, 38250, 38649, 1880.0, 1880.0),
    (40, 38650, 39649, 2300.0, 2300.0),
    (41, 39650, 41589, 2496.0, 2496.0),
    (42, 41590, 43589, 3400.0, 3400.0),
    (43, 43590, 45589, 3600.0, 3600.0),
)


def lte_band_from_dl_earfcn(earfcn: int) -> int:
    """Return a band from the canonical table, or zero for unknown channels."""
    if isinstance(earfcn, bool) or not isinstance(earfcn, int):
        return 0
    for band, low, high, _dl, _ul in _LTE_EARFCN_BANDS:
        if low <= earfcn <= high:
            return band
    return 0


def lte_frequency_mhz_from_dl_earfcn(earfcn: int) -> tuple[float, float]:
    """Return canonical ``(DL MHz, UL MHz)`` or zero for unavailable parts."""

    if isinstance(earfcn, bool):
        return 0.0, 0.0
    channel = int(earfcn or 0)
    for _band, low, high, dl_base, ul_base in _LTE_EARFCN_BANDS:
        if low <= channel <= high:
            offset = 0.1 * (channel - low)
            dl = round(dl_base + offset, 1)
            ul = 0.0 if ul_base is None else round(ul_base + offset, 1)
            return dl, ul
    return 0.0, 0.0


def lte_earfcn_bounds(band: int, *, uplink: bool = False) -> tuple[int, int] | None:
    """Covered channel bounds; None means this table has no band coverage."""
    for covered_band, low, high, _dl, ul in _LTE_EARFCN_BANDS:
        if band == covered_band:
            if uplink and ul is None:
                return (0, -1)  # Supplemental downlink has no UL channel.
            # TS36.101 tables5.7.3-1/2: covered FDD bands use +18000;
            # covered TDD bands use identical DL/UL channel numbering.
            offset = 18000 if uplink and covered_band < 32 else 0
            return low + offset, high + offset
    return None


def lte_earfcn_from_frequency_100khz(band: int, frequency: int, *, uplink: bool = False) -> int:
    """Convert a qualified native100kHz frequency using the sole LTE table."""
    if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0:
        return 0
    bounds = lte_earfcn_bounds(band, uplink=uplink)
    if bounds is None:
        return 0
    for covered_band, _low, _high, dl_base, ul_base in _LTE_EARFCN_BANDS:
        if band == covered_band:
            base = ul_base if uplink else dl_base
            if base is None:
                return 0
            channel = bounds[0] + frequency - round(base * 10)
            return channel if bounds[0] <= channel <= bounds[1] else 0
    return 0


def lte_channel_from_reported_value(band: int, channel: int, frequency_100khz: int = 0,
                                   *, uplink: bool = False) -> int:
    """Correct only a witnessed frequency-form token; retain unresolved input.

    Standard channels take precedence. A band/channel disagreement alone cannot
    identify which field is wrong. No Cell ID or PCI can be inferred here.
    """
    bounds = lte_earfcn_bounds(band, uplink=uplink)
    if bounds is None or bounds[0] <= channel <= bounds[1]:
        return channel
    if channel > 0 and channel == frequency_100khz:
        corrected = lte_earfcn_from_frequency_100khz(band, frequency_100khz, uplink=uplink)
        if corrected:
            return corrected
    return channel


__all__ = ("lte_frequency_mhz_from_dl_earfcn", "lte_band_from_dl_earfcn",
           "lte_earfcn_bounds", "lte_earfcn_from_frequency_100khz", "lte_channel_from_reported_value")
