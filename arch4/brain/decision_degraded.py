"""Deleted direct DEGRADED selector boundary.

DEGRADED observations are projected by ControllerPostureAuthority.  Candidate
construction belongs to normal_behavior_opportunities; Bandit and Brain select;
ActionTransactionAuthority executes.  This mixin remains only as an explicit
tripwire for any stale caller.
"""

from __future__ import annotations

from typing import Optional

from arch4.controller_frames import CellularIntelligenceGeneration, DerivedSignalFrames
from arch4.interfaces import LeverAction


class DecisionDegradedMixin:
    def _decide_degraded(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_degraded_action_selector_deleted")
