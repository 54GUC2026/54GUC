"""
arch4/brain/learning_authority.py
=================================
Unified learning authority (advisory-only).

Purpose:
- synchronize memory/historical/bouncer learning lanes
- keep one canonical context key for 24/7 learning
- ingest pre-action reasoning and post-action outcomes

Non-goals:
- no modem actuation
- no action authority override
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import threading
from typing import Any, Dict, Optional

from arch4.interfaces import (
    Arch4Config,
    LeverAction,
    LeverResult,
PolicyProfile,
    SystemState,
)
from arch4.brain.memory import Memory
from arch4.brain.historical import HistoricalJournal
from arch4.brain.referee import RefereeBrain, RefereeVerdict
from arch4.brain.learning_authority_context_owner import LearningAuthorityContextOwner
from arch4.brain.learning_authority_event_owner import LearningAuthorityEventOwner
from arch4.brain.tick_contract import DecisionTrace

import logging
log = logging.getLogger("arch4.brain.learning_authority")
from arch4.config_views import build_learning_authority_config


class LearningAuthority:
    """
    Single learning authority that synchronizes:
      - Memory (action/band/cell learning)
      - Historical journal (hourly priors/outcomes)
      - Bouncer verdicts (intent/outcome grading)

    Advisory-only: emits status/events, never proposes modem actions.
    """

    def __init__(
        self,
        cfg: Arch4Config,
        memory: Memory,
        historical: HistoricalJournal,
        referee: Optional[RefereeBrain] = None,
        memory_dir: str = "",
    ) -> None:
        self.cfg = cfg
        self.learning_cfg = build_learning_authority_config(cfg)
        self.memory = memory
        self.historical = historical
        self.referee = referee if referee is not None else RefereeBrain(cfg)

        self._status_lock = threading.RLock()

        self._context_owner = LearningAuthorityContextOwner()
        self._event_owner = LearningAuthorityEventOwner()
        # The pre-proof binary Referee journal is intentionally not loaded.
        # Delayed terminal proof is the only strategic reward input; legacy
        # files remain on disk for offline forensic compatibility only.
        self._legacy_quality_journal_policy = {
            "state": "quarantined",
            "reason": "terminal_proof_is_strategic_reward_owner",
            "load_enabled": False,
            "write_enabled": False,
            "strategic_influence": False,
        }

    def set_provider_context(self, provider_plmn: str, provider_name: str = "") -> None:
        self._context_owner.set_provider_context(provider_plmn, provider_name)

    def refresh_thresholds(self) -> None:
        """Refresh delegated threshold caches after calibration profile switch."""
        self.learning_cfg = build_learning_authority_config(self.cfg)

    def observe_tick(
        self,
        *,
        modem: Optional[CellularIntelligenceGeneration],
        computed: Optional[DerivedSignalFrames],
        state: Optional[SystemState],
        policy_profile: Optional[PolicyProfile],
        calibration_handoff: Optional[Dict[str, Any]] = None,
        learning_gate: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._context_owner.observe_tick(
            modem=modem,
            computed=computed,
            state=state,
            policy_profile=policy_profile,
            calibration_handoff=calibration_handoff,
            learning_gate=learning_gate,
            memory=self.memory,
            learning_cfg=self.learning_cfg,
        )

    def ingest_decision_trace(
        self,
        trace: Optional[Dict[str, Any] | DecisionTrace],
        *,
        decision_trace_id: str = "",
        action: Optional[LeverAction] = None,
        incident_assessment: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._event_owner.ingest_decision_trace(
            trace,
            decision_trace_id=decision_trace_id,
            action=action,
            incident_assessment=incident_assessment,
        )

    def evaluate_outcome(
        self,
        *,
        action: Optional[LeverAction],
        action_result: Optional[LeverResult],
        action_suppressed: bool,
        modem_before: Optional[CellularIntelligenceGeneration],
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_before: Optional[DerivedSignalFrames],
        computed_after: Optional[DerivedSignalFrames],
        decision_trace_id: str = "",
        incident_assessment: Optional[Dict[str, Any]] = None,
        bouncer_outcome: Optional[Dict[str, Any]] = None,
        decision_trace: Optional[Dict[str, Any]] = None,
        brain_vectors: Optional[Dict[str, Any]] = None,
    ) -> RefereeVerdict:
        verdict = self.referee.evaluate_outcome(
            action=action,
            action_result=action_result,
            action_suppressed=action_suppressed,
            modem_before=modem_before,
            modem_after=modem_after,
            computed_before=computed_before,
            computed_after=computed_after,
            bouncer_outcome=bouncer_outcome,
            decision_trace=decision_trace,
            brain_vectors=brain_vectors,
        )

        tid = str(decision_trace_id or "")
        self._event_owner.observe_outcome(
            verdict=verdict,
            action=action,
            action_suppressed=action_suppressed,
            modem_before=modem_before,
            modem_after=modem_after,
            decision_trace_id=tid,
            incident_assessment=incident_assessment,
            decision_trace=decision_trace,
        )

        return verdict

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._status_lock:
            return {
                **self._context_owner.runtime_status(),
                **self._event_owner.runtime_status(),
                "referee_journal": dict(self._legacy_quality_journal_policy),
            }

    def drain_runtime_events(self) -> list[dict]:
        out = self._context_owner.drain_runtime_events()
        out.extend(self._event_owner.drain_runtime_events())
        return out
