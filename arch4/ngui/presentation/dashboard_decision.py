"""Build Dashboard text from a Foundation connectivity transition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def connectivity_transition_conclusion(
    transition: Mapping[str, Any],
) -> str:
    """Format only values present in the Foundation transition."""

    old_state = _text(transition.get("old_state"))
    new_state = _text(transition.get("new_state"))
    reason = _text(transition.get("reason"))
    return reason or (
        f"{old_state} -> {new_state}" if old_state and new_state else "---"
    )


__all__ = ("connectivity_transition_conclusion",)
