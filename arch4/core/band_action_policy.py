"""Immutable contract for human cooldown and accepted-radio stability hold.

This module owns names and fixed durations only. ``ActionSourceStateAuthority``
is the only mutable deadline owner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

OWNER = "arch4.core.action_source_state_authority"
OPERATOR_BAND_COOLDOWN_S = 30.0
RADIO_STABILITY_HOLD_S = 21.0 * 60.0
EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS = frozenset(
    {"emergency_radio_down_recover"}
)

BAND_CONTROL_LEVERS = frozenset(
    {
        "BAND_SELECTION",
        "BAND_LOCK",
        "PBAND",
        "NR_BAND_LOCK",
        "CELL_LOCK",
        "RADIO_MODE",
    }
)
def normalized_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def normalized_lever(value: Any) -> str:
    raw = getattr(value, "name", value)
    return str(raw or "").strip().upper()


def is_band_control_action(
    lever: Any,
    params: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the action selects/changes a cellular band constraint.

    A timed CELL_LOCK release removes an operator constraint and therefore is
    not a new band choice. Every other CELL_LOCK may select a band and is
    governed by the same policy.
    """

    lever_name = normalized_lever(lever)
    if lever_name not in BAND_CONTROL_LEVERS:
        return False
    if lever_name == "CELL_LOCK" and bool(dict(params or {}).get("unlock")):
        return False
    return True


@dataclass(frozen=True, slots=True)
class BandActionPolicyDecision:
    lever: str
    origin: str
    allowed: bool
    reason: str
    operator_cooldown_active: bool
    operator_cooldown_remaining_s: float
    radio_stability_hold_active: bool
    radio_stability_hold_remaining_s: float
    radio_stability_bypass: bool
    evaluated_at_ts: float

    def as_dict(self) -> dict[str, Any]:
        return {"owner": OWNER, **asdict(self)}


__all__ = [
    "BAND_CONTROL_LEVERS",
    "BandActionPolicyDecision",
    "EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS",
    "OPERATOR_BAND_COOLDOWN_S",
    "RADIO_STABILITY_HOLD_S",
    "is_band_control_action",
    "normalized_lever",
    "normalized_name",
]
