"""
arch4/brain/decision_faults.py
───────────────────────────────────────────────────────────────────────────────
Explicit fault-state decision logic.

Owns recovery sequencing for:
  E_RADIO_DOWN      — modem restart + configured band stabilization
  BAD_RF            — bounded band restore / redeploy, then monitor
  E_SESSION_RESET   — passive modem-managed reconnect on HiLink; backend-family fallback elsewhere
  E_CONGESTION_STORM — hold/observe with one stabilization attempt

Called exclusively from DecisionStateMixin thin wrappers which are in turn
called by state_dispatch_policy.run_state_dispatch().

Design constraints:
  - No new heuristics or threshold changes in this file.
  - All fault decisions are single-step paced via _advance().
  - Accesses engine state only through self.* mixin attributes.
  - No direct hardware imports.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from typing import Optional

from arch4.interfaces import LeverAction, fault_state_class
from arch4.brain.outcome_profile import is_bad_rf_capacity_winner
from arch4.operator_profiles import infer_backend_family
from arch4.policy.incident_recovery_policy import run_emergency_incident_policy

log = logging.getLogger("arch4.brain.decision")


class DecisionFaultsMixin:
    """
    Recovery decision logic for all explicit E_* fault states.

    Mixed into DecisionEngine. Accesses self.posture_authority, self.memory,
    self.deployed_bands, self._thresholds, self._state_policy_config(),
    self._make_band_action(), self._make_emergency_backend_action()
    from sibling mixins, primarily DecisionSupportMixin.
    """

    # ─── Public entry points (called by DecisionStateMixin) ─────────────────

    def _decide_e_radio_down(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        return self._decide_fault_state(modem, computed, emergency_class="E_RADIO_DOWN")

    def _decide_bad_rf(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        return self._decide_fault_state(modem, computed, emergency_class="BAD_RF")

    def _decide_e_rf_collapse(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        return self._decide_bad_rf(modem, computed)

    def _decide_e_session_reset(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        return self._decide_fault_state(modem, computed, emergency_class="E_SESSION_RESET")

    def _decide_e_congestion_storm(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        return self._decide_fault_state(modem, computed, emergency_class="E_CONGESTION_STORM")

    # ─── Core fault sequencer ────────────────────────────────────────────────

    def _decide_fault_state(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        emergency_class: str,
    ) -> Optional[LeverAction]:
        """
        Explicit fault-state recovery mode (no explore/optimize actions).

        Rules:
          1. Observe-only grace first.
          2. Cause-specific recovery.
          3. One step at a time with wait between steps.
        """
        posture = self.posture_authority.current_posture
        ctx = posture
        active_class = fault_state_class(
            getattr(ctx, "state", None),
            str(getattr(ctx, "emergency_class", "") or ""),
        )
        if active_class and emergency_class != active_class:
            return None

        if emergency_class == "BAD_RF" and is_bad_rf_capacity_winner(modem, computed, self.cfg):
            try:
                self._emit_runtime_event(
                    "bad_rf_capacity_winner_hold",
                    primary_band=int(getattr(modem.cellular, 'primary_band', 0) or 0),
                    ca_signature=str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
                    selected_band_set_signature=str(getattr(modem.cellular, 'selected_band_set_signature', "") or ""),
                    dl_mbps=float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0),
                    rtt_ms=float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0),
                    congestion_pct=float(getattr(computed.quality, 'congestion_pct', 0.0) or 0.0),
                    reason="BAD_RF is diagnostic; CA-capable lane has healthy outcome",
                )
            except Exception:
                pass
            return None

        now = time.time()
        cfg = self._state_policy_config()
        soft = getattr(self, "_thresholds", {}).get("soft", {})
        if emergency_class == "BAD_RF":
            step_wait = max(
                30.0,
                float(getattr(self.cfg, "bad_rf_action_cooldown_s", 120.0) or 120.0),
            )
        else:
            step_wait = max(
                5.0,
                float(soft.get("emergency_step_wait_s", cfg.emergency_step_wait_s) or cfg.emergency_step_wait_s),
            )
        all_deployed = sorted(self.deployed_bands)

        # Initial observe-only grace (modem FW / self-recovery after transient drop).
        observe_until = float(getattr(ctx, "emergency_observe_until", 0.0) or 0.0)
        if now < observe_until:
            return None

        # Soft hold for congestion storms before taking any action.
        if emergency_class == "E_CONGESTION_STORM":
            hold_until = float(getattr(ctx, "soft_emergency_hold_until", 0.0) or 0.0)
            if hold_until > now:
                return None

        # Single-step pacing.
        next_action_at = float(
            getattr(
                ctx,
                "bad_rf_next_action_at" if emergency_class == "BAD_RF" else "emergency_next_action_at",
                0.0,
            ) or 0.0
        )
        if now < next_action_at:
            return None
        step_attr = "bad_rf_recovery_step" if emergency_class == "BAD_RF" else "emergency_recovery_step"
        next_attr = "bad_rf_next_action_at" if emergency_class == "BAD_RF" else "emergency_next_action_at"
        step = max(0, int(getattr(ctx, step_attr, 0) or 0))

        def _advance(mult: float = 1.0) -> None:
            self.posture_authority.note_fault_recovery_progress(
                posture_id=posture.posture_id,
                fault_class=emergency_class,
                step=step + 1,
                next_action_at=(
                    now + (step_wait * max(0.25, float(mult)))
                ),
            )

        if self._is_routeros_family_profile() and emergency_class in {"E_RADIO_DOWN", "E_SESSION_RESET"}:
            if not self.cooldowns.is_blocked_keyed("routeros_fault_passive", emergency_class, now=now):
                self.cooldowns.set_keyed(
                    "routeros_fault_passive", emergency_class, hold_s=300.0, now=now
                )
                try:
                    self._emit_runtime_event(
                        "routeros_fault_passive_hold",
                        emergency_class=emergency_class,
                        reason="RouterOS LTE recovery is observed externally; band writes drop LTE link",
                    )
                except Exception:
                    pass
            return None

        # Incident-aware override (e.g. CA restore for radio_ca_loss in soft faults).
        if emergency_class != "BAD_RF":
            handled, incident_action, consume_step = run_emergency_incident_policy(
                engine=self,
                modem=modem,
                computed=computed,
                emergency_class=emergency_class,
                step=step,
                timeout_reached=bool(
                    now >= float(getattr(ctx, "emergency_timeout_at", 0.0) or 0.0)
                ),
            )
            if incident_action is not None:
                if consume_step:
                    _advance(1.0)
                return incident_action
            if handled:
                return None

        # ── Cause-specific recovery plans ────────────────────────────────────

        if emergency_class == "E_RADIO_DOWN":
            if step == 0:
                _advance(1.0)
                return self._make_emergency_backend_action("E_RADIO_DOWN")
            if step == 1:
                target = all_deployed
                if target:
                    _advance(1.0)
                    return self._make_band_action(
                        bands=target,
                        params={"sequential": True},
                        reason="FAULT radio down: stabilize on recovery band set",
                        intent="emergency_recovery",
                    )
            return None

        if emergency_class == "E_SESSION_RESET":
            backend_family = infer_backend_family(str(self._emergency_backend() or ""))
            if backend_family == "huawei_hilink_like":
                if step == 0:
                    _advance(1.0)
                return None
            if step == 0:
                target = all_deployed
                if target:
                    _advance(1.0)
                    return self._make_band_action(
                        bands=target,
                        params={"sequential": True},
                        reason="SESSION RESET: stabilize on configured deployed band set",
                        intent="emergency_recovery",
                    )
                _advance(1.0)
                return None
            if step == 1:
                _advance(2.0)
                return self._make_emergency_backend_action("E_SESSION_RESET")
            return None

        if emergency_class == "BAD_RF":
            current_bands = sorted(c.band for c in modem.cellular.carriers)
            known_outage_hold_s = max(
                float(step_wait),
                float(getattr(self.cfg, "learning_retry_min_elapsed_s", 1800.0) or 1800.0),
            )
            if step == 0:
                action = self._make_restore_ca_action(
                    modem,
                    reason="BAD RF: restore known-good band set before passive hold",
                    source="bad_rf_known_good_restore",
                    intent="fault_monitoring",
                    params={"sequential": True},
                )
                if action is not None:
                    _advance(1.0)
                    return action
                _advance(0.5)
                return None
            if step == 1:
                try:
                    in_a1_night = bool(
                        hasattr(self, "_is_a1_overnight_profile")
                        and self._is_a1_overnight_profile()
                        and hasattr(self, "_is_a1_restricted_night_window")
                        and self._is_a1_restricted_night_window(now=now, modem=modem)
                    )
                except Exception:
                    in_a1_night = False
                available = set(getattr(self, "deployed_bands", set()) or set())
                if int(getattr(modem.cellular, 'primary_band', 0) or 0) > 0:
                    available.add(int(getattr(modem.cellular, 'primary_band', 0) or 0))
                can_try_b20_rescue = (
                    in_a1_night
                    and 20 in available
                    and current_bands != [20]
                    and int(getattr(modem.cellular, 'primary_band', 0) or 0) in {1, 3}
                )
                if can_try_b20_rescue:
                    anchor_pool = [
                        int(b)
                        for b in list(getattr(modem.cellular, 'selected_band_set', []) or all_deployed or current_bands or [])
                        if int(b) > 0
                    ]
                    dl_pool = [band for band in anchor_pool if int(band) != 20]
                    _advance(1.0)
                    return self._make_pband_action(
                        pband=20,
                        ca_bands=dl_pool,
                        reason="BAD RF: A1 night coverage rescue — anchor on B20 once while preserving the current CA pool",
                        intent="fault_monitoring",
                        modem=modem,
                    )
                if self._should_hold_known_outage_window(modem, lane="bad_rf_followup"):
                    self.posture_authority.note_fault_recovery_progress(
                        posture_id=posture.posture_id,
                        fault_class=emergency_class,
                        step=2,
                        next_action_at=now + known_outage_hold_s,
                    )
                    return None
                target = all_deployed
                if target and target != current_bands:
                    _advance(1.0)
                    return self._make_band_action(
                        bands=target,
                        params={"sequential": True},
                        reason="BAD RF: reopen full deployed band set once before hold/observe",
                        intent="fault_monitoring",
                    )
                _advance(0.5)
                return None
            # Step-ladder exhausted (step >= 2). If BAD_RF has persisted
            # beyond the configured threshold, escalate to systematic
            # band probing — slow 15-min dwells across all candidates.
            if self._should_hold_known_outage_window(modem, lane="systematic_probe"):
                self.posture_authority.note_fault_recovery_progress(
                    posture_id=posture.posture_id,
                    fault_class=emergency_class,
                    step=step,
                    next_action_at=now + known_outage_hold_s,
                )
                return None
            time_in_bad_rf = float(getattr(ctx, "time_in_state_s", 0.0) or 0.0)
            probe_after_s = float(
                getattr(self.cfg, "bad_rf_systematic_probe_after_s", 1800.0) or 1800.0
            )
            if time_in_bad_rf >= probe_after_s:
                # Sustained BAD_RF is now an owner-stamped posture fact.
                # Systematic probing is optimization and must pass through
                # opportunity -> Bandit -> Brain -> Bouncer; the emergency
                # sequencer cannot call the old selector.
                return None
            return None

        if emergency_class == "E_CONGESTION_STORM":
            current_bands = sorted(c.band for c in modem.cellular.carriers)
            if step == 0:
                target = all_deployed
                if target and target != current_bands:
                    _advance(1.0)
                    return self._make_band_action(
                        bands=target,
                        params={"sequential": True},
                        reason="CONGESTION STORM: stabilize on configured deployed band set",
                        intent="congested_recovery",
                    )
            return None

        # Unknown explicit fault class — hold and observe.
        return None
