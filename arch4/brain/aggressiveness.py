"""
arch4/brain/aggressiveness.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Aggressiveness Profiles

Per-state aggressiveness profiles define how hard the brain
pushes (foreground budget) and how much it explores
(background budget) in each state.

Orthogonal to state: every state has a character.
  OPTIMIZING  — hold what you have, probe steadily in background
  DEGRADED    — push hard to recover, keep exploring
  E_* faults  — all-out recovery, minimal exploration
  Action settlement is enforced by the admitted action lifecycle.
═══════════════════════════════════════════════════════════════
"""

import time
import logging
from typing import Optional, List, Any

from arch4.interfaces import (
    SystemState, Arch4Config, AggressivenessProfile,
)

log = logging.getLogger("arch4.brain.aggressiveness")

# ── Per-state aggressiveness profiles ──────────────────────

STATE_PROFILES = {
    SystemState.WORKING: AggressivenessProfile(
        foreground_budget=0.6,
        background_budget=0.5,
        probe_interval_s=180.0,
        max_actions_per_window=4,
        window_s=600.0,
    ),
    SystemState.DEGRADED: AggressivenessProfile(
        foreground_budget=0.9,
        background_budget=0.4,
        probe_interval_s=45.0,
        max_actions_per_window=6,
        window_s=600.0,
    ),
    SystemState.CONGESTED: AggressivenessProfile(
        foreground_budget=0.4,
        background_budget=0.1,
        probe_interval_s=180.0,
        max_actions_per_window=1,
        window_s=600.0,
    ),
    SystemState.E_RADIO_DOWN: AggressivenessProfile(
        foreground_budget=1.0,
        background_budget=0.1,
        probe_interval_s=30.0,
        max_actions_per_window=8,
        window_s=300.0,
    ),
    SystemState.BAD_RF: AggressivenessProfile(
        foreground_budget=0.2,
        background_budget=0.0,
        probe_interval_s=900.0,
        max_actions_per_window=1,
        window_s=1800.0,
    ),
    SystemState.E_SESSION_RESET: AggressivenessProfile(
        foreground_budget=1.0,
        background_budget=0.1,
        probe_interval_s=30.0,
        max_actions_per_window=8,
        window_s=300.0,
    ),
    SystemState.E_CONGESTION_STORM: AggressivenessProfile(
        foreground_budget=1.0,
        background_budget=0.1,
        probe_interval_s=30.0,
        max_actions_per_window=8,
        window_s=300.0,
    ),
}


class AggressivenessEngine:
    """
    Provides aggressiveness profiles and rate-limits background probes.
    """

    def __init__(self, config: Arch4Config):
        self.cfg = config
        self._bg_action_timestamps: List[float] = []
        self._last_bg_probe_time: float = 0.0

    def refresh_thresholds(self) -> None:
        """ACK the immutable generation; rate-limit state remains intact."""

    def get_profile(self, state: SystemState) -> AggressivenessProfile:
        """Get the aggressiveness profile for the current state."""
        return STATE_PROFILES.get(state, STATE_PROFILES[SystemState.WORKING])

    def get_profile_with_posture(
        self,
        state: SystemState,
        posture: Optional[Any] = None,
    ) -> AggressivenessProfile:
        """
        Get aggressiveness profile for current state, modulated by BrainPosture.

        The posture_modifier scales probe_interval_s inversely:
          modifier > 1.0 → shorter interval → more exploratory
          modifier < 1.0 → longer interval → more conservative

        Emergency and fault states are never modulated (modifier ignored).
        Without posture, returns the same result as get_profile().
        """
        base = STATE_PROFILES.get(state, STATE_PROFILES[SystemState.WORKING])

        # Never modulate emergency/fault states — safety behavior is invariant
        _no_modulate = {
            SystemState.E_RADIO_DOWN,
            SystemState.E_SESSION_RESET,
            SystemState.E_CONGESTION_STORM,
            SystemState.BAD_RF,
            SystemState.CONGESTED,
        }
        if state in _no_modulate or posture is None:
            return base

        modifier = float(getattr(posture, "posture_modifier", 1.0))
        if modifier == 1.0:
            return base

        # Scale probe_interval_s by 1/modifier (higher modifier = shorter interval)
        # Clamp: never below 15s, never above 10× the base interval
        raw_interval = base.probe_interval_s / modifier
        new_interval = max(15.0, min(base.probe_interval_s * 10.0, raw_interval))

        return AggressivenessProfile(
            foreground_budget=base.foreground_budget,
            background_budget=base.background_budget,
            probe_interval_s=new_interval,
            max_actions_per_window=base.max_actions_per_window,
            window_s=base.window_s,
        )

    def can_background_probe(self, state: SystemState, now: float = None) -> bool:
        """Check if a background probe is allowed given rate limits."""
        if now is None:
            now = time.time()

        posture = getattr(self, "_current_posture", None)
        profile = self.get_profile_with_posture(state, posture)
        if profile.background_budget <= 0:
            return False

        if now - self._last_bg_probe_time < profile.probe_interval_s:
            return False

        cutoff = now - profile.window_s
        self._bg_action_timestamps = [
            t for t in self._bg_action_timestamps if t > cutoff
        ]
        if len(self._bg_action_timestamps) >= profile.max_actions_per_window:
            return False

        return True

    def record_bg_probe(self, now: float = None) -> None:
        """Record that a background probe was initiated."""
        if now is None:
            now = time.time()
        self._bg_action_timestamps.append(now)
        self._last_bg_probe_time = now
