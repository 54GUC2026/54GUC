"""Retired provider sequence aliases.

All re-exported entrypoints fail loudly. The retained implementations are
non-public behavior oracles in ``a1_b818_sequences``.
"""

from __future__ import annotations

from arch4.policy.a1_b818_sequences import (
    a1_morning_routine_guard_status,
    decide_a1_b818_morning_sequence,
    decide_a1_overnight_recovery,
    decide_a1_provider_morning_sequence,
    decide_a1_provider_overnight_recovery,
    note_a1_overnight_action,
)

__all__ = [
    "a1_morning_routine_guard_status",
    "decide_a1_b818_morning_sequence",
    "decide_a1_overnight_recovery",
    "decide_a1_provider_morning_sequence",
    "decide_a1_provider_overnight_recovery",
    "note_a1_overnight_action",
]
