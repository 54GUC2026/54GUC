"""Permanent get_wwandst boundary: direction, quantity and period stay distinct."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


# These are API adapters, not alternate downstream truth owners. The existing
# GoForm rate/counter parser still owns interval/reset handling inside Modem Live.
_FIELDS: dict[str, tuple[str, ...]] = {
    "wan_rx_bytes": ("real_rx_bytes", "wan_rx_bytes", "rx_bytes", "rx_byte", "download_bytes"),
    "wan_tx_bytes": ("real_tx_bytes", "wan_tx_bytes", "tx_bytes", "tx_byte", "upload_bytes"),
    "realtime_rx_thrpt": ("real_rx_speed", "realtime_rx_thrpt", "rx_bytes_per_sec"),
    "realtime_tx_thrpt": ("real_tx_speed", "realtime_tx_thrpt", "tx_bytes_per_sec"),
    "ppp_uptime": ("real_time", "ppp_uptime", "up_time", "connect_time"),
    "monthly_rx_bytes": ("month_rx_bytes", "monthly_rx_bytes"),
    "monthly_tx_bytes": ("month_tx_bytes", "monthly_tx_bytes"),
}
_PERIOD_FIELDS = {
    "traffic_session_download_bytes": "wan_rx_bytes",
    "traffic_session_upload_bytes": "wan_tx_bytes",
    "traffic_session_duration_s": "ppp_uptime",
    "traffic_month_download_bytes": "monthly_rx_bytes",
    "traffic_month_upload_bytes": "monthly_tx_bytes",
}
for _period in ("day", "month", "total"):
    for _quantity, _native in (("download_bytes", "rx_bytes"), ("upload_bytes", "tx_bytes"), ("duration_s", "time")):
        _name = f"traffic_{_period}_{_quantity}"
        if _name not in _PERIOD_FIELDS:
            _FIELDS[_name] = (f"{_period}_{_native}",)

ZWRT_TRAFFIC_OUTPUT_FIELDS = frozenset(_FIELDS) | frozenset(_PERIOD_FIELDS) | {
    "traffic_device_download_counter_bytes", "traffic_device_upload_counter_bytes",
    "traffic_accounting_source", "zwrt_traffic_diagnostic",
}
# Retire parent-parser shortcuts from the unrelated radio reply. All ZWRT
# traffic enters through the explicit get_wwandst adapter above.
ZWRT_TRAFFIC_PARENT_FIELDS = ZWRT_TRAFFIC_OUTPUT_FIELDS | frozenset(
    name for direction in ("rx", "tx") for name in (
        f"{direction}_bytes", f"realtime_{direction}_bytes", f"traffic_{direction}",
        f"total_{direction}_bytes", f"{direction}_thrpt", f"{direction}_rate",
    )
)


def decode_zwrt_traffic(reply: Mapping[str, Any]) -> dict[str, Any]:
    index: dict[str, list[tuple[str, Any]]] = {}
    for name, value in reply.items():
        index.setdefault(_key(str(name)), []).append((str(name), value))
    output: dict[str, Any] = {}
    problems: dict[str, Any] = {}
    recognized = set()
    for target, aliases in _FIELDS.items():
        candidates = [pair for alias in aliases for pair in index.get(_key(alias), ())]
        recognized.update(_key(alias) for alias in aliases)
        if not candidates:
            continue
        values = []
        for name, value in candidates:
            integer = target.endswith("bytes")
            text = str(value).strip()
            valid = type(value) is not bool and len(text) <= 32 and bool(re.fullmatch(r"\d+(?:\.0+)?" if integer else r"\d+(?:\.\d+)?", text))
            number = (int(text.split(".")[0]) if integer else float(text)) if valid else None
            if number is None or (not integer and not math.isfinite(number)):
                break
            values.append(number)
        if len(values) != len(candidates) or len(set(values)) != 1:
            problems[target] = {"reason": "invalid_or_conflicting", "fields":
                                {name[:80]: str(value)[:100] for name, value in candidates[:8]}}
            continue
        output[target] = values[0]
    for target, source in _PERIOD_FIELDS.items():
        if source in output:
            output[target] = output[source]
    # Totals are physical counters only. Foundation owns subscription/cycle
    # attribution and reset handling; no calendar start is guessed here.
    for direction in ("download", "upload"):
        source = f"traffic_total_{direction}_bytes"
        if source in output:
            output[f"traffic_device_{direction}_counter_bytes"] = output[source]
    if any(name.startswith("traffic_") and name.endswith("bytes") for name in output):
        output["traffic_accounting_source"] = "modem_native"
    unknown = [name[:80] for name in map(str, reply) if _key(name) not in recognized
               and any(part in _key(name) for part in ("byte", "speed", "rate", "time"))][:16]
    output["zwrt_traffic_diagnostic"] = {"problems": problems, "unrecognized_fields": unknown}
    return output
