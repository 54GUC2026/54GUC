"""
arch4/brain/states.py
Controller posture authority with congestion confirmation lifecycle.
"""

from dataclasses import dataclass, field

from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)
from arch4.action_expected_effects import (
    FAULT_RADIO_DOWN,
    FAULT_SESSION_RESET,
)
from arch4.controller_posture import (
    BASIS_ACCEPTED_OBSERVATIONS,
    BASIS_ELAPSED_TIME,
    CONGESTION_AVAILABLE,
    CONGESTION_UNAVAILABLE,
    ControllerPosture,
    PostureRuleProgress,
    SESSION_CONTINUED,
    SESSION_NEW_RESET,
    build_controller_posture,
    build_startup_posture,
)

import time
import logging
from typing import Any, Optional

from arch4.interfaces import (
    SystemState,
    Arch4Config,
    emergency_state_class,
    fault_state_class,
    is_emergency_state,
    is_fault_state,
    is_hard_fault_state,
)
from arch4.config_views import build_controller_posture_config
from arch4.brain.congestion_signals import authority_congestion_flags
from arch4.brain.outcome_profile import is_bad_rf_capacity_winner
from arch4.brain.ca_contract import ca_ld_stable
from arch4.brain.rf_truth import (
    BAD_RF_COLLAPSE_RSRP_DBM,
    BAD_RF_COLLAPSE_RSRQ_DB,
    assess_rf_truth,
)
from arch4.brain.memory import Memory
from arch4.signal_processing.policy_thresholds import policy_threshold_authority
from arch4.runtime_registers import REGISTER_RADIO

log = logging.getLogger("arch4.brain.states")

FAULT_REENTRY_BYPASS_CLASSES = {"E_RADIO_DOWN", "BAD_RF"}
FAULT_STATE_BY_CLASS = {
    "E_RADIO_DOWN": SystemState.E_RADIO_DOWN,
    "BAD_RF": SystemState.BAD_RF,
    "E_RF_COLLAPSE": SystemState.BAD_RF,
    "E_SESSION_RESET": SystemState.E_SESSION_RESET,
    "E_CONGESTION_STORM": SystemState.E_CONGESTION_STORM,
}
# Post-fault dampening is only a settle window. It must not freeze the Brain
# for tens of minutes after live RF has recovered.
POST_FAULT_EXPLORATION_MIN_DAMP_S = 90.0
POST_FAULT_EXPLORATION_MAX_DAMP_S = 5 * 60.0


@dataclass
class _ControllerPostureState:
    """Private mutable state owned only by ControllerPostureAuthority."""

    state: SystemState = SystemState.WORKING
    entered_at: float = field(default_factory=time.time)
    degraded_actions_tried: int = 0
    degraded_timeout_at: float = 0.0
    congested_last_check: float = 0.0
    congestion_kind: str = ""
    congested_action_taken: bool = False
    emergency_timeout_at: float = 0.0
    emergency_class: str = ""
    emergency_origin_class: str = ""
    emergency_reason: str = ""
    soft_emergency_hold_until: float = 0.0
    emergency_observe_until: float = 0.0
    emergency_reentry_guard_until: float = 0.0
    emergency_recovery_since: float = 0.0
    emergency_recovery_step: int = 0
    emergency_next_action_at: float = 0.0
    bad_rf_recovery_since: float = 0.0
    bad_rf_recovery_step: int = 0
    bad_rf_next_action_at: float = 0.0
    rf_cliff_event_at: float = 0.0
    rf_cliff_event_reason: str = ""
    post_fault_class: str = ""
    post_fault_until: float = 0.0
    recovered_lane_bands: list[int] = field(default_factory=list)
    recovered_lane_ts: float = 0.0
    recovered_lane_from_fault: str = ""
    recovered_lane_until: float = 0.0
    warning_class: str = ""
    warning_source: str = ""
    warning_mechanism: str = ""
    warning_reason: str = ""
    normal_progress_receipt_ids: list[str] = field(default_factory=list)

    @property
    def time_in_state_s(self) -> float:
        return max(0.0, time.time() - float(self.entered_at))


