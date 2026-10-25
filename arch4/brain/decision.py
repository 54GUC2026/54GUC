"""
arch4/brain/decision.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Decision Engine

Split architecture:
- decision_policy.py
- decision_pipeline.py
- decision_night_training.py
- decision_states.py
- decision_faults.py
- decision_congestion.py

DecisionEngine remains the single action authority/orchestrator.
═══════════════════════════════════════════════════════════════
"""

import time
import logging
from typing import Optional, List, Dict, Any

from arch4.interfaces import (
    Constraints,
    Arch4Config,
    PolicyProfile,
)
from arch4.brain.memory import Memory
from arch4.brain.states import ControllerPostureAuthority
from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.historical import HistoricalJournal
from arch4.brain.anchor_flip_monitor import AnchorFlipMonitor
from arch4.brain.ca_flap_monitor import CaFlapMonitor
from arch4.brain.cooldown_registry import CooldownRegistry
from arch4.brain.decision_policy import DecisionPolicyMixin
from arch4.brain.decision_pipeline import DecisionPipelineMixin
from arch4.brain.decision_night_training import DecisionNightTrainingMixin
from arch4.brain.decision_support import DecisionSupportMixin
from arch4.brain.decision_degraded import DecisionDegradedMixin
from arch4.brain.decision_exploring import DecisionExploringMixin
from arch4.brain.decision_working import DecisionWorkingMixin
from arch4.brain.decision_faults import DecisionFaultsMixin
from arch4.brain.decision_congestion import DecisionCongestionMixin
from arch4.brain.tick_contract import DecisionContext, DecisionResult, DecisionTrace
from arch4.signal_processing.policy_thresholds import policy_threshold_authority
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.core.action_source_state_authority import ActionSourceStateAuthority
from arch4.config_views import build_decision_pipeline_config, build_decision_state_config
from arch4.config_views import build_decision_policy_config, build_night_training_config

log = logging.getLogger("arch4.brain.decision")


