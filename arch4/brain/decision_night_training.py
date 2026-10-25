"""Deleted direct systematic BAD_RF probe selector boundary."""

from __future__ import annotations

from typing import Any, Optional

from arch4.controller_frames import CellularIntelligenceGeneration, DerivedSignalFrames
from arch4.interfaces import LeverAction


class DecisionNightTrainingMixin:
    def _decide_systematic_bad_rf_probe(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_systematic_bad_rf_action_selector_deleted")

    def _reset_systematic_probe(self) -> None:
        raise RuntimeError("old_systematic_bad_rf_state_owner_deleted")

    def _night_train_candidate_sets(self, *args: Any, **kwargs: Any) -> list[list[int]]:
        raise RuntimeError("old_systematic_bad_rf_state_owner_deleted")

    def _night_train_record_observation(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("old_systematic_bad_rf_state_owner_deleted")

    def _night_train_finalize_candidate(self) -> None:
        raise RuntimeError("old_systematic_bad_rf_state_owner_deleted")

    def _night_train_pick_best(self, *args: Any, **kwargs: Any) -> Optional[list[int]]:
        raise RuntimeError("old_systematic_bad_rf_state_owner_deleted")
