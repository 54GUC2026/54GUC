from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.coerce import safe_bool

DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S = 20.0


def modem_read_miss_operator_min_s(cfg: Any = None, *, default: float = DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S) -> float:
    try:
        raw = getattr(cfg, "modem_read_missed_operator_min_s", default)
        return max(0.0, float(raw if raw is not None else default))
    except Exception:
        return float(default)


def modem_read_miss_duration_s(health: Mapping[str, Any] | None, *, now: float | None = None) -> float:
    data = health if isinstance(health, Mapping) else {}
    for key in ("modem_read_missed_duration_s", "time_since_last_success_s"):
        if key in data:
            try:
                return max(0.0, float(data.get(key) or 0.0))
            except Exception:
                return 0.0
    try:
        last_success_ts = float(data.get("last_success_ts", 0.0) or 0.0)
    except Exception:
        last_success_ts = 0.0
    if last_success_ts <= 0.0:
        return 0.0
    ts = float(now if now is not None else time.time())
    return max(0.0, ts - last_success_ts)


def modem_read_missed_operator_visible(
    health: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    min_s: float = DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S,
) -> bool:
    data = health if isinstance(health, Mapping) else {}
    explicit = data.get("modem_read_missed_operator_visible")
    if explicit is not None:
        return safe_bool(explicit)

    state = str(data.get("state", "") or "").strip().lower()
    if state in {"degraded", "connection_lost"}:
        return True
    if safe_bool(data.get("degraded", False), False):
        return True
    if safe_bool(data.get("reconnect_attempted", False), False):
        return True

    try:
        failures = int(data.get("consecutive_failures", 0) or 0)
    except Exception:
        failures = 0
    if failures <= 0 and state not in {"modem_read_missed", "reconnecting"}:
        return False
    return modem_read_miss_duration_s(data, now=now) >= max(0.0, float(min_s))


def annotate_modem_read_miss_visibility(
    health: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    min_s: float = DEFAULT_MODEM_READ_MISS_OPERATOR_MIN_S,
) -> dict[str, Any]:
    data = dict(health or {}) if isinstance(health, Mapping) else {}
    data.pop("modem_read_missed_operator_visible", None)
    threshold_s = max(0.0, float(min_s))
    duration_s = modem_read_miss_duration_s(data, now=now)
    data["modem_read_missed_operator_min_s"] = round(threshold_s, 1)
    data["modem_read_missed_duration_s"] = round(duration_s, 1)
    data["modem_read_missed_operator_visible"] = modem_read_missed_operator_visible(
        data,
        now=now,
        min_s=threshold_s,
    )
    return data
