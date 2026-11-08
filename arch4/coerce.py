"""
arch4/coerce.py
═══════════════════════════════════════════════════════════════
Shared safe coercion helpers for defensive data extraction.

Used by status_payload, runtime projection, and any module that needs to safely
extract numeric/string values from potentially malformed modem or backend data.

Single source of truth — do not duplicate these helpers elsewhere.
═══════════════════════════════════════════════════════════════
"""

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce value to float, handling lists and malformed data."""
    try:
        if isinstance(value, list):
            value = value[0] if value else default
        return float(value or 0.0)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Safely coerce value to int, handling lists, hex strings, and floats."""
    try:
        if isinstance(value, list):
            value = value[0] if value else default
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return default
            if text.lower().startswith(("0x", "-0x")):
                return int(text, 0)
            return int(float(text))
        return int(value or 0)
    except Exception:
        return default


def safe_getattr(obj: Any, name: str, default: Any) -> Any:
    """Safely get attribute from object, returning default on any error."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def safe_throughput_mbps(modem: Any, attr: str) -> float:
    """Extract throughput in Mbps from modem object attribute."""
    value = safe_float(safe_getattr(modem, attr, 0.0), 0.0)
    if value > 0.0:
        return value
    if str(attr or "").endswith("dl_mbps"):
        rx_bps = safe_float(safe_getattr(modem, "rx_bytes_per_sec", 0.0), 0.0)
        return max(0.0, (rx_bps * 8.0) / 1_000_000.0)
    if str(attr or "").endswith("ul_mbps"):
        tx_bps = safe_float(safe_getattr(modem, "tx_bytes_per_sec", 0.0), 0.0)
        return max(0.0, (tx_bps * 8.0) / 1_000_000.0)
    return 0.0


def safe_str(value: Any, default: str = "") -> str:
    """Safely coerce value to string."""
    try:
        if value is None:
            return default
        return str(value)
    except Exception:
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    """Strictly coerce value to bool.

    Runtime contracts often deserialize external state from JSON/PowerShell.
    Raw bool("false") is True, so string values must be parsed explicitly.
    """
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
            return bool(default)
        if value in (True, 1):
            return True
        if value in (False, 0):
            return False
        return bool(value)
    except Exception:
        return default


# ═══════════════════════════════════════════════════════════════════════════════
# Congestion helpers
# ═══════════════════════════════════════════════════════════════════════════════
# These provide canonical congestion classification without importing
# from brain modules.

CONGESTION_MILD_ENTER_PCT = 60.0
CONGESTION_MILD_CLEAR_PCT = 50.0
CONGESTION_HIGH_ENTER_PCT = 80.0
CONGESTION_HIGH_CLEAR_PCT = 70.0

def gui_congestion_valid(td: Any) -> bool:
    """Return True when fast congestion is currently trustworthy."""
    try:
        if isinstance(td, (int, float)):
            return True
        return bool(getattr(td, "congestion_pct_valid", False))
    except Exception:
        return False


def gui_congestion_pct(td: Any) -> float:
    """Extract congestion_pct from an object or number, clamped 0-100."""
    try:
        if isinstance(td, (int, float)):
            value = float(td)
        else:
            if not gui_congestion_valid(td):
                return 0.0
            value = float(getattr(td, "congestion_pct", 0.0) or 0.0)
    except Exception:
        value = 0.0
    return max(0.0, min(100.0, value))


def congestion_bucket_for_pct(
    pct_or_obj: Any,
    *,
    previous_bucket: str = "normal",
    mild_enter_pct: float = CONGESTION_MILD_ENTER_PCT,
    mild_clear_pct: float = CONGESTION_MILD_CLEAR_PCT,
    high_enter_pct: float = CONGESTION_HIGH_ENTER_PCT,
    high_clear_pct: float = CONGESTION_HIGH_CLEAR_PCT,
) -> str:
    """Canonical congestion bucket authority with hysteresis."""
    pct = gui_congestion_pct(pct_or_obj)
    prev = str(previous_bucket or "normal").strip().lower()
    if prev not in {"normal", "mild", "high"}:
        prev = "normal"

    if prev == "high":
        if pct >= float(high_clear_pct):
            return "high"
        if pct >= float(mild_clear_pct):
            return "mild"
        return "normal"

    if prev == "mild":
        if pct >= float(high_enter_pct):
            return "high"
        if pct >= float(mild_clear_pct):
            return "mild"
        return "normal"

    if pct >= float(high_enter_pct):
        return "high"
    if pct >= float(mild_enter_pct):
        return "mild"
    return "normal"


def gui_congestion_bucket(
    td: Any,
    *,
    previous_bucket: str = "normal",
    mild_enter_pct: float = CONGESTION_MILD_ENTER_PCT,
    mild_clear_pct: float = CONGESTION_MILD_CLEAR_PCT,
    high_enter_pct: float = CONGESTION_HIGH_ENTER_PCT,
    high_clear_pct: float = CONGESTION_HIGH_CLEAR_PCT,
) -> str:
    if not gui_congestion_valid(td):
        return ""
    explicit = ""
    try:
        explicit = str(getattr(td, "congestion_bucket", "") or "").strip().lower()
    except Exception:
        explicit = ""
    if explicit in {"normal", "mild", "high"}:
        return explicit
    return congestion_bucket_for_pct(
        td,
        previous_bucket=previous_bucket,
        mild_enter_pct=mild_enter_pct,
        mild_clear_pct=mild_clear_pct,
        high_enter_pct=high_enter_pct,
        high_clear_pct=high_clear_pct,
    )


def gui_operator_congestion_bucket(
    td: Any,
    *,
    previous_bucket: str = "normal",
    mild_enter_pct: float = CONGESTION_MILD_ENTER_PCT,
    mild_clear_pct: float = CONGESTION_MILD_CLEAR_PCT,
    high_enter_pct: float = CONGESTION_HIGH_ENTER_PCT,
    high_clear_pct: float = CONGESTION_HIGH_CLEAR_PCT,
) -> str:
    """Preferred operator-facing congestion bucket.

    Uses the slower confirmed lane when available, then falls back to the
    exported fast/stable bucket, then finally to local hysteresis over pct.
    """
    if not gui_congestion_valid(td):
        return ""
    confirmed = ""
    try:
        confirmed = str(getattr(td, "congestion_state_bucket", "") or "").strip().lower()
    except Exception:
        confirmed = ""
    if confirmed in {"normal", "mild", "high"}:
        return confirmed
    return gui_congestion_bucket(
        td,
        previous_bucket=previous_bucket,
        mild_enter_pct=mild_enter_pct,
        mild_clear_pct=mild_clear_pct,
        high_enter_pct=high_enter_pct,
        high_clear_pct=high_clear_pct,
    )


def gui_congestion_flags(
    td: Any,
    *,
    previous_bucket: str = "normal",
) -> tuple[bool, bool, bool]:
    """
    Operator-safe congestion classification from computed owner fields.

    Uses only congestion_pct (already computed by brain and written by owner).
    Does NOT re-derive from RTT/RSRQ — that's the brain's job.

    Returns (bad_now, high_pressure, moderate_pressure).
    """
    try:
        if bool(getattr(td, "congestion_flags_valid", False)):
            moderate = bool(getattr(td, "congestion_pressure_moderate", False))
            high = bool(getattr(td, "congestion_pressure_high", False))
            bad_now = bool(getattr(td, "congestion_bad_now", moderate))
            return bad_now, high, moderate
    except Exception:
        pass
    bucket = gui_operator_congestion_bucket(td, previous_bucket=previous_bucket)
    high = bucket == "high"
    moderate = bucket in {"mild", "high"}
    return moderate, high, moderate
