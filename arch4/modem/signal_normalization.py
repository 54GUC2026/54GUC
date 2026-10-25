"""
Signal normalization helpers for modem backends.

Purpose:
- enforce one internal unit contract before ModemInput is constructed
- support fixed per-backend scaling (e.g., x10 encoded values)
- warn on out-of-contract normalized values
"""

from __future__ import annotations

import time
from typing import Tuple, Dict, Any, Optional

# Conservative LTE sanity ranges (normalized values).
_RANGES = {
    "sinr": (-30.0, 60.0),    # dB
    "rsrp": (-160.0, -20.0),  # dBm
    "rsrq": (-40.0, 5.0),     # dB
    "rssi": (-140.0, -20.0),  # dBm
}


def normalize_signal(metric: str, raw_value: float, scale: float = 1.0) -> Tuple[float, bool]:
    """
    Normalize a raw modem value to internal units.

    Returns:
      (normalized_value, in_contract_range)
    """
    try:
        value = float(raw_value)
    except Exception:
        return 0.0, False

    # Keep sentinel "unknown" values untouched (used by some adapters).
    if metric in {"rsrp", "rsrq", "rssi"} and value <= -900.0:
        return value, True

    try:
        s = float(scale)
    except Exception:
        s = 1.0
    if s == 0.0:
        s = 1.0

    norm = value * s
    lo, hi = _RANGES.get(metric, (-1e9, 1e9))
    ok = lo <= norm <= hi
    return norm, ok


def range_for(metric: str) -> Tuple[float, float]:
    return _RANGES.get(metric, (-1e9, 1e9))


_AMBIG_DELTA = {
    "sinr": 2.0,   # dB
    "rsrp": 5.0,   # dBm
    "rsrq": 2.0,   # dB
    "rssi": 5.0,   # dBm
}

# Treat alternate 0.1x interpretations as plausible only when the raw magnitude
# is large enough to look like a scaled transport value instead of a normal
# whole-dB reading. This prevents ordinary Huawei values such as SINR=9 or
# RSRQ=-9 from being flagged as "ambiguous" on every sample.
_ALT_SCALE_AMBIG_RAW_FLOOR = {
    "sinr": 30.0,
    "rsrq": 30.0,
    "rsrp": 200.0,
    "rssi": 200.0,
}

_SIGNAL_WARNING_INTERVAL_S = 300.0


def assess_signal_contract(
    metric: str,
    raw_value: float,
    scale: float = 1.0,
    alt_scale: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Evaluate normalized value against contract and optional alternate-scale interpretation.

    Returns a dict with keys:
      norm, ok, alt_norm, alt_ok, ambiguous, raw, scale, alt_scale, lo, hi

    `ambiguous=True` means both configured-scale and alternate-scale normalize to
    in-range values with materially different magnitudes, which should be surfaced
    as a warning/event instead of being silently ignored.
    """
    s = float(scale or 1.0) if float(scale or 1.0) != 0.0 else 1.0
    if alt_scale is None:
        alt_s = s * 0.1
    else:
        alt_s = float(alt_scale or 1.0)
    if alt_s == 0.0:
        alt_s = 1.0
    norm, ok = normalize_signal(metric, raw_value, s)
    alt_norm, alt_ok = normalize_signal(metric, raw_value, alt_s)
    lo, hi = range_for(metric)
    delta = float(_AMBIG_DELTA.get(metric, 1.0))
    raw_floor = float(_ALT_SCALE_AMBIG_RAW_FLOOR.get(metric, 0.0))
    ambiguous = bool(
        ok
        and alt_ok
        and abs(float(norm) - float(alt_norm)) >= delta
        and abs(float(raw_value)) >= raw_floor
    )
    return {
        "metric": str(metric),
        "raw": raw_value,
        "scale": s,
        "alt_scale": alt_s,
        "norm": norm,
        "ok": bool(ok),
        "alt_norm": alt_norm,
        "alt_ok": bool(alt_ok),
        "ambiguous": ambiguous,
        "lo": lo,
        "hi": hi,
    }


def rate_limit_signal_contract_warning(
    state: Dict[Tuple[Any, ...], Dict[str, Any]],
    event_type: str,
    assess: Dict[str, Any],
    *,
    now: Optional[float] = None,
    interval_s: float = _SIGNAL_WARNING_INTERVAL_S,
) -> Dict[str, Any]:
    """
    Collapse repeated signal-contract warnings into periodic summaries.

    Returns:
      {"emit": bool, "suppressed": int}

    `suppressed` is the number of matching warnings skipped since the last emit.
    """
    ts = float(time.time() if now is None else now)
    key = (
        str(event_type or ""),
        str(assess.get("metric", "") or ""),
        round(float(assess.get("scale", 1.0) or 1.0), 4),
        round(float(assess.get("alt_scale", 1.0) or 1.0), 4),
        round(float(assess.get("lo", 0.0) or 0.0), 1),
        round(float(assess.get("hi", 0.0) or 0.0), 1),
        bool(assess.get("ok", False)),
        bool(assess.get("alt_ok", False)),
    )
    entry = state.get(key)
    if not isinstance(entry, dict):
        state[key] = {"last_emit_ts": ts, "suppressed": 0}
        return {"emit": True, "suppressed": 0}
    last_emit_ts = float(entry.get("last_emit_ts", 0.0) or 0.0)
    if (ts - last_emit_ts) >= float(interval_s):
        suppressed = int(entry.get("suppressed", 0) or 0)
        state[key] = {"last_emit_ts": ts, "suppressed": 0}
        return {"emit": True, "suppressed": suppressed}
    entry["suppressed"] = int(entry.get("suppressed", 0) or 0) + 1
    return {"emit": False, "suppressed": int(entry["suppressed"])}
