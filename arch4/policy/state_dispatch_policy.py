"""
State dispatch policy helpers.

Keeps state-to-action routing and provider-specific dispatch overrides outside
the central decision pipeline file.

── Seed 2: Dependency Boundary Contract ─────────────────────────────────────

This module routes incoming modem + computed state to the correct per-state
decision logic. It is a ROUTING TABLE, not a decision engine.

What it accepts:
  - engine (as opaque Any) — accessed only for decision mixin methods
  - modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames, state: SystemState

What it returns:
  - A LeverAction (or None) from the per-state decision logic

What it must NOT do:
  - Import or depend on modem hardware backends (MikroTik, Huawei HiLink, etc.)
  - Call modem APIs
  - Read or write runtime files
  - Own cooldowns or rate-limit state (those belong in decision mixin)

The routing logic here should stay thin. If it grows decision logic,
that logic belongs in DecisionPipelineMixin or DecisionStateMixin instead.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import (
    LeverAction,
SystemState,
    is_fault_state,
)


def run_state_dispatch(
    engine: Any,
    *,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    state: SystemState,
    now: float,
) -> Optional[LeverAction]:
    if not is_fault_state(state):
        raise RuntimeError("old_state_dispatch_autonomous_action_path_deleted")
    action: Optional[LeverAction] = None
    if state == SystemState.BAD_RF:
        decide_bad_rf = getattr(engine, "_decide_bad_rf", None)
        if decide_bad_rf is None:
            decide_bad_rf = engine._decide_e_rf_collapse
        action = decide_bad_rf(modem, computed)
    elif state == SystemState.E_RADIO_DOWN:
        action = engine._decide_e_radio_down(modem, computed)
    elif state == SystemState.E_SESSION_RESET:
        action = engine._decide_e_session_reset(modem, computed)
    elif state == SystemState.E_CONGESTION_STORM:
        action = engine._decide_e_congestion_storm(modem, computed)

    return action
