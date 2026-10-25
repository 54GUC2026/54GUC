"""Outcome-first link classification helpers.

These helpers keep RF labels separate from user outcome.  A low-SINR LTE
carrier can still be the correct production lane when it is CA-capable and
delivers stable capacity; consumers should not each reinvent that rule.
"""

from __future__ import annotations

from typing import Any

from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)


def _float_attr(obj: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(obj, name, default) or default)
    except Exception:
        return float(default)


def _dl_mbps(modem: CellularIntelligenceGeneration) -> float:
    dl = float(modem.traffic.dl_mbps or 0.0)
    if dl > 0.0:
        return dl
    rx_bps = float(modem.traffic.rx_bytes_per_sec or 0.0)
    if rx_bps <= 0.0:
        return 0.0
    return rx_bps * 8.0 / 1_000_000.0


def _congestion_blocks_capacity(
    computed: DerivedSignalFrames,
    cfg: Any,
    max_congestion: float,
) -> bool:
    if not bool(computed.quality.congestion_pct_valid):
        return False
    pct = float(computed.quality.congestion_pct or 0.0)
    if pct < max_congestion:
        return False
    if bool(computed.quality.congestion_pressure_high):
        return True
    hard_ceiling = max(
        _float_attr(cfg, "bad_rf_capacity_hard_congestion_pct", 75.0),
        max_congestion + 20.0,
    )
    return pct >= hard_ceiling


def _band_count(value: Any) -> int:
    if isinstance(value, (list, tuple, set)):
        bands: set[int] = set()
        for raw in value:
            try:
                band = int(raw or 0)
            except Exception:
                band = 0
            if band > 0:
                bands.add(band)
        return len(bands)
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return 0
    bands: set[int] = set()
    for token in text.split("+"):
        if token in {"", "CA"}:
            continue
        if token.startswith("B"):
            token = token[1:]
        try:
            band = int(token)
        except Exception:
            continue
        if band > 0:
            bands.add(band)
    return len(bands)


def has_ca_capacity(modem: CellularIntelligenceGeneration) -> bool:
    """Return True when the current context can plausibly use CA capacity."""
    if bool(modem.cellular.ca_active):
        return True
    if int(modem.cellular.carrier_count or 0) >= 2:
        return True
    if _band_count(getattr(modem.cellular, 'selected_band_set', [])) >= 2:
        return True
    if _band_count(getattr(modem.identity, 'configured_band_pool', [])) >= 2:
        return True
    if _band_count(getattr(modem.cellular, 'selected_band_set_signature', "")) >= 2:
        return True
    if _band_count(getattr(modem.identity, 'configured_band_pool_signature', "")) >= 2:
        return True
    sig = str(getattr(modem.cellular, 'ca_band_signature', "") or "").strip().upper()
    return "+CA" in sig or _band_count(sig) >= 2


def is_bad_rf_capacity_winner(
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    cfg: Any = None,
) -> bool:
    """Classify a low-RF tick as a usable capacity lane.

    This intentionally requires CA capability plus good end-user outcome.  It
    prevents B3/B7-style capacity carriers from being treated as failed just
    because SINR is ugly, while keeping single-band weak RF on the normal
    BAD_RF recovery path.
    """
    if not bool(modem.cellular.radio_connected):
        return False
    if not has_ca_capacity(modem):
        return False

    min_dl = _float_attr(cfg, "bad_rf_capacity_min_dl_mbps", 20.0)
    max_rtt = _float_attr(
        cfg,
        "bad_rf_capacity_max_rtt_ms",
        _float_attr(cfg, "rtt_warning_clear_ms", 120.0),
    )
    max_delta = _float_attr(cfg, "bad_rf_capacity_max_rtt_delta_ms", 60.0)
    max_jitter = _float_attr(cfg, "bad_rf_capacity_max_jitter_pct", 25.0)
    max_congestion = _float_attr(cfg, "bad_rf_capacity_max_congestion_pct", 50.0)
    min_session = _float_attr(cfg, "bad_rf_capacity_min_session_uptime_s", 30.0)

    dl = _dl_mbps(modem)
    if dl > 0.0 and dl < min_dl:
        return False

    session_uptime = float(modem.action.session_uptime_s or 0.0)
    if session_uptime > 0.0 and session_uptime < min_session:
        return False

    rtt = float(computed.latency.rtt_sliding_avg or 0.0)
    if rtt > 0.0 and rtt > max_rtt:
        return False

    if bool(computed.latency.rtt_delta_valid):
        delta = float(computed.latency.rtt_delta or 0.0)
        if delta > max_delta:
            return False

    if bool(computed.latency.rtt_dl_jitter_valid):
        if float(computed.latency.rtt_dl_jitter_pct or 0.0) > max_jitter:
            return False

    if _congestion_blocks_capacity(computed, cfg, max_congestion):
        return False

    return True
