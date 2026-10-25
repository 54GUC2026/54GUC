"""
arch4/policy/family_radio_drop_policy.py

Vendor-neutral family/runtime helpers for radio-drop recovery behavior.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from arch4.interfaces import LeverAction
from arch4.policy.family_hilink_policy import decide_hilink_radio_drop_recovery


def decide_family_radio_drop_recovery(engine: Any, modem: Any, now: float) -> Tuple[LeverAction | None, bool]:
    """Dispatch family-level radio-drop recovery while preserving legacy behavior."""
    backend = str(getattr(getattr(engine, 'cfg', None), 'modem_backend', '') or '').lower()
    family = str(getattr(engine, '_is_huawei_hilink_profile', lambda: False)() and 'hilink' or '').lower()
    if family in {'hilink', 'huawei'} or backend in {'b818', 'b535', 'huawei'}:
        return decide_hilink_radio_drop_recovery(engine, modem, now)
    return None, False


def runtime_radio_drop_alias_fields(engine: Any, now: float) -> Dict[str, Any]:
    """Expose compatibility-safe radio-drop status aliases for runtime/report surfaces."""
    state = getattr(engine, '_get_hilink_radio_drop_state', lambda: getattr(engine, '_a1_big_dip', {}) or {})() or {}
    hold_until = float(getattr(engine, '_hilink_radio_drop_hold_until', lambda: float((state or {}).get('hold_until', 0.0) or 0.0))() or 0.0)
    active = bool((state or {}).get('active', False))
    hold_active = bool(hold_until > float(now or 0.0))
    remaining_s = max(0, int(round(hold_until - float(now or 0.0))))
    return {
        'hilink_radio_drop_active': active,
        'hilink_radio_drop_hold_active': hold_active,
        'hilink_radio_drop_remaining_s': remaining_s,
        'huawei_radio_drop_active': active,
        'huawei_radio_drop_hold_active': hold_active,
        'huawei_radio_drop_remaining_s': remaining_s,
        'family_radio_drop_active': active,
        'family_radio_drop_hold_active': hold_active,
        'family_radio_drop_remaining_s': remaining_s,
    }
