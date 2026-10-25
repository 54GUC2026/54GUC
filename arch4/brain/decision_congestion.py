"""Deleted direct CONGESTED selector boundary.

CONGESTED observations are projected by ControllerPostureAuthority.  Candidate
construction belongs to normal_behavior_opportunities; Bandit and Brain select;
ActionTransactionAuthority executes.  This mixin remains only as an explicit
tripwire for any stale caller.
"""

from __future__ import annotations

from typing import Optional

from arch4.controller_frames import CellularIntelligenceGeneration, DerivedSignalFrames
from arch4.interfaces import LeverAction


class DecisionCongestionMixin:
    def _decide_congested(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_congested_action_selector_deleted")