class ControllerPostureAuthority:
    """
    Sole stateful owner of controller transition and hysteresis truth.

    Canonical states:
      WORKING
      DEGRADED
      CONGESTED
      E_RADIO_DOWN
      BAD_RF
      E_SESSION_RESET
      E_CONGESTION_STORM
    """

    def __init__(self, config: Arch4Config, memory: Memory):
        self.cfg = config
        self.state_cfg = build_controller_posture_config(config)
        self._thresholds = policy_threshold_authority(config)
        self.memory = memory
        now = time.time()
        self._ctx = _ControllerPostureState(entered_at=now)
        self._current_posture = build_startup_posture(now=now)
        self._last_generation_key: tuple[str, str, int] | None = None
        self._last_identity_key = ""

        self._degraded_ticks: int = 0
        self._degraded_recovery_ticks: int = 0
        self._degraded_idle_ticks: int = 0
        self._confirmed_clear_ticks: int = 0

        self._radio_down_since: float = 0.0
        self._lte_disabled_since: float = 0.0
        self._sinr_low_since: float = 0.0
        self._congestion_storm_since: float = 0.0

        self._degraded_reentry_guard_until: float = 0.0

        self._decision_input: Any = None

    def refresh_thresholds(self) -> None:
        """Reload cached thresholds after active calibration profile switch."""
        self.state_cfg = build_controller_posture_config(self.cfg)
        self._thresholds = policy_threshold_authority(self.cfg)

    def note_decision_input(self, decision_input: Any) -> None:
        """Attach the register snapshot used for this tick's state decisions."""
        self._decision_input = decision_input

    @property
    def state(self) -> SystemState:
        return self._current_posture.state

    @property
    def current_posture(self) -> ControllerPosture:
        return self._current_posture

    def evaluate(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> ControllerPosture:
        """Publish exactly one immutable posture for one accepted generation."""

        if not isinstance(modem, CellularIntelligenceGeneration):
            raise TypeError(
                "controller_posture_requires_cellular_intelligence_generation"
            )
        if not isinstance(computed, DerivedSignalFrames):
            raise TypeError("controller_posture_requires_derived_signal_frames")
        generation = modem.generation
        generation_key = (
            str(generation.source_epoch or ""),
            str(generation.session_scope_key or ""),
            int(generation.generation),
        )
        if (
            generation_key[2] <= 0
            or not generation_key[0]
            or not generation_key[1]
        ):
            raise ValueError("controller_posture_generation_scope_missing")
        if self._last_generation_key == generation_key:
            raise RuntimeError("controller_posture_duplicate_generation")
        if self._last_generation_key is not None:
            last_epoch, last_session, last_generation = self._last_generation_key
            if (
                generation_key[0] == last_epoch
                and generation_key[1] == last_session
                and generation_key[2] <= last_generation
            ):
                raise RuntimeError("controller_posture_stale_generation")

        identity_key = str(modem.identity.identity_key or "")
        if not identity_key:
            raise ValueError("controller_posture_identity_scope_missing")
        session_changed = bool(
            self._last_generation_key is None
            or generation_key[:2] != self._last_generation_key[:2]
        )
        if (
            not session_changed
            and self._last_identity_key
            and identity_key != self._last_identity_key
        ):
            raise RuntimeError(
                "controller_posture_identity_changed_without_session"
            )

        previous_state = self._ctx.state
        if session_changed:
            self._reset_for_new_session(now=time.time())
        session_disposition = (
            SESSION_NEW_RESET if session_changed else SESSION_CONTINUED
        )
        new_state = self._evaluate_state(modem, computed)
        published_ts = time.time()
        congestion_disposition, congestion_bucket, congestion_avg = (
            self._canonical_congestion_input(computed)
        )
        posture = self._publish_posture(
            modem=modem,
            previous_state=previous_state,
            state=new_state,
            published_ts=published_ts,
            session_disposition=session_disposition,
            congestion_disposition=congestion_disposition,
            congestion_bucket=congestion_bucket,
            congestion_avg=congestion_avg,
        )
        self._current_posture = posture
        self._last_generation_key = generation_key
        self._last_identity_key = identity_key
        return posture

    def _evaluate_state(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> SystemState:
        current = self._ctx.state

        if str(getattr(current, "name", "") or "") == "BLANKING":
            raise RuntimeError("legacy_blanket_blanking_state_rejected")

        if not is_fault_state(current):
            emergency_class = self._check_emergency(modem, computed)
            if emergency_class:
                now = time.time()
                guard_until = float(getattr(self._ctx, "emergency_reentry_guard_until", 0.0) or 0.0)
                if now < guard_until and emergency_class not in FAULT_REENTRY_BYPASS_CLASSES:
                    return current
                return self._enter_emergency(modem, emergency_class=emergency_class)

        if current == SystemState.WORKING:
            return self._eval_working(modem, computed)
        if current == SystemState.DEGRADED:
            return self._eval_degraded(modem, computed)
        if current == SystemState.CONGESTED:
            return self._eval_congested(modem, computed)
        if current == SystemState.E_RADIO_DOWN:
            return self._eval_e_radio_down(modem, computed)
        if current == SystemState.BAD_RF:
            return self._eval_bad_rf(modem, computed)
        if current == SystemState.E_SESSION_RESET:
            return self._eval_e_session_reset(modem, computed)
        if current == SystemState.E_CONGESTION_STORM:
            return self._eval_e_congestion_storm(modem, computed)

        return current

    def _reset_for_new_session(self, *, now: float) -> None:
        self._ctx = _ControllerPostureState(entered_at=float(now))
        self._reset_counters()
        self._radio_down_since = 0.0
        self._lte_disabled_since = 0.0
        self._sinr_low_since = 0.0
        self._congestion_storm_since = 0.0
        self._degraded_reentry_guard_until = 0.0

    def _publish_posture(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        previous_state: SystemState,
        state: SystemState,
        published_ts: float,
        session_disposition: str,
        congestion_disposition: str,
        congestion_bucket: str,
        congestion_avg: float | None,
    ) -> ControllerPosture:
        if self._ctx.state != state:
            raise RuntimeError("controller_posture_internal_state_mismatch")
        return build_controller_posture(
            source_generation=int(modem.generation.generation),
            source_epoch=str(modem.generation.source_epoch),
            session_scope_key=str(modem.generation.session_scope_key),
            identity_key=str(modem.identity.identity_key),
            previous_state=previous_state,
            state=state,
            state_changed=bool(state != previous_state),
            published_ts=float(published_ts),
            entered_at=float(self._ctx.entered_at),
            session_disposition=session_disposition,
            congestion_input_disposition=congestion_disposition,
            congestion_state_bucket=congestion_bucket,
            congestion_state_avg_pct=congestion_avg,
            congestion_kind=self._ctx.congestion_kind,
            warning_class=self._ctx.warning_class,
            warning_source=self._ctx.warning_source,
            warning_mechanism=self._ctx.warning_mechanism,
            warning_reason=self._ctx.warning_reason,
            fault_class=fault_state_class(
                state,
                self._ctx.emergency_class,
            ),
            emergency_class=self._ctx.emergency_class,
            emergency_origin_class=self._ctx.emergency_origin_class,
            emergency_reason=self._ctx.emergency_reason,
            degraded_actions_tried=self._ctx.degraded_actions_tried,
            degraded_timeout_at=self._ctx.degraded_timeout_at,
            congested_last_check=self._ctx.congested_last_check,
            congested_action_taken=self._ctx.congested_action_taken,
            emergency_timeout_at=self._ctx.emergency_timeout_at,
            soft_emergency_hold_until=self._ctx.soft_emergency_hold_until,
            emergency_observe_until=self._ctx.emergency_observe_until,
            emergency_reentry_guard_until=(
                self._ctx.emergency_reentry_guard_until
            ),
            emergency_recovery_since=self._ctx.emergency_recovery_since,
            emergency_recovery_step=self._ctx.emergency_recovery_step,
            emergency_next_action_at=self._ctx.emergency_next_action_at,
            bad_rf_recovery_since=self._ctx.bad_rf_recovery_since,
            bad_rf_recovery_step=self._ctx.bad_rf_recovery_step,
            bad_rf_next_action_at=self._ctx.bad_rf_next_action_at,
            rf_cliff_event_at=self._ctx.rf_cliff_event_at,
            rf_cliff_event_reason=self._ctx.rf_cliff_event_reason,
            post_fault_class=self._ctx.post_fault_class,
            post_fault_until=self._ctx.post_fault_until,
            recovered_lane_bands=self._ctx.recovered_lane_bands,
            recovered_lane_ts=self._ctx.recovered_lane_ts,
            recovered_lane_from_fault=self._ctx.recovered_lane_from_fault,
            recovered_lane_until=self._ctx.recovered_lane_until,
            rule_progress=self._rule_progress(
                now=float(published_ts),
                congestion_disposition=congestion_disposition,
            ),
        )

    def _rule_progress(
        self,
        *,
        now: float,
        congestion_disposition: str,
    ) -> tuple[PostureRuleProgress, ...]:
        hard = self._thresholds.get("hard", {})
        soft = self._thresholds.get("soft", {})

        def _observations(
            rule_id: str,
            threshold: int,
            progress: int,
            *,
            status: str = "available",
        ) -> PostureRuleProgress:
            limit = max(0, int(threshold))
            value = max(0, int(progress))
            return PostureRuleProgress(
                rule_id=rule_id,
                basis=BASIS_ACCEPTED_OBSERVATIONS,
                threshold=float(limit),
                progress=float(value),
                satisfied=bool(status == "available" and value >= limit),
                status=status,
            )

        def _elapsed(
            rule_id: str,
            threshold: float,
            since: float,
            *,
            status: str | None = None,
        ) -> PostureRuleProgress:
            limit = max(0.0, float(threshold))
            start = float(since or 0.0)
            resolved_status = (
                status
                if status is not None
                else ("available" if start > 0.0 else "inactive")
            )
            progress = max(0.0, float(now) - start) if start > 0.0 else 0.0
            return PostureRuleProgress(
                rule_id=rule_id,
                basis=BASIS_ELAPSED_TIME,
                threshold=limit,
                progress=progress,
                satisfied=bool(
                    resolved_status == "available" and progress >= limit
                ),
                status=resolved_status,
            )

        degraded_timeout_s = float(
            soft.get(
                "degraded_timeout_s",
                self.state_cfg.degraded_timeout_s,
            )
        )
        emergency_timeout_s = float(
            hard.get(
                "emergency_timeout_s",
                self.state_cfg.emergency_timeout_s,
            )
        )
        degraded_timeout_start = (
            float(self._ctx.degraded_timeout_at) - degraded_timeout_s
            if self._ctx.degraded_timeout_at > 0.0
            else 0.0
        )
        emergency_timeout_start = (
            float(self._ctx.emergency_timeout_at) - emergency_timeout_s
            if self._ctx.emergency_timeout_at > 0.0
            else 0.0
        )
        congestion_status = (
            "available"
            if congestion_disposition == CONGESTION_AVAILABLE
            else "unavailable"
        )
        rows = (
            _observations(
                "congestion_clear",
                self.state_cfg.congestion_confirm_clear_ticks,
                self._confirmed_clear_ticks,
                status=congestion_status,
            ),
            _observations(
                "degraded_entry",
                int(
                    soft.get(
                        "degraded_entry_ticks",
                        self.state_cfg.degraded_entry_ticks,
                    )
                ),
                self._degraded_ticks,
            ),
            _observations(
                "degraded_idle_exit",
                self.state_cfg.degraded_idle_exit_ticks,
                self._degraded_idle_ticks,
            ),
            _observations(
                "degraded_recovery",
                self.state_cfg.degraded_recovery_ticks,
                self._degraded_recovery_ticks,
            ),
            _elapsed(
                "degraded_timeout",
                degraded_timeout_s,
                degraded_timeout_start,
            ),
            _elapsed(
                "emergency_congestion_persist",
                float(
                    hard.get(
                        "emergency_congestion_persist_s",
                        self.state_cfg.emergency_congestion_persist_s,
                    )
                ),
                self._congestion_storm_since,
            ),
            _elapsed(
                "emergency_radio_down_persist",
                float(
                    hard.get(
                        "emergency_radio_down_persist_s",
                        self.state_cfg.emergency_radio_down_persist_s,
                    )
                ),
                self._radio_down_since,
            ),
            _elapsed(
                "emergency_recovery_stable",
                float(
                    hard.get(
                        "emergency_recovery_stable_s",
                        self.state_cfg.emergency_recovery_stable_s,
                    )
                ),
                self._ctx.emergency_recovery_since,
            ),
            _elapsed(
                "emergency_sinr_persist",
                float(
                    hard.get(
                        "emergency_sinr_persist_s",
                        self.state_cfg.emergency_sinr_persist_s,
                    )
                ),
                self._sinr_low_since,
            ),
            _elapsed(
                "emergency_timeout",
                emergency_timeout_s,
                emergency_timeout_start,
            ),
            _elapsed(
                "bad_rf_recovery_stable",
                float(
                    getattr(
                        self.cfg,
                        "bad_rf_recovery_stable_s",
                        20.0,
                    )
                    or 20.0
                ),
                self._ctx.bad_rf_recovery_since,
            ),
        )
        return tuple(sorted(rows, key=lambda row: row.rule_id))

    def _require_current_posture(self, posture_id: str) -> ControllerPosture:
        current = self._current_posture
        if (
            current.source_generation <= 0
            or str(posture_id or "") != current.posture_id
        ):
            raise RuntimeError("controller_posture_progress_scope_mismatch")
        return current

    def project_normal_optimization_progress(
        self,
        *,
        progress_scope_id: str,
        receipt_id: str,
        behavior_id: str,
        checked_at: float,
        action_taken: bool,
    ) -> bool:
        """Project one exact normal-policy evaluation or terminal receipt."""

        current = self._current_posture
        scope = str(progress_scope_id or "").strip()
        receipt = str(receipt_id or "").strip()
        behavior = str(behavior_id or "").strip()
        if not scope or scope != current.progress_scope_id:
            raise RuntimeError("controller_posture_progress_scope_mismatch")
        if not receipt:
            raise ValueError("controller_posture_progress_receipt_id_missing")
        if receipt in self._ctx.normal_progress_receipt_ids:
            return False
        degraded = {
            "degraded_best_live_neighbor",
            "degraded_open_all_deployed_bands",
        }
        congested = {
            "congested_neighbor_escape",
            "congested_restore_known_good_ca",
            "congested_normal_policy_evaluation",
        }
        if behavior in degraded:
            if current.state != SystemState.DEGRADED:
                raise RuntimeError(
                    "controller_posture_degraded_progress_state_mismatch"
                )
            if action_taken:
                self._ctx.degraded_actions_tried += 1
        elif behavior in congested:
            if current.state != SystemState.CONGESTED:
                raise RuntimeError(
                    "controller_posture_congestion_progress_state_mismatch"
                )
            self._ctx.congested_last_check = float(checked_at)
            if action_taken:
                self._ctx.congested_action_taken = True
        else:
            raise ValueError("controller_posture_progress_behavior_invalid")
        self._ctx.normal_progress_receipt_ids.append(receipt)
        del self._ctx.normal_progress_receipt_ids[:-256]
        return True

    def note_degraded_recovery_attempt(self, *, posture_id: str) -> int:
        raise RuntimeError(
            "controller_posture_direct_degraded_progress_deleted_use_receipt_projection"
        )

    def note_congestion_check(
        self,
        *,
        posture_id: str,
        checked_at: float,
        action_taken: bool,
    ) -> None:
        raise RuntimeError(
            "controller_posture_direct_congestion_progress_deleted_use_receipt_projection"
        )

    def note_fault_recovery_progress(
        self,
        *,
        posture_id: str,
        fault_class: str,
        step: int,
        next_action_at: float,
    ) -> None:
        current = self._require_current_posture(posture_id)
        expected = str(current.fault_class or "")
        requested = str(fault_class or "")
        if not expected or requested != expected:
            raise RuntimeError(
                "controller_posture_fault_progress_class_mismatch"
            )
        if int(step) < 0:
            raise ValueError("controller_posture_fault_progress_step_invalid")
        if requested == "BAD_RF":
            self._ctx.bad_rf_recovery_step = int(step)
            self._ctx.bad_rf_next_action_at = float(next_action_at)
        else:
            self._ctx.emergency_recovery_step = int(step)
            self._ctx.emergency_next_action_at = float(next_action_at)

    def _check_emergency(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[str]:
        now = time.time()
        hard = self._thresholds.get("hard", {})
        radio_down_persist_s = float(
            hard.get("emergency_radio_down_persist_s", self.state_cfg.emergency_radio_down_persist_s)
        )

        if self._radio_register_blocks_fault_decision():
            self._radio_down_since = 0.0
            self._sinr_low_since = 0.0
            return None

        if bool(getattr(modem.fault, 'lte_interface_disabled', False)):
            if self._lte_disabled_since <= 0.0:
                self._lte_disabled_since = now
        else:
            self._lte_disabled_since = 0.0

        if not modem.cellular.radio_connected:
            if self._radio_down_since <= 0.0:
                self._radio_down_since = now
            if now - self._radio_down_since >= radio_down_persist_s:
                disabled_persisted = (
                    self._lte_disabled_since > 0.0
                    and (now - self._lte_disabled_since) >= radio_down_persist_s
                )
                if (
                    not disabled_persisted
                    and self._action_effect_covers_fault(
                        modem,
                        fault_class=FAULT_RADIO_DOWN,
                        now=now,
                    )
                ):
                    return None
                return "E_RADIO_DOWN"
        else:
            self._radio_down_since = 0.0

        if modem.cellular.sinr_db < float(hard.get("emergency_sinr_floor", self.state_cfg.emergency_sinr_floor)):
            if self._sinr_low_since <= 0.0:
                self._sinr_low_since = now
            if now - self._sinr_low_since >= float(
                hard.get("emergency_sinr_persist_s", self.state_cfg.emergency_sinr_persist_s)
            ):
                if self._rf_collapse_entry_ready(modem, computed):
                    return "BAD_RF"
        else:
            self._sinr_low_since = 0.0

        session_uptime_source = str(getattr(modem.action, 'session_uptime_source', "") or "").strip().lower()
        uptime_authoritative = session_uptime_source not in {
            "maintenance_pending",
            "unavailable",
            "unsupported",
            "unknown",
        }
        if modem.action.session_uptime_s < 5.0 and self._ctx.time_in_state_s > 30 and uptime_authoritative:
            if self._action_effect_covers_fault(
                modem,
                fault_class=FAULT_SESSION_RESET,
                now=now,
            ):
                return None
            return "E_SESSION_RESET"

        congestion_storm = (
            self._ctx.state == SystemState.CONGESTED
            and modem.traffic.has_traffic
            and (
                self._rtt_above_cliff(computed)
                or (
                    computed.latency.rtt_sliding_avg > float(self._thresholds.get("hard", {}).get("rtt_cliff_ms", self.state_cfg.rtt_cliff_ms))
                    and self._lq_is_bad(modem, computed)
                )
            )
        )
        if congestion_storm:
            if self._congestion_storm_since <= 0.0:
                self._congestion_storm_since = now
            if now - self._congestion_storm_since >= float(
                hard.get("emergency_congestion_persist_s", self.state_cfg.emergency_congestion_persist_s)
            ):
                if self._classify_congestion_kind(modem, computed) == "attached_cell":
                    return "E_CONGESTION_STORM"
                self._set_warning(
                    warning_class="congestion",
                    warning_source="local_traffic",
                    warning_mechanism="pipe_saturation",
                    warning_reason="persistent local traffic pressure does not escalate to fault state",
                )
        else:
            self._congestion_storm_since = 0.0

        return None

    @staticmethod
    def _action_effect_covers_fault(
        modem: CellularIntelligenceGeneration,
        *,
        fault_class: str,
        now: float,
    ) -> bool:
        del fault_class, now
        return bool(modem.action.modem_live_io_quiet_active)

    def _radio_register_blocks_fault_decision(self) -> bool:
        decision_input = getattr(self, "_decision_input", None)
        if decision_input is None or not hasattr(decision_input, "lane"):
            return False
        try:
            lane = decision_input.lane(REGISTER_RADIO)
        except Exception:
            return False
        trust = str(getattr(lane, "trust", "") or "").strip().lower()
        error = str(getattr(lane, "error", "") or "")
        blocked = bool(
            not bool(getattr(lane, "valid", False))
            or bool(getattr(lane, "stale", False))
            or bool(getattr(lane, "dirty", False))
            or trust in {"unknown", "unsupported", "stale", "dirty", "transition"}
            or error
        )
        if blocked:
            reason_bits = []
            if not bool(getattr(lane, "valid", False)):
                reason_bits.append("invalid")
            if bool(getattr(lane, "stale", False)):
                reason_bits.append("stale")
            if bool(getattr(lane, "dirty", False)):
                reason_bits.append("dirty")
            if trust:
                reason_bits.append(f"trust={trust}")
            if error:
                reason_bits.append(f"error={error}")
            self._set_warning(
                warning_class="state_input",
                warning_source="runtime_register",
                warning_mechanism="radio_register_not_trusted",
                warning_reason="radio register blocks fault decision: " + ", ".join(reason_bits),
            )
        return blocked

    @staticmethod
    def _rtt_avg(computed: DerivedSignalFrames) -> float:
        return computed.latency.rtt_sliding_avg

    def _rtt_above_warning(self, computed: DerivedSignalFrames) -> bool:
        hard = self._thresholds.get("hard", {})
        return self._rtt_avg(computed) > float(hard.get("rtt_warning_ms", self.state_cfg.rtt_warning_ms))

    def _rtt_above_cliff(self, computed: DerivedSignalFrames) -> bool:
        hard = self._thresholds.get("hard", {})
        return self._rtt_avg(computed) > float(hard.get("rtt_cliff_ms", self.state_cfg.rtt_cliff_ms))

    def _rtt_below_warning_clear(self, computed: DerivedSignalFrames) -> bool:
        hard = self._thresholds.get("hard", {})
        return self._rtt_avg(computed) < float(hard.get("rtt_warning_clear_ms", self.state_cfg.rtt_warning_clear_ms))

    def _lq_is_bad(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        hard = self._thresholds.get("hard", {})
        soft = self._thresholds.get("soft", {})
        if not modem.traffic.has_traffic:
            return False
        if modem.cellular.sinr_db <= float(soft.get("v5_low_rf_sinr_gate_db", self.state_cfg.v5_low_rf_sinr_gate_db)):
            return False
        if not bool(computed.quality.cq_vector_a_valid):
            return False
        return computed.quality.cq_cell < float(hard.get("cq_bad_threshold", self.state_cfg.v5_cq_bad_threshold))

    def _lq_is_good(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        hard = self._thresholds.get("hard", {})
        soft = self._thresholds.get("soft", {})
        if not modem.traffic.has_traffic:
            return True
        if modem.cellular.sinr_db <= float(soft.get("v5_low_rf_sinr_gate_db", self.state_cfg.v5_low_rf_sinr_gate_db)):
            return False
        if not bool(computed.quality.cq_vector_a_valid):
            # No enough CQ samples: avoid false recovery signals from CQ lane.
            return False
        return computed.quality.cq_cell >= float(hard.get("cq_good_threshold", self.state_cfg.v5_cq_good_threshold))

    def _congestion_qualifier_flags(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        allow_low_rf: bool = False,
    ) -> tuple[bool, bool, bool]:
        """
        Shared congestion qualifier pressure evaluator.

        Returns (bad_now, high_pressure, moderate_pressure).

        Congestion is a CELL state, not a modem state.  The two primary
        signals — RSRQ gap and RTT delta — are cell-load indicators that
        work with or without modem traffic.  Individual signal processors
        gate themselves (rsrq_gap_valid, rtt_delta_valid) so no blanket
        traffic gate is needed here.

        SINR is RF path quality (signal-to-interference+noise), not cell
        load.  A cell can be congested at any SINR.  RF quality is handled
        by the BAD_RF state, not the congestion qualifier.
        """
        del modem, allow_low_rf
        return authority_congestion_flags(computed)

    @staticmethod
    def _canonical_congestion_input(
        computed: DerivedSignalFrames,
    ) -> tuple[str, str, float | None]:
        bucket = str(
            getattr(computed.quality, "congestion_state_bucket", "") or ""
        ).strip().lower()
        raw_avg = getattr(
            computed.quality,
            "congestion_state_avg_pct",
            None,
        )
        if bucket not in {"normal", "mild", "high"} or raw_avg in ("", None):
            return CONGESTION_UNAVAILABLE, "", None
        try:
            avg = float(raw_avg)
        except (TypeError, ValueError):
            return CONGESTION_UNAVAILABLE, "", None
        if not 0.0 <= avg <= 100.0:
            return CONGESTION_UNAVAILABLE, "", None
        return CONGESTION_AVAILABLE, bucket, avg

    @classmethod
    def _state_congestion_bucket(cls, computed: DerivedSignalFrames) -> str:
        return cls._canonical_congestion_input(computed)[1]

    @classmethod
    def _state_congestion_avg_pct(
        cls, computed: DerivedSignalFrames
    ) -> float:
        disposition, _, avg = cls._canonical_congestion_input(computed)
        if disposition != CONGESTION_AVAILABLE or avg is None:
            raise RuntimeError("canonical_congestion_input_unavailable")
        return avg

    @classmethod
    def _state_congestion_confirmed(
        cls,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> bool:
        del modem
        disposition, bucket, _ = cls._canonical_congestion_input(computed)
        return bool(
            disposition == CONGESTION_AVAILABLE
            and bucket in {"mild", "high"}
        )

    def _is_degraded_raw(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        return self._lq_is_bad(modem, computed) and not self._rtt_above_warning(computed)

    def _classify_congestion_kind(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> str:
        hinted = str(getattr(computed.quality, 'congestion_kind_hint', "") or "").strip().lower()
        if hinted in {"local_traffic", "attached_cell"}:
            return hinted
        dl = float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0)
        soft = self._thresholds.get("soft", {})
        rf_ok = (
            modem.cellular.sinr_db >= float(soft.get("congestion_rf_ok_sinr_db", self.state_cfg.congestion_rf_ok_sinr_db))
            and modem.cellular.rsrp_dbm >= float(soft.get("congestion_rf_ok_rsrp_dbm", self.state_cfg.congestion_rf_ok_rsrp_dbm))
        )
        if modem.traffic.has_traffic and rf_ok and dl >= float(soft.get("congestion_self_load_dl_mbps", self.state_cfg.congestion_self_load_dl_mbps)):
            return "local_traffic"
        return "attached_cell"

    def _eval_working(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        if self._state_congestion_confirmed(modem, computed):
            kind = self._classify_congestion_kind(modem, computed)
            log.info(
                "WORKING -> CONGESTED[%s]: congestion_state_bucket=%s avg_pct=%.1f",
                kind,
                self._state_congestion_bucket(computed) or "unknown",
                self._state_congestion_avg_pct(computed),
            )
            return self._enter_congested(kind)

        if self._is_degraded_raw(modem, computed):
            if time.time() < self._degraded_reentry_guard_until:
                self._degraded_ticks = 0
                return SystemState.WORKING
            self._degraded_ticks += 1
            soft = self._thresholds.get("soft", {})
            degraded_entry_ticks = int(soft.get("degraded_entry_ticks", self.state_cfg.degraded_entry_ticks))
            if self._degraded_ticks >= degraded_entry_ticks:
                log.info(
                    "WORKING -> DEGRADED: CQ %.2f < %.2f for %d ticks",
                    computed.quality.cq_cell,
                    float(self.state_cfg.v5_cq_bad_threshold),
                    self._degraded_ticks,
                )
                self._degraded_ticks = 0
                return self._enter_degraded()
        else:
            self._degraded_ticks = 0

        return SystemState.WORKING

    def _eval_degraded(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        if self._state_congestion_confirmed(modem, computed):
            kind = self._classify_congestion_kind(modem, computed)
            log.info(
                "DEGRADED -> CONGESTED[%s]: congestion_state_bucket=%s avg_pct=%.1f",
                kind,
                self._state_congestion_bucket(computed) or "unknown",
                self._state_congestion_avg_pct(computed),
            )
            return self._enter_congested(kind)

        if self._lq_is_good(modem, computed):
            self._degraded_recovery_ticks += 1
            if self._degraded_recovery_ticks >= max(1, int(self.state_cfg.degraded_recovery_ticks)):
                log.info(
                    "DEGRADED -> WORKING: CQ recovered to %.2f for %d ticks",
                    computed.quality.cq_cell,
                    self._degraded_recovery_ticks,
                )
                return self._enter_working()
        else:
            self._degraded_recovery_ticks = 0

        if not modem.traffic.has_traffic:
            self._degraded_idle_ticks += 1
            if self._degraded_idle_ticks >= max(1, int(self.state_cfg.degraded_idle_exit_ticks)):
                log.info(
                    "DEGRADED -> WORKING: traffic stopped for %d ticks (lost LQ evidence)",
                    self._degraded_idle_ticks,
                )
                return self._enter_working()
        else:
            self._degraded_idle_ticks = 0

        if time.time() >= self._ctx.degraded_timeout_at:
            # Avoid false E_STALLED_RECOVERY when policy suppresses all
            # degraded recovery actions (for example daytime B818 band actions).
            # In that case no real alternatives were executed, so timeout alone
            # is not evidence that recovery was exhausted.
            tried = max(0, int(getattr(self._ctx, "degraded_actions_tried", 0) or 0))
            if tried <= 0:
                self._ctx.degraded_timeout_at = time.time() + self.state_cfg.degraded_timeout_s
                log.info(
                    "DEGRADED timeout reached with zero recovery attempts; extending window by %ds",
                    self.state_cfg.degraded_timeout_s,
                )
                return SystemState.DEGRADED
            self._set_warning(
                warning_class="degraded",
                warning_source="controller",
                warning_mechanism="stalled_recovery",
                warning_reason=(
                    f"timeout ({self.state_cfg.degraded_timeout_s}s), all alternatives exhausted "
                    f"(attempts={tried})"
                ),
            )
            self._ctx.soft_emergency_hold_until = time.time() + float(
                self._thresholds.get("soft", {}).get("soft_emergency_hold_s", self.state_cfg.soft_emergency_hold_s)
            )
            self._ctx.degraded_timeout_at = time.time() + self.state_cfg.degraded_timeout_s
            log.warning(
                "DEGRADED stalled recovery warning: timeout (%ds), recovery attempts exhausted (attempts=%d)",
                self.state_cfg.degraded_timeout_s,
                tried,
            )
            return SystemState.DEGRADED

        return SystemState.DEGRADED

    def _eval_congested(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        congestion_disposition, _, _ = self._canonical_congestion_input(
            computed
        )
        if congestion_disposition != CONGESTION_AVAILABLE:
            self._confirmed_clear_ticks = 0
            self._set_warning(
                warning_class="state_input",
                warning_source="congestion_authority",
                warning_mechanism="canonical_congestion_unavailable",
                warning_reason=(
                    "canonical congestion bucket/average unavailable; "
                    "holding current congestion posture"
                ),
            )
            return SystemState.CONGESTED
        if self._state_congestion_confirmed(modem, computed):
            self._confirmed_clear_ticks = 0
            return SystemState.CONGESTED

        self._confirmed_clear_ticks += 1
        if self._confirmed_clear_ticks >= max(1, int(self.state_cfg.congestion_confirm_clear_ticks)):
            log.info("CONGESTED cleared")
            return self._resolve_non_congested_state(modem, computed)

        return SystemState.CONGESTED

    def _eval_emergency(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        # Compatibility dispatcher for callers that still route fault states here.
        if self._ctx.state == SystemState.E_RADIO_DOWN:
            return self._eval_e_radio_down(modem, computed)
        if self._ctx.state == SystemState.BAD_RF:
            return self._eval_bad_rf(modem, computed)
        if self._ctx.state == SystemState.E_SESSION_RESET:
            return self._eval_e_session_reset(modem, computed)
        if self._ctx.state == SystemState.E_CONGESTION_STORM:
            return self._eval_e_congestion_storm(modem, computed)
        return self._ctx.state

    def _eval_fault_state_common(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        recovery_feasible: bool,
        stable_reason: str,
    ) -> SystemState:
        now = time.time()
        hard = self._thresholds.get("hard", {})
        if recovery_feasible:
            if self._ctx.emergency_recovery_since <= 0.0:
                self._ctx.emergency_recovery_since = now
            dwell = now - self._ctx.emergency_recovery_since
            if dwell >= max(0.0, float(hard.get("emergency_recovery_stable_s", self.state_cfg.emergency_recovery_stable_s))):
                self._ctx.emergency_reentry_guard_until = (
                    now + max(0.0, float(hard.get("emergency_reentry_guard_s", self.state_cfg.emergency_reentry_guard_s)))
                )
                return self._resolve_post_emergency_state(modem, computed, reason=f"{stable_reason} for {dwell:.0f}s")
        else:
            self._ctx.emergency_recovery_since = 0.0

        if now >= self._ctx.emergency_timeout_at:
            if not recovery_feasible:
                extend_s = float(hard.get("emergency_timeout_s", self.state_cfg.emergency_timeout_s))
                self._ctx.emergency_timeout_at = now + extend_s
                log.warning(
                    "%s timeout while still active (%s) - extending by %.0fs",
                    self._ctx.state.name,
                    self._ctx.emergency_class,
                    extend_s,
                )
                return self._ctx.state
            self._ctx.emergency_reentry_guard_until = (
                now + max(0.0, float(hard.get("emergency_reentry_guard_s", self.state_cfg.emergency_reentry_guard_s)))
            )
            return self._resolve_post_emergency_state(modem, computed, reason="timeout window expired")

        return self._ctx.state

    def _rf_collapse_entry_ready(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        if not modem.cellular.radio_connected:
            return False
        if is_bad_rf_capacity_winner(modem, computed, self.cfg):
            return False
        return bool(
            assess_rf_truth(
                modem=modem,
                computed=computed,
                cfg=self.cfg,
                low_sinr_persistent=True,
            ).confirmed_bad_rf
        )

    @staticmethod
    def _rf_collapse_raw_trip_bad(modem: CellularIntelligenceGeneration) -> bool:
        try:
            sinr_db = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
            rsrp_dbm = float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0)
            rsrq_db = float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0)
        except Exception:
            return False
        return bool(
            sinr_db <= 3.0
            and rsrp_dbm <= BAD_RF_COLLAPSE_RSRP_DBM
            and rsrq_db <= BAD_RF_COLLAPSE_RSRQ_DB
        )

    def _radio_down_recovery_feasible(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        del computed
        return bool(modem.cellular.radio_connected and modem.action.session_uptime_s >= 2.0)

    def _rf_collapse_recovery_feasible(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        if not modem.cellular.radio_connected:
            return False

        hard = self._thresholds.get("hard", {})
        soft = self._thresholds.get("soft", {})
        sinr_gate = float(hard.get("sinr_feasibility_gate_db", self.state_cfg.sinr_feasibility_gate_db))
        physical_rf_recovered = bool(
            modem.cellular.sinr_db >= sinr_gate
            and modem.cellular.rsrp_dbm >= -105.0
            and modem.cellular.rsrq_db >= -13.0
        )
        if physical_rf_recovered:
            return True
        if assess_rf_truth(modem=modem, computed=computed, cfg=self.cfg).rules_out_bad_rf:
            return True

        # Hard RF emergencies should not stay latched just because congestion
        # is still bad. Once RF itself is back above the low-RF gate, release
        # BAD_RF and let the normal congestion state own RTT/cell-load pressure.
        emergency_floor = float(hard.get("emergency_sinr_floor", self.state_cfg.emergency_sinr_floor))
        low_rf_gate = float(soft.get("v5_low_rf_sinr_gate_db", self.state_cfg.v5_low_rf_sinr_gate_db))
        usable_sinr_floor = max(low_rf_gate + 1.0, emergency_floor + 3.0)
        if modem.cellular.sinr_db < usable_sinr_floor:
            return False
        if modem.cellular.rsrp_dbm < -105.0 or modem.cellular.rsrq_db < -13.0:
            return False
        if not modem.traffic.has_traffic:
            return True
        if ca_ld_stable(computed):
            return True
        return self._lq_is_good(modem, computed)

    def _session_reset_recovery_feasible(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        del computed
        return bool(modem.cellular.radio_connected and modem.action.session_uptime_s >= 5.0)

    def _congestion_storm_active(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        if not modem.traffic.has_traffic:
            return False
        if self._classify_congestion_kind(modem, computed) != "attached_cell":
            return False
        pressure_bad, _, _ = self._congestion_qualifier_flags(modem, computed, allow_low_rf=False)
        return bool(self._rtt_above_cliff(computed) or (pressure_bad and self._rtt_above_warning(computed)))

    def _eval_e_radio_down(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        return self._eval_fault_state_common(
            modem,
            computed,
            recovery_feasible=self._radio_down_recovery_feasible(modem, computed),
            stable_reason=f"radio reattached, session uptime {modem.action.session_uptime_s:.0f}s",
        )

    def _eval_bad_rf(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        if not modem.cellular.radio_connected:
            return self._enter_emergency(
                modem,
                emergency_class="E_RADIO_DOWN",
                reason_override="radio disconnected during BAD_RF hold",
            )
        now = time.time()
        capacity_winner = is_bad_rf_capacity_winner(modem, computed, self.cfg)
        if self._bad_rf_disproven_by_brain_evidence(modem, computed, capacity_winner=capacity_winner):
            hard = self._thresholds.get("hard", {})
            self._ctx.emergency_reentry_guard_until = (
                now + max(0.0, float(hard.get("emergency_reentry_guard_s", self.state_cfg.emergency_reentry_guard_s)))
            )
            return self._resolve_disproven_bad_rf_state(
                modem,
                computed,
                reason="BAD_RF cleared: live Brain evidence no longer proves RF collapse",
            )
        if capacity_winner or self._rf_collapse_recovery_feasible(modem, computed):
            if self._ctx.bad_rf_recovery_since <= 0.0:
                self._ctx.bad_rf_recovery_since = now
            dwell = now - self._ctx.bad_rf_recovery_since
            if dwell >= max(0.0, float(getattr(self.cfg, "bad_rf_recovery_stable_s", 20.0) or 20.0)):
                self._ctx.emergency_reentry_guard_until = (
                    now + max(0.0, float(self._thresholds.get("hard", {}).get("emergency_reentry_guard_s", self.state_cfg.emergency_reentry_guard_s)))
                )
                reason = (
                    "capacity outcome stable despite BAD_RF"
                    if capacity_winner
                    else f"RF stabilized after BAD_RF hold, SINR {modem.cellular.sinr_db:.1f} dB"
                )
                return self._resolve_post_emergency_state(
                    modem,
                    computed,
                    reason=f"{reason} for {dwell:.0f}s",
                )
        else:
            self._ctx.bad_rf_recovery_since = 0.0
        return SystemState.BAD_RF

    def _eval_e_rf_collapse(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        return self._eval_bad_rf(modem, computed)

    def _bad_rf_disproven_by_brain_evidence(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        capacity_winner: bool,
    ) -> bool:
        """Return True when the Brain has enough live evidence to clear BAD_RF.

        BAD_RF is a diagnosis, not a sticky label.  If the serving RF, CA
        capacity, or raw-collapse predicate contradicts it, the state machine
        must stop exporting BAD_RF authority immediately and let normal
        Working/Degaded/Congested evidence own the next state.
        """
        if not modem.cellular.radio_connected:
            return False
        if self._physical_rf_recovered_from_bad_rf(modem):
            return False
        if capacity_winner:
            return True
        verdict = assess_rf_truth(modem=modem, computed=computed, cfg=self.cfg)
        if verdict.rules_out_bad_rf:
            return True
        return not verdict.confirmed_bad_rf

    def _physical_rf_recovered_from_bad_rf(self, modem: CellularIntelligenceGeneration) -> bool:
        hard = self._thresholds.get("hard", {})
        sinr_gate = float(hard.get("sinr_feasibility_gate_db", self.state_cfg.sinr_feasibility_gate_db))
        return bool(
            modem.cellular.sinr_db >= sinr_gate
            and modem.cellular.rsrp_dbm >= -105.0
            and modem.cellular.rsrq_db >= -13.0
        )

    def _clear_disproven_fault_aftereffects(self) -> None:
        self._ctx.post_fault_class = ""
        self._ctx.post_fault_until = 0.0
        self._ctx.recovered_lane_bands = []
        self._ctx.recovered_lane_ts = 0.0
        self._ctx.recovered_lane_from_fault = ""
        self._ctx.recovered_lane_until = 0.0
        self._ctx.rf_cliff_event_reason = ""
        self._reset_fault_recovery_plan()

    def _resolve_disproven_bad_rf_state(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        reason: str,
    ) -> SystemState:
        self._clear_disproven_fault_aftereffects()
        self._ctx.emergency_class = ""
        self._ctx.emergency_origin_class = ""
        self._ctx.emergency_reason = ""
        if self._state_congestion_confirmed(modem, computed):
            kind = self._classify_congestion_kind(modem, computed)
            log.info("BAD_RF -> CONGESTED[%s]: %s", kind, reason)
            return self._enter_congested(kind)
        if self._is_degraded_raw(modem, computed):
            log.info("BAD_RF -> DEGRADED: %s", reason)
            return self._enter_degraded()
        log.info("BAD_RF -> WORKING: %s", reason)
        return self._enter_working()

    def _eval_e_session_reset(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        if not modem.cellular.radio_connected:
            origin = str(getattr(self._ctx, "emergency_origin_class", "") or self._ctx.emergency_class or "E_SESSION_RESET")
            self._ctx.emergency_origin_class = origin
            return self._enter_emergency(
                modem,
                emergency_class="E_RADIO_DOWN",
                reason_override="radio disconnected during session-reset recovery",
            )
        return self._eval_fault_state_common(
            modem,
            computed,
            recovery_feasible=self._session_reset_recovery_feasible(modem, computed),
            stable_reason=f"session uptime stabilized at {modem.action.session_uptime_s:.0f}s",
        )

    def _eval_e_congestion_storm(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        return self._eval_fault_state_common(
            modem,
            computed,
            recovery_feasible=not self._congestion_storm_active(modem, computed),
            stable_reason="congestion pressure eased",
        )

    def _resolve_post_emergency_state(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        reason: str,
    ) -> SystemState:
        fault_class = fault_state_class(self._ctx.state, self._ctx.emergency_class)
        origin_class = str(getattr(self._ctx, "emergency_origin_class", "") or "")
        if origin_class and fault_class == "E_RADIO_DOWN" and origin_class == "E_SESSION_RESET":
            fault_class = origin_class
        if fault_class:
            self._ctx.post_fault_class = fault_class
            fault_duration_s = max(0.0, float(self._ctx.time_in_state_s))
            damp_s = max(
                POST_FAULT_EXPLORATION_MIN_DAMP_S,
                min(fault_duration_s, POST_FAULT_EXPLORATION_MAX_DAMP_S),
            )
            now = time.time()
            self._ctx.post_fault_until = now + damp_s

            # ── Recovered lane authority ──────────────────────────────
            # Record which bands rescued us from BAD_RF / E_RADIO_DOWN.
            # The brain should prefer staying on these bands unless there
            # is materially stronger contrary evidence.
            if fault_class in {"BAD_RF", "E_RF_COLLAPSE", "E_RADIO_DOWN"}:
                active_bands = sorted(
                    {int(c.band) for c in modem.cellular.carriers
                     if (getattr(c, "is_pcell", False) or int(getattr(c, "state", 0) or 0) >= 1)
                     and int(c.band) > 0}
                )
                if not active_bands:
                    pb = int(getattr(modem.cellular, 'primary_band', 0) or 0)
                    if pb > 0:
                        active_bands = [pb]
                if active_bands:
                    self._ctx.recovered_lane_bands = list(active_bands)
                    self._ctx.recovered_lane_ts = now
                    self._ctx.recovered_lane_from_fault = fault_class
                    self._ctx.recovered_lane_until = now + damp_s
                    log.info(
                        "Recovered lane authority set: bands=%s from_fault=%s window=%.0fs",
                        active_bands, fault_class, damp_s,
                    )

        if self._state_congestion_confirmed(modem, computed):
            kind = self._classify_congestion_kind(modem, computed)
            log.info("%s -> CONGESTED[%s]: %s", self._ctx.state.name, kind, reason)
            return self._enter_congested(kind)
        if is_bad_rf_capacity_winner(modem, computed, self.cfg):
            log.info("%s -> WORKING: %s", self._ctx.state.name, reason)
            return self._enter_working()
        if self._is_degraded_raw(modem, computed):
            log.info("%s -> DEGRADED: %s", self._ctx.state.name, reason)
            return self._enter_degraded()
        log.info("%s -> WORKING: %s", self._ctx.state.name, reason)
        return self._enter_working()

    def _resolve_non_congested_state(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> SystemState:
        # Route clears through WORKING first so DEGRADED re-entry uses normal
        # entry hysteresis instead of a direct one-tick hop.
        return self._enter_working()

    def _reset_fault_recovery_plan(self) -> None:
        self._ctx.soft_emergency_hold_until = 0.0
        self._ctx.emergency_observe_until = 0.0
        self._ctx.emergency_recovery_since = 0.0
        self._ctx.emergency_recovery_step = 0
        self._ctx.emergency_next_action_at = 0.0
        self._ctx.bad_rf_recovery_since = 0.0
        self._ctx.bad_rf_recovery_step = 0
        self._ctx.bad_rf_next_action_at = 0.0

    def _enter_working(self) -> SystemState:
        was_degraded = self._ctx.state == SystemState.DEGRADED
        hard = self._thresholds.get("hard", {})
        self._transition(SystemState.WORKING)
        self._ctx.congestion_kind = ""
        self._ctx.emergency_class = ""
        self._ctx.emergency_reason = ""
        self._ctx.emergency_origin_class = ""
        self._reset_fault_recovery_plan()
        self._clear_warning()
        if was_degraded:
            self._degraded_reentry_guard_until = (
                time.time() + max(0.0, float(hard.get("degraded_reentry_guard_s", self.state_cfg.degraded_reentry_guard_s)))
            )
        self._reset_counters()
        return SystemState.WORKING

    def _enter_degraded(self) -> SystemState:
        self._transition(SystemState.DEGRADED)
        self._ctx.congestion_kind = ""
        self._ctx.emergency_class = ""
        self._ctx.emergency_reason = ""
        self._ctx.emergency_origin_class = ""
        self._reset_fault_recovery_plan()
        self._ctx.degraded_actions_tried = 0
        self._ctx.degraded_timeout_at = time.time() + self.state_cfg.degraded_timeout_s
        self._set_warning(
            warning_class="degraded",
            warning_source="radio_link",
            warning_mechanism="quality_drop",
            warning_reason="link quality is below target while RTT is still below congestion threshold",
        )
        self._reset_counters()
        return SystemState.DEGRADED

    def _enter_congested(self, kind: str = "attached_cell") -> SystemState:
        self._transition(SystemState.CONGESTED)
        self._ctx.congestion_kind = kind or "attached_cell"
        self._ctx.emergency_class = ""
        self._ctx.emergency_reason = ""
        self._ctx.emergency_origin_class = ""
        self._reset_fault_recovery_plan()
        self._ctx.congested_last_check = 0.0
        self._ctx.congested_action_taken = False
        if self._ctx.congestion_kind == "local_traffic":
            self._set_warning(
                warning_class="congestion",
                warning_source="local_traffic",
                warning_mechanism="pipe_saturation",
                warning_reason="heavy local traffic is filling the pipe under otherwise healthy RF",
            )
        else:
            self._set_warning(
                warning_class="congestion",
                warning_source="attached_cell",
                warning_mechanism="shared_cell_pressure",
                warning_reason="shared-cell congestion is confirmed under traffic pressure",
            )
        self._confirmed_clear_ticks = 0
        self._reset_counters()
        return SystemState.CONGESTED

    def _enter_confirmed_congestion(self, kind: str = "attached_cell") -> SystemState:
        return self._enter_congested(kind)

    def _enter_emergency(
        self,
        modem: CellularIntelligenceGeneration = None,
        emergency_class: str = "E_UNKNOWN",
        reason_override: Optional[str] = None,
    ) -> SystemState:
        hard = self._thresholds.get("hard", {})
        soft = self._thresholds.get("soft", {})
        reason = reason_override or "unknown"
        if reason_override is None and modem:
            if emergency_class == "E_RADIO_DOWN":
                reason = "radio down"
            elif emergency_class in {"BAD_RF", "E_RF_COLLAPSE"}:
                reason = self._rf_collapse_reason(modem, hard)
            elif emergency_class == "E_SESSION_RESET":
                reason = "session reset"
            elif emergency_class == "E_CONGESTION_STORM":
                reason = "persistent confirmed congestion under traffic pressure"
        if emergency_class == "E_STALLED_RECOVERY":
            self._set_warning(
                warning_class="degraded",
                warning_source="controller",
                warning_mechanism="stalled_recovery",
                warning_reason=reason or "degraded timeout with no viable alternatives",
            )
            self._ctx.soft_emergency_hold_until = time.time() + float(
                soft.get("soft_emergency_hold_s", self.state_cfg.soft_emergency_hold_s)
            )
            return self._ctx.state

        target_state = FAULT_STATE_BY_CLASS.get(emergency_class)
        if target_state is None:
            log.error("Unknown fault-state class requested: %s", emergency_class)
            self._set_warning(
                warning_class="controller",
                warning_source="controller_posture_authority",
                warning_mechanism="unknown_fault_class",
                warning_reason=f"unknown fault class requested: {emergency_class}",
            )
            return self._ctx.state
        log.warning("%s -> %s [%s]: %s", self._ctx.state.name, target_state.name, emergency_class, reason)
        self._transition(target_state)
        self._ctx.congestion_kind = ""
        now = time.time()
        self._ctx.emergency_timeout_at = now + float(hard.get("emergency_timeout_s", self.state_cfg.emergency_timeout_s))
        if emergency_class in {"BAD_RF", "E_RF_COLLAPSE"}:
            self._ctx.emergency_class = "BAD_RF"
        else:
            self._ctx.emergency_class = emergency_class
        current_origin = str(getattr(self._ctx, "emergency_origin_class", "") or "")
        if not current_origin:
            self._ctx.emergency_origin_class = self._ctx.emergency_class
        self._ctx.emergency_reason = reason
        self._reset_fault_recovery_plan()
        if target_state == SystemState.BAD_RF:
            self._ctx.bad_rf_next_action_at = now + max(
                0.0,
                float(hard.get("emergency_observe_s", self.state_cfg.emergency_observe_s)),
            )
            self._ctx.rf_cliff_event_at = now
            self._ctx.rf_cliff_event_reason = reason
        if emergency_class == "E_CONGESTION_STORM":
            self._ctx.soft_emergency_hold_until = now + float(
                soft.get("soft_emergency_hold_s", self.state_cfg.soft_emergency_hold_s)
            )
        else:
            self._ctx.soft_emergency_hold_until = 0.0
        if emergency_class in {"E_RADIO_DOWN", "E_SESSION_RESET"}:
            self._ctx.emergency_observe_until = now + float(
                hard.get("emergency_observe_s", self.state_cfg.emergency_observe_s)
            )
        else:
            self._ctx.emergency_observe_until = 0.0
        if emergency_class == "E_RADIO_DOWN":
            self._set_warning(
                warning_class="radio",
                warning_source="modem_link",
                warning_mechanism="radio_down",
                warning_reason=reason,
            )
        elif emergency_class in {"BAD_RF", "E_RF_COLLAPSE"}:
            self._set_warning(
                warning_class="rf_event",
                warning_source="radio_environment",
                warning_mechanism="rf_cliff",
                warning_reason=reason,
            )
        elif emergency_class == "E_SESSION_RESET":
            self._set_warning(
                warning_class="session",
                warning_source="modem_session",
                warning_mechanism="reset",
                warning_reason=reason,
            )
        elif emergency_class == "E_CONGESTION_STORM":
            self._set_warning(
                warning_class="congestion",
                warning_source="attached_cell",
                warning_mechanism="storm",
                warning_reason=reason,
            )
        self._reset_counters()
        return target_state

    def _rf_collapse_reason(self, modem: CellularIntelligenceGeneration, hard: dict) -> str:
        floor = float(hard.get("emergency_sinr_floor", self.state_cfg.emergency_sinr_floor))
        return (
            "corroborated RF collapse evidence: "
            f"SINR={modem.cellular.sinr_db:.1f}dB floor={floor:.1f}dB, "
            f"RSRP={modem.cellular.rsrp_dbm:.1f}dBm, "
            f"RSRQ={modem.cellular.rsrq_db:.1f}dB, "
            f"traffic={bool(modem.traffic.has_traffic)}"
        )

    # Legacy entry aliases
    def _enter_optimizing(self) -> SystemState:
        return self._enter_working()

    def _enter_exploring(self) -> SystemState:
        return self._enter_working()

    def _clear_warning(self) -> None:
        self._ctx.warning_class = ""
        self._ctx.warning_source = ""
        self._ctx.warning_mechanism = ""
        self._ctx.warning_reason = ""

    def _set_warning(
        self,
        *,
        warning_class: str,
        warning_source: str,
        warning_mechanism: str,
        warning_reason: str,
    ) -> None:
        self._ctx.warning_class = str(warning_class or "")
        self._ctx.warning_source = str(warning_source or "")
        self._ctx.warning_mechanism = str(warning_mechanism or "")
        self._ctx.warning_reason = str(warning_reason or "")

    def _transition(self, new_state: SystemState) -> None:
        old = self._ctx.state
        self._ctx.state = new_state
        if old != new_state:
            self._ctx.entered_at = time.time()
            self._ctx.normal_progress_receipt_ids.clear()
            log.info("State: %s -> %s", old.name, new_state.name)

    def _reset_counters(self) -> None:
        self._degraded_ticks = 0
        self._degraded_recovery_ticks = 0
        self._degraded_idle_ticks = 0
        self._confirmed_clear_ticks = 0