class DecisionEngine(
    DecisionPipelineMixin,
    DecisionPolicyMixin,
    DecisionNightTrainingMixin,
    DecisionSupportMixin,
    DecisionDegradedMixin,
    DecisionExploringMixin,
    DecisionWorkingMixin,
    DecisionFaultsMixin,
    DecisionCongestionMixin,
):
    """
    Core decision logic.

    Called once per tick with fresh signals. Returns an optional LeverAction.
    The runner executes the selected action through the admitted lifecycle.
    """

    def __init__(self, config: Arch4Config, memory: Memory,
                 posture_authority: ControllerPostureAuthority,
                 constraints: Constraints,
                 aggressiveness: AggressivenessEngine = None,
                 historical: HistoricalJournal = None,
                 policy_profile: Optional[PolicyProfile] = None,
                 calibration_authority: Optional[Any] = None,
                 crystal_ball: Optional[Any] = None,
                 learning_authority: Optional[Any] = None,
                 smart_loop_strategy_feed: Optional[BrainFeedRegisterBank] = None,
                 action_transaction_authority: Optional[ActionTransactionAuthority] = None,
                 action_source_state_authority: Optional[ActionSourceStateAuthority] = None):
        self.cfg = config
        self.memory = memory
        self.posture_authority = posture_authority
        self.constraints = constraints
        self.aggressiveness = aggressiveness
        self.crystal_ball = crystal_ball
        self.learning_authority = learning_authority
        self.historical = historical
        self.policy_profile = policy_profile
        self._smart_loop_strategy_feed_port = smart_loop_strategy_feed
        self.action_transaction_authority = action_transaction_authority
        # Core owns this state.  A standalone Intelligence instance may remain
        # explicitly unattached, but it may never manufacture a second owner.
        self.action_source_state_authority = action_source_state_authority
        # Night training → calibration bridge: store reference so the mixin
        # can signal the authority when a better band is discovered at night.
        self._calibration_authority = calibration_authority
        self._thresholds = policy_threshold_authority(config)
        self.decision_state_cfg = build_decision_state_config(config)
        self.decision_pipeline_cfg = build_decision_pipeline_config(config)
        self.decision_policy_cfg = build_decision_policy_config(config)
        self.night_training_cfg = build_night_training_config(config)
        self.deployed_bands = set()
        self._band_rotation_time: float = 0.0
        self._band_rotation_target: int = 0
        self._band_rotation_checked: bool = True
        self._observed_cell_lock_key: tuple[int, int] | None = None
        self._observed_cell_lock_since: float = 0.0

        self._last_action_time: float = 0.0
        self._startup_time: float = 0.0  # set by engine.startup(); 0 = grace expired
        self._probe_interval_s: float = 120.0  # probe neighbors every 2 min
        # Layer 3 action trust map for current firmware profile.
        # Updated each tick by Engine before decide() is called.
        # Maps action_key -> trust level (supported/degraded/unsupported/untested).
        # Empty dict = no overlay data = all actions pass through.
        self._layer3_action_trust: dict = {}
        self._night_no_ca_since: float = 0.0
        # Centralized cooldown/rate-limit registry — replaces scattered
        # self._xxx_cooldown_until / self._xxx_retry_until variables.
        self.cooldowns = CooldownRegistry()
        # Exploration pressure: composite signal driving band probe decisions.
        # Replaces the simple can_background_probe() timer gate with a
        # pressure/resistance model that responds to multiple signals.
        from arch4.brain.exploration_pressure import ExplorationPressure
        self._exploration_pressure = ExplorationPressure()
        self._night_train: dict = {
            "active": False,
            "phase": "idle",          # idle|probing|hold
            "candidates": [],
            "idx": 0,
            "current": None,
            "started_at": 0.0,
            "last_change_at": 0.0,
            "last_hold_check_at": 0.0,
            "scores": {},             # tuple(bands) -> score dict
            "best": None,
        }
        self._runtime_events: List[dict] = []
        # Guardrail against repeated disruptive band retries.
        self._band_fail_streak: int = 0
        self._global_band_action_times: List[float] = []
        self._band_last_target_sig: str = ""
        # Per-combo exploration repeat counter.
        # Key: band signature (e.g. "1+3+20"). After 2 probes of the same
        # combo with no improvement, set a 60-min cooldown on that combo.
        self._explore_probe_counts: Dict[str, int] = {}
        self._explore_repeat_block_s: float = max(
            0.0, float(self.decision_pipeline_cfg.explore_repeat_block_s)
        )
        # Per-combo restore_ca repeat counter (same pattern as explore).
        # After 2 failed restore attempts for the same band combo,
        # block that combo for 60 minutes.
        self._restore_ca_probe_counts: Dict[str, int] = {}
        self._restore_ca_repeat_block_s: float = max(
            0.0, float(self.decision_pipeline_cfg.restore_ca_repeat_block_s)
        )
        self._emergency_actions_window: List[float] = []
        # HiLink/RouterOS rotate quiet windows — kept as direct variables since
        # policy/ files use getattr() to access them. Migrated to registry in
        # a future pass when policy/ modules are refactored.
        self._hilink_rotate_success_quiet_until: float = 0.0
        # MikroTik priority-rotate lifecycle windows.
        self._mikrotik_rotate_success_quiet_until: float = 0.0
        self._routeros_rotate_success_quiet_until: float = 0.0
        self._mikrotik_rotate_last_attempt_at: float = 0.0
        self._mikrotik_rotate_attempt_times: List[float] = []
        # MikroTik explore lifecycle guard (low-priority explore only):
        # - minimum spacing between same-objective attempts
        # - fail-budget escalation to longer cooldown
        self._mikrotik_explore_fail_count: dict[str, int] = {}
        self._mikrotik_cell_lock_fail_count: dict[str, int] = {}
        # CA_LOST confidence tracker (1.0=CA present under qualifying load, 0.0=CA lost).
        self._ca_lost_samples: List[tuple[float, float]] = []
        self._ca_recovery_trend_samples: List[tuple[float, float]] = []
        self._ca_recovery_trend_last: Dict[str, Any] = {}
        self._ca_lost_avg: float = 1.0
        self._ca_lost_state: bool = False
        self._ca_flap_monitor = CaFlapMonitor()
        self._ca_flapping_status: Dict[str, Any] = {}
        self._anchor_flip_monitor = AnchorFlipMonitor()
        self._anchor_flip_status: Dict[str, Any] = {}
        self._soft_congestion_ticks: int = 0
        self._soft_congestion_clear_ticks: int = 0
        self._soft_congestion_active: bool = False
        self._decision_trace_last: dict = {}
        self._decision_trace_last_obj: Optional[DecisionTrace] = None
        self._decision_result_last_obj: Optional[DecisionResult] = None
        self._decision_context_last_obj: Optional[DecisionContext] = None
        self._decision_trace_seq: int = 0
        self._action_trace_ids: dict[int, str] = {}
        self._last_action_frame: Any = None
        self._calibration_handoff: Dict[str, Any] = {}
        self._constraint_vector: Dict[str, Any] = {}
        self._constraint_gate_reason: str = ""
        self._prediction_gate_reason: str = ""
        self._crystal_ball_gate_reason: str = ""
        # Objective feedback lane:
        # remembers repeated no-effect/no-value requests so proposers can be damped.
        self._objective_feedback_reason: dict[str, str] = {}
        self._last_objective_block_reason: str = ""
        self._known_outage_hold_notice: dict[str, dict[str, Any]] = {}
        self._health_status: dict = {}
        self._incident_assessment: dict = {}
        self._last_health_hold_reason: str = ""
        self._last_incident_hold_reason: str = ""
        # Runtime keepalive should only run after Brain has positive idle
        # evidence from the Modem Live traffic lane. A one-tick idle sample is
        # not enough because buffered video traffic can look idle between
        # bursts while RTT EMA remains high.
        self._idle_keepalive_idle_ticks: int = 0
        self._idle_keepalive_last_traffic_ts: float = 0.0
        self._last_gate_contract_failure: dict[str, Any] = {}
        self._idle_keepalive_min_idle_s: float = 120.0
        self._idle_keepalive_min_idle_ticks: int = 5
        # Stability keyframe: snapshot of decision-relevant signals.
        # When consecutive ticks produce the same keyframe, lanes 2-4 are
        # skipped because the decision outcome cannot differ.
        self._stability_keyframe: tuple | None = None
        self._stability_skip_count: int = 0
        self._exploration_suppression_events: List[tuple[float, str, str]] = []
        self._escape_probe_last_ts: float = 0.0
        self._escape_probe_last_by_context: dict[str, float] = {}
        self._escape_probe_pending_by_lane: dict[str, dict[str, Any]] = {}
        self._escape_probe_seq: int = 0
        self._escape_probe_rf_cliff_good_ticks: int = 0

    def _report_gate_contract_failure(self, reason: str) -> None:
        text = str(reason or "typed_gate_unavailable")
        prior = str(self._last_gate_contract_failure.get("reason", "") or "")
        self._last_gate_contract_failure = {
            "reason": text,
            "reported": True,
            "safe_behavior": "hold_gate_dependent_decision",
            "ts": float(time.time()),
        }
        if prior != text:
            log.error("Decision typed-gate contract failure: %s", text)

    def note_calibration_handoff(self, handoff: Optional[Dict[str, Any]]) -> None:
        self._calibration_handoff = dict(handoff) if isinstance(handoff, dict) else {}

    def arm_startup_grace(self, *, now: Optional[float] = None) -> None:
        self._startup_time = float(time.time() if now is None else now)

    def clear_startup_grace(self) -> None:
        self._startup_time = 0.0

    def note_constraint_vector(self, constraint_vector: Optional[Dict[str, Any]]) -> None:
        self._constraint_vector = dict(constraint_vector) if isinstance(constraint_vector, dict) else {}

    def set_tick_learning_strategy_input(self, strategy: Optional[Dict[str, Any]]) -> None:
        raise RuntimeError("old_tick_learning_strategy_input_path_deleted")

    def _get_hilink_rotate_success_quiet_until(self) -> float:
        return float(getattr(self, "_hilink_rotate_success_quiet_until", 0.0) or 0.0)

    def _set_hilink_rotate_success_quiet_until(self, until: float) -> None:
        ts = float(until or 0.0)
        self._hilink_rotate_success_quiet_until = ts

    def _get_hilink_radio_drop_state(self) -> dict:
        raise RuntimeError("old_provider_radio_drop_state_owner_deleted")

    def _hilink_radio_drop_hold_until(self) -> float:
        raise RuntimeError("old_provider_radio_drop_state_owner_deleted")

    def _get_routeros_rotate_success_quiet_until(self) -> float:
        return max(
            float(getattr(self, "_routeros_rotate_success_quiet_until", 0.0) or 0.0),
            float(getattr(self, "_mikrotik_rotate_success_quiet_until", 0.0) or 0.0),
        )

    def _set_routeros_rotate_success_quiet_until(self, until: float) -> None:
        ts = float(until or 0.0)
        self._routeros_rotate_success_quiet_until = ts
        self._mikrotik_rotate_success_quiet_until = ts

    def refresh_thresholds(self) -> None:
        """Reload cached threshold authority map after active calibration profile switch."""
        self._thresholds = policy_threshold_authority(self.cfg)
        self.decision_state_cfg = build_decision_state_config(self.cfg)
        self.decision_pipeline_cfg = build_decision_pipeline_config(self.cfg)
        self.decision_policy_cfg = build_decision_policy_config(self.cfg)
        self.night_training_cfg = build_night_training_config(self.cfg)
