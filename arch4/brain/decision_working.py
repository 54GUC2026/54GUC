"""Deleted direct WORKING/OPTIMIZING selector boundaries."""

from __future__ import annotations

from typing import Optional

from arch4.controller_frames import CellularIntelligenceGeneration, DerivedSignalFrames
from arch4.interfaces import LeverAction


class DecisionWorkingMixin:
    def _set_prediction_gate_reason(self, reason: str) -> None:
        self._prediction_gate_reason = str(reason or "")

    def _decide_working(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_working_action_selector_deleted")

    def _decide_optimizing(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_optimizing_action_selector_deleted")

    def _try_preemptive_congestion_avoidance(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_preemptive_congestion_selector_deleted")
