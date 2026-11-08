from __future__ import annotations

import time
from typing import Any

from arch4.coerce import safe_float as _safe_float


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return False


def _prior_ts(previous: dict[str, Any], key: str) -> float:
    return _safe_float(previous.get(key), 0.0) if isinstance(previous, dict) else 0.0


def build_delta_display_surface(
    *,
    raw: dict[str, Any] | None,
    previous: dict[str, Any] | None = None,
    now_ts: float = 0.0,
    rsrq_supported: bool = True,
    rsrq_hold_s: float = 30.0,
    rtt_hold_s: float = 30.0,
) -> dict[str, Any]:
    """Build canonical display semantics for RTT/RSRQ delta rows.

    This intentionally lives outside renderers so NGUI projection and
    current-state/runtime exports can apply the same held/live/invalid rules.
    """
    if now_ts <= 0.0:
        now_ts = time.time()
    raw_map = dict(raw) if isinstance(raw, dict) else {}
    prev_map = dict(previous) if isinstance(previous, dict) else {}

    out: dict[str, Any] = {}

    has_canonical_rtt = bool(
        _bool(raw_map.get("rtt_delta_display_valid"))
        or _bool(raw_map.get("rtt_delta_display_held"))
        or _safe_float(raw_map.get("rtt_delta_display_live_ts"), 0.0) > 0.0
    )
    has_canonical_rsrq_delta = bool(
        _bool(raw_map.get("rsrq_delta_display_valid"))
        or _bool(raw_map.get("rsrq_delta_display_held"))
        or _safe_float(raw_map.get("rsrq_delta_display_live_ts"), 0.0) > 0.0
    )
    has_canonical_rsrq_baseline = bool(
        _bool(raw_map.get("rsrq_baseline_display_valid"))
        or _bool(raw_map.get("rsrq_baseline_display_held"))
        or _safe_float(raw_map.get("rsrq_baseline_display_live_ts"), 0.0) > 0.0
    )

    # RTT delta display
    if has_canonical_rtt:
        canonical_rtt_value = _optional_float(raw_map.get("rtt_delta_display_ms"))
        canonical_rtt_valid = _bool(raw_map.get("rtt_delta_display_valid"))
        canonical_rtt_held = _bool(raw_map.get("rtt_delta_display_held"))
        canonical_rtt_live_ts = _safe_float(raw_map.get("rtt_delta_display_live_ts"), 0.0)
        if canonical_rtt_valid and canonical_rtt_value is not None:
            out["rtt_delta_display_ms"] = canonical_rtt_value
            out["rtt_delta_display_valid"] = True
            out["rtt_delta_display_held"] = canonical_rtt_held
            out["rtt_delta_display_live_ts"] = canonical_rtt_live_ts
        elif (
            _bool(prev_map.get("rtt_delta_display_valid"))
            and _optional_float(prev_map.get("rtt_delta_display_ms")) is not None
            and _prior_ts(prev_map, "rtt_delta_display_live_ts") > 0.0
            and (now_ts - _prior_ts(prev_map, "rtt_delta_display_live_ts")) <= float(rtt_hold_s)
        ):
            out["rtt_delta_display_ms"] = float(_optional_float(prev_map.get("rtt_delta_display_ms")) or 0.0)
            out["rtt_delta_display_valid"] = True
            out["rtt_delta_display_held"] = True
            out["rtt_delta_display_live_ts"] = float(_prior_ts(prev_map, "rtt_delta_display_live_ts"))
        else:
            out["rtt_delta_display_ms"] = canonical_rtt_value
            out["rtt_delta_display_valid"] = canonical_rtt_valid
            out["rtt_delta_display_held"] = canonical_rtt_held
            out["rtt_delta_display_live_ts"] = canonical_rtt_live_ts
    else:
        prev_rtt_value = _optional_float(prev_map.get("rtt_delta_display_ms"))
        prev_rtt_valid = _bool(prev_map.get("rtt_delta_display_valid"))
        prev_rtt_live_ts = _prior_ts(prev_map, "rtt_delta_display_live_ts")

        if (
            prev_rtt_valid
            and prev_rtt_value is not None
            and prev_rtt_live_ts > 0.0
            and (now_ts - prev_rtt_live_ts) <= float(rtt_hold_s)
        ):
            out["rtt_delta_display_ms"] = float(prev_rtt_value)
            out["rtt_delta_display_valid"] = True
            out["rtt_delta_display_held"] = True
            out["rtt_delta_display_live_ts"] = float(prev_rtt_live_ts)
        else:
            out["rtt_delta_display_ms"] = None
            out["rtt_delta_display_valid"] = False
            out["rtt_delta_display_held"] = False
            out["rtt_delta_display_live_ts"] = 0.0

    # RSRQ delta display
    if not rsrq_supported:
        out["rsrq_delta_display_db"] = None
        out["rsrq_delta_display_valid"] = False
        out["rsrq_delta_display_held"] = False
        out["rsrq_delta_display_live_ts"] = 0.0
    elif has_canonical_rsrq_delta:
        canonical_rsrq_value = _optional_float(raw_map.get("rsrq_delta_display_db"))
        canonical_rsrq_valid = _bool(raw_map.get("rsrq_delta_display_valid"))
        canonical_rsrq_held = _bool(raw_map.get("rsrq_delta_display_held"))
        canonical_rsrq_live_ts = _safe_float(raw_map.get("rsrq_delta_display_live_ts"), 0.0)
        if canonical_rsrq_valid and canonical_rsrq_value is not None:
            out["rsrq_delta_display_db"] = canonical_rsrq_value
            out["rsrq_delta_display_valid"] = True
            out["rsrq_delta_display_held"] = canonical_rsrq_held
            out["rsrq_delta_display_live_ts"] = canonical_rsrq_live_ts
        elif (
            _bool(prev_map.get("rsrq_delta_display_valid"))
            and _optional_float(prev_map.get("rsrq_delta_display_db")) is not None
            and _prior_ts(prev_map, "rsrq_delta_display_live_ts") > 0.0
            and (now_ts - _prior_ts(prev_map, "rsrq_delta_display_live_ts")) <= float(rsrq_hold_s)
        ):
            out["rsrq_delta_display_db"] = float(_optional_float(prev_map.get("rsrq_delta_display_db")) or 0.0)
            out["rsrq_delta_display_valid"] = True
            out["rsrq_delta_display_held"] = True
            out["rsrq_delta_display_live_ts"] = float(_prior_ts(prev_map, "rsrq_delta_display_live_ts"))
        else:
            out["rsrq_delta_display_db"] = canonical_rsrq_value
            out["rsrq_delta_display_valid"] = canonical_rsrq_valid
            out["rsrq_delta_display_held"] = canonical_rsrq_held
            out["rsrq_delta_display_live_ts"] = canonical_rsrq_live_ts
    else:
        prev_rsrq_value = _optional_float(prev_map.get("rsrq_delta_display_db"))
        prev_rsrq_valid = _bool(prev_map.get("rsrq_delta_display_valid"))
        prev_rsrq_live_ts = _prior_ts(prev_map, "rsrq_delta_display_live_ts")

        if (
            prev_rsrq_valid
            and prev_rsrq_value is not None
            and prev_rsrq_live_ts > 0.0
            and (now_ts - prev_rsrq_live_ts) <= float(rsrq_hold_s)
        ):
            out["rsrq_delta_display_db"] = float(prev_rsrq_value)
            out["rsrq_delta_display_valid"] = True
            out["rsrq_delta_display_held"] = True
            out["rsrq_delta_display_live_ts"] = float(prev_rsrq_live_ts)
        else:
            out["rsrq_delta_display_db"] = None
            out["rsrq_delta_display_valid"] = False
            out["rsrq_delta_display_held"] = False
            out["rsrq_delta_display_live_ts"] = 0.0

    # RSRQ baseline display remains fixed once learned.
    if has_canonical_rsrq_baseline:
        canonical_baseline_value = _optional_float(raw_map.get("rsrq_baseline_display_db"))
        canonical_baseline_valid = _bool(raw_map.get("rsrq_baseline_display_valid"))
        canonical_baseline_held = _bool(raw_map.get("rsrq_baseline_display_held"))
        canonical_baseline_live_ts = _safe_float(raw_map.get("rsrq_baseline_display_live_ts"), 0.0)
        prev_rsrq_baseline = _optional_float(prev_map.get("rsrq_baseline_display_db"))
        prev_rsrq_baseline_valid = _bool(prev_map.get("rsrq_baseline_display_valid"))
        prev_rsrq_baseline_live_ts = _prior_ts(prev_map, "rsrq_baseline_display_live_ts")
        if canonical_baseline_valid and canonical_baseline_value is not None:
            out["rsrq_baseline_display_db"] = float(canonical_baseline_value)
            out["rsrq_baseline_display_valid"] = True
            out["rsrq_baseline_display_held"] = canonical_baseline_held
            out["rsrq_baseline_display_live_ts"] = canonical_baseline_live_ts
        elif prev_rsrq_baseline_valid and prev_rsrq_baseline is not None:
            out["rsrq_baseline_display_db"] = float(prev_rsrq_baseline)
            out["rsrq_baseline_display_valid"] = True
            out["rsrq_baseline_display_held"] = True
            out["rsrq_baseline_display_live_ts"] = float(prev_rsrq_baseline_live_ts or now_ts)
        else:
            out["rsrq_baseline_display_db"] = canonical_baseline_value
            out["rsrq_baseline_display_valid"] = canonical_baseline_valid
            out["rsrq_baseline_display_held"] = canonical_baseline_held
            out["rsrq_baseline_display_live_ts"] = canonical_baseline_live_ts
    else:
        prev_rsrq_baseline = _optional_float(prev_map.get("rsrq_baseline_display_db"))
        prev_rsrq_baseline_valid = _bool(prev_map.get("rsrq_baseline_display_valid"))
        prev_rsrq_baseline_live_ts = _prior_ts(prev_map, "rsrq_baseline_display_live_ts")

        if prev_rsrq_baseline_valid and prev_rsrq_baseline is not None:
            out["rsrq_baseline_display_db"] = float(prev_rsrq_baseline)
            out["rsrq_baseline_display_valid"] = True
            out["rsrq_baseline_display_held"] = True
            out["rsrq_baseline_display_live_ts"] = float(prev_rsrq_baseline_live_ts or now_ts)
        else:
            out["rsrq_baseline_display_db"] = None
            out["rsrq_baseline_display_valid"] = False
            out["rsrq_baseline_display_held"] = False
            out["rsrq_baseline_display_live_ts"] = 0.0

    return out
