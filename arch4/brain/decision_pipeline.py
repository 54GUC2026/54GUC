from arch4.controller_frames import DerivedSignalFrames
from arch4.policy.family_runtime_state import (
    export_routeros_rotate_state,
    restore_routeros_rotate_state,
)
from arch4.brain.ca_contract import ca_ld_stable, cart_ld_active, cart_ld_valid
"""
Decision pipeline/runtime mixin extracted from arch4/brain/decision.py.

── Seed 2: Dependency Boundary Audit ────────────────────────────────────────

The decision layer should depend ONLY on:
  - arch4/controller_frames.py (CellularIntelligenceGeneration, DerivedSignalFrames)
  - arch4/interfaces.py  (LeverAction, SystemState)
  - arch4/config_views.py (typed config views — no raw Arch4Config field reads)
  - arch4/control/band_resolver.py (resolve_band_intent — stable interface)
  - arch4/policy/* (policy helpers that accept plain dicts/dataclasses)
  - arch4/brain/blocker_authority.py (blocker guard chain)

The decision layer must NOT reach into:
  - arch4/modem/* (hardware abstraction layer)
  - arch4/engine.py (tick orchestrator)
  - arch4/ngui/* (presentation layer)
  - arch4/telemetry/* (logging/persistence)

Current state: boundaries are respected. All imports in this module are
from stable interfaces or policy helpers. The `decide()` method receives
typed inputs (CellularIntelligenceGeneration, DerivedSignalFrames) and emits a typed output
(Optional[LeverAction]) — no raw dict access or hardware calls.

──────────────────────────────────────────────────────────────────────────────

── Seed 1: Replay Determinism Contract ──────────────────────────────────────

The core decision path (decide() → lanes 1-4 → guards) aims for determinism:
  same CellularIntelligenceGeneration + DerivedSignalFrames + Memory state → same LeverAction output

Sources of non-determinism that are ACCEPTED (by design):
  - time.time() calls inside guards (band embargo, rate limits, cooldowns)
    These use wall-clock time to enforce durations. Replay tools should inject
    a fixed time via the model backend's tick timestamps to remove this.
  - Random tie-breaking in band selection (does not currently exist)
  - Night-training phase transitions (time-of-day gated)

Sources of non-determinism that would be BUGS:
  - Reading from external files during decide()
  - Any modem I/O inside the decision path
  - Mutable shared state modified by multiple threads during a tick

Replay contract (bounded, core path only):
  GUARANTEE: same input capture → same decisions
  - Same CellularIntelligenceGeneration sequence + same Memory state + fixed clock →
    same sequence of LeverAction outputs from lanes 1-4
  - This is a hard correctness contract for the core brain/policy path,
    not merely a convenience for analysis.
  - Validated by: test_engine_reporting.py determinism tests and
    the model harness in scripts/run_model_hardening_sweeps.py

  Clock injection for deterministic replay:
  - All wall-clock time.time() calls inside decide() and guards use `now`
    passed from the engine tick, NOT direct time.time() in the policy.
  - To achieve deterministic replay: inject a fixed monotonic clock
    by overriding the `now` parameter through the engine's tick timestamp.
  - The model backend produces timestamps from replay frame `t` fields,
    which makes clock-dependent guards (cooldowns, rate limits) deterministic.

── Seed 3: Decision-Order Precedence ────────────────────────────────────────

decide() executes exactly four lanes in this order. Earlier lanes have full
priority — if they return, later lanes do not run.

  Lane 1 — Preflight blockers (highest priority)
    ✓ Global rate limit / action interval enforcement
    Returns: None if blocked

  Lane 2 — Pre-state policy
    ✓ A1 night-freeze window enforcement
    ✓ A1 forced forced-schedule and morning-sequence override
    Returns: pre_action if handled (may be None = "handled, no action needed")

  Lane 3 — State dispatch
    ✓ Per-SystemState decision logic (WORKING, DEGRADED, CONGESTED, explicit fault states)
    ✓ Soft congestion detection
    Returns: proposed action or None

  Lane 4 — Action pipeline / transport checks
    Remaining guard chain is restricted to final profile/safety filters:
      1. layer3_action_trust   — reject impossible firmware actions
      2. a1_restricted_filter  — A1 band restriction enforcement
      3. learning_guard        — LTM/STM band learning suppression
      4. a1_antithrash         — A1 antithrash circuit breaker

  Generic holds and cooldowns must be resolved before owner selection.

──────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from typing import Any, Mapping, Optional, List

from arch4.interfaces import (
    SystemState,
    LeverAction,
    LeverType,
    fault_state_class,
    is_emergency_state,
    is_fault_state,
)
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.control.band_resolver import resolve_band_intent
from arch4.control.manual_queue import build_authority_lever_action
from arch4.brain.decision_action_format import (
    action_brief,
    band_sig_from_action,
    current_active_bands,
    objective_id_for_bands,
)
from arch4.brain.blocker_authority import classify_blockers, ordered_blocker_guard_chain
from arch4.brain.band_action_learning_guard import screen_band_action
from arch4.brain.congestion_signals import (
    authority_congestion_flags,
    canonical_congestion_flags,
    canonical_congestion_pct,
    canonical_rsrq_gap,
    congestion_rtt_delta_ready,
    congestion_rtt_ref_ms,
)
from arch4.brain.memory_objective_lifecycle_owner import (
    record_lifecycle_event as record_objective_lifecycle_event_to_memory,
)
from arch4.action_transaction import action_transaction_key_from_action
from arch4.signal_processing.policy_thresholds import policy_threshold_authority
from arch4.smart_loop.strategy_register import (
    SmartLoopStrategyUnavailable,
    TSAR_ARBITRATION_SCHEMA as SMART_LOOP_TSAR_ARBITRATION_SCHEMA,
    TSAR_PLAN_SCHEMA as SMART_LOOP_TSAR_PLAN_SCHEMA,
    publish_smart_loop_strategy_action_block_to_bank,
    read_smart_loop_strategy_from_bank,
    strategy_tsar_plan_projection,
)
from arch4.brain.tsar_action_shaping import (
    ca_flap_recovery_ca_pool_for_anchor,
    current_anchor_pool,
    largest_safe_ca_pool_for_anchor,
    preserved_ca_pool_for_pband_anchor,
    tsar_authority_band_list,
)
from arch4.smart_loop.closed_loop import OWNER as SELF_LEARNING_STRATEGY_OWNER
from arch4.smart_loop.closed_loop import SCHEMA as SELF_LEARNING_STRATEGY_SCHEMA
from arch4.smart_loop.closed_loop import SMART_LOOP_COMMAND_INTENT
from arch4.action_session_scope import (
    current_action_context_key,
    current_session_action_scope_from_generation,
)
from arch4.core.selected_action_register import (
    SelectedActionClaimContext,
)
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.customer_contracts.selected_action import SelectedActionContract
from arch4.smart_loop.action_availability import (
    OWNER as ACTION_AVAILABILITY_OWNER,
    SMART_LOOP_ROI_OBSERVATION_COOLDOWN,
    evaluate_action_availability,
)
from arch4.runtime_band_scope import (
    normalize_runtime_band_values,
    remembered_runtime_bands,
    resolve_runtime_available_bands,
)
from arch4.policy.family_status_policy import apply_family_event_texts, apply_family_status_aliases, build_family_runtime_events
from arch4.policy.health_action_policy import (
    health_action_hint,
    health_hold_reason,
    health_stage,
    should_hold_action_for_health,
)
from arch4.policy.incident_action_policy import (
    incident_class_name,
    incident_hold_reason,
    incident_next_step,
    incident_suppresses_exploration,
    should_hold_action_for_incident,
)
from arch4.policy.family_rotate_policy import family_rotation_has_confidence
from arch4.operator_profiles import FAMILY_HUAWEI_HILINK, FAMILY_ROUTEROS_AT
from arch4.policy.state_dispatch_policy import run_state_dispatch
from arch4.brain.tick_contract import (
    DecisionActionSummary,
    DecisionContext,
    DecisionResult,
    DecisionTrace,
    DecisionTraceStep,
)

class SmartLoopTSARPlanUnavailable(ValueError):
    """Raised when the smart-loop TSAR projection cannot be consumed."""


def _smart_loop_text(value: Any) -> str:
    return str(value or "").strip()


def _require_smart_loop_tsar_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(plan or {}) if isinstance(plan, Mapping) else {}
    if _smart_loop_text(row.get("schema")) != SMART_LOOP_TSAR_PLAN_SCHEMA:
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_plan_schema_mismatch")
    if _smart_loop_text(row.get("owner")) != SELF_LEARNING_STRATEGY_OWNER:
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_plan_owner_mismatch")
    if not _smart_loop_text(row.get("context_ref")):
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_context_ref_missing")
    arbitration = row.get("arbitration_outcome") if isinstance(row.get("arbitration_outcome"), Mapping) else {}
    if _smart_loop_text(arbitration.get("schema")) != SMART_LOOP_TSAR_ARBITRATION_SCHEMA:
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_arbitration_schema_mismatch")
    if _smart_loop_text(arbitration.get("owner")) != SELF_LEARNING_STRATEGY_OWNER:
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_arbitration_owner_mismatch")
    if not _smart_loop_text(arbitration.get("verdict")) or not _smart_loop_text(arbitration.get("reason_code")):
        raise SmartLoopTSARPlanUnavailable("smart_loop_tsar_arbitration_incomplete")
    return row

log = logging.getLogger("arch4.brain.decision")


class DecisionPipelineMixin:
    @staticmethod
    def _normalize_runtime_band_list(values) -> list[int]:
        return normalize_runtime_band_values(values)

    def _remembered_runtime_band_list(self) -> list[int]:
        return remembered_runtime_bands(getattr(self, "memory", None))

    def _resolve_runtime_available_bands(self, modem: CellularIntelligenceGeneration) -> set[int]:
        return set(
            resolve_runtime_available_bands(
                modem=modem,
                memory=getattr(self, "memory", None),
            )
        )

    def _tsar_authority_band_list(self) -> list[int]:
        return tsar_authority_band_list(memory=getattr(self, "memory", None), cfg=self.cfg)

    def _pipeline_policy_config(self):
        return getattr(self, "decision_pipeline_cfg", self.cfg)

    @staticmethod
    def _now() -> float:
        return time.time()

    def _clear_tick_decision_exports(self) -> None:
        self._decision_tick_exports_fresh = False
        self._clear_tsar_soft_no_go()
        self._constraint_gate_reason = ""
        self._prediction_gate_reason = ""
        self._crystal_ball_gate_reason = ""
        self._last_crystal_ball_advisory = {}

    def _mark_tick_decision_exports_fresh(self) -> None:
        self._decision_tick_exports_fresh = True

    def _tick_decision_exports_fresh(self) -> bool:
        return bool(getattr(self, "_decision_tick_exports_fresh", False))

    def _clear_tsar_soft_no_go(self) -> None:
        self._tsar_soft_no_go_verdict = ""
        self._tsar_soft_no_go_reason = ""
        self._tsar_soft_no_go_summary = ""

    def _note_tsar_soft_no_go(self, *, verdict: str, reason: str, summary: str) -> None:
        verdict_text = str(verdict or "").strip().lower()
        if verdict_text not in {"hold", "suppress"}:
            return
        if str(getattr(self, "_tsar_soft_no_go_verdict", "") or "").strip():
            return
        self._tsar_soft_no_go_verdict = verdict_text
        self._tsar_soft_no_go_reason = str(reason or "").strip()
        self._tsar_soft_no_go_summary = str(summary or "").strip()

    def _active(self, until_ts: float, now: Optional[float] = None) -> bool:
        ts = self._now() if now is None else float(now)
        return ts < float(until_ts or 0.0)

    def _count_active_until(self, mapping: dict, now: Optional[float] = None) -> int:
        ts = self._now() if now is None else float(now)
        return len([1 for _, until in (mapping or {}).items() if ts < float(until)])

    def _run_special_band_pipeline(
        self,
        *,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        state: SystemState,
        now: float,
        source: str,
    ) -> Optional[LeverAction]:
        """
        Unified pipeline for special sequence-like BAND_SELECTION sources.

        These paths should use objective lifecycle and common guards, while
        keeping emergency guard disabled (handled by special source policy).
        """
        action = self._apply_action_availability(
            action=action,
            modem=modem,
            state=state,
            now=now,
            source=source,
            check_objective=True,
            emit_trace=True,
        )
        if action is None:
            return None
        return self._run_action_pipeline(
            action=action,
            modem=modem,
            state=state,
            now=now,
            source=source,
            apply_emergency_guard=False,
        )

    def _apply_guard_step(
        self,
        *,
        steps: List[dict],
        gate: str,
        action: Optional[LeverAction],
        guard_fn,
    ) -> Optional[LeverAction]:
        """Apply one guard and append a normalized decision-trace step."""
        before = action
        action = guard_fn(action)
        steps.append(
            {
                "gate": gate,
                "result": "pass" if action is not None else "blocked",
                "changed": self._action_brief(before) != self._action_brief(action),
            }
        )
        if gate == "incident_hold" and action is None:
            reason = str(getattr(self, "_last_incident_hold_reason", "") or "")
            if reason:
                steps[-1]["reason"] = reason
        if gate == "health_hold" and action is None:
            reason = str(getattr(self, "_last_health_hold_reason", "") or "")
            if reason:
                steps[-1]["reason"] = reason
        return action

    @staticmethod
    def _tsar_reason_from_steps(
        steps: List[dict],
        *,
        primary_blocker_reason: str,
        primary_blocker_gate: str,
    ) -> str:
        if str(primary_blocker_reason or "").strip():
            return str(primary_blocker_reason or "").strip()
        for step in steps:
            if str(step.get("result", "") or "") == "blocked":
                reason = str(step.get("reason", "") or "").strip()
                if reason:
                    return reason
                gate = str(step.get("gate", "") or "").strip()
                if gate:
                    return gate
        return str(primary_blocker_gate or "").strip()

    def _classify_tsar_verdict(
        self,
        *,
        source: str,
        state: SystemState,
        proposed: Optional[LeverAction],
        final: Optional[LeverAction],
        steps: List[dict],
        primary_blocker_gate: str,
        primary_blocker_reason: str,
    ) -> tuple[str, str, str]:
        reason = self._tsar_reason_from_steps(
            steps,
            primary_blocker_reason=primary_blocker_reason,
            primary_blocker_gate=primary_blocker_gate,
        )
        if final is not None:
            intent = str(getattr(final, "intent", "") or "").strip().lower()
            action_reason = str(getattr(final, "reason", "") or "").strip()
            if intent.startswith("explore"):
                return "explore", action_reason or reason, "TSAR chose exploration."
            if (
                is_fault_state(state)
                or is_emergency_state(state)
                or any(token in intent for token in ("recover", "restore", "fault", "restart", "escape"))
            ):
                return "recover", action_reason or reason, "TSAR chose a recovery action."
            return "act", action_reason or reason, "TSAR chose an active control action."
        if proposed is not None:
            action_reason = str(getattr(proposed, "reason", "") or "").strip()
            return "suppress", reason or action_reason, "TSAR proposed an action but suppressed it this tick."
        soft_verdict = str(getattr(self, "_tsar_soft_no_go_verdict", "") or "").strip().lower()
        if soft_verdict in {"hold", "suppress"}:
            soft_reason = str(getattr(self, "_tsar_soft_no_go_reason", "") or "").strip()
            soft_summary = str(getattr(self, "_tsar_soft_no_go_summary", "") or "").strip()
            return soft_verdict, soft_reason, soft_summary or "TSAR explicitly deferred a soft lane this tick."
        if primary_blocker_gate or any(str(step.get("result", "") or "") == "blocked" for step in steps):
            return "hold", reason, "TSAR explicitly held action this tick."
        return "idle", "", "TSAR stayed idle because no action was warranted."

    def get_runtime_status(self) -> dict:
        """Small status snapshot for current_state export."""
        nt = self._night_train
        now = self._now()
        posture = self.posture_authority.current_posture
        action_frame = getattr(self, "_last_action_frame", None)
        context_obj = self.get_last_decision_context_object()
        trace_obj = self.get_last_decision_trace_object()
        result_obj = self.get_last_decision_result_object()
        trace_last = self._decision_trace_last if self._tick_decision_exports_fresh() else {}
        try:
            smart_loop_strategy_status = self._read_smart_loop_strategy(now=now)
        except SmartLoopStrategyUnavailable:
            smart_loop_strategy_status = {}
        source_status = self.action_source_state_authority.status(now=now)
        provider_status = dict(source_status.get("provider") or {})
        antithrash_status = dict(
            provider_status.get("antithrash") or {}
        )
        radio_drop_status = dict(
            provider_status.get("radio_drop") or {}
        )
        morning_status = dict(provider_status.get("morning") or {})
        radio_drop_hold_until = float(
            radio_drop_status.get("hold_until", 0.0) or 0.0
        )
        radio_drop_fields = {
            "hilink_radio_drop_active": bool(
                radio_drop_status.get("active", False)
            ),
            "hilink_radio_drop_hold_active": self._active(
                radio_drop_hold_until, now
            ),
            "hilink_radio_drop_remaining_s": max(
                0,
                int(round(radio_drop_hold_until - now)),
            ),
            "huawei_radio_drop_active": bool(
                radio_drop_status.get("active", False)
            ),
            "huawei_radio_drop_hold_active": self._active(
                radio_drop_hold_until, now
            ),
            "huawei_radio_drop_remaining_s": max(
                0,
                int(round(radio_drop_hold_until - now)),
            ),
            "family_radio_drop_active": bool(
                radio_drop_status.get("active", False)
            ),
            "family_radio_drop_hold_active": self._active(
                radio_drop_hold_until, now
            ),
            "family_radio_drop_remaining_s": max(
                0,
                int(round(radio_drop_hold_until - now)),
            ),
        }
        status = {
            "policy_profile": self.policy_profile.name if self.policy_profile else "",
            "policy_key": self.policy_profile.policy_key if self.policy_profile else "",
            "policy_family": self.policy_profile.policy_family if self.policy_profile else "",
            "threshold_authority": policy_threshold_authority(self.cfg),
            "normal_policy_safety": {
                "exploration_blocked": bool(
                    getattr(getattr(self, "_tick_safety", None), "exploration_blocked", False)
                ),
                "incident_gate": str(
                    getattr(getattr(self, "_tick_safety", None), "incident_gate", "") or ""
                ),
                "incident_class": str(
                    getattr(getattr(self, "_tick_safety", None), "incident_class", "") or ""
                ),
                "rf_cliff_hold": bool(
                    getattr(getattr(self, "_tick_safety", None), "rf_cliff_hold", False)
                ),
                "post_fault_hold": bool(
                    getattr(getattr(self, "_tick_safety", None), "post_fault_hold", False)
                ),
                "known_outage_hold": bool(
                    getattr(getattr(self, "_tick_safety", None), "known_outage_hold", False)
                ),
                "throughput_hold": bool(
                    getattr(getattr(self, "_tick_safety", None), "throughput_hold", False)
                ),
                "rf_tier": str(
                    getattr(getattr(self, "_tick_safety", None), "rf_tier", "") or ""
                ),
                "traffic_state": str(
                    getattr(getattr(self, "_tick_safety", None), "traffic_state", "") or ""
                ),
            },
            "exploration_pressure": {
                "pressure": round(
                    float(getattr(getattr(self, "_exploration_pressure", None), "pressure", 0.0) or 0.0),
                    6,
                ),
                "resistance": round(
                    float(getattr(getattr(self, "_exploration_pressure", None), "resistance", 0.0) or 0.0),
                    6,
                ),
                "net_pressure": round(
                    float(getattr(getattr(self, "_exploration_pressure", None), "net_pressure", 0.0) or 0.0),
                    6,
                ),
                "active_reasons": list(
                    getattr(getattr(self, "_exploration_pressure", None), "active_reasons", ()) or ()
                ),
            },
            # Systematic BAD_RF probing status (replaces clock-gated night training).
            "night_training_enabled": bool(nt.get("active", False)),
            "night_phase": nt.get("phase", "idle"),
            "night_active": bool(nt.get("active", False)),
            "night_candidate": (nt.get("current") or {}).get("bands"),
            "night_best": nt.get("best"),
            "emergency_class": getattr(self.posture_authority.current_posture, "emergency_class", ""),
            "emergency_origin_class": getattr(self.posture_authority.current_posture, "emergency_origin_class", ""),
            "emergency_reason": getattr(self.posture_authority.current_posture, "emergency_reason", ""),
            "emergency_recovery_step": int(posture.emergency_recovery_step),
            "bad_rf_recovery_step": int(posture.bad_rf_recovery_step),
            "fault_recovery_step": int(
                getattr(
                    posture,
                    "bad_rf_recovery_step" if self.posture_authority.state == SystemState.BAD_RF else "emergency_recovery_step",
                    0,
                ) or 0
            ),
            "emergency_cooldown_active": self.cooldowns.is_blocked("emergency", now=now),
            "post_fault_class": str(posture.post_fault_class or ""),
            "post_fault_active": self._active(posture.post_fault_until, now),
            "post_fault_remaining_s": max(
                0,
                int(round(float(posture.post_fault_until) - now)),
            ),
            "recovered_lane_active": self._active(posture.recovered_lane_until, now),
            "recovered_lane_bands": list(posture.recovered_lane_bands),
            "recovered_lane_from_fault": str(posture.recovered_lane_from_fault or ""),
            "recovered_lane_remaining_s": max(
                0,
                int(round(float(posture.recovered_lane_until) - now)),
            ),
            "a1_antithrash_hold_active": self._active(
                antithrash_status.get("hold_until", 0.0), now
            ),
            "a1_antithrash_freeze_bands": list(
                antithrash_status.get("freeze_bands", []) or []
            ),
            **radio_drop_fields,
            "a1_hard_night_freeze_active": bool(self._is_a1_hard_night_freeze_window(now)),
            "a1_morning_active": bool(morning_status.get("active", False)),
            "ca_lost_avg": float(self._ca_lost_avg),
            "ca_lost_on": bool(self._ca_lost_state),
            "ca_recovery_trend": dict(getattr(self, "_ca_recovery_trend_last", {}) or {}),
            "ca_flapping": dict(getattr(self, "_ca_flapping_status", {}) or {}),
            "anchor_flip_instability": dict(getattr(self, "_anchor_flip_status", {}) or {}),
            "calibration_handoff": dict(getattr(self, "_calibration_handoff", {}) or {}),
            "constraint_vector": dict(getattr(self, "_constraint_vector", {}) or {}),
            "self_learning_strategy": dict(smart_loop_strategy_status),
            "constraint_gate_reason": str(getattr(self, "_constraint_gate_reason", "") or ""),
            "prediction_gate_reason": str(getattr(self, "_prediction_gate_reason", "") or ""),
            "crystal_ball_gate_reason": str(getattr(self, "_crystal_ball_gate_reason", "") or ""),
            "crystal_ball_advisory": dict(getattr(self, "_last_crystal_ball_advisory", {}) or {}),
            "smart_loop_tsar_plan": self._tsar_arbitration_plan(),
            "ca_lost_eval_blocked": self.cooldowns.is_blocked("ca_lost_eval_block", now=now),
            "hilink_rotate_cooldown_active": self.cooldowns.is_blocked("hilink_rotate", now=now),
            "hilink_rotate_quiet_active": self._active(self._get_hilink_rotate_success_quiet_until(), now),
            "mikrotik_rotate_cooldown_active": self.cooldowns.is_blocked("mikrotik_rotate", now=now),
            "mikrotik_rotate_quiet_active": self._active(self._get_routeros_rotate_success_quiet_until(), now),
            "routeros_rotate_quiet_active": self._active(self._get_routeros_rotate_success_quiet_until(), now),
            "mikrotik_rotate_budget_used": len(self._mikrotik_rotate_attempt_times),
            "mikrotik_explore_retry_objectives": self.cooldowns.keyed_count("mikrotik_explore_retry"),
            "global_band_churn_cooldown_active": self._active(
                getattr(self, "_global_band_churn_cooldown_until", 0.0), now
            ),
            "global_band_actions_recent": len(getattr(self, "_global_band_action_times", []) or []),
            "congestion_mitigation_cooldown_active": self.cooldowns.is_blocked("congestion_mitigation", now=now),
            "congestion_kind": (
                context_obj.congestion_kind
                if context_obj is not None
                else getattr(self.posture_authority.current_posture, "congestion_kind", "")
            ),
            "warning_class": (
                context_obj.warning_class
                if context_obj is not None
                else getattr(self.posture_authority.current_posture, "warning_class", "")
            ),
            "warning_source": (
                context_obj.warning_source
                if context_obj is not None
                else getattr(self.posture_authority.current_posture, "warning_source", "")
            ),
            "warning_mechanism": (
                context_obj.warning_mechanism
                if context_obj is not None
                else getattr(self.posture_authority.current_posture, "warning_mechanism", "")
            ),
            "warning_reason": (
                context_obj.warning_reason
                if context_obj is not None
                else getattr(self.posture_authority.current_posture, "warning_reason", "")
            ),
            "soft_congestion_active": bool(self._soft_congestion_active),
            "soft_congestion_ticks": int(self._soft_congestion_ticks),
            "soft_congestion_clear_ticks": int(self._soft_congestion_clear_ticks),
            "cell_lock_retry_objectives": self.cooldowns.keyed_count("cell_lock_retry"),
            "restart_retry_objectives": self.cooldowns.keyed_count("restart_retry"),
            "cell_locked": bool(getattr(action_frame, "cell_locked", False)),
            "cell_lock_pci": int(getattr(action_frame, "cell_lock_pci", 0) or 0),
            "cell_lock_earfcn": int(
                getattr(action_frame, "cell_lock_earfcn", 0) or 0
            ),
            "cell_lock_band": 0,
            "decision_trace_source": (
                trace_obj.source
                if trace_obj is not None
                else trace_last.get("source", "")
            ),
            "decision_trace_final": (
                trace_obj.final
                if trace_obj is not None
                else trace_last.get("final", "")
            ),
            "decision_trace_passed": (
                int(trace_obj.passed)
                if trace_obj is not None
                else int(trace_last.get("passed", 0))
            ),
            "decision_trace_blocked": (
                int(trace_obj.blocked)
                if trace_obj is not None
                else int(trace_last.get("blocked", 0))
            ),
            "decision_trace_id": (
                trace_obj.decision_trace_id
                if trace_obj is not None
                else trace_last.get("decision_trace_id", "")
            ),
            "objective_feedback_active": self._count_active_until(getattr(self, "_objective_feedback_until", {}), now),
            "radio_stability_hold_active": bool(
                source_status.get("band_action_policy", {}).get(
                    "radio_stability_hold_active", False
                )
            ),
            "radio_stability_hold_remaining_s": max(
                0,
                int(
                    round(
                        float(
                            source_status.get("band_action_policy", {}).get(
                                "radio_stability_hold_remaining_s", 0.0
                            )
                            or 0.0
                        )
                    )
                ),
            ),
            "radio_stability_hold_target": str(
                source_status.get("band_action_policy", {}).get(
                    "radio_stability_after_signature", ""
                )
                or ""
            ),
            "smart_loop_roi_observation_active": self.cooldowns.is_blocked(
                SMART_LOOP_ROI_OBSERVATION_COOLDOWN,
                now=now,
            ),
            "smart_loop_roi_observation_remaining_s": max(
                0,
                int(round(self.cooldowns.remaining_s(SMART_LOOP_ROI_OBSERVATION_COOLDOWN, now=now))),
            ),
            "smart_loop_roi_observation_target": str(
                getattr(self, "_smart_loop_roi_observation_target_sig", "") or ""
            ),
            "health_stage": (
                context_obj.health_stage
                if context_obj is not None
                else health_stage(getattr(self, "_health_status", {}))
            ),
            "health_action_hint": (
                context_obj.health_action_hint
                if context_obj is not None
                else health_action_hint(getattr(self, "_health_status", {}))
            ),
            "incident_class": (
                context_obj.incident_class
                if context_obj is not None
                else incident_class_name(getattr(self, "_incident_assessment", {}))
            ),
            "incident_next_step": (
                context_obj.incident_next_step
                if context_obj is not None
                else incident_next_step(getattr(self, "_incident_assessment", {}))
            ),
            "incident_suppress_exploration": (
                bool(context_obj.incident_suppress_exploration)
                if context_obj is not None
                else incident_suppresses_exploration(getattr(self, "_incident_assessment", {}))
            ),
        }
        if trace_obj is not None:
            status["decision_trace"] = trace_obj.to_dict()
            status["decision_proof"] = trace_obj.to_compact_proof()
        if result_obj is not None:
            status["decision_result"] = result_obj.to_dict()
        if context_obj is not None:
            status["decision_context"] = context_obj.to_dict()
        if nt.get("current"):
            cur = nt["current"]
            status["night_probe_samples"] = cur.get("samples", 0)
        return status

    def drain_runtime_events(self) -> List[dict]:
        events = self._runtime_events[:]
        self._runtime_events.clear()
        return events

    def get_decision_cooldown_state(self) -> dict:
        """Serialize Decision-owned cooldown and rotation state."""
        out = self.cooldowns.save_state_legacy()
        out["band_rotation_state"] = {
            "checked": bool(self._band_rotation_checked),
            "target": int(self._band_rotation_target),
            "attempted_at": float(self._band_rotation_time),
        }

        rotate_state = export_routeros_rotate_state(self)
        out["routeros_rotate_state"] = dict(rotate_state)
        out["mikrotik_rotate_state"] = dict(rotate_state)
        out["family_rotate_state"] = dict(rotate_state)

        return out

    def restore_decision_cooldown_state(self, state: dict) -> None:
        """Restore Decision-owned cooldown and rotation state."""
        if not isinstance(state, dict):
            return
        self.cooldowns.restore_state_legacy(state)
        rotation = state.get("band_rotation_state")
        if isinstance(rotation, dict):
            self._band_rotation_checked = bool(
                rotation.get("checked", True)
            )
            self._band_rotation_target = int(
                rotation.get("target", 0) or 0
            )
            self._band_rotation_time = float(
                rotation.get("attempted_at", 0.0) or 0.0
            )

        rotate_state = state.get("routeros_rotate_state")
        if not isinstance(rotate_state, dict):
            rotate_state = state.get("mikrotik_rotate_state")
        if not isinstance(rotate_state, dict):
            rotate_state = state.get("family_rotate_state")
        if isinstance(rotate_state, dict):
            restore_routeros_rotate_state(self, rotate_state)

    def get_last_decision_trace(self) -> dict:
        """Return a copy of the most recent decision trace metadata."""
        if not self._tick_decision_exports_fresh():
            return {}
        return dict(self._decision_trace_last or {})

    def get_last_decision_trace_object(self) -> Optional[DecisionTrace]:
        """Return the most recent typed decision trace object, if available."""
        if not self._tick_decision_exports_fresh():
            return None
        return self._decision_trace_last_obj

    def get_last_decision_result_object(self) -> Optional[DecisionResult]:
        """Return the most recent typed decision result object, if available."""
        if not self._tick_decision_exports_fresh():
            return None
        return self._decision_result_last_obj

    def get_last_decision_context_object(self) -> Optional[DecisionContext]:
        """Return the most recent typed decision context object, if available."""
        if not self._tick_decision_exports_fresh():
            return None
        return self._decision_context_last_obj

    def get_outage_window_active(self) -> bool:
        """Whether a known outage window is currently suppressing exploration."""
        safety = getattr(self, "_tick_safety", None)
        return bool(getattr(safety, "known_outage_hold", False)) if safety else False

    def note_health_status(self, health_status: Optional[dict]) -> None:
        self._health_status = dict(health_status or {})

    def note_incident_assessment(self, assessment: Optional[dict]) -> None:
        self._incident_assessment = dict(assessment or {})

    def _lane_constraint_vector_blockers(
        self,
        *,
        modem: Optional[CellularIntelligenceGeneration],
        state: SystemState,
    ) -> bool:
        constraint = getattr(self, "_constraint_vector", None)
        if not isinstance(constraint, dict):
            return False
        supports_active_control = bool(constraint.get("supports_active_control", True))
        status = str(constraint.get("status", "") or "").strip().lower()
        observe_only = bool(constraint.get("observe_only", False))
        planned_quiet_active = bool(constraint.get("planned_radio_quiet_active", False))
        if supports_active_control or is_fault_state(state) or is_emergency_state(state):
            self._constraint_gate_reason = ""
            return False

        if observe_only:
            reason = "observe_only_mode"
            remaining_s = 0
        elif planned_quiet_active:
            reason = "planned_radio_quiet"
            remaining_s = int(round(float(constraint.get("planned_radio_quiet_remaining_s", 0.0) or 0.0)))
        else:
            reason = "constraint_vector"
            remaining_s = 0
        self._constraint_gate_reason = reason
        step = {
            "gate": "constraint_vector",
            "result": "blocked",
            "reason": reason,
            "status": status or "restricted",
        }
        if remaining_s > 0:
            step["remaining_s"] = remaining_s
        self._emit_decision_trace(
            source="constraint_vector",
            state=state,
            proposed=None,
            final=None,
            modem=modem,
            steps=[step],
        )
        return True

    def _guard_constraint_vector_action(
        self,
        *,
        action: Optional[LeverAction],
        modem: Optional[CellularIntelligenceGeneration],
        state: SystemState,
        source: str,
    ) -> Optional[LeverAction]:
        raise RuntimeError("old_constraint_vector_action_guard_deleted")

    def _emit_runtime_event(self, kind: str, **payload) -> None:
        payload.setdefault("event_ts", time.time())
        self._runtime_events.extend(build_family_runtime_events({"kind": kind, **payload}))

    def _record_objective_lifecycle_event(
        self,
        *,
        stage: str,
        status: str,
        action: Optional[LeverAction] = None,
        objective_id: str = "",
        reason: str = "",
        until_ts: float = 0.0,
        decision_trace_id: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        try:
            if action is not None:
                lever = action.lever.name
                intent = action.intent or ""
                oid = str(objective_id or action.objective_id or "")
            else:
                lever = ""
                intent = ""
                oid = str(objective_id or "")
            record_objective_lifecycle_event_to_memory(
                self.memory,
                stage=stage,
                status=status,
                objective_id=oid,
                lever=lever,
                intent=intent,
                reason=reason,
                decision_trace_id=str(decision_trace_id or ""),
                until_ts=float(until_ts or 0.0),
                metadata=dict(metadata or {}),
                provider_plmn=getattr(self.constraints, "provider_plmn", ""),
                provider_name=getattr(self.constraints, "provider_name", ""),
            )
        except Exception as exc:
            log.debug("unknown: suppressed %s", exc)
            pass

    def decide(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> Optional[LeverAction]:
        """
        Main decision function. Called once per tick.
        
        Returns a LeverAction if the brain wants to act, None otherwise.
        """
        if not isinstance(modem, CellularIntelligenceGeneration):
            raise TypeError("decision_requires_cellular_intelligence_generation")

        state = self.posture_authority.state
        now = time.time()
        self._last_action_frame = modem.action
        self._clear_tick_decision_exports()
        self.deployed_bands = self._resolve_runtime_available_bands(modem)

        # ── Tick safety evaluation (once per tick) ────────────────────
        # Evaluated here, consumed by all exploration lanes. Replaces the
        # pattern where exploration holds were re-evaluated once per lane.
        from arch4.brain.tick_safety import evaluate_tick_safety
        self._tick_safety = evaluate_tick_safety(
            modem=modem, computed=computed, engine=self, now=now,
        )

        # ── RF quality evaluation (once per tick) ─────────────────────
        # Evaluated here, consumed by all decision lanes that need RF gates.
        # Replaces scattered inline checks like `modem.sinr_db >= threshold`.
        from arch4.brain.rf_quality_gate import (
            evaluate_rf_quality,
            build_rf_quality_config_from_engine,
        )
        self._rf_quality = evaluate_rf_quality(
            modem=modem,
            cfg=build_rf_quality_config_from_engine(self),
        )

        # ── Traffic evaluation (once per tick) ────────────────────────
        # Evaluated here, consumed by all decision lanes that need traffic gates.
        # Replaces scattered inline checks like `modem.has_traffic`.
        from arch4.brain.traffic_gate import (
            evaluate_traffic,
            build_traffic_config_from_engine,
        )
        self._traffic = evaluate_traffic(
            modem=modem,
            cfg=build_traffic_config_from_engine(self),
        )

        # Lane 1: hard block lane (global rate limit).
        blocked = self._lane_preflight_blockers(modem=modem, state=state, now=now)
        if blocked:
            return None

        # Lane 1b: explicit constraint / authority lane.
        blocked = self._lane_constraint_vector_blockers(modem=modem, state=state)
        if blocked:
            return None

        # ── State tracking (always runs — keeps internal model fresh) ─
        self._update_ca_lost_signal(modem, computed, now)
        try:
            action_settle_active = bool(
                now - float(getattr(self, "_last_action_time", 0.0) or 0.0) < 60.0
            )
            planned_quiet_active = bool(
                (getattr(self, "_constraint_vector", {}) or {}).get(
                    "planned_radio_quiet_active", False
                )
            )
            traffic = getattr(self, "_traffic", None)
            active_load_mbps = 0.0
            traffic_active_for_ca = bool(cart_ld_valid(computed))
            if traffic is not None:
                active_load_mbps = float(getattr(traffic, "throughput_dl_mbps", 0.0) or 0.0)
            self._ca_flapping_status = self._ca_flap_monitor.observe(
                modem=modem,
                state=state,
                now=now,
                planned_quiet_active=planned_quiet_active,
                action_settle_active=action_settle_active,
                traffic_active=traffic_active_for_ca,
                active_load_mbps=active_load_mbps,
            )
        except Exception as exc:
            log.debug("ca flapping monitor suppressed: %s", exc)
            self._ca_flapping_status = {}
        try:
            self._anchor_flip_status = self._anchor_flip_monitor.observe(
                modem=modem,
                computed=computed,
                state=state,
                now=now,
                ca_recovery_trend=dict(getattr(self, "_ca_recovery_trend_last", {}) or {}),
            )
        except Exception as exc:
            log.debug("anchor flip monitor suppressed: %s", exc)
            self._anchor_flip_status = {}
        self._update_soft_congestion(modem, computed)

        # ── Stability keyframe ────────────────────────────────────────
        # Snapshot decision-relevant signals. When consecutive ticks match,
        # lanes 2-4 cannot produce a different result — skip them.
        # Cap: at most 4 consecutive skips (8s at 2s tick) before forcing
        # a full run. This catches internal tick-count-dependent state
        # (escape probe SINR ticks, NR lock ticks) without coupling to it.
        state_cfg = self._state_policy_config()
        pipeline_cfg = self._pipeline_policy_config()
        rtt_warning = float(state_cfg.rtt_warning_ms)
        rtt_cliff = float(getattr(pipeline_cfg, "rtt_cliff_ms", getattr(self.cfg, "rtt_cliff_ms", 400.0)))
        rtt_avg = computed.latency.rtt_sliding_avg
        rtt_bucket = (
            "alarm" if rtt_avg >= rtt_cliff else
            "warning" if rtt_avg >= rtt_warning else
            "normal"
        )
        ep = getattr(self, "_exploration_pressure", None)
        keyframe = (
            state,
            int(modem.cellular.primary_band),
            rtt_bucket,
            bool(ep.should_probe()) if ep else False,
            bool(modem.traffic.has_traffic),
            time.localtime(now).tm_hour,
            bool(self._soft_congestion_active),
            bool(self._ca_lost_state),          # filtered CA signal (sliding window)
            bool((getattr(self, "_ca_flapping_status", {}) or {}).get("active")),
            self.cooldowns.active_count(now=now),
            self._self_learning_candidate_keyframe_signature(now=now),
            # Internal tick counters that accumulate across ticks
            int(getattr(self, "_escape_probe_rf_cliff_good_ticks", 0) or 0),
            int(getattr(self, "_nr_lock_bad_sinr_ticks", 0) or 0),
        )
        _skip_limit = 4
        if (
            self._stability_keyframe is not None
            and keyframe == self._stability_keyframe
            and self._stability_skip_count < _skip_limit
        ):
            self._stability_skip_count += 1
            self._emit_no_action_trace(
                source="stability_keyframe",
                state=state,
                modem=modem,
                gate="stability_keyframe",
                reason="decision_inputs_unchanged",
                skip_count=int(self._stability_skip_count),
                skip_limit=int(_skip_limit),
            )
            return None
        self._stability_keyframe = keyframe
        self._stability_skip_count = 0

        # Normal autonomous action ownership now belongs to SmartLoop only.
        # Old state/NR/cell lanes may publish facts elsewhere, but they must
        # not directly choose radio mutations after the owner cutover. Fault
        # states keep their emergency recovery lane because that is safety
        # containment, not a self-learning candidate path.
        if is_fault_state(state):
            handled, pre_action = self._lane_pre_state_policy(modem=modem, state=state, now=now)
            if handled:
                return pre_action
            action = self._lane_state_dispatch(modem=modem, computed=computed, state=state, now=now)
        else:
            action = self._evaluate_tsar_vector_candidate(modem, computed, state=state, now=now)

        if action is None:
            emitted = self._emit_brain_posture_trace_if_deferred(
                state=state,
                modem=modem,
            )
            if not emitted:
                self._flag_unmaterialized_tsar_candidate_contract_violation(state=state)
                self._emit_no_action_trace(
                    source="decision_cycle",
                    state=state,
                    modem=modem,
                    gate=self._no_action_trace_gate(),
                    reason=self._no_action_trace_reason(),
                    blocked=self._no_action_trace_blocked(),
                    **self._tsar_candidate_plan_trace_fields(),
                )
            return None

        # Lane 4: final arbiter lane (objective lifecycle + guards + trace).
        action_params = dict(getattr(action, "params", {}) or {})
        is_selected_action = bool(
            action_params.get("_selected_action_command")
        )
        if not is_fault_state(state) and not is_selected_action:
            raise RuntimeError("old_non_fault_autonomous_action_owner_deleted")
        source = (
            "brain_selected_action"
            if is_selected_action
            else f"state:{state.name}"
        )
        action = self._apply_action_availability(
            action=action,
            modem=modem,
            state=state,
            now=now,
            source=source,
            check_objective=not is_selected_action,
            emit_trace=True,
        )
        if action is None:
            return None

        result = self._run_action_pipeline(
            action=action,
            modem=modem,
            state=state,
            now=now,
            source=source,
            apply_emergency_guard=is_fault_state(state),
        )

        # If an action was produced, invalidate the keyframe so the next tick
        # runs the full pipeline to evaluate the post-action world.
        if result is not None:
            self._stability_keyframe = None

        return result

    def _read_smart_loop_strategy(self, *, now: float) -> dict[str, Any]:
        feed = getattr(self, "_smart_loop_strategy_feed_port", None)
        if feed is None:
            raise SmartLoopStrategyUnavailable("smart_loop_strategy_feed_port_unavailable")
        return read_smart_loop_strategy_from_bank(feed, now=now)

    def _action_transaction_authority_port(self) -> ActionTransactionAuthority:
        authority = getattr(self, "action_transaction_authority", None)
        if not isinstance(authority, ActionTransactionAuthority):
            raise SmartLoopStrategyUnavailable("action_transaction_authority_unavailable")
        return authority

    def _reject_selected_action(
        self,
        action: Optional[LeverAction],
        *,
        reason: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        params = dict(getattr(action, "params", {}) or {}) if action is not None else {}
        command_id = str(params.get("_selected_action_command_id") or "")
        if not command_id:
            return
        authority = self._action_transaction_authority_port()
        authority.reject_selection(
            command_id=command_id,
            reason=str(reason or "decision_pipeline_rejected"),
            receipt=dict(receipt or {}),
            now=time.time(),
        )

    def _self_learning_candidate_keyframe_signature(self, *, now: float) -> tuple:
        """Stable signature for the canonical smart-loop strategy input.

        The stability shortcut may only skip decision lanes when every
        decision input is unchanged. The clean smart-loop strategy is now the
        arbitration input, so the keyframe includes its canonical register
        signature instead of treating RF state as the whole world.
        """

        try:
            publication = self._read_smart_loop_strategy(now=now)
        except SmartLoopStrategyUnavailable as exc:
            return ("unavailable", str(exc or ""))
        counts = publication.get("candidate_counts") if isinstance(publication.get("candidate_counts"), dict) else {}
        candidates = publication.get("candidates") if isinstance(publication.get("candidates"), list) else []
        candidate_sig = []
        for candidate in candidates[:8]:
            if not isinstance(candidate, dict):
                continue
            candidate_sig.append((
                str(candidate.get("source", "") or "smart_loop"),
                str(candidate.get("action", "") or ""),
                self._safe_int(candidate.get("band", candidate.get("target_band", 0))),
                tuple(self._normalize_runtime_band_list(candidate.get("bands") or [])),
                tuple(self._normalize_runtime_band_list(candidate.get("target_bands") or [])),
                round(self._safe_float(candidate.get("confidence", 0.0)), 3),
                self._safe_int(candidate.get("evidence_count", 0)),
                str(candidate.get("candidate_class", "") or ""),
                str(candidate.get("reason_ref", "") or ""),
            ))
        try:
            command_status = self._action_transaction_authority_port().status()
        except SmartLoopStrategyUnavailable as exc:
            command_signature = ("unavailable", str(exc or ""))
        else:
            active_command = command_status.get("active") if isinstance(command_status.get("active"), dict) else {}
            command_signature = (
                int(command_status.get("sequence", 0) or 0),
                str(active_command.get("command_id") or ""),
                str(active_command.get("stage") or ""),
            )
        return (
            str(publication.get("status", "") or ""),
            str(publication.get("reason", "") or ""),
            str(publication.get("required_next_step", "") or ""),
            self._safe_int(counts.get("total", 0) if isinstance(counts, dict) else 0),
            self._safe_int(counts.get("operational", 0) if isinstance(counts, dict) else 0),
            self._safe_int(counts.get("actionable", 0) if isinstance(counts, dict) else 0),
            self._safe_int(counts.get("advisory", 0) if isinstance(counts, dict) else 0),
            tuple(candidate_sig),
            command_signature,
        )

    @staticmethod
    def _safe_int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _evaluate_tsar_vector_candidate(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        state: SystemState,
        now: float,
    ) -> Optional[LeverAction]:
        """Idle-lane TSAR candidate consumer.

        TSAR considers every vector candidate each tick, then proposes at most
        one eligible radio action through the normal action pipeline.
        """

        _ = computed

        try:
            strategy = self._read_smart_loop_strategy(now=now)
        except SmartLoopStrategyUnavailable:
            strategy = {}

        def _block_for_strategy_owner(reason_key: str, *, strategy_reason: str) -> None:
            self._emit_runtime_event(
                "tsar_vector_candidate_deferred",
                reason=reason_key,
                strategy_reason=strategy_reason,
                strategy_required_next_step=str(strategy.get("required_next_step") or ""),
            )

        def _selected_clock_action() -> Optional[LeverAction]:
            try:
                action_scope = current_session_action_scope_from_generation(
                    modem,
                    now=now,
                )
                transaction = self._action_transaction_authority_port().claim(
                    consumer="arch4.brain.decision_pipeline",
                    claim_context=SelectedActionClaimContext(
                        action_scope=action_scope,
                        context_key=current_action_context_key(
                            modem,
                            scope=action_scope,
                        ),
                    ),
                    now=now,
                )
            except Exception as exc:
                _block_for_strategy_owner(
                    "action_transaction_authority_fault",
                    strategy_reason=str(exc or "action_transaction_authority_fault"),
                )
                return None
            selected_action = (
                dict(transaction.selected_action)
                if transaction is not None
                else {}
            )
            if not selected_action:
                return None
            command_id = str(selected_action.get("command_id") or "")
            try:
                materialized = SelectedActionContract.from_mapping(selected_action)
                action = materialized.to_lever_action()
            except Exception as exc:
                reason = (
                    "brain_selected_action_reconstruction_failed:"
                    + type(exc).__name__
                )
                _block_for_strategy_owner(
                    "brain_selected_action_invalid",
                    strategy_reason=reason,
                )
                self._action_transaction_authority_port().reject_selection(
                    command_id=command_id,
                    reason=reason,
                    now=now,
                )
                return None
            action.params["_smart_loop_strategy_outcome"] = str(
                strategy.get("outcome") or ""
            )
            return action

        strategy_seen = bool(strategy)
        strategy_valid = (
            str(strategy.get("schema") or "") == SELF_LEARNING_STRATEGY_SCHEMA
            and str(strategy.get("owner") or "") == SELF_LEARNING_STRATEGY_OWNER
        )
        strategy_outcome = str(strategy.get("outcome") or "")
        if not strategy_seen:
            self._emit_runtime_event(
                "smart_loop_strategy_unavailable",
                state=str(getattr(state, "name", state)),
                reason="smart_loop_strategy_missing",
                strategy_reason="smart_loop_strategy_register_missing",
                required_next_step="publish_smart_loop_strategy_register",
            )
            return None
        if strategy_seen and not strategy_valid:
            _block_for_strategy_owner(
                "self_learning_strategy_invalid",
                strategy_reason=str(strategy.get("reason") or "self_learning_strategy_contract_invalid"),
            )
            return None
        if strategy_valid:
            action = _selected_clock_action()
            if action is not None:
                return action
            if strategy_outcome != "action_selected_execution_pending":
                return None
            missing_reason = str(
                strategy.get("reason_code")
                or strategy_outcome
                or "selected_action_missing"
            )
            _block_for_strategy_owner(
                "brain_selected_action_missing",
                strategy_reason=missing_reason,
            )
            raise RuntimeError(
                "brain_selected_action_missing_for_selected_strategy:"
                + missing_reason
            )
        raise RuntimeError("old_self_learning_candidate_action_path_deleted")

    def _lane_preflight_blockers(
        self,
        *,
        modem: Optional[CellularIntelligenceGeneration] = None,
        state: SystemState,
        now: float,
    ) -> bool:
        # Rate limit: don't act faster than min_action_interval
        cfg = self._pipeline_policy_config()
        min_action_interval_s = float(cfg.min_action_interval_s)
        if now - self._last_action_time < min_action_interval_s:
            remaining_s = max(0.0, min_action_interval_s - (now - self._last_action_time))
            self._emit_decision_trace(
                source="rate_limit",
                state=state,
                proposed=None,
                final=None,
                modem=modem,
                steps=[{
                    "gate": "min_action_interval",
                    "result": "blocked",
                    "remaining_s": int(round(remaining_s)),
                }],
            )
            return True
        return False

    def _lane_pre_state_policy(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        state: SystemState,
        now: float,
    ) -> tuple[bool, Optional[LeverAction]]:
        del modem, state, now
        return False, None

    def _lane_state_dispatch(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        state: SystemState,
        now: float,
    ) -> Optional[LeverAction]:
        if not is_fault_state(state):
            raise RuntimeError("old_state_dispatch_autonomous_action_path_deleted")
        return run_state_dispatch(self, modem=modem, computed=computed, state=state, now=now)

    def _emit_brain_posture_trace_if_deferred(
        self,
        *,
        state: SystemState,
        modem: Optional[CellularIntelligenceGeneration],
    ) -> bool:
        verdict = str(getattr(self, "_tsar_soft_no_go_verdict", "") or "").strip().lower()
        if verdict not in {"hold", "suppress"}:
            return False
        reason = str(getattr(self, "_tsar_soft_no_go_reason", "") or "").strip() or "brain_posture"
        self._emit_decision_trace(
            source="brain_posture",
            state=state,
            proposed=None,
            final=None,
            modem=modem,
            steps=[{
                "gate": "brain_posture",
                "result": "blocked",
                "reason": reason,
                "verdict": verdict,
            }],
        )
        return True

    def _emit_no_action_trace(
        self,
        *,
        source: str,
        state: SystemState,
        modem: Optional[CellularIntelligenceGeneration],
        gate: str,
        reason: str,
        blocked: bool = False,
        **fields: object,
    ) -> None:
        """Emit the mandatory per-cycle Brain receipt when no action is selected."""

        step = {
            "gate": str(gate or "no_action"),
            "result": "blocked" if blocked else "pass",
            "reason": str(reason or "no_action_warranted"),
        }
        merged_fields = dict(self._tsar_candidate_plan_trace_fields())
        merged_fields.update(fields)
        for key, value in merged_fields.items():
            if value in (None, ""):
                continue
            step[str(key)] = value
        self._emit_decision_trace(
            source=str(source or "no_action"),
            state=state,
            proposed=None,
            final=None,
            modem=modem,
            steps=[step],
        )

    def _hold_tsar_selected_candidate_plan(
        self,
        plan: dict,
        selected: dict,
        *,
        state: SystemState,
        reason: str,
        candidate_hold_reason: str = "",
        target_bands: Optional[List[int]] = None,
        target_sig: str = "",
        event_kind: str = "tsar_vector_candidate_deferred",
    ) -> None:
        """Convert a selected TSAR plan into an explicit materialization hold."""

        held = dict(selected or {})
        held["brain_hold_reason"] = str(
            candidate_hold_reason or reason or "tsar_candidate_materialization_hold"
        )
        if target_bands is not None:
            held["target_bands"] = list(target_bands)
        if target_sig:
            held["target_signature"] = str(target_sig)
        self._emit_runtime_event(
            event_kind,
            state=str(getattr(state, "name", state)),
            bands=list(target_bands or held.get("target_bands") or held.get("bands") or []),
            source=str(held.get("source", "") or ""),
            candidate_action=str(held.get("action", "") or ""),
            reason=str(reason or held["brain_hold_reason"]),
            score=self._safe_float(held.get("score", 0.0)),
            confidence=self._safe_float(held.get("confidence", 0.0)),
            target_signature=str(target_sig or held.get("target_signature", "") or ""),
        )

    def _flag_unmaterialized_tsar_candidate_contract_violation(self, *, state: SystemState) -> None:
        plan = self._tsar_arbitration_plan()
        if not plan:
            return
        if str(plan.get("status", "") or "") != "selected":
            return
        selected = plan.get("selected") if isinstance(plan.get("selected"), dict) else {}
        self._hold_tsar_selected_candidate_plan(
            plan,
            selected,
            state=state,
            reason="tsar_candidate_materialization_contract_violation",
            target_bands=(
                selected.get("target_bands")
                if isinstance(selected.get("target_bands"), list)
                else selected.get("bands")
                if isinstance(selected.get("bands"), list)
                else [selected.get("band")]
                if selected.get("band") not in (None, "")
                else []
            ),
            event_kind="tsar_vector_candidate_contract_violation",
        )

    def _tsar_arbitration_plan(self) -> dict:
        try:
            strategy = self._read_smart_loop_strategy(now=time.time())
            return _require_smart_loop_tsar_plan(strategy_tsar_plan_projection(strategy))
        except (SmartLoopStrategyUnavailable, SmartLoopTSARPlanUnavailable):
            return {}

    def _tsar_candidate_plan_trace_fields(self) -> dict[str, object]:
        plan = self._tsar_arbitration_plan()
        if not plan:
            return {}
        try:
            strategy = self._read_smart_loop_strategy(now=time.time())
        except SmartLoopStrategyUnavailable:
            strategy = {}
        counts = strategy.get("candidate_counts") if isinstance(strategy.get("candidate_counts"), dict) else {}
        selected = plan.get("selected") if isinstance(plan.get("selected"), dict) else {}
        held = plan.get("held_candidate") if isinstance(plan.get("held_candidate"), dict) else {}
        candidate = selected or held
        arbitration = (
            plan.get("arbitration_outcome")
            if isinstance(plan.get("arbitration_outcome"), dict)
            else {}
        )
        return {
            "tsar_plan_status": str(arbitration.get("status", "") or ""),
            "tsar_plan_reason": str(arbitration.get("reason", "") or ""),
            "tsar_arbitration_verdict": str(arbitration.get("verdict", "") or ""),
            "tsar_arbitration_required_next_step": str(
                arbitration.get("required_next_step", "") or ""
            ),
            "tsar_arbitration_no_action_reason": str(
                arbitration.get("no_action_reason", "") or ""
            ),
            "tsar_considered_count": len(plan.get("considered") or []),
            "tsar_deferred_count": len(plan.get("deferred") or []),
            "smart_loop_strategy_status": str(strategy.get("outcome", "") or ""),
            "smart_loop_strategy_reason": str(strategy.get("reason", "") or strategy.get("reason_code", "") or ""),
            "smart_loop_strategy_required_next_step": str(strategy.get("required_next_step", "") or ""),
            "smart_loop_candidate_total": int(counts.get("total", 0) or 0),
            "smart_loop_candidate_actionable": int(counts.get("actionable", counts.get("operational", 0)) or 0),
            "candidate_action": str(candidate.get("action", "") or ""),
            "candidate_source": str(candidate.get("source", "") or ""),
            "candidate_band": self._safe_int(candidate.get("band", 0)),
        }

    def _no_action_trace_gate(self) -> str:
        plan = self._tsar_arbitration_plan()
        if plan:
            return "smart_loop_tsar_plan"
        return "smart_loop_strategy_register"

    def _no_action_trace_reason(self) -> str:
        plan = self._tsar_arbitration_plan()
        if plan:
            arbitration = plan.get("arbitration_outcome") if isinstance(plan.get("arbitration_outcome"), dict) else {}
            return str(arbitration.get("reason", "") or "smart_loop_tsar_plan_no_action")
        return "no_action_candidate_selected"

    def _no_action_trace_blocked(self) -> bool:
        plan = self._tsar_arbitration_plan()
        if not plan:
            return False
        arbitration = plan.get("arbitration_outcome") if isinstance(plan.get("arbitration_outcome"), dict) else {}
        return str(arbitration.get("verdict", "") or "").lower() in {"blocked", "hold"}

    @staticmethod
    def _safe_float(value: object) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _update_soft_congestion(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        """Cross-state soft congestion detector (V5 CQ/RTT evidence lanes)."""
        cfg = self._pipeline_policy_config()
        if not bool(cfg.soft_congestion_enabled):
            self._soft_congestion_active = False
            self._soft_congestion_ticks = 0
            self._soft_congestion_clear_ticks = 0
            return False

        state = self.posture_authority.state
        if is_fault_state(state):
            self._soft_congestion_active = False
            self._soft_congestion_ticks = 0
            self._soft_congestion_clear_ticks = 0
            return False

        # The typed per-tick gate is the sole RF decision input.
        rf_quality = getattr(self, "_rf_quality", None)
        if rf_quality is not None:
            rf_ok = rf_quality.congestion_rf_ok and not rf_quality.low_rf_active
        else:
            self._report_gate_contract_failure("rf_quality_gate_unavailable")
            rf_ok = False
        using_rtt_delta = congestion_rtt_delta_ready(computed)
        rtt_cong_ms = congestion_rtt_ref_ms(
            computed,
            baseline_ms=float(getattr(computed.latency, 'rtt_congestion_avg', 0.0) or 0.0),
        )
        bad_now, high_now, mod_now = authority_congestion_flags(computed)
        bad_now = bool(rf_ok and bad_now)

        if bad_now:
            self._soft_congestion_ticks += 1
            self._soft_congestion_clear_ticks = 0
            if (
                not self._soft_congestion_active
                and self._soft_congestion_ticks >= max(1, int(cfg.soft_congestion_entry_ticks))
            ):
                self._soft_congestion_active = True
        else:
            self._soft_congestion_ticks = 0
            if self._soft_congestion_active:
                self._soft_congestion_clear_ticks += 1
                if self._soft_congestion_clear_ticks >= max(1, int(cfg.soft_congestion_clear_ticks)):
                    self._soft_congestion_active = False
                    self._soft_congestion_clear_ticks = 0
            else:
                self._soft_congestion_clear_ticks = 0
        return bool(self._soft_congestion_active)

    @staticmethod
    def _action_brief(action: Optional[LeverAction]) -> str:
        return action_brief(action)

    def _emit_decision_trace(
        self,
        source: str,
        state: SystemState,
        proposed: Optional[LeverAction],
        final: Optional[LeverAction],
        steps: List[dict],
        modem: Optional[CellularIntelligenceGeneration] = None,
    ) -> None:
        self._decision_trace_seq += 1
        trace_id = f"dt_{self._decision_trace_seq}"
        passed = sum(1 for s in steps if s.get("result") == "pass")
        blocked = sum(1 for s in steps if s.get("result") == "blocked")
        primary_blocker_gate, primary_blocker_reason, also_blocked_by = classify_blockers(steps)
        tsar_verdict, tsar_reason, tsar_summary = self._classify_tsar_verdict(
            source=source,
            state=state,
            proposed=proposed,
            final=final,
            steps=steps,
            primary_blocker_gate=primary_blocker_gate,
            primary_blocker_reason=primary_blocker_reason,
        )
        trace_obj = DecisionTrace(
            decision_trace_id=trace_id,
            source=source,
            state=state.name,
            proposed=self._action_brief(proposed),
            final=self._action_brief(final),
            passed=passed,
            blocked=blocked,
            tsar_verdict=tsar_verdict,
            tsar_reason=tsar_reason,
            tsar_summary=tsar_summary,
            primary_blocker_gate=primary_blocker_gate,
            primary_blocker_reason=primary_blocker_reason,
            also_blocked_by=tuple(str(x) for x in also_blocked_by),
            steps=tuple(DecisionTraceStep.from_mapping(s) for s in steps),
        )
        result_obj = DecisionResult(
            decision_trace_id=trace_id,
            source=source,
            state=state.name,
            proposed_action=DecisionActionSummary.from_action(
                proposed,
                brief=trace_obj.proposed,
            ),
            final_action=DecisionActionSummary.from_action(
                final,
                brief=trace_obj.final,
            ),
            passed=passed,
            blocked=blocked,
            tsar_verdict=tsar_verdict,
            tsar_reason=tsar_reason,
            primary_blocker_gate=primary_blocker_gate,
            primary_blocker_reason=primary_blocker_reason,
        )
        assessment = getattr(self, "_incident_assessment", {})
        health = getattr(self, "_health_status", {})
        context_health_stage = health_stage(health)
        context_health_action_hint = health_action_hint(health)
        assessment_health_stage = str((assessment or {}).get("health_stage", "") or "")
        assessment_health_action_hint = str((assessment or {}).get("health_action_hint", "") or "")
        if context_health_stage == "healthy" and assessment_health_stage and assessment_health_stage != "healthy":
            context_health_stage = assessment_health_stage
        if context_health_action_hint in {"", "none"} and assessment_health_action_hint and assessment_health_action_hint != "none":
            context_health_action_hint = assessment_health_action_hint
        _rl_until = float(getattr(self.posture_authority.current_posture, "recovered_lane_until", 0.0) or 0.0)
        _rl_bands = list(getattr(self.posture_authority.current_posture, "recovered_lane_bands", []) or [])
        _current_bands = self._current_active_bands(modem) if modem is not None else []
        _rl_active = bool(
            time.time() < _rl_until
            and _rl_bands
            and _current_bands
            and set(_current_bands) == set(_rl_bands)
        )
        _rl_remaining = max(0, int(round(_rl_until - time.time()))) if _rl_active else 0
        context_obj = DecisionContext(
            decision_trace_id=trace_id,
            state=state.name,
            fault_class=fault_state_class(state, getattr(self.posture_authority.current_posture, "emergency_class", "")),
            congestion_kind=str(getattr(self.posture_authority.current_posture, "congestion_kind", "") or ""),
            warning_class=str(getattr(self.posture_authority.current_posture, "warning_class", "") or ""),
            warning_source=str(getattr(self.posture_authority.current_posture, "warning_source", "") or ""),
            warning_mechanism=str(getattr(self.posture_authority.current_posture, "warning_mechanism", "") or ""),
            warning_reason=str(getattr(self.posture_authority.current_posture, "warning_reason", "") or ""),
            health_stage=context_health_stage,
            health_action_hint=context_health_action_hint,
            incident_class=incident_class_name(assessment),
            incident_next_step=incident_next_step(assessment),
            incident_suppress_exploration=incident_suppresses_exploration(assessment),
            recovered_lane_active=_rl_active,
            recovered_lane_bands=tuple(_rl_bands),
            recovered_lane_from_fault=str(getattr(self.posture_authority.current_posture, "recovered_lane_from_fault", "") or ""),
            recovered_lane_remaining_s=_rl_remaining,
            recovered_lane_evidence_multiplier=2.0 if _rl_active else 1.0,
        )
        self._decision_trace_last_obj = trace_obj
        self._decision_result_last_obj = result_obj
        self._decision_context_last_obj = context_obj
        self._mark_tick_decision_exports_fresh()
        trace = trace_obj.to_dict()
        selected_action = final if final is not None else proposed
        if selected_action is not None and selected_action.lever == LeverType.BAND_SELECTION:
            params = dict(selected_action.params or {})
            if bool(params.get("escape_probe", False)):
                trace["escape_probe"] = True
                trace["escape_probe_id"] = str(params.get("escape_probe_id", "") or "")
                trace["escape_bypassed_guards"] = list(params.get("escape_bypassed_guards", []) or [])
                trace["escape_lane"] = str(params.get("escape_lane", "") or "")
                trace["escape_reason"] = str(params.get("escape_reason", "") or "")
        self._decision_trace_last = trace
        if final is not None:
            self._action_trace_ids[id(final)] = trace_id
        self._emit_runtime_event("decision_trace", **trace)

    def pop_action_trace_id(self, action: Optional[LeverAction]) -> str:
        if action is None:
            return ""
        return str(self._action_trace_ids.pop(id(action), "") or "")

    def _action_availability_objective_feedback_keys(
        self,
        action: Optional[LeverAction],
        *,
        modem: Optional[CellularIntelligenceGeneration],
        current_state: Optional[SystemState],
    ) -> list[str]:
        if action is None:
            return []
        keys = [self._objective_feedback_key(action)]
        if action.objective_id:
            keys.append(action.objective_id)
        context_key = self._objective_feedback_context_key(
            action,
            modem=modem,
            current_state=current_state,
        )
        if context_key:
            keys.append(context_key)
        seen: set[str] = set()
        out: list[str] = []
        for raw in keys:
            key = str(raw or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def _action_availability_verdict(
        self,
        *,
        action: Optional[LeverAction],
        modem: Optional[CellularIntelligenceGeneration],
        state: SystemState,
        now: float,
        check_objective: bool,
        scalar_cooldowns: Optional[list[dict]] = None,
    ) -> dict:
        routeros_explore_block: dict[str, Any] = {}
        if (
            check_objective
            and action is not None
            and action.objective_id
            and self._is_routeros_family_profile()
            and action.intent == "explore"
        ):
            blocked, reason = self._check_routeros_explore_objective_block(action, now)
            routeros_explore_block = {"blocked": bool(blocked), "reason": str(reason or "")}
        keys = (
            self._action_availability_objective_feedback_keys(
                action,
                modem=modem,
                current_state=state,
            )
            if check_objective
            else []
        )
        constraint = dict(getattr(self, "_constraint_vector", {}) or {})
        if "health_status" not in constraint:
            constraint["health_status"] = dict(getattr(self, "_health_status", {}) or {})
        if "incident_assessment" not in constraint:
            constraint["incident_assessment"] = dict(getattr(self, "_incident_assessment", {}) or {})
        posture = self.posture_authority.current_posture
        post_fault_until = float(posture.post_fault_until)
        if post_fault_until > now:
            constraint["post_fault_cooldown_remaining_s"] = max(0.0, post_fault_until - now)
            constraint["post_fault_class"] = str(
                posture.post_fault_class or ""
            )
        rf_cliff_event_at = float(posture.rf_cliff_event_at)
        if rf_cliff_event_at > 0.0:
            from arch4.brain.decision_support import DecisionSupportMixin

            remaining_s = (rf_cliff_event_at + DecisionSupportMixin._CELL_OPT_RF_CLIFF_COOLDOWN_S) - now
            if remaining_s > 0.0:
                constraint["rf_cliff_cooldown_remaining_s"] = max(0.0, remaining_s)
                constraint["rf_cliff_reason"] = str(
                    posture.rf_cliff_event_reason or ""
                )
        return evaluate_action_availability(
            action=action,
            now=now,
            state_name=getattr(state, "name", str(state or "")),
            is_fault_state=is_fault_state(state),
            is_emergency_state=is_emergency_state(state),
            constraint=constraint,
            radio_stability_hold_until=float(
                self.action_source_state_authority.band_action_policy_status(
                    now=now
                ).get("radio_stability_hold_until_ts", 0.0)
                or 0.0
            ),
            cooldowns=self.cooldowns if check_objective else None,
            objective_feedback_keys=keys,
            objective_feedback_reason=getattr(self, "_objective_feedback_reason", {}) or {},
            scalar_cooldowns=list(scalar_cooldowns or []),
            routeros_explore_block=routeros_explore_block,
            user_traffic_hold=bool(
                getattr(getattr(self, "_tick_safety", None), "throughput_hold", False)
            ),
        )

    def _record_action_availability_block(
        self,
        *,
        action: Optional[LeverAction],
        verdict: Mapping[str, Any],
        source: str,
    ) -> None:
        reason_code = str(verdict.get("reason_code", "") or "")
        reason = str(verdict.get("reason", "") or "")
        cooldown_s = float(verdict.get("cooldown_s", 0.0) or 0.0)
        metadata = dict(verdict.get("metadata", {}) or {})
        self._constraint_gate_reason = reason_code
        if bool(
            (getattr(action, "params", {}) or {}).get(
                "_selected_action_command", False
            )
        ):
            self._publish_smart_loop_action_availability_block(
                action=action,
                verdict=verdict,
                reason_code=reason_code,
                reason=reason,
            )
        if reason_code in {"objective_feedback", "cell_lock_retry", "restart_retry"} and action is not None:
            self._last_objective_block_reason = reason
            self._record_objective_lifecycle_event(
                stage="action_availability",
                status="blocked",
                action=action,
                reason=reason,
                until_ts=time.time() + cooldown_s if cooldown_s > 0.0 else 0.0,
                metadata={"availability_owner": ACTION_AVAILABILITY_OWNER, **metadata},
            )
        if reason_code == "radio_stability_hold" and action is not None:
            self._note_brain_posture_deferred(
                lane="radio_stability_hold",
                reason=reason,
                verdict="hold",
                summary="Automatic radio proposal is waiting for the accepted connection to remain stable.",
                state=str(self.posture_authority.current_posture.state),
                posture="stability_hold",
                detail="Routine automation waits 21 minutes after the latest accepted PBAND, CA-pool, or radio-recovery transition.",
            )
            self._emit_runtime_event(
                "radio_stability_hold_deferred",
                lever=getattr(getattr(action, "lever", None), "name", ""),
                intent=str(getattr(action, "intent", "") or ""),
                bands=list((getattr(action, "params", {}) or {}).get("bands", []) or []),
                reason=reason,
            )
        elif reason_code == "restore_ca_retry":
            self._emit_runtime_event(
                "restore_ca_retry_deferred",
                objective_id=str(metadata.get("objective_id", "") or getattr(action, "objective_id", "") or ""),
                band_sig=str(metadata.get("band_sig", "") or ""),
                source=str(source or "restore_ca"),
                reason=reason,
            )
        elif reason_code == "restore_ca_repeat":
            self._emit_runtime_event(
                "restore_ca_repeat_deferred",
                objective_id=str(metadata.get("objective_id", "") or getattr(action, "objective_id", "") or ""),
                band_sig=str(metadata.get("band_sig", "") or ""),
                source=str(source or "restore_ca"),
                reason=reason,
            )
        elif reason_code == "explore_repeat":
            self._emit_runtime_event(
                "explore_repeat_blocked",
                lane="explore_band_probe",
                band_sig=str(metadata.get("band_sig", "") or ""),
                probe_count=0,
                reason=reason,
            )
        self._emit_runtime_event(
            "action_availability_blocked",
            owner=ACTION_AVAILABILITY_OWNER,
            source=str(source or ""),
            reason_code=reason_code,
            reason=reason,
            cooldown_s=cooldown_s,
            objective_id=str(getattr(action, "objective_id", "") or ""),
            lever=str(getattr(getattr(action, "lever", None), "name", "") or ""),
            intent=str(getattr(action, "intent", "") or ""),
            metadata=metadata,
        )

    def _publish_smart_loop_action_availability_block(
        self,
        *,
        action: Optional[LeverAction],
        verdict: Mapping[str, Any],
        reason_code: str,
        reason: str,
    ) -> None:
        try:
            self._reject_selected_action(
                action,
                reason=str(reason_code or reason or "action_availability_blocked"),
                receipt={
                    "owner": ACTION_AVAILABILITY_OWNER,
                    "reason": str(reason or ""),
                    "required_next_step": str(verdict.get("required_next_step") or ""),
                    "cooldown_s": float(verdict.get("cooldown_s", 0.0) or 0.0),
                },
            )
            current = self._read_smart_loop_strategy(now=time.time())
            feed = getattr(self, "_smart_loop_strategy_feed_port", None)
            if feed is None:
                raise SmartLoopStrategyUnavailable("smart_loop_strategy_feed_port_unavailable")
            result = publish_smart_loop_strategy_action_block_to_bank(
                feed,
                current_strategy=current,
                reason_code=reason_code,
                reason=reason,
                required_next_step=str(verdict.get("required_next_step") or ""),
                now=time.time(),
            )
            if not bool(result.get("published", False)):
                self._emit_runtime_event(
                    "smart_loop_action_block_publication_failed",
                    reason=str(result.get("reason") or "publish_failed"),
                    reason_code=str(reason_code or ""),
                )
                raise RuntimeError(
                    f"smart_loop_action_block_publication_failed:{result.get('reason') or 'publish_failed'}"
                )
        except Exception as exc:
            self._emit_runtime_event(
                "smart_loop_action_block_publication_failed",
                reason=str(exc or "smart_loop_action_block_publication_failed"),
                reason_code=str(reason_code or ""),
            )
            raise RuntimeError("smart_loop_action_block_publication_failed") from exc

    def _apply_action_availability(
        self,
        *,
        action: Optional[LeverAction],
        modem: Optional[CellularIntelligenceGeneration],
        state: SystemState,
        now: float,
        source: str,
        check_objective: bool,
        emit_trace: bool,
        scalar_cooldowns: Optional[list[dict]] = None,
    ) -> Optional[LeverAction]:
        verdict = self._action_availability_verdict(
            action=action,
            modem=modem,
            state=state,
            now=now,
            check_objective=check_objective,
            scalar_cooldowns=scalar_cooldowns,
        )
        if bool(verdict.get("allowed", False)):
            return action
        self._record_action_availability_block(action=action, verdict=verdict, source=source)
        if emit_trace:
            reason_code = str(verdict.get("reason_code", "") or "action_availability")
            gate = reason_code if reason_code in {"health_hold", "incident_hold"} else "action_availability"
            self._emit_decision_trace(
                source=source,
                state=state,
                proposed=action,
                final=None,
                modem=modem,
                steps=[{
                    "gate": gate,
                    "result": "blocked",
                    "owner": ACTION_AVAILABILITY_OWNER,
                    "reason": str(verdict.get("reason", "") or ""),
                    "reason_code": reason_code,
                }],
            )
        return None

    def _proposal_action_available(
        self,
        action: Optional[LeverAction],
        *,
        modem: Optional[CellularIntelligenceGeneration],
        state: Optional[SystemState] = None,
        now: Optional[float] = None,
        source: str = "proposal",
        check_objective: bool = True,
        scalar_cooldowns: Optional[list[dict]] = None,
    ) -> Optional[LeverAction]:
        if action is None:
            return None
        current_state = (
            state
            if state is not None
            else self.posture_authority.current_posture.state
        )
        ts = time.time() if now is None else float(now)
        return self._apply_action_availability(
            action=action,
            modem=modem,
            state=current_state,
            now=ts,
            source=source,
            check_objective=check_objective,
            emit_trace=False,
            scalar_cooldowns=scalar_cooldowns,
        )

    def _availability_scalar_probe_allowed(
        self,
        *,
        cooldown: str,
        reason_code: str,
        reason: str,
        source: str,
        now: Optional[float] = None,
    ) -> bool:
        ts = time.time() if now is None else float(now)
        state = self.posture_authority.current_posture.state
        probe = build_authority_lever_action(
            lever=LeverType.BAND_SELECTION,
            params={"_availability_probe": source},
            reason=reason,
            intent=source,
            objective_id=source,
            source="brain_availability_probe",
        )
        verdict = self._action_availability_verdict(
            action=probe,
            modem=None,
            state=state,
            now=ts,
            check_objective=True,
            scalar_cooldowns=[{
                "name": cooldown,
                "reason_code": reason_code,
                "reason": reason,
            }],
        )
        if bool(verdict.get("allowed", False)):
            return True
        self._record_action_availability_block(action=probe, verdict=verdict, source=source)
        return False

    def _run_action_pipeline(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        state: SystemState,
        now: float,
        source: str,
        apply_emergency_guard: bool,
    ) -> Optional[LeverAction]:
        proposed = action
        steps: List[dict] = []

        if apply_emergency_guard:
            before = action
            action = self._guard_emergency_action(action)
            steps.append(
                {
                    "gate": "emergency_guard",
                    "result": "pass" if action is not None else "blocked",
                    "changed": self._action_brief(before) != self._action_brief(action),
                }
            )

        if action is None:
            steps.append({"gate": "no_action", "result": "pass"})
            self._emit_decision_trace(source, state, proposed, None, steps, modem=modem)
            return None

        if action.lever == LeverType.BAND_SELECTION:
            before = action
            action = self._canonicalize_band_action(action, modem)
            steps.append(
                {
                    "gate": "band_resolver",
                    "result": "pass",
                    "changed": self._action_brief(before) != self._action_brief(action),
                }
            )

        guard_map = {
            "layer3_action_trust": lambda a: self._guard_layer3_action_trust(a),
            "learning_guard": lambda a: self._apply_learning_band_guard(a, modem, now),
        }
        for gate, guard_fn in ordered_blocker_guard_chain(guard_map):
            action = self._apply_guard_step(
                steps=steps,
                gate=gate,
                action=action,
                guard_fn=guard_fn,
            )
            if action is None:
                self._reject_selected_action(
                    proposed,
                    reason=f"decision_guard_blocked:{gate}",
                    receipt={"gate": gate, "source": source},
                )
                self._emit_decision_trace(source, state, proposed, None, steps, modem=modem)
                return None

        self._emit_decision_trace(source, state, proposed, action, steps, modem=modem)
        return action

    @staticmethod
    def _band_sig_from_action(action: LeverAction) -> str:
        return band_sig_from_action(action)

    def _guard_layer3_action_trust(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        """Block brain-proposed actions that Layer 3 firmware overlay marks unsupported.

        The trust map is set by Engine._update_layer3_action_trust() each tick
        before decide() is called.  An empty map (no overlay match, or model not
        yet known) is a no-op — all actions pass through.

        Trust semantics:
          "unsupported" → return None  (brain should not propose this action)
          "degraded"    → return action (warn once; known caveat, not a hard block)
          anything else → return action (pass through to remaining guards)

        This guard fires inside decide() — earlier than the engine-level
        Core Bouncer exact-profile gate — so unsupported actions are never
        logged as executable intent or scheduled into the proposal objective.
        """
        if not action:
            return action
        trust_map = getattr(self, "_layer3_action_trust", None) or {}
        if not trust_map:
            return action
        # Map LeverType to the action_trust key used in overlays.
        # Must stay in sync with the Core action-admission lever map.
        _lever_map = {
            LeverType.BAND_SELECTION: "band_selection",
            LeverType.BAND_LOCK:      "band_lock",
            LeverType.CELL_LOCK:      "cell_lock",
            LeverType.PBAND: "pband",
            LeverType.NR_BAND_LOCK:   "nr_band_lock",
            LeverType.RESTART:        "restart",
        }
        action_key = _lever_map.get(action.lever)
        if action_key is None:
            return action  # No Layer 3 truth for this lever — pass through
        trust = str(trust_map.get(action_key, trust_map.get("pband") if action_key == "pband" else None) or "untested")
        if trust == "unsupported":
            import logging as _log
            _log.getLogger("arch4.decision").warning(
                "Layer 3: brain proposed %s but firmware profile says unsupported; blocking",
                action_key,
            )
            # Emit a structured event so the suppression is visible in JSONL
            # without needing to read raw logs or decision trace steps.
            try:
                self._emit_runtime_event(
                    "action_suppressed_layer3_unsupported",
                    lever=action.lever.name,
                    action_key=action_key,
                    source="brain_guard",
                    objective_id=str(action.objective_id or ""),
                )
            except Exception as _ev_exc:
                _log.getLogger("arch4.decision").debug(
                    "layer3 brain guard event suppressed: %s", _ev_exc
                )
            return None
        return action

    def _guard_health_hold(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        raise RuntimeError("old_post_owner_health_hold_guard_deleted")
        self._last_health_hold_reason = ""
        if action is None:
            return None
        status = getattr(self, "_health_status", {}) or {}
        if not should_hold_action_for_health(status=status, action=action):
            return action
        self._last_health_hold_reason = health_hold_reason(status=status, action=action)
        self._emit_runtime_event(
            "health_hold_action_suppressed",
            stage=health_stage(status),
            action_hint=health_action_hint(status),
            lever=action.lever.name,
            intent=str(action.intent or ""),
            objective_id=str(action.objective_id or ""),
        )
        return None

    def _guard_incident_hold(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        raise RuntimeError("old_post_owner_incident_hold_guard_deleted")
        self._last_incident_hold_reason = ""
        if action is None:
            return None
        assessment = getattr(self, "_incident_assessment", {}) or {}
        if not should_hold_action_for_incident(assessment=assessment, action=action):
            return action
        self._last_incident_hold_reason = incident_hold_reason(assessment=assessment, action=action)
        self._emit_runtime_event(
            "incident_hold_action_suppressed",
            incident_class=incident_class_name(assessment),
            recommended_next_step=incident_next_step(assessment),
            lever=action.lever.name,
            intent=str(action.intent or ""),
            objective_id=str(action.objective_id or ""),
        )
        return None

    @staticmethod
    def _objective_id_restore_ca(bands: List[int]) -> str:
        return DecisionPipelineMixin._objective_id_for_bands("restore_ca", bands)

    @staticmethod
    def _objective_id_for_bands(prefix: str, bands: List[int]) -> str:
        return objective_id_for_bands(prefix, bands)

    @staticmethod
    def _current_active_bands(modem: CellularIntelligenceGeneration) -> List[int]:
        return current_active_bands(modem)

    def _canonicalize_band_action(
        self,
        action: LeverAction,
        modem: CellularIntelligenceGeneration,
    ) -> LeverAction:
        """
        Canonicalize BAND_SELECTION semantics to one full-set authority.

        After this stage all downstream gates/modem executors see:
          - resolved_target_bands: canonical full target set
          - band_sequence_steps: cumulative steps when sequential
          - band_mode='set' (add semantics already merged)
        """
        if action.lever != LeverType.BAND_SELECTION:
            return action
        current = resolve_runtime_available_bands(
            modem=modem,
            memory=getattr(self, "memory", None),
        )
        if not current:
            current = self._current_active_bands(modem)
        res = resolve_band_intent(action.params, current_bands=current)
        p = dict(action.params or {})
        p["requested_bands"] = list(res.requested_bands)
        p["resolved_current_bands"] = list(res.current_bands)
        p["resolved_target_bands"] = list(res.target_bands)
        p["resolved_target_sig"] = str(res.target_signature)
        p["band_sequence_steps"] = [list(s) for s in (res.steps or [])]
        p["band_mode"] = "set"
        p["sequential"] = bool(res.sequential)
        # Keep `bands` as resolved full target so all call paths are aligned.
        p["bands"] = list(res.target_bands)
        action.params = p
        if action.lever == LeverType.BAND_SELECTION and (not action.objective_id):
            action.objective_id = self._objective_id_for_bands(action.intent or "bands", res.target_bands)
        return action

    @staticmethod
    def _objective_feedback_key(action: LeverAction) -> str:
        if action.lever == LeverType.BAND_SELECTION:
            sig = action.params.get("resolved_target_sig") or ""
            if not sig:
                sig = DecisionPipelineMixin._band_sig_from_action(action)
            return f"band:{action.intent or '-'}:{sig}"
        return f"{action.lever.name}:{action.intent or '-'}:{action.objective_id or '-'}"

    def _objective_feedback_context_key(
        self,
        action: LeverAction,
        *,
        modem: Optional[CellularIntelligenceGeneration] = None,
        current_state: Optional[SystemState] = None,
    ) -> str:
        if action.lever != LeverType.BAND_SELECTION:
            return ""
        if str(action.intent or "") != "explore":
            return ""
        state = current_state if current_state is not None else getattr(self.posture_authority, "state", None)
        if state != SystemState.WORKING:
            return ""
        if modem is None:
            return ""

        target_sig = action.params.get("resolved_target_sig") or self._band_sig_from_action(action)
        if not target_sig:
            return ""

        provider_plmn = ""
        try:
            provider_plmn = str(self._policy_provider_plmn() or "")
        except Exception:
            provider_plmn = ""
        if not provider_plmn:
            provider_plmn = str(getattr(getattr(self, "constraints", None), "provider_plmn", "") or "")
        provider_name = str(getattr(getattr(self, "constraints", None), "provider_name", "") or "")
        provider_token = provider_plmn or provider_name or "-"

        serving_band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        pci = int(getattr(modem.cellular, 'pci', 0) or 0)
        cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
        earfcn = int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0)
        ca_sig = str(getattr(modem.cellular, 'ca_band_signature', "") or "") or "-"
        return (
            f"bandctx:{action.intent or '-'}:{provider_token}:"
            f"B{serving_band}:PCI{pci}:CID{cell_id}:E{earfcn}:{ca_sig}:{target_sig}"
        )

    def _record_objective_feedback(
        self,
        action: LeverAction,
        *,
        code: str,
        reason: str,
        block_s: Optional[float] = None,
        modem: Optional[CellularIntelligenceGeneration] = None,
        current_state: Optional[SystemState] = None,
    ) -> None:
        cfg = self._pipeline_policy_config()
        ttl = float(
            block_s
            if block_s is not None
            else max(60.0, float(cfg.objective_feedback_no_effect_block_s))
        )
        now = time.time()
        key = self._objective_feedback_key(action)
        self.cooldowns.set_keyed("objective_feedback", key, hold_s=ttl, now=now)
        self._objective_feedback_reason[key] = f"{code}: {reason}".strip()
        if action.objective_id:
            self.cooldowns.set_keyed("objective_feedback", action.objective_id, hold_s=ttl, now=now)
            self._objective_feedback_reason[action.objective_id] = f"{code}: {reason}".strip()
        context_key = self._objective_feedback_context_key(
            action,
            modem=modem,
            current_state=current_state,
        )
        if context_key:
            self.cooldowns.set_keyed("objective_feedback", context_key, hold_s=ttl, now=now)
            self._objective_feedback_reason[context_key] = f"{code}: {reason}".strip()
        self._record_objective_lifecycle_event(
            stage="proposal_feedback",
            status="hold_set",
            action=action,
            reason=f"{code}: {reason}".strip(),
            until_ts=now + ttl,
            metadata={"feedback_key": key, "context_feedback_key": context_key, "block_s": int(ttl)},
        )
        self._emit_runtime_event(
            "objective_feedback_recorded",
            feedback_key=key,
            context_feedback_key=context_key,
            objective_id=action.objective_id or "",
            code=code,
            reason=reason,
            block_s=int(ttl),
        )

    def _proposal_feedback_blocked_for(
        self,
        *,
        intent: str,
        bands: List[int],
        now: Optional[float] = None,
        modem: Optional[CellularIntelligenceGeneration] = None,
        current_state: Optional[SystemState] = None,
    ) -> tuple[bool, str]:
        raise RuntimeError("old_proposal_feedback_owner_deleted")

    def _clear_objective_feedback(self, action: LeverAction) -> None:
        key = self._objective_feedback_key(action)
        for k in [key, action.objective_id or ""]:
            if not k:
                continue
            self.cooldowns.clear_keyed("objective_feedback", k)
            self._objective_feedback_reason.pop(k, None)

    def _update_ca_lost_signal(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames, now: float) -> None:
        cfg = self._pipeline_policy_config()
        window_s = max(0.0, float(cfg.ca_lost_window_s))
        self._ca_lost_samples = [
            (ts, val) for ts, val in self._ca_lost_samples
            if (now - ts) <= window_s
        ]
        if self.cooldowns.is_blocked("ca_lost_eval_block", now=now):
            return

        # Typed gates are mandatory. Missing gates hold learning safely and
        # surface an architecture fault; raw modem values are never substituted.
        rf_quality = getattr(self, "_rf_quality", None)
        if rf_quality is not None:
            rf_feasible = rf_quality.feasibility_ok
        else:
            self._report_gate_contract_failure("rf_quality_gate_unavailable")
            rf_feasible = False
        traffic = getattr(self, "_traffic", None)
        if traffic is not None:
            has_traffic = traffic.has_traffic
        else:
            self._report_gate_contract_failure("traffic_gate_unavailable")
            has_traffic = False
        ld_sample_valid = bool(cart_ld_valid(computed))
        self._update_ca_recovery_trend(
            modem=modem,
            computed=computed,
            now=now,
            sample_allowed=bool(has_traffic and rf_feasible and ld_sample_valid),
        )
        if has_traffic and rf_feasible and ld_sample_valid:
            sample = 1.0 if cart_ld_active(computed) else 0.0
            self._ca_lost_samples.append((now, sample))
            self._ca_lost_samples = [
                (ts, val) for ts, val in self._ca_lost_samples
                if (now - ts) <= window_s
            ]
        if not self._ca_lost_samples:
            return

        n_samples = len(self._ca_lost_samples)
        n_positive = sum(1 for _, v in self._ca_lost_samples if v > 0)
        self._ca_lost_avg = n_positive / float(n_samples)  # kept for telemetry
        was_on = self._ca_lost_state
        min_samples = int(getattr(cfg, "ca_lost_min_samples", 10))

        # ── Asymmetric hysteresis (count-based) ──────────────────────
        #
        # Was ACTIVE → declare LOST:
        #   Need ≥min_samples AND zero positives. Even 1 ca_active=True
        #   in the window means CA is still there — don't trigger.
        #   This kills false positives from brief congestion CA drops.
        #
        # Was LOST → declare ACTIVE:
        #   Need ≥min_samples AND ≥3 positives. CA must appear
        #   consistently, not just a single blip.
        #
        # Single band / B20: never produces positive samples, so the
        # detector correctly identifies CA as lost after min_samples.
        ca_lost_restore_min = int(getattr(cfg, "ca_lost_restore_min_positive", 3))
        if not was_on and n_samples >= min_samples and n_positive == 0:
            self._ca_lost_state = True
            self._emit_runtime_event("ca_lost_on", positive=n_positive, samples=n_samples)
        elif was_on and n_samples >= min_samples and n_positive >= ca_lost_restore_min:
            self._ca_lost_state = False
            self._emit_runtime_event("ca_lost_off", positive=n_positive, samples=n_samples)

    def _update_ca_recovery_trend(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        now: float,
        sample_allowed: bool,
    ) -> None:
        """Track CA-rate trend over 10/5/2 minute windows.

        This is host/modem behavior evidence, not a command.  It prevents the
        Brain from disrupting a modem that is already restoring CA by itself.
        """
        max_window_s = 600.0
        samples = list(getattr(self, "_ca_recovery_trend_samples", []) or [])
        samples = [(ts, val) for ts, val in samples if (float(now) - float(ts)) <= max_window_s]
        if bool(sample_allowed) and modem is not None and bool(getattr(modem.cellular, 'radio_connected', False)):
            samples.append((float(now), 1.0 if cart_ld_active(computed) else 0.0))
        self._ca_recovery_trend_samples = samples
        self._ca_recovery_trend_last = self._build_ca_recovery_trend(now=float(now))

    def _build_ca_recovery_trend(self, *, now: float) -> dict:
        samples = list(getattr(self, "_ca_recovery_trend_samples", []) or [])

        def _rate(window_s: float) -> tuple[float, int]:
            window_samples = [
                float(val)
                for ts, val in samples
                if (float(now) - float(ts)) <= float(window_s)
            ]
            if not window_samples:
                return 0.0, 0
            return sum(window_samples) / float(len(window_samples)), len(window_samples)

        rate_10m, count_10m = _rate(600.0)
        rate_5m, count_5m = _rate(300.0)
        rate_2m, count_2m = _rate(120.0)
        enough = count_10m >= 5 and count_5m >= 3 and count_2m >= 2
        improving = bool(
            enough
            and rate_2m >= 0.50
            and (
                (rate_2m >= rate_5m + 0.05 and rate_5m >= rate_10m + 0.05)
                or rate_2m >= rate_10m + 0.15
            )
        )
        worsening = bool(
            enough
            and rate_10m >= rate_5m + 0.05
            and rate_5m >= rate_2m + 0.05
        )
        return {
            "schema": "ca_recovery_trend_v1",
            "sample_count_10m": int(count_10m),
            "sample_count_5m": int(count_5m),
            "sample_count_2m": int(count_2m),
            "ca_rate_10m": round(float(rate_10m), 4),
            "ca_rate_5m": round(float(rate_5m), 4),
            "ca_rate_2m": round(float(rate_2m), 4),
            "improving": bool(improving),
            "worsening": bool(worsening),
            "hold_restore_ca": bool(improving and not worsening),
            "reason": (
                "CA metric is recovering; defer disruptive CA restore"
                if improving and not worsening
                else "CA metric not recovering"
            ),
        }

    def _should_hold_restore_ca_for_ca_trend(self) -> bool:
        trend = dict(getattr(self, "_ca_recovery_trend_last", {}) or {})
        if not bool(trend.get("hold_restore_ca", False)):
            return False
        self._emit_runtime_event(
            "restore_ca_deferred_ca_metric_improving",
            ca_rate_10m=float(trend.get("ca_rate_10m", 0.0) or 0.0),
            ca_rate_5m=float(trend.get("ca_rate_5m", 0.0) or 0.0),
            ca_rate_2m=float(trend.get("ca_rate_2m", 0.0) or 0.0),
            sample_count_10m=int(trend.get("sample_count_10m", 0) or 0),
            sample_count_5m=int(trend.get("sample_count_5m", 0) or 0),
            sample_count_2m=int(trend.get("sample_count_2m", 0) or 0),
            reason=str(trend.get("reason", "") or ""),
        )
        return True

    def _profile_backend_name(self) -> str:
        policy_backend = str(getattr(self.policy_profile, "modem_backend", "") or "").strip()
        if policy_backend:
            return policy_backend
        return str(getattr(self.cfg, "modem_backend", "") or "").strip()

    def _is_huawei_hilink_profile(self) -> bool:
        backend_name = self._profile_backend_name()
        if not backend_name:
            return False
        from arch4.operator_profiles import infer_backend_family
        return infer_backend_family(backend_name) == FAMILY_HUAWEI_HILINK

    def _is_mikrotik_profile(self) -> bool:
        backend_name = self._profile_backend_name()
        if not backend_name:
            return False
        from arch4.operator_profiles import infer_backend_family
        return infer_backend_family(backend_name) == FAMILY_ROUTEROS_AT

    def _is_routeros_family_profile(self) -> bool:
        return self._is_mikrotik_profile()

    def _b818_rotation_has_confidence(self, target_band: int) -> bool:
        return family_rotation_has_confidence(self, target_band, family=FAMILY_HUAWEI_HILINK)

    def _hilink_rotation_has_confidence(self, target_band: int) -> bool:
        return self._b818_rotation_has_confidence(target_band)

    def _is_objective_blocked(self, action: Optional[LeverAction], now: float) -> bool:
        raise RuntimeError("old_objective_block_owner_deleted")

    @staticmethod
    def _requested_bands_attached(action: LeverAction, modem_after: Optional[CellularIntelligenceGeneration]) -> bool:
        if modem_after is None or not modem_after.cellular.radio_connected:
            return False
        requested = set()
        requested_bands = action.params.get("bands") or action.params.get("ca_pool") or []
        for b in requested_bands:
            try:
                bi = int(b)
            except (TypeError, ValueError):
                continue
            if bi > 0:
                requested.add(bi)
        if not requested:
            return False
        active = {c.band for c in modem_after.cellular.carriers if c.is_pcell or c.state >= 1}
        # Success means we are attached to at least one requested band
        # and primary anchor belongs to the requested set.
        return bool(requested & active) and modem_after.cellular.primary_band in requested

    def _guard_band_action(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        raise RuntimeError("old_post_owner_band_cooldown_guard_deleted")

    def _apply_learning_band_guard(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        """Block repeated bad band actions using short/long memory outcomes."""
        if not action or action.lever != LeverType.BAND_SELECTION:
            return action
        if action.params.get("forced_sequence"):
            return action
        if action.params.get("_brain_learning_screened"):
            return action
        bands = action.params.get("bands", []) or []
        hour = time.localtime(now).tm_hour
        learned_guard = screen_band_action(
            self.memory,
            hour=hour,
            bands=bands,
            now=now,
            current_sinr=float(modem.cellular.sinr_db),
        )
        reason = str(learned_guard.reason or "")
        if (not learned_guard.blocked) and reason and "retry allowed" in reason.lower():
            sig = self._band_sig_from_action(action)
            log.info("Learning guard re-opened BAND_SELECTION %s: %s", sig, reason)
            self._emit_runtime_event(
                "learning_guard_reopen",
                target=sig,
                reason=reason,
                hour=int(hour),
                sinr_db=float(modem.cellular.sinr_db),
            )
            return action
        if not learned_guard.blocked:
            return action
        sig = self._band_sig_from_action(action)
        log.info("Learning guard blocked BAND_SELECTION %s: %s", sig, reason)
        self._emit_runtime_event(
            "learning_guard_blocked",
            target=sig,
            reason=reason,
            hour=int(hour),
        )
        return None

    def mark_action_taken(self) -> None:
        """Called by runner after executing an action."""
        self._last_action_time = time.time()

    def _radio_roi_observation_hold_seconds(self) -> float:
        state_cfg = self._state_policy_config()
        return max(
            0.0,
            float(getattr(state_cfg, "preemptive_switch_cooldown_s", 1800.0) or 0.0),
        )

    def _start_radio_roi_observation_window(
        self,
        action: LeverAction,
        *,
        success: bool,
        now: float,
        reason: str,
    ) -> None:
        if action.lever not in {LeverType.BAND_SELECTION, LeverType.PBAND}:
            return
        hold_s = self._radio_roi_observation_hold_seconds()
        sig = self._band_sig_from_action(action)
        if hold_s <= 0.0:
            self.cooldowns.clear(SMART_LOOP_ROI_OBSERVATION_COOLDOWN)
            self._smart_loop_roi_observation_target_sig = ""
            return
        self._smart_loop_roi_observation_target_sig = sig
        self.cooldowns.set(SMART_LOOP_ROI_OBSERVATION_COOLDOWN, hold_s=hold_s, now=now)
        if sig:
            self.cooldowns.set_keyed(SMART_LOOP_ROI_OBSERVATION_COOLDOWN, sig, hold_s=hold_s, now=now)
        self._emit_runtime_event(
            "smart_loop_roi_observation_started",
            target=sig,
            success=bool(success),
            cooldown_s=int(round(hold_s)),
            owner=ACTION_AVAILABILITY_OWNER,
            reason=reason,
            source_intent=str(getattr(action, "intent", "") or ""),
        )

    def _start_smart_loop_roi_observation_window(
        self,
        action: LeverAction,
        *,
        success: bool,
        now: float,
    ) -> None:
        if str(getattr(action, "intent", "") or "") != SMART_LOOP_COMMAND_INTENT:
            return
        self._start_radio_roi_observation_window(
            action,
            success=success,
            now=now,
            reason="wait_for_referee_window_before_next_smart_loop_radio_action",
        )

    def note_band_action_result(
        self,
        action: LeverAction,
        success: bool,
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_after: Optional[DerivedSignalFrames] = None,
    ) -> None:
        """
        Learn from BAND_SELECTION outcome and apply retry guardrail.

        After N consecutive unsuccessful attempts, suppress further automatic
        band actions for a cooldown window to avoid repeated modem interruptions.
        """
        if action.lever != LeverType.BAND_SELECTION:
            return

        now = time.time()
        cfg = self._pipeline_policy_config()
        self._last_action_time = now
        sig = self._band_sig_from_action(action)
        self._band_last_target_sig = sig
        self.cooldowns.set(
            "ca_lost_eval_block",
            hold_s=max(0.0, float(cfg.ca_lost_eval_guard_s)),
            now=now,
        )
        self._start_smart_loop_roi_observation_window(action, success=success, now=now)
        if action.intent == "tsar_vector_candidate":
            state_cfg = self._state_policy_config()
            hold_s = max(
                300.0,
                float(getattr(state_cfg, "preemptive_switch_cooldown_s", 1800.0) or 0.0),
            )
            if sig:
                self.cooldowns.set_keyed("tsar_vector_candidate", sig, hold_s=hold_s, now=now)
            self.cooldowns.set("preemptive_switch", hold_s=hold_s, now=now)
            self._emit_runtime_event(
                "tsar_vector_candidate_cooldown_started",
                target=sig,
                success=bool(success),
                cooldown_s=int(hold_s),
            )

        if action.intent == "restore_ca":
            objective_id = action.objective_id or self._objective_id_restore_ca(
                action.params.get("bands", [])
            )
            _band_sig = "+".join(str(b) for b in sorted(action.params.get("bands", [])))
            restore_ok = (
                success
                and modem_after is not None
                and modem_after.cellular.radio_connected
                and computed_after is not None
                and ca_ld_stable(computed_after)
                and self._requested_bands_attached(action, modem_after)
            )
            # ── Restore-CA repeat guard ───────────────────────────────────
            # Count EVERY restore attempt (success or failure) per band combo.
            # After 2 attempts, block that combo for 60 min. This stops the
            # cycle where restore succeeds → congestion oscillation drops CA
            # → restore fires again immediately.
            if _band_sig:
                count = self._restore_ca_probe_counts.get(_band_sig, 0) + 1
                self._restore_ca_probe_counts[_band_sig] = count
                if count >= 2:
                    block_s = max(0.0, float(cfg.restore_ca_repeat_block_s))
                    self.cooldowns.set_keyed("restore_ca_repeat", _band_sig, hold_s=block_s, now=now)
                    self._restore_ca_probe_counts[_band_sig] = 0
                    log.info(
                        "Restore-CA repeat guard: %s attempted %d times, blocking for %.0f min",
                        _band_sig, count, block_s / 60.0,
                    )
                    self._emit_runtime_event(
                        "restore_ca_repeat_cooldown",
                        band_sig=_band_sig,
                        probe_count=count,
                        block_s=int(block_s),
                        last_ok=restore_ok,
                    )
            if restore_ok:
                self.cooldowns.clear_keyed("restore_ca_retry", objective_id)
                self._clear_objective_feedback(action)
                self._record_objective_lifecycle_event(
                    stage="restore_ca",
                    status="success",
                    action=action,
                    objective_id=objective_id,
                    reason="ca_restored",
                )
                self._emit_runtime_event(
                    "restore_ca_objective_succeeded",
                    objective_id=objective_id,
                    target=sig,
                )
            else:
                retry_s = max(0.0, float(cfg.restore_ca_retry_after_s))
                self.cooldowns.set_keyed("restore_ca_retry", objective_id, hold_s=retry_s, now=now)
                self._record_objective_lifecycle_event(
                    stage="restore_ca",
                    status="retry_scheduled",
                    action=action,
                    objective_id=objective_id,
                    reason="restore_failed_or_no_ca",
                    until_ts=now + retry_s,
                    metadata={"retry_after_s": int(retry_s)},
                )
                self._emit_runtime_event(
                    "restore_ca_objective_retry_scheduled",
                    objective_id=objective_id,
                    target=sig,
                    retry_after_s=int(retry_s),
                )
            return

        attached = success and self._requested_bands_attached(action, modem_after)

        # ── Explore repeat guard ─────────────────────────────────────────
        # Count every exploration probe of the same band combo.
        # After 2 probes, block that specific combo for 60 minutes.
        # Prevents cycling through the same combos when none improve things.
        if action.intent == "explore":
            _band_sig = "+".join(str(b) for b in sorted(action.params.get("bands", [])))
            if _band_sig:
                count = self._explore_probe_counts.get(_band_sig, 0) + 1
                self._explore_probe_counts[_band_sig] = count
                if count >= 2:
                    block_s = max(0.0, float(cfg.explore_repeat_block_s))
                    self.cooldowns.set_keyed("explore_repeat", _band_sig, hold_s=block_s, now=now)
                    self._explore_probe_counts[_band_sig] = 0  # reset for next cycle
                    log.info(
                        "Explore repeat guard: %s probed %d times, blocking for %.0f min",
                        _band_sig, count, block_s / 60.0,
                    )
                    self._emit_runtime_event(
                        "explore_repeat_cooldown",
                        band_sig=_band_sig,
                        probe_count=count,
                        block_s=int(block_s),
                    )

        # MikroTik low-priority explore lifecycle:
        # keep spacing between explore attempts and enforce fail-budget cooldown,
        # without polluting global band-fail guard used by stronger actions.
        if self._is_routeros_family_profile() and action.intent == "explore":
            self._note_routeros_explore_result(
                action,
                attached=attached,
                now=now,
                target_sig=sig,
            )
            return

        if attached:
            self._band_fail_streak = 0
            self._clear_objective_feedback(action)
            return

        self._band_fail_streak += 1
        limit = max(1, int(cfg.band_action_fail_limit))
        if self._band_fail_streak >= limit:
            cooldown_s = max(0.0, float(cfg.band_action_cooldown_s))
            self.cooldowns.set("band_cooldown", hold_s=cooldown_s, now=now)
            self._band_fail_streak = 0
            log.warning(
                "Band action guard: %d unsuccessful attempts for %s; "
                "suppressing automatic band actions for %.0f min",
                limit, sig, cooldown_s / 60.0,
            )
            self._emit_runtime_event(
                "band_action_cooldown_started",
                target=sig,
                fail_limit=limit,
                cooldown_s=int(cooldown_s),
            )

    def note_action_result(
        self,
        action: LeverAction,
        success: bool,
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_after: Optional[DerivedSignalFrames] = None,
    ) -> None:
        """Objective-aware result handling for decision-origin actions."""
        if action.lever in {LeverType.BAND_SELECTION, LeverType.PBAND}:
            action_key = action_transaction_key_from_action(action)
            target_bands = (
                action.params.get("bands")
                or action.params.get("ca_pool")
                or action.params.get("dl_bands")
                or (
                    [action.params.get("pband")]
                    if int(action.params.get("pband", 0) or 0) > 0
                    else []
                )
            )
            self.action_source_state_authority.note_provider_radio_result(
                lever=action.lever.name,
                target_bands=target_bands,
                intent=str(action.intent or ""),
                objective_id=str(action.objective_id or ""),
                success=bool(success),
                target_confirmed=bool(
                    success
                    and self._requested_bands_attached(action, modem_after)
                ),
                observed_at=time.time(),
                receipt_id=str(action_key.root_id if action_key else ""),
                identity_key=str(
                    action_key.identity_key if action_key else ""
                ),
                session_scope_key=str(
                    action_key.matrix_identity.session_scope_key
                    if action_key
                    else ""
                ),
            )

        if action.lever == LeverType.BAND_SELECTION:
            self.note_band_action_result(action, success, modem_after, computed_after)
            return

        now = time.time()
        cfg = self._pipeline_policy_config()
        if action.lever == LeverType.PBAND:
            sig = self._band_sig_from_action(action)
            self._last_action_time = now
            self._band_last_target_sig = sig
            self.cooldowns.set(
                "ca_lost_eval_block",
                hold_s=max(0.0, float(cfg.ca_lost_eval_guard_s)),
                now=now,
            )
            self._start_smart_loop_roi_observation_window(action, success=success, now=now)
            if action.intent == "tsar_vector_candidate":
                state_cfg = self._state_policy_config()
                hold_s = max(
                    300.0,
                    float(getattr(state_cfg, "preemptive_switch_cooldown_s", 1800.0) or 0.0),
                )
                if sig:
                    self.cooldowns.set_keyed("tsar_vector_candidate", sig, hold_s=hold_s, now=now)
                self.cooldowns.set("preemptive_switch", hold_s=hold_s, now=now)
                self._emit_runtime_event(
                    "tsar_vector_candidate_cooldown_started",
                    target=sig,
                    success=bool(success),
                    cooldown_s=int(hold_s),
                )
            attached = success and self._requested_bands_attached(action, modem_after)
            if attached:
                self._band_fail_streak = 0
                self._clear_objective_feedback(action)
                return
            self._band_fail_streak += 1
            limit = max(1, int(cfg.band_action_fail_limit))
            if self._band_fail_streak >= limit:
                cooldown_s = max(0.0, float(cfg.band_action_cooldown_s))
                self.cooldowns.set("band_cooldown", hold_s=cooldown_s, now=now)
                self._band_fail_streak = 0
                self._emit_runtime_event(
                    "band_action_cooldown_started",
                    target=sig,
                    fail_limit=limit,
                    cooldown_s=int(cooldown_s),
                )
            return

        if action.lever == LeverType.CELL_LOCK and not action.params.get("unlock"):
            objective_id = action.objective_id or ""
            if not objective_id:
                return
            requested_pci = int(action.params.get("pci", 0) or 0)
            requested_earfcn = int(action.params.get("earfcn", 0) or 0)
            lock_ok = (
                success
                and modem_after is not None
                and requested_pci > 0
                and int(modem_after.cellular.pci or 0) == requested_pci
                and (requested_earfcn <= 0 or int(modem_after.cellular.earfcn_dl or 0) == requested_earfcn)
            )
            if lock_ok:
                self.cooldowns.clear_keyed("cell_lock_retry", objective_id)
                self._mikrotik_cell_lock_fail_count.pop(objective_id, None)
                self._record_objective_lifecycle_event(
                    stage="cell_lock",
                    status="success",
                    action=action,
                    objective_id=objective_id,
                    reason="target_cell_verified",
                    metadata={"pci": requested_pci, "earfcn": requested_earfcn},
                )
                self._emit_runtime_event(
                    "cell_lock_objective_succeeded",
                    objective_id=objective_id,
                    pci=requested_pci,
                    earfcn=requested_earfcn,
                )
            else:
                retry_s = max(0.0, float(cfg.cell_lock_retry_after_s))
                if self._is_routeros_family_profile() and self._is_routeros_cell_lock_objective(action):
                    self._note_routeros_cell_lock_retry(
                        action,
                        objective_id=objective_id,
                        requested_pci=requested_pci,
                        requested_earfcn=requested_earfcn,
                        now=now,
                    )
                else:
                    self.cooldowns.set_keyed("cell_lock_retry", objective_id, hold_s=retry_s, now=now)
                    self._record_objective_lifecycle_event(
                        stage="cell_lock",
                        status="retry_scheduled",
                        action=action,
                        objective_id=objective_id,
                        reason="lock_not_verified",
                        until_ts=now + retry_s,
                        metadata={
                            "pci": requested_pci,
                            "earfcn": requested_earfcn,
                            "retry_after_s": int(retry_s),
                        },
                    )
                    self._emit_runtime_event(
                        "cell_lock_objective_retry_scheduled",
                        objective_id=objective_id,
                        pci=requested_pci,
                        earfcn=requested_earfcn,
                        retry_after_s=int(retry_s),
                    )
            return

        if action.intent in {"emergency_fast_restart", "emergency_restart"}:
            objective_id = action.objective_id or ""
            if not objective_id:
                return
            if success:
                self.cooldowns.clear_keyed("restart_retry", objective_id)
                self._record_objective_lifecycle_event(
                    stage="restart",
                    status="success",
                    action=action,
                    objective_id=objective_id,
                    reason="restart_completed",
                )
                self._emit_runtime_event(
                    "restart_objective_succeeded",
                    objective_id=objective_id,
                    lever=action.lever.name,
                )
            else:
                retry_s = max(0.0, float(cfg.restart_retry_after_s))
                self.cooldowns.set_keyed("restart_retry", objective_id, hold_s=retry_s, now=now)
                self._record_objective_lifecycle_event(
                    stage="restart",
                    status="retry_scheduled",
                    action=action,
                    objective_id=objective_id,
                    reason="restart_failed",
                    until_ts=now + retry_s,
                    metadata={"retry_after_s": int(retry_s)},
                )
                self._emit_runtime_event(
                    "restart_objective_retry_scheduled",
                    objective_id=objective_id,
                    lever=action.lever.name,
                    retry_after_s=int(retry_s),
                )
