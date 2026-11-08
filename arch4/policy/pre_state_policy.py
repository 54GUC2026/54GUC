"""
Pre-state provider/backend policy helpers.

These helpers run ahead of generic state dispatch so provider-specific guarded
windows and sequences do not stay embedded directly in the core
decision pipeline implementation.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction, SystemState


def run_pre_state_policy(
    engine: Any,
    *,
    modem: CellularIntelligenceGeneration,
    state: SystemState,
    now: float,
) -> tuple[bool, Optional[LeverAction]]:
    del engine, modem, state, now
    raise RuntimeError("old_provider_pre_state_action_path_deleted")
