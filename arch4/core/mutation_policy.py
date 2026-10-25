"""Generic Core lock for non-band mutations. Modem Live owns settle timing and target
confirmation. Core only prevents conflicting device or session changes.
"""

from __future__ import annotations

import time
from typing import Optional

from arch4.coerce import safe_bool
from arch4.core.band_action_policy import is_band_control_action
from arch4.interfaces import LeverAction, LeverType


def uses_generic_mutating_action_lock(action: LeverAction) -> bool:
    """Return whether the non-band device/session exclusion lock is required."""

    if is_band_control_action(action.lever, action.params):
        return False
    return action.lever in {LeverType.UE_CAPABILITY, LeverType.RESTART}


def is_reboot_class_action(action: LeverAction) -> bool:
    params = getattr(action, "params", {}) or {}
    return action.lever == LeverType.RESTART or (
        action.lever == LeverType.UE_CAPABILITY
        and safe_bool(params.get("restart"), False)
    )


def compute_mutating_action_lock(
    *,
    current_until: float,
    lever: LeverType,
    lock_seconds: float,
    now: Optional[float] = None,
) -> tuple[float, str, int]:
    now_ts = float(now if now is not None else time.time())
    new_until = max(
        float(current_until or 0.0),
        now_ts + max(0.0, float(lock_seconds or 0.0)),
    )
    remaining = max(0, int(round(new_until - now_ts)))
    return new_until, lever.name, remaining


def clear_mutating_action_lock_state(
    *,
    current_until: float,
    current_lever: str,
    now: Optional[float] = None,
) -> tuple[bool, float, str]:
    """Return the cleared generic mutation-lock state."""

    now_ts = float(now if now is not None else time.time())
    if float(current_until or 0.0) <= now_ts:
        already_clean = not str(current_lever or "").strip()
        return (not already_clean), 0.0, ""
    return True, 0.0, ""
