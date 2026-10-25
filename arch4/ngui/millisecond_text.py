"""Format operator-visible millisecond values."""

from __future__ import annotations

from math import floor, isfinite
from typing import Any


def whole_milliseconds(value: Any) -> int:
    milliseconds = float(value)
    if not isfinite(milliseconds) or milliseconds < 0.0:
        raise ValueError("operator_milliseconds_invalid")
    return int(floor(milliseconds + 0.5))


def format_milliseconds(value: Any) -> str:
    return f"{whole_milliseconds(value)} ms"


def format_elapsed_milliseconds(
    value: Any,
    *,
    missing_text: str = "---",
) -> str:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return missing_text
    if not isfinite(milliseconds) or milliseconds < 0.0:
        return missing_text
    if milliseconds >= 1000.0:
        return f"{milliseconds / 1000.0:.3f} s"
    return format_milliseconds(milliseconds)


__all__ = (
    "format_elapsed_milliseconds",
    "format_milliseconds",
    "whole_milliseconds",
)
