"""Typed availability contract for facts in the Core current-state registers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CoreCurrentFieldState(str, Enum):
    """Closed truth state for one current Core fact.

    The state is owned at the Core producer boundary.  Customers may format it,
    but may not infer a different state from a numeric value or placeholder.
    """

    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    INVALID = "invalid"
    UNATTACHED = "unattached"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class CoreCurrentField:
    value: Any
    state: CoreCurrentFieldState
    reason: str
    capability: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, CoreCurrentFieldState):
            raise TypeError("core_current_field_state_required")
        reason = str(self.reason or "").strip()
        if self.state is not CoreCurrentFieldState.AVAILABLE and not reason:
            raise ValueError("unavailable_core_current_field_requires_reason")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "capability", str(self.capability or "").strip())

    @property
    def valid(self) -> bool:
        return self.state is CoreCurrentFieldState.AVAILABLE


def core_current_field_state(value: Any) -> CoreCurrentFieldState:
    if isinstance(value, CoreCurrentFieldState):
        return value
    try:
        return CoreCurrentFieldState(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"invalid_core_current_field_state:{value!r}") from exc


__all__ = (
    "CoreCurrentField",
    "CoreCurrentFieldState",
    "core_current_field_state",
)
