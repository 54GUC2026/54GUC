"""Pure ZTE goform parser helpers."""

from __future__ import annotations

import re
from typing import Any, Optional

CONNECTED_PPP_STATES = {
    "connected",
    "connect",
    "ppp_connected",
    "ipv4_connected",
    "ipv6_connected",
    "ipv4_ipv6_connected",
}

NEGATIVE_STATES = {
    "",
    "disconnect",
    "disconnected",
    "not connected",
    "noservice",
    "no service",
}

TRUTHY_STRINGS = {
    "1",
    "true",
    "on",
    "yes",
    "enable",
    "enabled",
    "active",
    "activated",
    "ca",
    "ca_activated",
}


def is_limited_service(value: Any) -> bool:
    """ZTE limited-service state, independent of the appended radio technology."""
    return bool(re.fullmatch(r"limited[\s_-]+service(?:[\s_-]+[a-z0-9]+)*",
                             str(value or "").strip(), re.IGNORECASE))

ZTE_BAND_MASKS: dict[int, int] = {
    1: 0x0000000001,
    2: 0x0000000002,
    3: 0x0000000004,
    5: 0x0000000010,
    7: 0x0000000040,
    8: 0x0000000080,
    20: 0x0000080000,
    28: 0x0008000000,
    32: 0x0080000000,
    38: 0x0800000000,
    40: 0x1000000000,
    41: 0x2000000000,
}



# ZWRT/NG uses 3GPP band-minus-one positions; GoForm's table is a separate dialect.
ZWRT_LTE_BANDS = (1, 2, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41)


def encode_zwrt_lte_mask(bands: Any) -> int:
    mask = 0
    for band in bands:
        if type(band) is not int or band not in ZWRT_LTE_BANDS:
            raise ValueError("zte_zwrt_lte_band_unqualified")
        mask |= 1 << (band - 1)
    if not mask:
        raise ValueError("zte_zwrt_empty_lte_mask")
    return mask


def decode_zwrt_lte_mask(value: Any) -> list[int]:
    try:
        mask = int(str(value).strip(), 0)
    except (ValueError, TypeError):
        return []
    return [band for band in ZWRT_LTE_BANDS if mask & (1 << (band - 1))]

def parse_antenna_mode_text(raw: dict[str, Any]) -> str:
    values = [
        str(raw.get("antenna_mode") or "").strip().lower(),
        str(raw.get("antenna_type") or "").strip().lower(),
        str(raw.get("antenna_status") or "").strip().lower(),
    ]
    for value in values:
        if "external" in value or value in {"ext", "ts9"}:
            return "External"
        if "internal" in value or value == "int":
            return "Internal"
        if "auto" in value:
            return "Auto"
    return ""


def is_5g_only_mode(raw: dict[str, Any]) -> bool:
    mode_text = " ".join(
        str(raw.get(key) or "").strip().upper()
        for key in ("current_network_mode", "network_type")
    )
    if not mode_text:
        return False
    if "5G" not in mode_text and "NR" not in mode_text:
        return False
    return "ONLY" in mode_text


def normalize_imsi(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if 14 <= len(digits) <= 16 and digits == text:
        return digits
    return ""


def parse_wifi_enabled(raw: dict[str, Any]) -> Optional[bool]:
    value = raw.get("wifiEnabled")
    if value is None or str(value).strip() == "":
        value = raw.get("wifi_onoff_state")
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip().lower() in ("1", "true", "on", "enabled")


def parse_wifi_band_enabled(
    raw: dict[str, Any],
    *,
    band: str,
) -> Optional[bool]:
    field = {
        "2.4": "wifi_chip1_ssid1_switch_onoff",
        "5": "wifi_chip2_ssid1_switch_onoff",
    }.get(str(band))
    value = raw.get(field) if field else None
    if value is None or str(value).strip() == "":
        return parse_wifi_enabled(raw)
    return str(value).strip().lower() in (
        "1",
        "true",
        "on",
        "enabled",
    )


def decode_band_mask(value: Any) -> list[int]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        mask = int(text, 0)
    except Exception:
        return []
    return [int(band) for band, bit in sorted(ZTE_BAND_MASKS.items()) if mask & int(bit)]


def parse_band_values(value: Any) -> list[int]:
    text = str(value or "").strip().upper()
    if not text:
        return []
    bands: list[int] = []
    for token in re.findall(r"\d+", text):
        try:
            band = int(token)
        except Exception:
            continue
        if band > 0 and band not in bands:
            bands.append(band)
    return bands


def dedupe_bands(values: list[int]) -> list[int]:
    out: list[int] = []
    for raw in values:
        try:
            band = int(raw or 0)
        except Exception:
            band = 0
        if band > 0 and band not in out:
            out.append(band)
    return out


def parse_float(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except Exception:
        return 0.0


def parse_metric_float(value: Any) -> float:
    return parse_float(str(value or "").replace("MHz", "").replace("dBm", ""))


def parse_dbm_metric(value: Any) -> float:
    number = parse_metric_float(value)
    if number > 0.0:
        return -number
    return number


def parse_nr_band(value: Any) -> int:
    text = str(value or "").strip().lower().replace("band", "").replace("nr", "").replace("lte", "")
    match = re.search(r"n?(\d+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def first_non_zero(*values: float) -> float:
    for value in values:
        try:
            number = float(value)
        except Exception:
            number = 0.0
        if abs(number) > 0.0001:
            return number
    return 0.0


def parse_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text, 0)
    except Exception:
        pass
    match = re.search(r"(0x[0-9a-fA-F]+|-?\d+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1), 0)
    except Exception:
        return 0


def parse_duration(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return float(text)
        except Exception:
            return 0.0
    parts = text.split(":")
    if len(parts) == 3:
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        except Exception:
            return 0.0
        return max(0.0, hours * 3600 + minutes * 60 + seconds)
    return 0.0


def profile_input_overlay(raw, overrides):
    """Bind only this native caller's current inputs to a replay projection.

    Retain other physically returned RPC keys so a human source-key correction
    remains visible. No source acquisition, mutable cache or consumer policy.
    """
    return {**raw, **(overrides or {})}
