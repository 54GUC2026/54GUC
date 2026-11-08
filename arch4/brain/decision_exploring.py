"""Deleted direct exploration and service-action selector boundaries.

All retained candidates are source facts consumed by ActionOpportunitySet.
These methods remain only as hard tripwires for stale dynamic callers.
"""

from __future__ import annotations

from typing import Any, Optional

from arch4.controller_frames import CellularIntelligenceGeneration, DerivedSignalFrames
from arch4.interfaces import LeverAction


class DecisionExploringMixin:
    @staticmethod
    def _deleted() -> None:
        raise RuntimeError("old_exploration_action_path_deleted")

    def _decide_exploring(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        self._deleted()

    def _try_band_exploration(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _try_cell_exploration(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _try_band_priority(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _check_anchor_enforcement(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _check_cell_lock_health(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _try_keepalive_adjustment(self, *args: Any, **kwargs: Any) -> Optional[LeverAction]:
        self._deleted()

    def _idle_keepalive_evidence_ready(self, *args: Any, **kwargs: Any) -> bool:
        self._deleted()

    def _band_learning_memory_summary(self) -> dict[str, Any]:
        self._deleted()

    def _history_exploration_policy(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._deleted()

    def _exploration_lane_posture(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._deleted()

    def _tsar_should_defer_exploration_lane(self, *args: Any, **kwargs: Any) -> bool:
        self._deleted()

    def _tsar_should_defer_band_probe(self, *args: Any, **kwargs: Any) -> bool:
        self._deleted()

    def _emit_probe_untested_settled_skip_event_once(self, *args: Any, **kwargs: Any) -> None:
        self._deleted()
